"""Tests for the generative layer — the only module that touches the network.

WHAT THESE TESTS PROTECT AGAINST
    Three failure modes, in descending order of how badly they would hurt:

    1. A fallback masquerading as a live generation. CONTRACT.md ground rules 1 and 2
       make this an automatic judging disqualifier, and it is also a lie told to
       someone in a crisis. Every failure path here is asserted to carry `live=False`
       AND a populated `error`.
    2. A credential leaking into a log line, an error string, or a response body.
       Security is a scored category; a key rendered into the UI is the classic way it
       goes wrong. The same discipline covers the session secret owned by `app/auth.py`
       — this module never reads a credential other than the API key.
    3. The app failing to start, or a crisis screen raising, when no key is exported.
       The key is genuinely not set yet, so this is the *normal* state today.

WHAT THEY DELIBERATELY DO NOT DO
    They never make a real network call and never require `OPENROUTER_API_KEY`. Every
    HTTP interaction is mocked at the httpx transport layer, so the suite is
    deterministic and runs on a laptop with no credentials. Asserting on model prose
    would make the suite non-deterministic, which is its own bug.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import httpx
import pytest

from app import genai
from app.models import Event, Tier, ToleranceEvent, UserProfile, VaultClip
from app.prompts import vault_select as vault_select_prompt

# A recognisably fake key in OpenRouter's format, so the redaction tests exercise the
# real pattern. It is not, and never was, a valid credential.
FAKE_KEY = "sk-or-v1-TESTKEY0000000000000000000000000000"


# --------------------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    """Point the disk cache at a temp dir for every test in this module.

    Autouse and unconditional: without it, a test that writes a cache entry would leave
    a real file in `data/cache/` and could make a later "no cached response" assertion
    pass or fail depending on what ran before it.
    """
    monkeypatch.setattr(genai, "CACHE_DIR", tmp_path / "cache")
    return tmp_path / "cache"


@pytest.fixture(autouse=True)
def no_key_by_default(monkeypatch):
    """Guarantee the key is unset unless a test explicitly sets it.

    Protects against the developer's own environment: if someone runs this suite with a
    real key exported, the no-key tests must still test the no-key path.
    """
    monkeypatch.delenv(genai.ENV_KEY, raising=False)


@pytest.fixture(autouse=True)
def no_backoff_sleep(monkeypatch):
    """Make retry backoff instant.

    The retry policy is asserted by counting attempts, not by timing them, so the real
    sleeps would only make the suite slow.
    """

    async def _instant(attempt: int) -> None:
        return None

    monkeypatch.setattr(genai, "_sleep_backoff", _instant)


@pytest.fixture
def profile() -> UserProfile:
    """A realistic profile including the identifying fields the 911 script needs."""
    return UserProfile(
        id="u1",
        name="Sam Rivera",
        address="418 Delacourt Street",
        unit="Apt 3B",
        entry_code="4471",
        cross_street="Ninth and Delacourt",
        state_code="KY",
        substances=["fentanyl", "benzos"],
        naloxone_on_hand=True,
        tolerance_events=[
            ToleranceEvent(
                kind="detox",
                date=datetime(2026, 7, 10, tzinfo=timezone.utc),
                note="left the programme early",
            )
        ],
    )


@pytest.fixture
def clips() -> list[VaultClip]:
    """Two clips with clearly different emotional registers, so selection is meaningful."""
    return [
        VaultClip(
            id="clip-mum",
            recorded_by="Maria",
            relation="mother",
            transcript="It's mum. I'm not upset with you. Just stay where I can find you.",
            tags=["steady", "presence"],
        ),
        VaultClip(
            id="clip-sponsor",
            recorded_by="Dee",
            relation="sponsor",
            transcript="Hey. You called me at 4am once and it was fine. Call me again.",
            tags=["permission"],
        ),
    ]


def _mock_transport(handler) -> httpx.AsyncClient:
    """Build an AsyncClient whose requests are served by `handler`.

    Mocking at the transport layer rather than monkeypatching `generate` means the real
    header construction, status classification, JSON parsing, and SSE framing all run —
    which is where the bugs would actually be.
    """
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _ok_completion(text: str) -> httpx.Response:
    """A well-formed non-streaming OpenRouter success response."""
    return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})


def _sse(*deltas: str, done: bool = True) -> str:
    """Build an OpenRouter-shaped SSE body from token deltas."""
    lines = [
        "data: " + json.dumps({"choices": [{"delta": {"content": d}}]}) for d in deltas
    ]
    if done:
        lines.append("data: [DONE]")
    return "\n\n".join(lines) + "\n\n"


# --------------------------------------------------------------------------------------
# The no-key path — the app's actual state today
# --------------------------------------------------------------------------------------


def test_module_imports_and_reports_offline_without_a_key():
    """CONTRACT.md: the app must start cleanly with no key and report ai_online=False."""
    assert genai.ai_online() is False
    assert genai.is_online() is False  # the alias app/main.py calls


def test_blank_key_is_treated_as_absent(monkeypatch):
    """`export OPENROUTER_API_KEY=` must read as offline, not as a key that 401s.

    Otherwise an empty export produces a confusing "key rejected" instead of the
    honest "no API key" state, and the UI shows the wrong recovery instruction.
    """
    monkeypatch.setenv(genai.ENV_KEY, "   ")
    assert genai.ai_online() is False


def test_key_is_read_lazily_not_captured_at_import(monkeypatch):
    """Exporting the key must take effect with no code change and no restart.

    This is the literal CONTRACT.md requirement, and it fails the moment anyone caches
    the key in a module-level global.
    """
    assert genai.ai_online() is False
    monkeypatch.setenv(genai.ENV_KEY, FAKE_KEY)
    assert genai.ai_online() is True


@pytest.mark.asyncio
async def test_no_key_returns_honest_offline_generation(profile):
    """With no key: live=False, a clear error, and no invented prose."""
    gen = await genai.checkin(profile, "hey")

    assert gen.live is False
    assert gen.error is not None
    assert genai.ERR_NO_KEY in gen.error
    # Ground rule 1: no hardcoded prose presented as model output. With nothing cached,
    # empty is the only honest answer.
    assert gen.text == ""
    assert gen.model == genai.MODEL_FAST


@pytest.mark.asyncio
async def test_no_key_never_raises_on_any_task_helper(profile, clips):
    """Every generative surface must degrade, never throw.

    A stranger clicking every button with no key exported must not hit a 500 — the
    judging rules require every feature to work end-to-end in any order.
    """
    results = [
        await genai.checkin(profile, "hey"),
        await genai.script_911(profile),
        await genai.refusal(profile),
        await genai.tolerance(profile),
        await genai.caregiver_brief(profile, Tier.ACTIVE_USE, "manual override", []),
    ]
    _, vault_gen = await genai.vault_select(profile, clips, "a hard moment")
    results.append(vault_gen)

    for gen in results:
        assert gen.live is False, "offline generation labelled live"
        assert gen.error, "failure without an error message"


@pytest.mark.asyncio
async def test_streaming_without_a_key_yields_only_the_offline_generation(profile):
    """The SSE path must degrade identically to the blocking path — no tokens, no fake."""
    items = [item async for item in genai.checkin_stream(profile, "hey")]

    assert len(items) == 1, "emitted tokens without a key"
    delta, gen = items[0]
    assert delta == ""
    assert gen is not None and gen.live is False
    assert genai.ERR_NO_KEY in gen.error


# --------------------------------------------------------------------------------------
# Live success — and the fact that success is what gets cached
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_successful_call_is_labelled_live(monkeypatch, profile):
    """The happy path: live=True, no error, the right model, real latency."""
    monkeypatch.setenv(genai.ENV_KEY, FAKE_KEY)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        # The model split in CONTRACT.md is a real requirement, so assert it on the wire.
        assert body["model"] == genai.MODEL_FAST
        assert body["stream"] is False
        return _ok_completion("Still here. What's going on tonight?")

    async with _mock_transport(handler) as client:
        gen = await genai.generate(
            "sys", "user", model=genai.MODEL_FAST, client=client
        )

    assert gen.live is True
    assert gen.error is None
    assert gen.text == "Still here. What's going on tonight?"
    assert gen.latency_ms >= 0


@pytest.mark.asyncio
async def test_deep_model_used_for_script_and_brief(monkeypatch, profile):
    """CONTRACT.md assigns Pro to the 911 script and the caregiver brief."""
    monkeypatch.setenv(genai.ENV_KEY, FAKE_KEY)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content)["model"])
        return _ok_completion("line one\nline two")

    async with _mock_transport(handler) as client:
        monkeypatch.setattr(
            genai,
            "generate",
            _pin_client(genai.generate, client),
        )
        await genai.script_911(profile)
        await genai.caregiver_brief(profile, Tier.EMERGENCY, "sensor: no response", [])

    assert seen == [genai.MODEL_DEEP, genai.MODEL_DEEP]


def _pin_client(original, client):
    """Wrap `generate` so task helpers use the mock transport.

    The helpers deliberately do not expose a `client` parameter — production callers
    should not be choosing transports — so the test injects one here rather than
    widening the production signature for testing's sake.
    """

    async def wrapper(system, user, **kwargs):
        kwargs.setdefault("client", client)
        return await original(system, user, **kwargs)

    return wrapper


# --------------------------------------------------------------------------------------
# Cache behaviour
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_only_live_generations_are_cached(monkeypatch, isolated_cache):
    """A failed call must leave nothing behind that a later call could serve as text.

    This is the mechanism that makes "a fallback never masquerades as live" hold over
    time: if failures were cached, a fallback would eventually become the source of
    another fallback and the provenance would be lost.
    """
    monkeypatch.setenv(genai.ENV_KEY, FAKE_KEY)

    def failing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "upstream exploded"})

    async with _mock_transport(failing) as client:
        gen = await genai.generate("sys", "user", client=client)

    assert gen.live is False
    assert genai.cache_read(genai.MODEL_FAST, "sys", "user") is None
    # And nothing at all was written to disk.
    assert not isolated_cache.exists() or not list(isolated_cache.glob("*.json"))


@pytest.mark.asyncio
async def test_cache_is_written_on_success_and_served_on_later_failure(monkeypatch):
    """The whole point of the cache: warm it live, then survive an outage honestly."""
    monkeypatch.setenv(genai.ENV_KEY, FAKE_KEY)

    async with _mock_transport(lambda r: _ok_completion("Say the address out loud.")) as c:
        live = await genai.generate("sys", "user", client=c)
    assert live.live is True

    # Now the provider goes down entirely.
    def dead(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    async with _mock_transport(dead) as c:
        fallback = await genai.generate("sys", "user", client=c)

    # The user still sees the last real text...
    assert fallback.text == "Say the address out loud."
    # ...but it is unambiguously labelled as not live, with the reason and provenance.
    assert fallback.live is False
    assert fallback.error and "showing last saved response" in fallback.error


@pytest.mark.asyncio
async def test_cache_key_separates_models_and_prompts(monkeypatch):
    """A cached Flash answer must never be served as a Pro answer, or vice versa.

    They are different artefacts, and serving one as the other would misreport `model`
    on the Generation the UI renders.
    """
    monkeypatch.setenv(genai.ENV_KEY, FAKE_KEY)
    async with _mock_transport(lambda r: _ok_completion("flash text")) as c:
        await genai.generate("sys", "user", model=genai.MODEL_FAST, client=c)

    assert genai.cache_read(genai.MODEL_FAST, "sys", "user") is not None
    assert genai.cache_read(genai.MODEL_DEEP, "sys", "user") is None
    assert genai.cache_read(genai.MODEL_FAST, "sys", "different user") is None


def test_corrupt_cache_entry_degrades_to_none(isolated_cache):
    """A truncated or hand-edited cache file must not raise on a crisis screen."""
    isolated_cache.mkdir(parents=True, exist_ok=True)
    key = genai._cache_key(genai.MODEL_FAST, "sys", "user")
    (isolated_cache / f"{key}.json").write_text("{not json", encoding="utf-8")

    assert genai.cache_read(genai.MODEL_FAST, "sys", "user") is None


def test_empty_text_is_never_cached(isolated_cache):
    """An empty body would be a useless fallback and a misleading one."""
    genai.cache_write(genai.MODEL_FAST, "sys", "user", "   ")
    assert genai.cache_read(genai.MODEL_FAST, "sys", "user") is None


# --------------------------------------------------------------------------------------
# Retry policy
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limit_is_retried_then_surfaced_clearly(monkeypatch):
    """429 is transient, so retry — but say "rate limited", not something generic."""
    monkeypatch.setenv(genai.ENV_KEY, FAKE_KEY)
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(429, json={"error": "slow down"})

    async with _mock_transport(handler) as client:
        gen = await genai.generate("sys", "user", client=client)

    assert attempts == genai.MAX_ATTEMPTS
    assert gen.live is False
    assert genai.ERR_RATE_LIMIT in gen.error


@pytest.mark.asyncio
async def test_transient_failure_then_success_is_live(monkeypatch):
    """A retry that succeeds is a genuine live generation — not downgraded."""
    monkeypatch.setenv(genai.ENV_KEY, FAKE_KEY)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, json={"error": "unavailable"})
        return _ok_completion("recovered")

    async with _mock_transport(handler) as client:
        gen = await genai.generate("sys", "user", client=client)

    assert calls == 2
    assert gen.live is True and gen.text == "recovered"


@pytest.mark.asyncio
async def test_auth_error_is_not_retried(monkeypatch):
    """PRD-adjacent product rule: never burn a crisis screen retrying a 401.

    Retrying an auth failure cannot succeed, adds seconds of dead air, and can trip
    provider abuse protections.
    """
    monkeypatch.setenv(genai.ENV_KEY, FAKE_KEY)
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(401, json={"error": "invalid api key"})

    async with _mock_transport(handler) as client:
        gen = await genai.generate("sys", "user", client=client)

    assert attempts == 1, "retried an auth error"
    assert gen.live is False
    # The distinction the task requires: a rejected key is not the same UI state as no
    # key at all, because the recovery action is different.
    assert genai.ERR_AUTH in gen.error
    assert genai.ERR_NO_KEY not in gen.error


@pytest.mark.asyncio
async def test_timeout_is_retried_and_labelled(monkeypatch):
    """A hung provider must produce a timeout error, not a hung request."""
    monkeypatch.setenv(genai.ENV_KEY, FAKE_KEY)
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("timed out")

    async with _mock_transport(handler) as client:
        gen = await genai.generate("sys", "user", client=client)

    assert attempts == genai.MAX_ATTEMPTS
    assert genai.ERR_TIMEOUT in gen.error


@pytest.mark.asyncio
async def test_malformed_success_body_is_not_labelled_live(monkeypatch):
    """A 200 with no usable content is a failed generation, not an empty success."""
    monkeypatch.setenv(genai.ENV_KEY, FAKE_KEY)

    async with _mock_transport(lambda r: httpx.Response(200, json={"choices": []})) as c:
        gen = await genai.generate("sys", "user", client=c)

    assert gen.live is False
    assert genai.ERR_BAD_RESPONSE in gen.error


# --------------------------------------------------------------------------------------
# Streaming
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_yields_deltas_then_a_live_generation(monkeypatch):
    """The frontend needs tokens as they arrive AND a terminal verdict on the whole."""
    monkeypatch.setenv(genai.ENV_KEY, FAKE_KEY)

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(
            200,
            text=_sse("Still ", "here", "."),
            headers={"content-type": "text/event-stream"},
        )

    async with _mock_transport(handler) as client:
        items = [
            item
            async for item in genai.generate_stream("sys", "user", client=client)
        ]

    deltas = [d for d, g in items if g is None]
    assert deltas == ["Still ", "here", "."]

    _, final = items[-1]
    assert final.live is True and final.text == "Still here."
    # A completed stream is a real generation, so it warms the fallback cache too.
    assert genai.cache_read(genai.MODEL_FAST, "sys", "user")[0] == "Still here."


@pytest.mark.asyncio
async def test_mid_stream_failure_returns_partial_text_marked_not_live(monkeypatch):
    """Tokens already on screen must not be relabelled as a complete live reply.

    This is the subtlest way a fallback could masquerade as live: the text looks real
    because it *is* real — it is just truncated. The Generation says so.
    """
    monkeypatch.setenv(genai.ENV_KEY, FAKE_KEY)

    def handler(request: httpx.Request) -> httpx.Response:
        # A stream that stops without [DONE], then the connection drops on read.
        return httpx.Response(
            200,
            stream=_BrokenStream(_sse("I'm ", "still ", done=False)),
            headers={"content-type": "text/event-stream"},
        )

    async with _mock_transport(handler) as client:
        items = [
            item
            async for item in genai.generate_stream("sys", "user", client=client)
        ]

    _, final = items[-1]
    assert final.live is False, "truncated stream labelled live"
    assert final.error and "cut off" in final.error
    assert final.text == "I'm still"
    # Critically: a truncated reply must never be cached as a good fallback.
    assert genai.cache_read(genai.MODEL_FAST, "sys", "user") is None


class _BrokenStream(httpx.AsyncByteStream):
    """Emits some bytes, then raises — simulating a connection dropped mid-stream."""

    def __init__(self, body: str) -> None:
        self._body = body.encode("utf-8")

    async def __aiter__(self):
        yield self._body
        raise httpx.ReadError("connection dropped")


@pytest.mark.asyncio
async def test_stream_survives_a_malformed_sse_frame(monkeypatch):
    """One bad frame must not discard a reply the user is already reading."""
    monkeypatch.setenv(genai.ENV_KEY, FAKE_KEY)
    body = (
        ": keep-alive\n\n"
        'data: {"choices":[{"delta":{"content":"good "}}]}\n\n'
        "data: {not json\n\n"
        'data: {"choices":[{"delta":{"content":"still good"}}]}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    async with _mock_transport(handler) as client:
        items = [i async for i in genai.generate_stream("sys", "user", client=client)]

    _, final = items[-1]
    assert final.live is True
    assert final.text == "good still good"


@pytest.mark.asyncio
async def test_stream_auth_failure_is_not_retried(monkeypatch):
    """Same non-retry rule as the blocking path, on the SSE path."""
    monkeypatch.setenv(genai.ENV_KEY, FAKE_KEY)
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(403, json={"error": "forbidden"})

    async with _mock_transport(handler) as client:
        items = [i async for i in genai.generate_stream("sys", "user", client=client)]

    assert attempts == 1
    _, final = items[-1]
    assert final.live is False and genai.ERR_AUTH in final.error


# --------------------------------------------------------------------------------------
# Security: the key must never leave this module
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_key_never_appears_in_a_generation_on_any_failure(monkeypatch, profile):
    """Sweep every failure mode and assert the key is in none of the serialised output.

    Checks the full JSON dump, not just `error`, so a leak into `text` or `model` would
    also be caught.
    """
    monkeypatch.setenv(genai.ENV_KEY, FAKE_KEY)

    responses = [
        lambda r: httpx.Response(401, json={"error": f"bad key {FAKE_KEY}"}),
        lambda r: httpx.Response(429, json={"error": f"limit for {FAKE_KEY}"}),
        lambda r: httpx.Response(500, text=f"upstream saw Bearer {FAKE_KEY}"),
        lambda r: (_ for _ in ()).throw(httpx.ConnectError(f"failed with {FAKE_KEY}")),
    ]

    for handler in responses:
        async with _mock_transport(handler) as client:
            gen = await genai.generate("sys", "user", client=client)
        dumped = json.dumps(gen.model_dump())
        assert FAKE_KEY not in dumped, f"key leaked into Generation: {dumped}"
        assert "sk-or-" not in dumped, f"key-shaped string leaked: {dumped}"
        assert "Bearer" not in dumped, f"authorization header echoed: {dumped}"


@pytest.mark.asyncio
async def test_key_never_appears_in_logs(monkeypatch, caplog):
    """Nothing this module logs may contain the key, in any log record, at any level."""
    monkeypatch.setenv(genai.ENV_KEY, FAKE_KEY)
    caplog.set_level(logging.DEBUG, logger="threshold.genai")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text=f"boom sk-or-v1-LEAK {FAKE_KEY}")

    async with _mock_transport(handler) as client:
        await genai.generate("sys", "user", client=client)

    combined = "\n".join(r.getMessage() for r in caplog.records)
    assert FAKE_KEY not in combined
    assert "sk-or-" not in combined


def test_redact_removes_key_shaped_strings(monkeypatch):
    """Unit-test the redactor directly, including a key it has never seen."""
    monkeypatch.setenv(genai.ENV_KEY, FAKE_KEY)

    assert FAKE_KEY not in genai._redact(f"error with {FAKE_KEY}")
    # An unrelated key-shaped token is caught by pattern, not by exact match.
    assert "sk-or-" not in genai._redact("stray sk-or-v1-SOMEOTHERKEY000000 here")
    assert "Bearer" not in genai._redact("Authorization: Bearer abc123def456ghi")
    # Ordinary text is left alone — over-redaction would hide real errors.
    assert genai._redact("connection refused") == "connection refused"


def test_key_is_sent_only_in_the_authorization_header(monkeypatch):
    """The key belongs in one header and must never appear in a request body."""
    monkeypatch.setenv(genai.ENV_KEY, FAKE_KEY)

    headers = genai._headers(FAKE_KEY)
    assert headers["Authorization"] == f"Bearer {FAKE_KEY}"

    body = json.dumps(genai._body("m", "sys", "user", stream=False, max_tokens=10))
    assert FAKE_KEY not in body


# --------------------------------------------------------------------------------------
# Vault selection: defensive parsing of model JSON
# --------------------------------------------------------------------------------------


def test_vault_parse_accepts_clean_json(clips):
    """The happy path: a valid id and reason parse cleanly."""
    raw = '{"clip_id": "clip-sponsor", "reason": "Dee has taken this call before."}'
    clip, reason, clean = vault_select_prompt.parse_selection(raw, clips)

    assert clip.id == "clip-sponsor"
    assert reason == "Dee has taken this call before."
    assert clean is True


def test_vault_parse_extracts_json_from_a_fenced_reply(clips):
    """Models wrap JSON in fences despite instructions; that is not a failure."""
    raw = 'Sure!\n```json\n{"clip_id": "clip-mum", "reason": "Her mum sounds calm."}\n```'
    clip, reason, clean = vault_select_prompt.parse_selection(raw, clips)

    assert clip.id == "clip-mum"
    assert clean is True


@pytest.mark.parametrize(
    "raw,why",
    [
        ("", "empty reply"),
        ("I couldn't decide.", "prose with no JSON"),
        ('{"clip_id": "clip-mum"', "truncated JSON"),
        ('["clip-mum"]', "valid JSON of the wrong type"),
        ('{"clip_id": "clip-does-not-exist", "reason": "x"}', "hallucinated clip id"),
        ('{"clip_id": 7, "reason": "x"}', "wrong id type"),
    ],
)
def test_vault_parse_falls_back_on_malformed_output(clips, raw, why):
    """Every malformed shape degrades to a real clip and reports the parse as unclean.

    The hallucinated-id case matters most: an invented id would play nothing at all,
    which on a Tier 2 screen is a blank where a familiar voice should be.
    """
    clip, reason, clean = vault_select_prompt.parse_selection(raw, clips)

    assert clip in clips, f"{why}: did not fall back to a real clip"
    assert clean is False, f"{why}: salvaged parse reported as clean"
    assert reason, f"{why}: no caption produced"


def test_vault_parse_keeps_a_valid_id_with_an_unusable_reason(clips):
    """Half a good answer is still worth keeping — but the parse is still unclean."""
    clip, reason, clean = vault_select_prompt.parse_selection(
        '{"clip_id": "clip-sponsor", "reason": ""}', clips
    )
    assert clip.id == "clip-sponsor"
    assert clean is False
    assert reason


def test_vault_parse_requires_clips():
    """With nothing to fall back to, raising is correct — there is no safe default."""
    with pytest.raises(ValueError):
        vault_select_prompt.parse_selection("{}", [])


@pytest.mark.asyncio
async def test_vault_select_with_no_clips_is_a_ui_state_not_a_crash(profile):
    """Having recorded nothing yet is a normal state an evaluator will hit first."""
    clip, gen = await genai.vault_select(profile, [], "any context")

    assert clip is None
    assert gen.live is False
    assert gen.error


@pytest.mark.asyncio
async def test_vault_select_downgrades_a_salvaged_selection(monkeypatch, profile, clips):
    """A live call whose JSON had to be salvaged must not be presented as live.

    The call really happened, but the selection did not — so `live` is False and the
    error says the default clip was played.
    """
    monkeypatch.setenv(genai.ENV_KEY, FAKE_KEY)

    async with _mock_transport(lambda r: _ok_completion("I really can't choose.")) as c:
        monkeypatch.setattr(genai, "generate", _pin_client(genai.generate, c))
        clip, gen = await genai.vault_select(profile, clips, "a hard moment")

    assert clip in clips
    assert gen.live is False
    assert "unreadable selection" in gen.error


# --------------------------------------------------------------------------------------
# Prompt hygiene — the constraints a judge will look for
# --------------------------------------------------------------------------------------


def test_no_prompt_leaks_the_tier_to_the_model(profile):
    """PRD P4 / CONTRACT.md: the model is kept entirely out of tier decisions.

    Passing a tier into the check-in builder (as app/main.py does) must not put it in
    the prompt, and every system prompt must forbid referencing risk state.
    """
    from app.prompts import checkin

    system, user = checkin.build(profile, "I'm going to use", tier=Tier.ACTIVE_USE)
    combined = (system + user).lower()

    assert "active use" not in combined
    assert "tier" not in user.lower(), "tier leaked into the user message"
    # The prohibition itself is present in the system prompt.
    assert "never mention" in system.lower()


def test_911_script_reproduces_identifying_details_verbatim(profile):
    """The script's whole value is that the address is right, so it must be sent exactly."""
    from app.prompts import script_911

    _, user = script_911.build(profile)

    assert "418 Delacourt Street" in user
    assert "Apt 3B" in user
    assert "4471" in user
    assert "Ninth and Delacourt" in user


