"""Spoken output: synthetic narration, and consented caregiver voice cloning.

WHAT THIS MODULE DOES
    Wraps ElevenLabs text-to-speech. Two distinct jobs, deliberately separated:

    1. NARRATION — the app's own speaking voice. The member picks any stock voice
       and it reads scripts, prompts and guidance aloud. Nobody is imitated.
    2. CLONED SUPPORTER VOICE — a voice model built from recordings a specific
       supporter made and consented to, which the member may choose to hear
       instead of the stock narrator.

WHAT THIS MODULE DELIBERATELY DOES NOT DO
    * It never clones a voice from audio the app did not receive with explicit,
      recorded consent from the person being cloned. There is no "upload any
      clip" path, and no path that clones from Memory Vault audio silently.
    * It never synthesises a Memory Vault clip. Those are real recordings and
      play as recorded. If a supporter recorded it, that is what plays.
    * It never speaks as a cloned supporter without labelling it in the UI.
    * It makes no triage decision and holds no clinical logic.

WHY THIS FEATURE IS DELIBERATELY CONSENTED
    A familiar caregiver voice can reduce cognitive load when reading is hard,
    but it also carries relationship-level risk: generated words may be heard
    as the real person's words, and a calm-time choice is used during crisis.
    The PRD therefore includes cloning only with structural safeguards:
      - the SUPPORTER consents, in their own account, to their own voice;
      - the MEMBER chooses to enable it, and can turn it off in one action;
      - every cloned utterance is visibly labelled as an AI voice in the UI;
      - the model is deletable, and deleting a supporter link deletes it;
      - a cloned voice is NEVER used to claim presence — it does not say "I am
        here with you" or anything implying the real person is live on the line,
        which is the P5 line we are not crossing.

    These controls are the feature boundary, not optional disclosure copy.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

import httpx

log = logging.getLogger("threshold.voice")

_API_ROOT = "https://api.elevenlabs.io/v1"

# Normal interventions favor the most expressive model. Urgent instructions
# use the low-latency Flash path so cloud quality never delays rescue guidance.
# Both remain deployment-configurable without a code change.
_TTS_MODEL_EXPRESSIVE = os.getenv("ELEVENLABS_TTS_MODEL", "eleven_v3").strip()
_TTS_MODEL_URGENT = os.getenv(
    "ELEVENLABS_URGENT_TTS_MODEL", "eleven_flash_v2_5"
).strip()

# A stock narrator used when the member has expressed no preference. Calm and
# unhurried rather than bright — this voice reads overdose instructions.
_DEFAULT_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"

_TIMEOUT = httpx.Timeout(connect=4.0, read=20.0, write=10.0, pool=4.0)
_client: httpx.AsyncClient | None = None


async def startup() -> None:
    """Create the shared provider client during application startup."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=_TIMEOUT)


