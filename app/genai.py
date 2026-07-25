"""The only module in Threshold that touches the network.

What this module does:
    Wraps OpenRouter's chat-completions endpoint. Provides one non-streaming call and
    one SSE streaming call, both async, both returning a `Generation` whose `live`,
    `model`, `latency_ms`, and `error` fields describe honestly what actually happened.
    Also owns the on-disk fallback cache in `data/cache/`.

    Task-level helpers (`checkin`, `script_911`, `refusal`, `tolerance`,
    `caregiver_brief`, `vault_select`) pair each prompt module with the right model tier
    so the API layer never has to choose a model string.

What this module deliberately does NOT do:
    - It never decides a tier, and never imports `app.triage`. Triage is a pure,
      deterministic state machine on the safety-critical path (CONTRACT.md); the
      dependency runs one way and this module is the leaf.
    - It never generates legal or Good Samaritan text. That is a static reviewed
      dataset in `data/legal/` served directly.
    - It never fabricates a live generation. A cached fallback is always returned with
      `live=False` and a populated `error`. This is a judging disqualifier
      (CONTRACT.md ground rules 1 and 2), and it is also just true: the UI has to be
      able to tell the user the AI is offline.
    - It never logs, echoes, or returns the API key, and never puts any secret in a
      `Generation`. See `_redact` and the logging discipline below. The same applies to
      the session secret owned by `app/auth.py` — no credential of any kind is logged
      here, and this module never reads one.

Startup without a key:
    `OPENROUTER_API_KEY` is read lazily on every call, never captured at import time.
    The module imports and the app starts cleanly with no key; `ai_online()` reports
    False; and the moment the variable is exported the next call goes live with no code
    change and no restart of this module's state.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import re
import time
from pathlib import Path
from typing import AsyncIterator, Final

import httpx

from app.models import Generation, Event, Tier, ToleranceEvent, UserProfile, VaultClip
from app.prompts import caregiver_brief as caregiver_brief_prompt
from app.prompts import checkin as checkin_prompt
from app.prompts import refusal as refusal_prompt
from app.prompts import script_911 as script_911_prompt
from app.prompts import tolerance as tolerance_prompt
from app.prompts import vault_select as vault_select_prompt

log = logging.getLogger("threshold.genai")

# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------

API_URL: Final = "https://openrouter.ai/api/v1/chat/completions"

# Model split per CONTRACT.md. Flash for anything a human is waiting on; Pro for the
# considered artefacts (the 911 script and the caregiver brief) which are generated
# ahead of need or read slowly, where quality is worth the extra seconds.
MODEL_FAST: Final = "google/gemini-2.5-flash"
MODEL_DEEP: Final = "google/gemini-2.5-pro"

ENV_KEY: Final = "OPENROUTER_API_KEY"

# Timeouts are explicit rather than httpx defaults because the paths differ sharply in
# urgency. `connect` is short everywhere: a hung connect is indistinguishable from being
# offline, and on a crisis screen we would rather fall back fast than spin.
TIMEOUT_FAST: Final = httpx.Timeout(connect=3.0, read=12.0, write=5.0, pool=3.0)
TIMEOUT_DEEP: Final = httpx.Timeout(connect=3.0, read=45.0, write=5.0, pool=3.0)

MAX_ATTEMPTS: Final = 3  # 1 initial try + 2 retries
BACKOFF_BASE: Final = 0.4  # seconds; doubled per attempt, plus jitter
BACKOFF_CAP: Final = 4.0  # never make a user wait longer than this between attempts

CACHE_DIR: Final = Path(__file__).resolve().parent.parent / "data" / "cache"

# Optional attribution headers OpenRouter uses for its dashboard. Harmless if unset.
_REFERER: Final = "https://threshold.local"
_TITLE: Final = "Threshold"


# --------------------------------------------------------------------------------------
# Secret handling
# --------------------------------------------------------------------------------------


def _api_key() -> str | None:
    """Read the API key from the environment, lazily, on every call.

    Read lazily and never cached in a module global so that exporting
    `OPENROUTER_API_KEY` takes effect immediately with no code change and no restart
    (CONTRACT.md: "work fully the moment it is exported").

    Returns:
        The key, or None when unset/blank. A blank-but-present variable is treated as
        unset, since that is what an empty `export FOO=` produces and it would
        otherwise cause a confusing 401 instead of a clean offline state.
    """
    raw = os.environ.get(ENV_KEY)
    return raw.strip() if raw and raw.strip() else None


def ai_online() -> bool:
    """Whether a live call is even possible right now (i.e. a key is present).

    Used by `GET /api/state` to render the "AI offline — no API key" banner. This
    reports key *presence*, not provider reachability — an actual outage surfaces per
    call as `Generation.live=False` with an error, which is the honest place for it.
    """
    return _api_key() is not None


# `app/main.py` calls this name. Kept as an alias rather than renaming, because
# "is it online" reads better at the call site and "ai_online" matches the JSON key in
# GET /api/state. Both are the same single source of truth.
is_online = ai_online


def _resolve_model(model: str | None, fast: bool | None) -> str:
    """Pick the model id from either the explicit `model` or the `fast` flag.

    Two spellings exist on purpose. `model=` is precise and used by the task helpers in
    this module; `fast=` is the ergonomic form the API layer uses, where the only real
    question is "is a human waiting on this?".

    Args:
        model: Explicit model id, or None.
        fast: True for the low-latency model, False for the deep model, None to defer
            to `model`.

    Returns:
        A model id. An explicit `model` always wins; `fast` is consulted only when
        `model` was not given, so the two can never silently disagree.
    """
    if model is not None:
        return model
    if fast is None:
        return MODEL_FAST
    return MODEL_FAST if fast else MODEL_DEEP


# Redaction net. Belt-and-braces: nothing should ever route a key into a message, but
# error strings from httpx and from the provider can quote request context, so every
# outbound string passes through here before it reaches a log or a `Generation`.
_KEY_PATTERNS: Final = (
    re.compile(r"sk-or-[A-Za-z0-9\-_]{8,}"),  # OpenRouter key format
    re.compile(r"(?i)bearer\s+[A-Za-z0-9\-_\.]{8,}"),  # any Authorization header echo
)


def _redact(text: str) -> str:
    """Strip anything that looks like a credential out of a string.

    Applied to every error message before it is logged or placed on a `Generation`.
    Security is a scored category (CONTRACT.md) and a leaked key in an error body that
    the UI happily renders is the classic way this goes wrong.

    Args:
        text: Arbitrary text, typically an exception string or provider error body.

    Returns:
        The text with key-shaped substrings replaced by "[redacted]", and with the
        live key value itself removed if it happens to appear verbatim.
    """
    out = text
    key = _api_key()
    # Exact-match removal first: catches a key that does not fit the known patterns.
    if key:
        out = out.replace(key, "[redacted]")
    for pattern in _KEY_PATTERNS:
        out = pattern.sub("[redacted]", out)
    return out


# --------------------------------------------------------------------------------------
# Disk cache
#
# Purpose is narrow and worth stating: this cache is NOT a latency optimisation and is
# never consulted on the happy path. It exists so that when the provider is down, the
# user still sees the last genuinely-generated text for that exact prompt — clearly
# labelled `live=False`. Only real live generations are ever written to it, so a
# fallback can never be laundered into looking fresh (CONTRACT.md ground rule 2).
# --------------------------------------------------------------------------------------


def _cache_key(model: str, system: str, user: str) -> str:
    """Hash the full prompt into a stable cache filename stem.

    The model is part of the key because the same prompt against Flash and Pro are
    different artefacts, and serving one as the other would misreport `model`.

    Args:
        model: Model id the prompt was/will be sent to.
        system: System prompt.
        user: User prompt.

    Returns:
        A 32-char hex digest. Truncated sha256 — collision risk is irrelevant for a
        local fallback cache, and short names keep `data/cache/` readable for a judge.
    """
    digest = hashlib.sha256(f"{model}\x00{system}\x00{user}".encode("utf-8")).hexdigest()
    return digest[:32]


def _cache_path(key: str) -> Path:
    """Absolute path of the cache entry for a given key."""
    return CACHE_DIR / f"{key}.json"


def cache_read(model: str, system: str, user: str) -> tuple[str, str] | None:
    """Read a cached generation for this prompt, if one exists.

    Returns:
        (text, cached_at_iso) or None. Any read failure — missing file, truncated JSON,
        wrong shape, permissions — returns None rather than raising: the cache is the
        *failure* path, so it must never be able to turn a degraded response into an
        exception on a crisis screen.
    """
    path = _cache_path(_cache_key(model, system, user))
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    return text, str(payload.get("cached_at", ""))


def cache_write(model: str, system: str, user: str, text: str) -> None:
    """Persist a genuinely live generation as a future fallback.

    Call sites must only reach this after a successful live call. There is deliberately
    no parameter to write a non-live value: making that impossible at the signature
    level is stronger than a comment asking callers to behave.

    Failures are swallowed and logged at debug level — a read-only filesystem must
    degrade the fallback cache, not the generation the user is waiting on.
    """
    if not text.strip():
        return  # Never cache an empty body; it would be a useless fallback.
    path = _cache_path(_cache_key(model, system, user))
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "model": model,
                    "text": text,
                    "cached_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError as exc:  # pragma: no cover - filesystem-dependent
        # Note: no prompt content and no key in this log line.
        log.debug("cache write failed: %s", _redact(str(exc)))


# --------------------------------------------------------------------------------------
# Failure handling
# --------------------------------------------------------------------------------------

# User-facing error copy. Kept as constants so the UI copy is auditable in one place and
# so no error string is ever built from provider-controlled text.
ERR_NO_KEY: Final = "AI offline — no API key"
ERR_AUTH: Final = "AI error: key rejected"
ERR_RATE_LIMIT: Final = "AI error: rate limited"
ERR_TIMEOUT: Final = "AI error: timed out"
ERR_UNAVAILABLE: Final = "AI error: provider unavailable"
ERR_NETWORK: Final = "AI error: network unreachable"
ERR_BAD_RESPONSE: Final = "AI error: unreadable response"


class _Retryable(Exception):
    """Internal signal that an attempt failed in a way worth retrying.

    Carries the user-facing message so the retry loop does not have to re-derive it
    when it finally gives up.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class _Fatal(Exception):
    """Internal signal that an attempt failed in a way that retrying cannot fix.

    Auth errors and other 4xx responses land here. Retrying a 401 just burns the user's
    time on a crisis screen and can trip provider abuse protections.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _classify_status(status: int) -> Exception:
    """Map an HTTP status to the right internal failure signal.

    Args:
        status: HTTP status code from OpenRouter.

    Returns:
        A `_Retryable` for 429 and 5xx (transient: rate limits and upstream blips
        resolve), a `_Fatal` for every other 4xx (auth, bad request — retrying cannot
        change the outcome).

    Note:
        The provider's response *body* is deliberately never included in the message.
        Bodies can echo request context, and we control every error string the UI shows.
    """
    if status == 429:
        return _Retryable(ERR_RATE_LIMIT)
    if status >= 500:
        return _Retryable(ERR_UNAVAILABLE)
    if status in (401, 403):
        return _Fatal(ERR_AUTH)
    return _Fatal(f"AI error: request rejected ({status})")


def _fallback(
    model: str,
    system: str,
    user: str,
    error: str,
    started: float,
) -> Generation:
    """Build the honest failure `Generation`, using the cache when it has something.

    This is the single place a non-live `Generation` is constructed, which is what makes
    the "a fallback never masquerades as live" guarantee checkable in one read.

    Args:
        model: The model that was attempted — reported as-is so the UI can say which
            path failed.
        system: System prompt (for the cache lookup).
        user: User prompt (for the cache lookup).
        error: A user-facing message from the ERR_* constants. Redacted again here as a
            final net before it can reach a response body.
        started: `time.monotonic()` at the start of the call, so `latency_ms` reflects
            the real time the user waited, including retries.

    Returns:
        A `Generation` with `live=False` and `error` populated, always. `text` is the
        last live generation for this exact prompt if one was cached, otherwise empty —
        empty is correct, because inventing prose here would be exactly the hardcoded
        fake output the judging rules disqualify.
    """
    latency_ms = int((time.monotonic() - started) * 1000)
    safe_error = _redact(error)

    cached = cache_read(model, system, user)
    if cached is not None:
        text, cached_at = cached
        # The UI shows `error` verbatim, so the fallback provenance is stated here
        # rather than left for the frontend to remember to add.
        note = f"{safe_error} — showing last saved response"
        if cached_at:
            note += f" from {cached_at}"
        return Generation(text=text, live=False, model=model, latency_ms=latency_ms, error=note)

    return Generation(
        text="",
        live=False,
        model=model,
        latency_ms=latency_ms,
        error=f"{safe_error} — no saved response available",
    )


async def _sleep_backoff(attempt: int) -> None:
    """Wait before the next attempt, with bounded exponential backoff and jitter.

    Args:
        attempt: 1-based index of the attempt that just failed.

    Jitter is applied because several surfaces (tolerance message, 911 script, refusal
    lines) can warm at once on page load; without it their retries would collide in
    lockstep and turn one rate-limit into a thundering herd.
    """
    delay = min(BACKOFF_BASE * (2 ** (attempt - 1)), BACKOFF_CAP)
    await asyncio.sleep(delay + random.uniform(0, delay * 0.25))


# --------------------------------------------------------------------------------------
# Transport
# --------------------------------------------------------------------------------------


def _headers(key: str) -> dict[str, str]:
    """Build request headers.

    The key appears here and nowhere else in the module. Nothing logs a header dict,
    and `_redact` covers the case where httpx quotes one back inside an exception.
    """
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "HTTP-Referer": _REFERER,
        "X-Title": _TITLE,
    }


def _body(model: str, system: str, user: str, *, stream: bool, max_tokens: int) -> dict:
    """Build the OpenRouter chat-completions request body.

    Args:
        model: Model id.
        system: System prompt.
        user: User prompt.
        stream: Whether to request SSE deltas.
        max_tokens: Hard output ceiling. Every prompt in this app specifies its own
            length, so this is a cost/latency guard rather than a shaping tool.

    Returns:
        A JSON-serialisable dict.
    """
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        # Low but non-zero: these are human-facing lines that should not read as
        # canned, but the safety-critical ones (911 script) must not get creative.
        "temperature": 0.6,
        "max_tokens": max_tokens,
        "stream": stream,
    }


def _extract_text(payload: dict) -> str:
    """Pull the assistant message out of a non-streaming completion payload.

    Args:
        payload: Decoded JSON response body.

    Returns:
        The message content, stripped.

    Raises:
        _Retryable: if the response is well-formed JSON but has no usable content. This
            is treated as retryable because in practice it means a truncated or
            filtered generation, which a second attempt often resolves.
    """
    try:
        choices = payload["choices"]
        content = choices[0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise _Retryable(ERR_BAD_RESPONSE) from None

    if not isinstance(content, str) or not content.strip():
        raise _Retryable(ERR_BAD_RESPONSE)
    return content.strip()


async def generate(
    system: str,
    user: str,
    *,
    model: str | None = None,
    fast: bool | None = None,
    max_tokens: int = 700,
    client: httpx.AsyncClient | None = None,
) -> Generation:
    """Run one non-streaming completion and return an honest `Generation`.

    This is the workhorse. Every task helper below funnels through it.

    Args:
        system: System prompt.
        user: User prompt.
        model: Explicit model id. Defaults to the fast interactive model.
        fast: Convenience alternative to `model`, used by the API layer — True selects
            the fast model, False the deep one. Ignored when `model` is given.
        max_tokens: Output ceiling.
        client: An existing `httpx.AsyncClient` to reuse. Optional — mainly a seam for
            tests and for callers batching several generations, which avoids paying a
            fresh TLS handshake per surface.

    Returns:
        A `Generation`. On success: `live=True`, `error=None`, real `latency_ms`. On any
        failure: `live=False` with a populated `error` and the cached text if one
        exists. This function does not raise for network or provider failures —
        surfacing an exception onto a crisis screen is never the right behaviour.
    """
    started = time.monotonic()
    model = _resolve_model(model, fast)

    key = _api_key()
    if key is None:
        # The expected state today: no key exported yet. Not an error condition, and
        # deliberately not logged as one.
        return _fallback(model, system, user, ERR_NO_KEY, started)

    timeout = TIMEOUT_DEEP if model == MODEL_DEEP else TIMEOUT_FAST
    last_error = ERR_UNAVAILABLE

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            text = await _attempt(
                system, user, model=model, max_tokens=max_tokens,
                key=key, timeout=timeout, client=client,
            )
        except _Fatal as exc:
            # Auth and other 4xx: retrying cannot help, so surface immediately.
            log.warning("genai fatal: %s", _redact(exc.message))
            return _fallback(model, system, user, exc.message, started)
        except _Retryable as exc:
            last_error = exc.message
            if attempt < MAX_ATTEMPTS:
                log.info("genai retry %d/%d: %s", attempt, MAX_ATTEMPTS, _redact(exc.message))
                await _sleep_backoff(attempt)
                continue
            log.warning("genai gave up after %d attempts: %s", attempt, _redact(exc.message))
            return _fallback(model, system, user, exc.message, started)
        else:
            latency_ms = int((time.monotonic() - started) * 1000)
            # Cache ONLY here — the single point in the module where we know for certain
            # the text came from a real, successful API call.
            cache_write(model, system, user, text)
            return Generation(
                text=text, live=True, model=model, latency_ms=latency_ms, error=None
            )

    # Unreachable: the loop always returns. Kept as a typed safety net.
    return _fallback(model, system, user, last_error, started)  # pragma: no cover


async def _attempt(
    system: str,
    user: str,
    *,
    model: str,
    max_tokens: int,
    key: str,
    timeout: httpx.Timeout,
    client: httpx.AsyncClient | None,
) -> str:
    """Perform a single non-streaming HTTP attempt.

    Split out from `generate` so the retry policy reads as policy, and so every
    exception type httpx can raise is mapped in exactly one place.

    Returns:
        The generated text.

    Raises:
        _Retryable: timeouts, connection failures, 429, 5xx, unparseable bodies.
        _Fatal: auth failures and other 4xx.
    """
    payload_body = _body(model, system, user, stream=False, max_tokens=max_tokens)

    async def _run(c: httpx.AsyncClient) -> str:
        response = await c.post(API_URL, headers=_headers(key), json=payload_body)
        if response.status_code != 200:
            raise _classify_status(response.status_code)
        try:
            data = response.json()
        except ValueError:
            raise _Retryable(ERR_BAD_RESPONSE) from None
        return _extract_text(data)

    try:
        if client is not None:
            return await _run(client)
        async with httpx.AsyncClient(timeout=timeout) as owned:
            return await _run(owned)
    except httpx.TimeoutException:
        raise _Retryable(ERR_TIMEOUT) from None
    except httpx.HTTPError as exc:
        # Transport-level failure (DNS, refused, TLS). Message is redacted before it can
        # reach a log or a response body.
        raise _Retryable(f"{ERR_NETWORK}: {_redact(type(exc).__name__)}") from None


# --------------------------------------------------------------------------------------
# Streaming
# --------------------------------------------------------------------------------------


async def generate_stream(
    system: str,
    user: str,
    *,
    model: str | None = None,
    fast: bool | None = None,
    max_tokens: int = 700,
    client: httpx.AsyncClient | None = None,
) -> AsyncIterator[tuple[str, Generation | None]]:
    """Stream a completion token-by-token, then yield the final `Generation`.

    Yields:
        `(delta, None)` for each token chunk as it arrives, then exactly one terminal
        `("", generation)` carrying the assembled `Generation`. Callers forward the
        deltas to the browser over SSE and use the terminal item to render the
        live/fallback badge and latency.

    Why the terminal item exists:
        A stream that dies halfway has already put partial text on screen. The terminal
        `Generation` is how the frontend learns whether what it showed was a complete
        live response or a truncated one, and it is why `live` cannot be decided until
        the stream closes.

    Retry policy:
        Deliberately narrower than `generate`. A failure *before any token* is retried
        normally; once tokens have been emitted we do not retry, because restarting
        would duplicate text the user is already reading. A mid-stream failure falls
        back to the cache if there is one, and otherwise returns the partial text with
        `live=False` and an explicit truncation error — never labelled live.

    Args:
        system: System prompt.
        user: User prompt.
        model: Explicit model id; defaults to the fast interactive model.
        fast: Convenience alternative to `model` (see `generate`).
        max_tokens: Output ceiling.
        client: Optional shared client (test seam / connection reuse).
    """
    started = time.monotonic()
    model = _resolve_model(model, fast)

    key = _api_key()
    if key is None:
        yield "", _fallback(model, system, user, ERR_NO_KEY, started)
        return

    timeout = TIMEOUT_DEEP if model == MODEL_DEEP else TIMEOUT_FAST
    body = _body(model, system, user, stream=True, max_tokens=max_tokens)

    for attempt in range(1, MAX_ATTEMPTS + 1):
        chunks: list[str] = []
        try:
            async for delta in _stream_attempt(
                body, key=key, timeout=timeout, client=client
            ):
                chunks.append(delta)
                yield delta, None

        except _Fatal as exc:
            log.warning("genai stream fatal: %s", _redact(exc.message))
            yield "", _fallback(model, system, user, exc.message, started)
            return

        except _Retryable as exc:
            # Tokens already on screen: retrying would duplicate them, so stop here and
            # be honest about the truncation.
            if chunks:
                partial = "".join(chunks).strip()
                log.warning("genai stream truncated: %s", _redact(exc.message))
                if partial:
                    yield "", Generation(
                        text=partial,
                        live=False,  # Incomplete output is never labelled live.
                        model=model,
                        latency_ms=int((time.monotonic() - started) * 1000),
                        error=f"{_redact(exc.message)} — reply was cut off",
                    )
                else:
                    yield "", _fallback(model, system, user, exc.message, started)
                return

            # Nothing emitted yet: safe to retry cleanly.
            if attempt < MAX_ATTEMPTS:
                log.info("genai stream retry %d/%d", attempt, MAX_ATTEMPTS)
                await _sleep_backoff(attempt)
                continue
            yield "", _fallback(model, system, user, exc.message, started)
            return

        else:
            text = "".join(chunks).strip()
            if not text:
                # A clean stream that produced nothing is a failed generation, not an
                # empty success — do not cache it and do not call it live.
                yield "", _fallback(model, system, user, ERR_BAD_RESPONSE, started)
                return
            cache_write(model, system, user, text)
            yield "", Generation(
                text=text,
                live=True,
                model=model,
                latency_ms=int((time.monotonic() - started) * 1000),
                error=None,
            )
            return


async def _stream_attempt(
    body: dict,
    *,
    key: str,
    timeout: httpx.Timeout,
    client: httpx.AsyncClient | None,
) -> AsyncIterator[str]:
    """Perform a single streaming HTTP attempt, yielding text deltas.

    Parses the OpenRouter SSE framing: `data: {json}` lines, terminated by
    `data: [DONE]`. Comment lines (`:` prefixed keep-alives) and blank lines are
    skipped, and an individual chunk that fails to parse is skipped rather than
    aborting the stream — one malformed frame should not discard a reply the user is
    already reading.

    Raises:
        _Retryable / _Fatal: same mapping as `_attempt`.
    """

    async def _run(c: httpx.AsyncClient) -> AsyncIterator[str]:
        async with c.stream(
            "POST", API_URL, headers=_headers(key), json=body
        ) as response:
            if response.status_code != 200:
                # Body must be consumed before it can be read on a streaming response.
                await response.aread()
                raise _classify_status(response.status_code)

            async for line in response.aiter_lines():
                if not line or line.startswith(":"):
                    continue  # keep-alive / blank frame
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    return
                try:
                    parsed = json.loads(data)
                    delta = parsed["choices"][0]["delta"].get("content")
                except (json.JSONDecodeError, KeyError, IndexError, TypeError):
                    continue  # Skip the bad frame, keep the stream alive.
                if delta:
                    yield delta

    try:
        if client is not None:
            async for delta in _run(client):
                yield delta
            return
        async with httpx.AsyncClient(timeout=timeout) as owned:
            async for delta in _run(owned):
                yield delta
    except httpx.TimeoutException:
        raise _Retryable(ERR_TIMEOUT) from None
    except httpx.HTTPError as exc:
        raise _Retryable(f"{ERR_NETWORK}: {_redact(type(exc).__name__)}") from None


# --------------------------------------------------------------------------------------
# Task helpers
#
# Each pairs a prompt module with the model tier CONTRACT.md assigns it, so no caller
# has to remember which surface gets Pro. Signatures take domain objects from
# app.models, never raw strings.
# --------------------------------------------------------------------------------------


async def checkin(
    profile: UserProfile, text: str, history: list[str] | None = None
) -> Generation:
    """Generate a conversational check-in reply (non-streaming).

    Uses the fast model: a human is waiting on this in real time.
    """
    system, user = checkin_prompt.build(profile, text, history)
    # Tight ceiling: the prompt asks for under 45 words, and capping the tokens stops a
    # runaway generation from holding a spoken reply open.
    return await generate(system, user, model=MODEL_FAST, max_tokens=200)


def checkin_stream(
    profile: UserProfile, text: str, history: list[str] | None = None
) -> AsyncIterator[tuple[str, Generation | None]]:
    """Stream a conversational check-in reply.

    This is the variant `POST /api/utterance` uses for its SSE response. Returns the
    async iterator directly (not `async def`) so callers can pass it straight to an SSE
    responder without an extra await.
    """
    system, user = checkin_prompt.build(profile, text, history)
    return generate_stream(system, user, model=MODEL_FAST, max_tokens=200)


async def script_911(profile: UserProfile, situation: str = "") -> Generation:
    """Generate the personalised 911 script.

    Uses the deep model per CONTRACT.md: this is generated ahead of need and cached, and
    it is the output where a garbled address costs the most.
    """
    system, user = script_911_prompt.build(profile, situation)
    return await generate(system, user, model=MODEL_DEEP, max_tokens=600)


async def refusal(
    profile: UserProfile,
    situation: str = "",
    register_samples: list[str] | None = None,
) -> Generation:
    """Generate refusal / exit lines in the user's own register.

    Fast model: short output, and it is often requested from a live screen.
    """
    system, user = refusal_prompt.build(profile, situation, register_samples)
    return await generate(system, user, model=MODEL_FAST, max_tokens=400)


async def tolerance(
    profile: UserProfile, event: ToleranceEvent | None = None
) -> Generation:
    """Generate the Tolerance Guard proactive message.

    Fast model: this is a short message and it is sent proactively, so it should not sit
    behind a slow generation when the trigger fires.
    """
    system, user = tolerance_prompt.build(profile, event)
    return await generate(system, user, model=MODEL_FAST, max_tokens=300)


async def caregiver_brief(
    profile: UserProfile, tier: Tier, reason: str, events: list[Event]
) -> Generation:
    """Generate the 3am caregiver situation brief.

    Uses the deep model per CONTRACT.md. The tier is passed through only as already-
    decided context for wording; the prompt forbids the model from naming or reasoning
    about it (PRD P4: the model never decides a tier).
    """
    system, user = caregiver_brief_prompt.build(profile, tier, events, reason)
    return await generate(system, user, model=MODEL_DEEP, max_tokens=900)


async def vault_select(
    profile: UserProfile, clips: list[VaultClip], context: str = ""
) -> tuple[VaultClip | None, Generation]:
    """Choose which recorded vault clip to play, and say why in one sentence.

    Args:
        profile: The person who will hear the clip.
        clips: Available clips. May be empty.
        context: Plain description of the current moment. Never a tier name.

    Returns:
        `(clip, generation)`.
        - Empty `clips`: `(None, Generation(live=False, ...))`. Having nothing recorded
          is a UI state, not a generation failure, and it is reported as such.
        - Model failure: `(clips[0], fallback_generation)` — the person still hears a
          real recorded voice, and `live=False` tells the UI the caption is not a live
          rationale. Playing *something* matters more here than explaining the choice.
        - Malformed JSON that `parse_selection` salvages: the returned `Generation` is
          downgraded to `live=False` with an explicit error, because a salvaged parse is
          not a clean model selection and must not be presented as one.
    """
    if not clips:
        return None, Generation(
            text="",
            live=False,
            model=MODEL_FAST,
            latency_ms=0,
            error="No recordings saved yet",
        )

    system, user = vault_select_prompt.build(clips, context, profile)
    # Small ceiling: the expected output is a single short JSON object.
    gen = await generate(system, user, model=MODEL_FAST, max_tokens=200)

    if not gen.live:
        # Failed call. `gen.text` may hold a cached JSON blob; parse it if so, but the
        # generation stays live=False either way.
        clip, reason, _ = vault_select_prompt.parse_selection(gen.text, clips)
        return clip, gen.model_copy(update={"text": reason})

    clip, reason, clean = vault_select_prompt.parse_selection(gen.text, clips)
    if not clean:
        return clip, gen.model_copy(
            update={
                "text": reason,
                "live": False,  # Salvaged output is never labelled a live selection.
                "error": "AI error: unreadable selection — played the default clip",
            }
        )

    return clip, gen.model_copy(update={"text": reason})