def test_only_the_911_prompt_receives_the_address(profile, clips):
    """Data minimisation: a door code has no business in a chat turn.

    Security is scored, and the cheapest win is not sending identifying data to a
    third-party API for tasks that do not need it.
    """
    from app.prompts import caregiver_brief, checkin, refusal, tolerance, vault_select

    others = [
        checkin.build(profile, "hey"),
        refusal.build(profile),
        tolerance.build(profile),
        caregiver_brief.build(profile, Tier.CRAVING, []),
        vault_select.build(clips, "a hard moment", profile),
    ]

    for system, user in others:
        combined = system + user
        assert profile.entry_code not in combined, "entry code sent unnecessarily"
        assert profile.address not in combined, "address sent unnecessarily"


def test_tolerance_prompt_forbids_numbers_and_frames_it_non_judgementally():
    """The lead demo feature. Its constraints are the feature."""
    from app.prompts import tolerance

    profile = UserProfile(id="u", name="Sam", address="x")
    system, _ = tolerance.build(profile)
    low = system.lower()

    # No fabricated clinical numbers — the hard constraint from the task brief.
    assert "no numbers of any kind" in low
    # The core message: tolerance drops, the remembered dose can kill.
    assert "tolerance drops" in low
    # The two behaviours that actually change the outcome.
    assert "slower" in low and "alone" in low
    # Non-judgemental framing, explicitly.
    assert "never shame" in low or "no praise" in low


