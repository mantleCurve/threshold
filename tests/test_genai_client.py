"""Tests for the shared `httpx.AsyncClient` lifecycle in `app/genai.py`.

WHAT THESE TESTS PROTECT AGAINST
    1. **A regression back to one client per generation** (c_s.md Efficiency P2 #2).
       Every generation used to build its own client, its own TCP connection, and its
       own TLS handshake. On a page that warms three surfaces at once that is three
       handshakes to the same host, paid again on every reload. The test below proves
       the pooled client is actually the one used.
    2. **A crash at startup when no API key is configured.** That is the *normal* state
       today (CONTRACT.md), so `startup()` must be safe with no key and must not
       consult one.
    3. **A leaked pool on shutdown**, and a shutdown that raises on an unclean exit
       path and masks the real failure.

WHAT THEY DELIBERATELY DO NOT DO
    They make no real network call and require no credential — every HTTP interaction
    is mocked at the httpx transport layer, exactly as `tests/test_genai.py` does.
"""

from __future__ import annotations

import httpx
import pytest

from app import genai

# Recognisably fake, in OpenRouter's format. Never a valid credential.
FAKE_KEY = "sk-or-v1-TESTKEY0000000000000000000000000000"


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    """Point the disk fallback cache at a temp dir so tests never touch `data/cache/`."""
    monkeypatch.setattr(genai, "CACHE_DIR", tmp_path / "cache")


@pytest.fixture(autouse=True)
def no_key_by_default(monkeypatch):
    """Start every test in the offline state, which is the real state today."""
    monkeypatch.delenv(genai.ENV_KEY, raising=False)


@pytest.fixture(autouse=True)
def clean_client():
    """Guarantee no shared client leaks between tests or into the API suite.

    The client is a module global by design (this module owns the only socket in the
    app), so it has to be torn down explicitly rather than garbage-collected.
    """
    genai._client = None
    yield
    genai._client = None


# --------------------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_startup_is_safe_with_no_api_key():
    """The expected state today: the app must boot cleanly with no key exported.

    Creating an AsyncClient opens no socket by itself — connections are lazy — so an
    unused pool costs nothing and the key is deliberately never consulted here.
    """
    assert genai.ai_online() is False
    await genai.startup()
    assert genai._shared_client() is not None
    await genai.shutdown()


@pytest.mark.asyncio
async def test_startup_is_idempotent_and_does_not_strand_a_client():
    """Driving the lifespan twice must not leak the first pool."""
    await genai.startup()
    first = genai._shared_client()
    await genai.startup()
    assert genai._shared_client() is first
    await genai.shutdown()


@pytest.mark.asyncio
async def test_shutdown_closes_the_pool_and_clears_the_global():
    await genai.startup()
    client = genai._shared_client()
    await genai.shutdown()

    assert genai._shared_client() is None
    assert client.is_closed


@pytest.mark.asyncio
async def test_shutdown_without_startup_is_a_no_op():
    """An unclean exit path must not raise on the way out and mask the real failure."""
    await genai.shutdown()
    await genai.shutdown()
    assert genai._shared_client() is None


@pytest.mark.asyncio
async def test_key_exported_after_startup_still_goes_live(monkeypatch):
    """CONTRACT.md: the key works the moment it is exported, with no restart.

    The pool is created before any key exists, so this proves the pool does not capture
    or depend on the credential.
    """
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "hello"}}]})

    await genai.startup()
    genai._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    # Key arrives only now, after the pool already exists.
    monkeypatch.setenv(genai.ENV_KEY, FAKE_KEY)
    gen = await genai.generate("sys", "user")

    assert gen.live is True
    assert calls and calls[0].headers["authorization"] == f"Bearer {FAKE_KEY}"
    await genai.shutdown()