def _http() -> httpx.AsyncClient:
    """Return one connection-pooled ElevenLabs client."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=_TIMEOUT)
    return _client


async def close() -> None:
    """Close the pooled provider client during application shutdown."""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


@dataclass(frozen=True)
class Speech:
    """One synthesis result.

    Attributes:
        audio: MP3 bytes, empty when synthesis failed.
        live: True only when audio came back from a real API call. The UI must
            fall back to the browser's own speech synthesis when this is False
            rather than going silent — silence during an emergency is the one
            unacceptable outcome.
        voice_id: Which voice actually spoke.
        cloned: True when this was a cloned supporter voice, so the UI can
            label it. Never inferred at render time; carried explicitly.
        error: Why it failed, safe to display. Never contains the API key.
    """

    audio: bytes
    live: bool
    voice_id: str
    cloned: bool = False
    error: str | None = None
    latency_ms: int = 0
    model_id: str = ""


def _key() -> str:
    """Read the API key at call time.

    Deliberately not a module-level constant: the app must boot without a key
    and start working the moment one is exported, with no code change and no
    restart-ordering trap.
    """
    return os.getenv("ELEVENLABS_API_KEY", "").strip()


def is_online() -> bool:
    """Whether cloud speech is configured. Reported honestly in the UI."""
    return bool(_key())


def _redact(text: str) -> str:
    """Strip the API key from anything we are about to log or display."""
    k = _key()
    return text.replace(k, "[redacted]") if k else text


async def synthesize(
    text: str,
    *,
    voice_id: str | None = None,
    cloned: bool = False,
    urgent: bool = False,
) -> Speech:
    """Speak `text` in `voice_id`.

    Args:
        text: What to say. Bounded by the caller; long input costs money and
            latency, and nothing this app says aloud is long.
        voice_id: Stock or cloned voice. Falls back to the default narrator.
        cloned: Whether this voice is a cloned supporter, for UI labelling.

    Returns:
        A Speech. On any failure it returns live=False with an error rather
        than raising: the caller then falls back to browser speech, because
        going silent mid-emergency is worse than sounding robotic.
    """
    key = _key()
    vid = voice_id or _DEFAULT_VOICE_ID
    model_id = _TTS_MODEL_URGENT if urgent else _TTS_MODEL_EXPRESSIVE
    started = time.monotonic()

    if not key:
        return Speech(
            b"", False, vid, cloned, "Cloud voice offline — no API key",
            model_id=model_id,
        )

    # Hard ceiling. This is spoken aloud, so anything long is a bug upstream.
    text = text.strip()[:900]
    if not text:
        return Speech(b"", False, vid, cloned, "Nothing to say", model_id=model_id)

    try:
        async def request(model: str) -> httpx.Response:
            return await _http().post(
                f"{_API_ROOT}/text-to-speech/{vid}",
                headers={"xi-api-key": key, "Content-Type": "application/json"},
                json={
                "text": text,
                "model_id": model,
                "voice_settings": {
                    "stability": 0.65 if urgent else 0.5,
                    "similarity_boost": 0.8,
                    "style": 0.0 if urgent else 0.3,
                    "use_speaker_boost": True,
                },
            },
            )

        res = await request(model_id)
        if (
            res.status_code != 200
            and urgent
            and model_id != _TTS_MODEL_EXPRESSIVE
        ):
            # Flash is the latency preference, not a single point of failure.
            # ElevenLabs can transiently reject one model while v3 remains
            # healthy. Retry once within the cloud provider before asking the
            # browser to fall back to its robotic local voice.
            log.warning(
                "urgent tts model returned HTTP %s; retrying with %s",
                res.status_code,
                _TTS_MODEL_EXPRESSIVE,
            )
            model_id = _TTS_MODEL_EXPRESSIVE
            res = await request(model_id)
        if res.status_code != 200:
            # Never surface the provider's raw body — it can echo the request.
            return Speech(
                b"", False, vid, cloned,
                f"Voice unavailable (HTTP {res.status_code})",
                int((time.monotonic() - started) * 1000),
                model_id,
            )
        return Speech(
            res.content, True, vid, cloned, None,
            int((time.monotonic() - started) * 1000),
            model_id,
        )
    except Exception as exc:
        log.warning("tts failed: %s", _redact(str(exc)))
        return Speech(
            b"", False, vid, cloned, "Voice unavailable",
            int((time.monotonic() - started) * 1000),
            model_id,
        )


async def list_voices() -> list[dict]:
    """Stock voices the member can pick for the app's narration.

    Returns a trimmed projection rather than the provider's full payload: the
    picker needs a name and an id, and forwarding everything else would leak
    provider detail into our client for no benefit.
    """
    if not _key():
        return []
    try:
        res = await _http().get(
            f"{_API_ROOT}/voices", headers={"xi-api-key": _key()}
        )
        if res.status_code != 200:
            return []
        return [
            {
                "voice_id": v.get("voice_id"),
                "name": v.get("name"),
                "cloned": v.get("category") == "cloned",
            }
            for v in res.json().get("voices", [])
            if v.get("voice_id")
        ]
    except Exception as exc:
        log.warning("voice list failed: %s", _redact(str(exc)))
        return []


async def clone_supporter_voice(
    display_name: str, samples: list[tuple[str, bytes]]
) -> tuple[str | None, str | None]:
    """Create a voice model from a supporter's consented recordings.

    THE CONSENT GATE IS THE CALLER'S RESPONSIBILITY AND IT IS NOT OPTIONAL.
    This function does the mechanical work; it cannot verify who is speaking in
    the audio. The route that calls it must establish that the person being
    cloned is the authenticated account holder, that they ticked an explicit
    consent statement in their own session, and that the consent is recorded
    with a timestamp. Cloning a third party from audio they did not knowingly
    provide is the abuse case this feature makes possible, and the gate is the
    only thing preventing it.

    Args:
        display_name: Label shown to the member, e.g. "Sarah's voice".
        samples: (filename, audio bytes) pairs recorded by that supporter.

    Returns:
        (voice_id, None) on success, or (None, error) on failure.
    """
    if not _key():
        return None, "Cloud voice offline — no API key"
    if not samples:
        return None, "No recordings provided"

    try:
        files = [("files", (name, blob, "audio/mpeg")) for name, blob in samples]
        res = await _http().post(
            f"{_API_ROOT}/voices/add",
            headers={"xi-api-key": _key()},
            data={
                "name": display_name[:64],
                # Stored on the provider so a later audit can show the
                # consent basis without opening our database.
                "description": "Consented supporter voice, Threshold",
            },
            files=files,
            timeout=httpx.Timeout(60.0),
        )
        if res.status_code not in (200, 201):
            return None, f"Could not create voice (HTTP {res.status_code})"
        return res.json().get("voice_id"), None
    except Exception as exc:
        log.warning("clone failed: %s", _redact(str(exc)))
        return None, "Could not create voice"


async def delete_voice(voice_id: str) -> bool:
    """Delete a cloned voice model.

    Revocation must be real and must be easy. §7.2's hardest objection is what
    happens when a relationship ends or the person dies — the least we can do is
    make deletion a single call that actually removes the model upstream, rather
    than only unlinking it on our side.
    """
    if not _key() or not voice_id:
        return False
    try:
        res = await _http().delete(
            f"{_API_ROOT}/voices/{voice_id}", headers={"xi-api-key": _key()}
        )
        return res.status_code in (200, 204)
    except Exception as exc:
        log.warning("voice delete failed: %s", _redact(str(exc)))
        return False