def test_tolerance_elapsed_is_vague_never_a_day_count():
    """A precise day count invites numeric risk reasoning, which the rules forbid."""
    from app.prompts import tolerance

    now = datetime(2026, 7, 25, tzinfo=timezone.utc)
    phrase = tolerance._describe_elapsed(datetime(2026, 7, 10, tzinfo=timezone.utc), now)

    assert phrase == "a couple of weeks ago"
    assert not any(ch.isdigit() for ch in phrase)


def test_every_person_facing_prompt_carries_the_safety_rules(profile, clips):
    """The non-negotiables must be in every system prompt, not just most of them."""
    from app.prompts import caregiver_brief, checkin, refusal, script_911, tolerance, vault_select

    builders = [
        checkin.build(profile, "hey"),
        script_911.build(profile),
        refusal.build(profile),
        tolerance.build(profile),
        caregiver_brief.build(profile, Tier.EMERGENCY, []),
        vault_select.build(clips, "context", profile),
    ]

    for system, _ in builders:
        low = system.lower()
        assert "never diagnose" in low, "missing the no-diagnosis rule"
        assert "never claim" in low and "human" in low, "missing the no-human rule"
        assert "legal" in low, "missing the no-legal-claims rule"


def test_caregiver_brief_includes_the_craft_grounded_do_not_section():
    """CRAFT: telling a panicking caregiver what to hold back is the active ingredient."""
    from app.prompts import caregiver_brief

    profile = UserProfile(id="u", name="Sam", address="x")
    events = [
        Event(
            id="e1",
            user_id="u",
            at=datetime(2026, 7, 25, 3, 12, tzinfo=timezone.utc),
            tier=Tier.EMERGENCY,
            trigger_source="sensor",
            reason="no response for 20 seconds after stillness",
            actions_taken=["fire_contact_tree", "show_911_script"],
        )
    ]
    system, user = caregiver_brief.build(profile, Tier.EMERGENCY, events)

    for section in (
        "WHAT HAPPENED",
        "WHAT THE SYSTEM ALREADY DID",
        "NEXT 60 SECONDS",
        "WHAT NOT TO DO RIGHT NOW",
    ):
        assert section in system, f"missing section: {section}"

    assert "do not accuse" in system.lower()
    assert "hide" in system.lower(), "missing the reason confrontation backfires"
    # The reason is recovered from the log rather than invented.
    assert "no response for 20 seconds" in user
    # And the tier is never named to the model.
    assert "tier" not in user.lower()
