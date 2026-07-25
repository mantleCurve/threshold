"""Provider-adapter tests: real request shapes, deterministic fake transports."""

from __future__ import annotations

import httpx
import pytest

from app import email, voice


@pytest.fixture
def anyio_backend():
    return "asyncio"


class Response:
    def __init__(self, status_code=200, payload=None, content=b"ID3-audio"):
        self.status_code = status_code
        self._payload = payload or {}
        self.content = content
        self.is_success = 200 <= status_code < 300

    def json(self):
        return self._payload


class Client:
    def __init__(self, response=None, error=None):
        self.response = response or Response()
        self.error = error
        self.calls = []
        self.is_closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, url, **kwargs):
        self.calls.append(("post", url, kwargs))
        if self.error:
            raise self.error
        return self.response

    async def get(self, url, **kwargs):
        self.calls.append(("get", url, kwargs))
        if self.error:
            raise self.error
        return self.response

    async def delete(self, url, **kwargs):
        self.calls.append(("delete", url, kwargs))
        if self.error:
            raise self.error
        return self.response

    async def aclose(self):
        self.is_closed = True


@pytest.mark.anyio
async def test_email_delivery_success_and_payload(monkeypatch):
    client = Client()
    monkeypatch.setenv("RESEND_API_KEY", "resend-secret")
    monkeypatch.setenv("THRESHOLD_EMAIL_FROM", "Threshold <verify@example.com>")
    monkeypatch.setattr(email, "_http", lambda: client)

    assert await email.send_verification_code(
        "member@example.com", "123456", idempotency_key="signup/1"
    ) == (True, None)
    assert await email.send_caregiver_alert(
        "caregiver@example.com",
        "Alex",
        tier_name="Medical emergency",
        idempotency_key="alert/1",
    ) == (True, None)
    assert client.calls[0][2]["json"]["to"] == ["member@example.com"]
    assert "123456" in client.calls[0][2]["json"]["text"]
    assert "Alex" in client.calls[1][2]["json"]["subject"]


@pytest.mark.anyio
async def test_email_offline_refusal_timeout_and_network(monkeypatch):
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    assert email.is_online() is False
    assert (await email.send_verification_code("a@b.co", "1", idempotency_key="x"))[0] is False
    assert (await email.send_caregiver_alert(
        "a@b.co", "A", tier_name="Emergency", idempotency_key="x"
    ))[0] is False

    monkeypatch.setenv("RESEND_API_KEY", "key")
    refused = Client(Response(400))
    monkeypatch.setattr(email, "_http", lambda: refused)
    assert (await email.send_verification_code("a@b.co", "1", idempotency_key="x"))[0] is False
    assert (await email.send_caregiver_alert(
        "a@b.co", "A", tier_name="Emergency", idempotency_key="x"
    ))[0] is False

    monkeypatch.setattr(
        email, "_http", lambda: Client(error=httpx.ReadTimeout("slow"))
    )
    assert "timed out" in (await email.send_verification_code(
        "a@b.co", "1", idempotency_key="x"
    ))[1]
    monkeypatch.setattr(
        email, "_http", lambda: Client(error=httpx.ConnectError("down"))
    )
    assert "could not be sent" in (await email.send_verification_code(
        "a@b.co", "1", idempotency_key="x"
    ))[1]
    assert "could not be delivered" in (await email.send_caregiver_alert(
        "a@b.co", "A", tier_name="Emergency", idempotency_key="x"
    ))[1]


@pytest.mark.anyio
async def test_email_shared_client_lifecycle(monkeypatch):
    email._client = None
    made = Client()
    monkeypatch.setattr(email.httpx, "AsyncClient", lambda **kwargs: made)
    assert email._http() is made
    assert email._http() is made
    await email.close()
    assert made.is_closed is True
    assert email._client is None
    await email.close()


@pytest.mark.anyio
async def test_voice_model_routing_and_failures(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "voice-secret")
    expressive = Client(Response(content=b"expressive"))
    monkeypatch.setattr(voice, "_http", lambda: expressive)
    result = await voice.synthesize("Take a breath.")
    assert result.live is True
    assert result.model_id == "eleven_v3"
    assert expressive.calls[0][2]["json"]["model_id"] == "eleven_v3"

    urgent = Client(Response(content=b"urgent"))
    monkeypatch.setattr(voice, "_http", lambda: urgent)
    result = await voice.synthesize("Call 911.", urgent=True)
    assert result.model_id == "eleven_flash_v2_5"
    assert urgent.calls[0][2]["json"]["model_id"] == "eleven_flash_v2_5"

    monkeypatch.setattr(voice, "_http", lambda: Client(Response(429)))
    assert (await voice.synthesize("Hello")).live is False
    monkeypatch.setattr(
        voice, "_http", lambda: Client(error=httpx.ConnectError("voice-secret"))
    )
    failed = await voice.synthesize("Hello")
    assert failed.live is False
    assert "voice-secret" not in (failed.error or "")


@pytest.mark.anyio
async def test_voice_listing_cloning_deletion_and_offline(monkeypatch):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    assert voice.is_online() is False
    assert await voice.list_voices() == []
    assert (await voice.clone_supporter_voice("Caregiver", []))[0] is None
    assert await voice.delete_voice("") is False

    monkeypatch.setenv("ELEVENLABS_API_KEY", "key")
    listing = Client(Response(payload={"voices": [
        {"voice_id": "stock", "name": "Calm", "category": "premade"},
        {"voice_id": "clone", "name": "Caregiver", "category": "cloned"},
        {"name": "broken"},
    ]}))
    monkeypatch.setattr(voice, "_http", lambda: listing)
    assert [v["voice_id"] for v in await voice.list_voices()] == ["stock", "clone"]

    created = Client(Response(201, {"voice_id": "new-clone"}))
    monkeypatch.setattr(voice, "_http", lambda: created)
    assert await voice.clone_supporter_voice(
        "Caregiver", [("sample.mp3", b"audio")]
    ) == ("new-clone", None)

    deleted = Client(Response(204))
    monkeypatch.setattr(voice, "_http", lambda: deleted)
    assert await voice.delete_voice("new-clone") is True

    monkeypatch.setattr(voice, "_http", lambda: Client(Response(500)))
    assert (await voice.clone_supporter_voice(
        "Caregiver", [("sample.mp3", b"audio")]
    ))[0] is None
    assert await voice.delete_voice("new-clone") is False

    monkeypatch.setattr(
        voice, "_http", lambda: Client(error=httpx.ConnectError("down"))
    )
    assert await voice.list_voices() == []
    assert (await voice.clone_supporter_voice(
        "Caregiver", [("sample.mp3", b"audio")]
    ))[0] is None
    assert await voice.delete_voice("new-clone") is False