# --------------------------------------------------------------------------------------
# Reuse — the actual efficiency claim
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generations_reuse_the_shared_client_instead_of_building_one_each(
    monkeypatch,
):
    """The regression net for c_s.md Efficiency #2.

    A mock transport is installed on the *shared* client only. If any generation built
    its own client it would bypass the mock and attempt a real connection, so three
    successful mocked calls prove all three went through the one pooled client.
    """
    monkeypatch.setenv(genai.ENV_KEY, FAKE_KEY)

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    genai._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    shared = genai._client

    for prompt in ("one", "two", "three"):
        gen = await genai.generate("sys", prompt)
        assert gen.live is True

    assert len(seen) == 3
    assert genai._shared_client() is shared, "a call replaced the shared client"
    await genai.shutdown()


@pytest.mark.asyncio
async def test_an_explicit_client_argument_still_wins_over_the_shared_pool(monkeypatch):
    """Precedence: explicit client > shared pool > throwaway.

    The explicit seam is what `tests/test_genai.py` uses, so it must keep taking
    priority even once a pool exists.
    """
    monkeypatch.setenv(genai.ENV_KEY, FAKE_KEY)

    shared_hits: list[str] = []
    explicit_hits: list[str] = []

    def shared_handler(request: httpx.Request) -> httpx.Response:
        shared_hits.append("shared")
        return httpx.Response(200, json={"choices": [{"message": {"content": "shared"}}]})

    def explicit_handler(request: httpx.Request) -> httpx.Response:
        explicit_hits.append("explicit")
        return httpx.Response(200, json={"choices": [{"message": {"content": "explicit"}}]})

    genai._client = httpx.AsyncClient(transport=httpx.MockTransport(shared_handler))
    async with httpx.AsyncClient(transport=httpx.MockTransport(explicit_handler)) as c:
        gen = await genai.generate("sys", "user", client=c)

    assert gen.text == "explicit"
    assert shared_hits == []
    assert explicit_hits == ["explicit"]
    await genai.shutdown()


@pytest.mark.asyncio
async def test_streaming_also_uses_the_shared_client(monkeypatch):
    """The streamed check-in reply is the one generation a person is waiting to hear.

    Removing a TLS handshake removes it from the front of that wait, so the streaming
    path must not be left on the old per-call-client behaviour.
    """
    import json as _json

    monkeypatch.setenv(genai.ENV_KEY, FAKE_KEY)

    body = (
        "\n".join(
            "data: " + _json.dumps({"choices": [{"delta": {"content": d}}]})
            for d in ("hel", "lo")
        )
        + "\ndata: [DONE]\n"
    )

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text=body)

    genai._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    deltas, final = [], None
    async for delta, gen in genai.generate_stream("sys", "user"):
        if gen is None:
            deltas.append(delta)
        else:
            final = gen

    assert "".join(deltas) == "hello"
    assert final is not None and final.live is True
    assert len(seen) == 1
    await genai.shutdown()


@pytest.mark.asyncio
async def test_no_key_short_circuits_before_any_request_is_made():
    """With no key there is nothing to send, so the pool must not be touched at all."""
    touched: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        touched.append(request)
        return httpx.Response(200, json={})

    genai._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    gen = await genai.generate("sys", "user")

    assert gen.live is False
    assert gen.error == f"{genai.ERR_NO_KEY} — no saved response available"
    assert touched == []
    await genai.shutdown()


# --------------------------------------------------------------------------------------
# Timeouts must survive the move to a shared client
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_deep_model_keeps_its_longer_read_budget(monkeypatch):
    """The shared client carries no default timeout, so it is passed per request.

    A client-level default would silently cap the deep model's 45s read budget at
    whatever the pool happened to carry — a real risk when one client serves two paths
    whose budgets differ by a factor of four.
    """
    monkeypatch.setenv(genai.ENV_KEY, FAKE_KEY)

    timeouts: list[object] = []

    def handler(request: httpx.Request) -> httpx.Response:
        timeouts.append(request.extensions.get("timeout"))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    genai._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    await genai.generate("sys", "user", model=genai.MODEL_DEEP)
    await genai.generate("sys", "user", model=genai.MODEL_FAST)

    assert timeouts[0]["read"] == genai.TIMEOUT_DEEP.read
    assert timeouts[1]["read"] == genai.TIMEOUT_FAST.read
    assert timeouts[0]["read"] != timeouts[1]["read"]
    await genai.shutdown()
