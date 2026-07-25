"""The consented supporter-voice feature: the consent gate and the revocation paths.

WHAT THESE TESTS PROTECT AGAINST
    This feature is the one place in the product where we build a synthetic copy
    of a real person. `app/voice.py`'s module docstring is candid that PRD §7.2
    declined it and that the objections stand; what makes shipping it defensible
    is a short list of mitigations, each of which is a line of code that a
    refactor could quietly remove. These tests pin each one:

    1. NO CONSENT, NO CLONE. Audio is never forwarded to the provider without an
       explicit tick. Asserted at the request level AND by proving the provider
       was never called.
    2. THE OWNER IS THE SESSION. There is no path by which a request can name a
       different account as the person being cloned.
    3. ROLE. Only a caregiver's own account may record a supporter voice.
    4. SHARING IS A SEPARATE DECISION. A fresh clone is unshared, and a member
       cannot hear or address an unshared voice.
    5. EITHER SIDE MAY REVOKE, and revocation deletes the model UPSTREAM.
    6. THE API KEY NEVER APPEARS IN A RESPONSE.

WHAT THEY DELIBERATELY DO NOT DO
    They never make a real ElevenLabs call. `app/voice.py`'s four network
    functions are monkeypatched with recording fakes, so the suite is
    deterministic, free, and — most importantly — able to assert on what WOULD
    have been sent. A test that hit the real API could not prove that a
    consent-less request sends nothing, because "nothing was sent" is
    unobservable from the outside.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import auth, store, voice, voice_store
from app.main import app
from app.routes import voice as voice_routes


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    """Fresh database file per test.

    `voice_store` caches which database files it has created its table in, so
    that cache is cleared alongside the path — otherwise the second test would
    believe the table already existed in a file that had just been replaced.
    """
    monkeypatch.setattr(store, "_db_path", tmp_path / "voice.db")
    voice_store._ensured.clear()
    store.init_db()
    yield
    voice_store._ensured.clear()


class FakeProvider:
    """Records what the app asked the speech provider to do.

    The assertions that matter most in this file are NEGATIVE — "the audio was
    never uploaded", "the model was deleted upstream" — and negatives are only
    observable if something is watching. This is that something.
    """

    def __init__(self) -> None:
        self.clone_calls: list[tuple[str, list]] = []
        self.deleted: list[str] = []
        self.synth_calls: list[tuple[str, str | None, bool]] = []
        self.online = True

    async def clone(self, display_name, samples):
        self.clone_calls.append((display_name, list(samples)))
        return f"provider-voice-{len(self.clone_calls)}", None

    async def delete(self, voice_id):
        self.deleted.append(voice_id)
        return True

    async def synthesize(self, text, *, voice_id=None, cloned=False):
        self.synth_calls.append((text, voice_id, cloned))
        return voice.Speech(b"ID3-fake-mp3-bytes", True, voice_id or "stock", cloned)

    async def list_voices(self):
        return [
            {"voice_id": "stock-narrator", "name": "Calm narrator", "cloned": False},
        ]


@pytest.fixture
def provider(monkeypatch):
    """Patch every network function in `app.voice` with the recording fake."""
    fake = FakeProvider()
    monkeypatch.setattr(voice, "clone_supporter_voice", fake.clone)
    monkeypatch.setattr(voice, "delete_voice", fake.delete)
    monkeypatch.setattr(voice, "synthesize", fake.synthesize)
    monkeypatch.setattr(voice, "list_voices", fake.list_voices)
    monkeypatch.setattr(voice, "is_online", lambda: fake.online)
    return fake


@pytest.fixture
def accounts():
    """A linked caregiver and member, created through the real auth path.

    The link is the privacy boundary this feature rides on: a shared voice is
    only visible through a consented `caregiver_links` row.
    """
    caregiver = auth.register("sarah_t", "threshold", role="caregiver")
    member = auth.register("sam_t", "threshold", role="user")
    store.link_caregiver(caregiver.id, member.id)
    return caregiver, member


@pytest.fixture
def client():
    """A raw client with no lifespan, so seeding does not fight the temp DB."""
    from app import security

    security._hits.clear()
    with TestClient(app) as c:
        yield c
    security._hits.clear()


def _sign_in(client, user):
    """Attach a real signed session cookie for `user` to the client.

    Uses `auth.issue_token` rather than posting to /api/auth/login so these
    tests exercise the voice gate rather than re-testing authentication, and so
    they do not consume the login rate limit.
    """
    client.cookies.set(auth.SESSION_COOKIE, auth.issue_token(user.id))


def _samples():
    """Two fake recorded passages, in the multipart shape the browser sends."""
    return [
        ("samples", ("passage-1.webm", b"fake-audio-one", "audio/webm")),
        ("samples", ("passage-2.webm", b"fake-audio-two", "audio/webm")),
    ]


def _clone(client, *, consent="true", name="Sarah's voice"):
    """POST a clone request. Returns the raw response so tests can assert status.

    Clears the rate-limit bucket first. /api/voice/clone is deliberately limited
    to 3 per 5 minutes (see `security._LIMITS`), which is right for a real
    supporter and wrong for a test that legitimately probes five rejected consent
    values in a row — a 429 partway through would mask the gate this file exists
    to assert. The limit itself is pinned separately, further down.
    """
    from app import security

    security._hits.clear()
    return client.post(
        "/api/voice/clone",
        data={"consent": consent, "display_name": name},
        files=_samples(),
    )


# ---------------------------------------------------------------------------
# 1. THE CONSENT GATE
# ---------------------------------------------------------------------------
def test_clone_without_consent_is_rejected(client, provider, accounts):
    """No tick, no clone. The single most important test in this file.

    `voice.clone_supporter_voice` states that it cannot verify who is speaking
    in the audio and that the gate is the caller's responsibility. This is that
    gate. Without it the feature is an "upload any clip and impersonate someone"
    endpoint, which is the abuse case the whole design exists to prevent.
    """
    caregiver, _ = accounts
    _sign_in(client, caregiver)

    assert _clone(client, consent="false").status_code == 400
    assert not voice_store.list_for_caregiver(caregiver.id), "a voice was stored"


def test_consent_less_request_never_reaches_the_provider(client, provider, accounts):
    """A refused request must not transmit the audio at all.

    Rejecting AFTER uploading would still have put a person's voice in front of
    a third party without their agreement. The check runs before the files are
    read, and this proves it: the provider recorded zero calls.
    """
    caregiver, _ = accounts
    _sign_in(client, caregiver)

    _clone(client, consent="false")
    _clone(client, consent="")
    _clone(client, consent="0")

    assert provider.clone_calls == [], "audio was sent without consent"


def test_consent_is_not_truthiness(client, provider, accounts):
    """"false" must not be accepted because non-empty strings are truthy.

    Multipart carries no JSON booleans, so `consent` arrives as a string. A
    `if consent:` check would treat the literal string "false" as agreement —
    a consent bypass in one character, and precisely the kind of bug that looks
    correct in review.
    """
    caregiver, _ = accounts
    _sign_in(client, caregiver)

    for value in ("false", "no", "0", "off", "undefined"):
        assert _clone(client, consent=value).status_code == 400, value
    assert provider.clone_calls == []


def test_consent_wording_is_stored_verbatim(client, provider, accounts):
    """An audit must show WHAT was consented to, not merely that something was.

    A boolean column would answer "did they agree?" but not "to what?" — and the
    wording on the checkbox is the only thing that makes the agreement mean
    anything. Six months and one copy edit later, the row still carries the text
    the person actually saw.
    """
    caregiver, _ = accounts
    _sign_in(client, caregiver)

    assert _clone(client).status_code == 200
    row = voice_store.list_for_caregiver(caregiver.id)[0]

    assert row.consent_text == voice_store.CONSENT_TEXT
    assert row.consented_at is not None
    # The clauses that map to the mitigations named in app/voice.py. If the
    # wording is ever weakened, this fails rather than silently shipping.
    assert "my own voice" in row.consent_text
    assert "AI recreation" in row.consent_text
    assert "delete it at" in row.consent_text


# ---------------------------------------------------------------------------
# 2 & 3. IDENTITY AND ROLE
# ---------------------------------------------------------------------------
def test_non_caregiver_cannot_clone(client, provider, accounts):
    """A member account may not record a supporter voice.

    The feature exists so a supporter can offer their OWN voice. Any other shape
    of it — a member cloning a voice, from any audio — is the abuse case, so the
    role check is a hard 403 rather than a UI affordance that is merely hidden.
    """
    _, member = accounts
    _sign_in(client, member)

    assert _clone(client).status_code == 403
    assert provider.clone_calls == []


def test_anonymous_cannot_clone(client, provider, accounts):
    """No session, no clone.

    Every other read surface in this app falls back to the published demo
    account so an evaluator sees the product work. That trade is wrong here: an
    anonymous caller silently becoming "sam" would let a stranger attribute a
    voice model to a real account.
    """
    assert _clone(client).status_code == 401
    assert provider.clone_calls == []


def test_owner_is_the_session_not_the_body(client, provider, accounts):
    """A client-supplied account id must never decide who is being cloned.

    An id in a request body is a request, not a permission — the same rule the
    caregiver privacy boundary is built on. Here it is enforced by there being
    no such parameter at all: extra fields are ignored and the row is filed
    under the authenticated caller regardless.
    """
    caregiver, member = accounts
    _sign_in(client, caregiver)

    res = client.post(
        "/api/voice/clone",
        data={
            "consent": "true",
            "display_name": "Sarah's voice",
            # A hostile client trying to file this under the member's account.
            "caregiver_user_id": member.id,
            "user_id": member.id,
        },
        files=_samples(),
    )
    assert res.status_code == 200

    assert voice_store.list_for_caregiver(caregiver.id), "not filed under the caller"
    assert not voice_store.list_for_caregiver(member.id), "filed under a body-supplied id"


# ---------------------------------------------------------------------------
# 4. SHARING IS A SEPARATE DECISION, AND DEFAULT OFF
# ---------------------------------------------------------------------------
def test_fresh_clone_is_not_shared(client, provider, accounts):
    """Recording a voice is not publishing it.

    Default off at every step. If the clone call could also share, the
    caregiver's second decision would never actually be made by anyone.
    """
    caregiver, _ = accounts
    _sign_in(client, caregiver)

    body = _clone(client).json()
    assert body["voice"]["shared"] is False
    assert voice_store.list_for_caregiver(caregiver.id)[0].shared is False


def test_member_cannot_see_an_unshared_voice(client, provider, accounts):
    """An unshared voice is invisible to the member, even from a linked caregiver."""
    caregiver, member = accounts
    _sign_in(client, caregiver)
    _clone(client)

    _sign_in(client, member)
    available = client.get("/api/voice/available").json()
    assert available["supporter"] == []
    # Browser speech remains the default the client pre-selects.
    assert available["default_is_browser"] is True


def test_member_cannot_speak_in_an_unshared_voice(client, provider, accounts):
    """Knowing the voice id must not be enough to be spoken to in it.

    The picker hiding it is a rendering preference; this is the boundary. A
    member who obtained the id out of band is refused BEFORE any provider call,
    so a guess cannot even generate a billable request.
    """
    caregiver, member = accounts
    _sign_in(client, caregiver)
    voice_id = _clone(client).json()["voice"]["voice_id"]

    _sign_in(client, member)
    res = client.post(
        "/api/voice/speak", json={"text": "Take your time.", "voice_id": voice_id}
    )
    assert res.status_code == 403
    assert provider.synth_calls == [], "an unauthorised voice was still synthesized"


def test_sharing_then_member_may_use(client, provider, accounts):
    """Once the caregiver shares, the member can see and use it."""
    caregiver, member = accounts
    _sign_in(client, caregiver)
    created = _clone(client).json()["voice"]
    assert client.post(
        "/api/voice/share", json={"id": created["id"], "shared": True}
    ).status_code == 200

    _sign_in(client, member)
    available = client.get("/api/voice/available").json()
    assert [v["voice_id"] for v in available["supporter"]] == [created["voice_id"]]
    # Every entry is flagged cloned so the UI can label it without inferring.
    assert all(v["cloned"] is True for v in available["supporter"])

    res = client.post(
        "/api/voice/speak",
        json={"text": "Take your time with it.", "voice_id": created["voice_id"]},
    )
    assert res.status_code == 200
    assert res.headers["content-type"] == "audio/mpeg"
    # The label travels on the response, not on what the client thinks it asked
    # for. This header is what makes the AI-recreation label impossible to skip.
    assert res.headers["X-Voice-Cloned"] == "true"


def test_unsharing_revokes_use_without_deleting(client, provider, accounts):
    """Un-sharing is a pause, not a destruction.

    Forcing "stop letting them hear it" and "throw the recording away" to be the
    same action would push people toward leaving something shared they would
    rather pause.
    """
    caregiver, member = accounts
    _sign_in(client, caregiver)
    created = _clone(client).json()["voice"]
    client.post("/api/voice/share", json={"id": created["id"], "shared": True})
    client.post("/api/voice/share", json={"id": created["id"], "shared": False})

    _sign_in(client, member)
    assert client.get("/api/voice/available").json()["supporter"] == []
    assert provider.deleted == [], "un-sharing must not delete the model"
    assert voice_store.get_supporter_voice(created["id"]) is not None


def test_member_cannot_share_a_voice_to_themselves(client, provider, accounts):
    """Only the owner decides. Otherwise the caregiver's decision is a formality."""
    caregiver, member = accounts
    _sign_in(client, caregiver)
    created = _clone(client).json()["voice"]

    _sign_in(client, member)
    res = client.post("/api/voice/share", json={"id": created["id"], "shared": True})
    assert res.status_code == 403
    assert voice_store.get_supporter_voice(created["id"]).shared is False


def test_unlinked_member_sees_nothing(client, provider, accounts):
    """The consent chain is a live join, not a copied flag.

    A shared voice is only reachable through a consented `caregiver_links` row.
    Remove the link and the voice disappears from the member's list on the very
    next request, with no cleanup step needing to have run correctly.
    """
    caregiver, member = accounts
    _sign_in(client, caregiver)
    created = _clone(client).json()["voice"]
    client.post("/api/voice/share", json={"id": created["id"], "shared": True})

    store.unlink_caregiver(caregiver.id, member.id)

    _sign_in(client, member)
    assert client.get("/api/voice/available").json()["supporter"] == []
    assert voice_store.member_may_use(member.id, created["voice_id"]) is False


# ---------------------------------------------------------------------------
# 5. REVOCATION, FROM BOTH SIDES
# ---------------------------------------------------------------------------
def test_caregiver_can_revoke_and_the_model_dies_upstream(client, provider, accounts):
    """The owner may destroy their own voice, and the model actually goes.

    §7.2's hardest objection is what happens to the model when the relationship
    ends or the person dies. Deleting only our row would leave the voice alive
    at the provider and make revocation a lie, so the upstream call is asserted
    explicitly rather than assumed.
    """
    caregiver, _ = accounts
    _sign_in(client, caregiver)
    created = _clone(client).json()["voice"]

    res = client.delete(f"/api/voice/{created['id']}")
    assert res.status_code == 200
    assert res.json()["upstream_deleted"] is True
    assert provider.deleted == [created["voice_id"]], "model not deleted upstream"
    assert voice_store.get_supporter_voice(created["id"]) is None


def test_member_can_revoke_a_voice_shared_with_them(client, provider, accounts):
    """The other side of revocation, and the one that is easy to leave out.

    The member is the person the voice would speak to. Being able to stop
    hearing a copy of someone's voice must not depend on that someone agreeing —
    which, in the relationships this product serves, is not a safe assumption.
    """
    caregiver, member = accounts
    _sign_in(client, caregiver)
    created = _clone(client).json()["voice"]
    client.post("/api/voice/share", json={"id": created["id"], "shared": True})

    _sign_in(client, member)
    res = client.delete(f"/api/voice/{created['id']}")
    assert res.status_code == 200
    assert provider.deleted == [created["voice_id"]]
    assert voice_store.get_supporter_voice(created["id"]) is None


def test_stranger_cannot_revoke(client, provider, accounts):
    """Neither owner nor recipient means no.

    404 rather than 403, deliberately: a distinguishable response would let a
    caller probe which voice ids exist.
    """
    caregiver, _ = accounts
    stranger = auth.register("nosy", "threshold", role="user")
    _sign_in(client, caregiver)
    created = _clone(client).json()["voice"]
    client.post("/api/voice/share", json={"id": created["id"], "shared": True})

    _sign_in(client, stranger)
    assert client.delete(f"/api/voice/{created['id']}").status_code == 404
    assert provider.deleted == []
    assert voice_store.get_supporter_voice(created["id"]) is not None


def test_member_cannot_revoke_an_unshared_voice(client, provider, accounts):
    """An unshared voice was never offered to the member, so it is not theirs to destroy."""
    caregiver, member = accounts
    _sign_in(client, caregiver)
    created = _clone(client).json()["voice"]

    _sign_in(client, member)
    assert client.delete(f"/api/voice/{created['id']}").status_code == 404
    assert voice_store.get_supporter_voice(created["id"]) is not None


@pytest.mark.anyio
async def test_account_deletion_destroys_the_voice(provider, accounts, anyio_backend):
    """Hard constraint: deleting an account deletes the voice model."""
    caregiver, _ = accounts
    voice_store.create_supporter_voice(
        id="row-1",
        caregiver_user_id=caregiver.id,
        voice_id="provider-abc",
        display_name="Sarah's voice",
        consent_text=voice_store.CONSENT_TEXT,
    )

    assert await voice_routes.purge_voices_for_user(caregiver.id) == 1
    assert provider.deleted == ["provider-abc"]
    assert voice_store.get_supporter_voice("row-1") is None


@pytest.mark.anyio
async def test_unlinking_a_caregiver_destroys_the_shared_voice(
    provider, accounts, anyio_backend
):
    """Hard constraint: deleting a caregiver link deletes the voice shared through it.

    And only that one. A voice the caregiver recorded but never shared was never
    part of this relationship, so severing the link is not a reason to reach into
    their account and delete their own property.
    """
    caregiver, member = accounts
    shared = voice_store.create_supporter_voice(
        id="row-shared",
        caregiver_user_id=caregiver.id,
        voice_id="provider-shared",
        display_name="Shared",
        consent_text=voice_store.CONSENT_TEXT,
    )
    voice_store.set_shared(shared.id, True)
    voice_store.create_supporter_voice(
        id="row-private",
        caregiver_user_id=caregiver.id,
        voice_id="provider-private",
        display_name="Private",
        consent_text=voice_store.CONSENT_TEXT,
    )

    assert await voice_routes.purge_voices_for_link(caregiver.id, member.id) == 1
    assert provider.deleted == ["provider-shared"]
    assert voice_store.get_supporter_voice("row-private") is not None


@pytest.fixture
def anyio_backend():
    """Run the async tests on asyncio only; trio is not a dependency here."""
    return "asyncio"


# ---------------------------------------------------------------------------
# 6. THE KEY NEVER LEAVES THE SERVER
# ---------------------------------------------------------------------------
def test_api_key_never_appears_in_any_voice_response(client, monkeypatch, accounts):
    """The provider credential must not reach a browser through any voice route.

    Server-side synthesis exists for exactly this reason. This walks every voice
    endpoint — including the failure paths, which are where a raw provider error
    body would otherwise be echoed back — and asserts the key appears in none of
    them, in headers or body.
    """
    secret = "sk-elevenlabs-SUPERSECRET-do-not-leak"
    monkeypatch.setenv("ELEVENLABS_API_KEY", secret)

    caregiver, member = accounts
    _sign_in(client, caregiver)

    responses = [
        client.get("/api/voice/status"),
        client.get("/api/voice/script"),
        # Real (unpatched) provider functions here: with a bogus key the calls
        # fail, and the failure path is precisely where a leak would happen.
        _clone(client),
        client.post("/api/voice/share", json={"id": "nope", "shared": True}),
        client.delete("/api/voice/nope"),
        client.get("/api/voice/available"),
        client.post("/api/voice/speak", json={"text": "Take your time."}),
    ]

    for res in responses:
        assert secret not in res.text, f"key leaked in body of {res.url}"
        assert secret not in str(res.headers), f"key leaked in headers of {res.url}"


def test_offline_is_reported_honestly_not_faked(client, monkeypatch, accounts):
    """With no key, /api/voice/status says so rather than pretending.

    CONTRACT.md ground rule: the app boots cleanly without a key and surfaces a
    clear offline state. A voice picker that silently listed voices it cannot
    speak would be a fallback wearing a live feature's clothes.
    """
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    caregiver, _ = accounts
    _sign_in(client, caregiver)

    assert client.get("/api/voice/status").json()["online"] is False


# ---------------------------------------------------------------------------
# PRD P5 — a cloned voice never claims presence
# ---------------------------------------------------------------------------
def test_cloned_voice_may_not_claim_presence(client, provider, accounts):
    """PRD P5: the one line this feature does not cross.

    A copy of someone's mother saying "I'm here with you" to a person mid-crisis
    is the precise harm §7.2 objected to: consent is obtained while calm and
    spent during crisis, and someone in that state cannot process an on-screen
    label contradicting a voice they trust.
    """
    caregiver, member = accounts
    _sign_in(client, caregiver)
    created = _clone(client).json()["voice"]
    client.post("/api/voice/share", json={"id": created["id"], "shared": True})

    _sign_in(client, member)
    for claim in ("I'm here with you.", "I am listening.", "I'm on my way."):
        res = client.post(
            "/api/voice/speak", json={"text": claim, "voice_id": created["voice_id"]}
        )
        assert res.status_code == 400, claim
    assert provider.synth_calls == [], "a presence claim was synthesized"


def test_stock_narrator_is_not_held_to_the_presence_rule(client, provider, accounts):
    """The restriction is about impersonation, not about the words.

    A stock narrator is nobody; it impersonates no one, so the same sentence
    carries none of the harm. Over-applying the rule would be cargo-culting it.
    """
    _, member = accounts
    _sign_in(client, member)
    res = client.post(
        "/api/voice/speak", json={"text": "I'm here.", "voice_id": "stock-narrator"}
    )
    assert res.status_code == 200


# ---------------------------------------------------------------------------
# The sample script
# ---------------------------------------------------------------------------
def test_sample_script_is_not_distressing_or_crisis_shaped(client):
    """The passages must be safe to read aloud and safe to overhear.

    A supporter is often someone's mother or sponsor. Asking them to speak
    overdose language into a microphone would be a small cruelty for nothing.
    And the raw recordings could be replayed out of context — so nothing in them
    may sound like a crisis line, and nothing may claim the speaker is present
    (PRD P5).
    """
    passages = client.get("/api/voice/script").json()["passages"]
    assert 2 <= len(passages) <= 3

    joined = " ".join(passages).lower()
    for word in (
        "overdose", "naloxone", "narcan", "breathe", "breathing", "dying",
        "die", "emergency", "911", "ambulance", "relapse", "using", "drug",
        "sober", "hospital", "wake up", "stay awake", "stay with me",
        "help is coming", "hold on", "don't go",
    ):
        assert word not in joined, f"the sample script says {word!r}"

    # No presence claim, for the same reason the synthesis route refuses one:
    # these recordings are the raw material for a voice that must never assert
    # the real person is live.
    for claim in ("i'm here", "i am here", "i'm with you", "i'm listening"):
        assert claim not in joined, f"the sample script claims presence: {claim!r}"


def test_sample_script_covers_varied_phonetics(client):
    """A clone built on thin phonetics mispronounces.

    A voice people love, sounding wrong, is its own harm — so the script is
    checked for breadth rather than only for safety. Spot-checks the sounds most
    often missing from a short read: /ŋ/, the voiced and unvoiced 'th', /ʃ/, the
    affricates, and spoken digits.
    """
    joined = " ".join(client.get("/api/voice/script").json()["passages"]).lower()
    for sound, example in (
        ("ng", "ng"), ("voiced th", "the"), ("unvoiced th", "thursday"),
        ("sh", "sh"), ("ch", "ch"), ("j/dʒ", "generous"), ("z", "degrees"),
        ("digits", "forty-one"), ("v", "v"), ("w", "w"),
    ):
        assert example in joined, f"script is missing {sound}"

    # 30-60 seconds of speech at roughly 150 words per minute.
    words = len(joined.split())
    assert 70 <= words <= 200, f"script is {words} words — outside the 30-60s target"


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------
def test_paid_voice_paths_are_rate_limited_but_the_ladder_is_not():
    """Speech costs money per call; the emergency path costs lives.

    Both facts are encoded in `_LIMITS`, and this test pins the distinction. The
    voice paths may be limited only because a throttled response degrades to
    browser speech in the client rather than to silence.
    """
    from app import security

    assert "/api/voice/speak" in security._LIMITS
    assert "/api/voice/clone" in security._LIMITS
    for path in ("/api/utterance", "/api/sensor", "/api/tier", "/api/rescind", "/api/events"):
        assert path not in security._LIMITS, f"{path} must never be rate limited"


# ---------------------------------------------------------------------------
# Memory Vault is untouched
# ---------------------------------------------------------------------------
def test_this_feature_never_touches_memory_vault():
    """Vault clips are REAL recordings and are never synthesized (PRD §7.2).

    Asserted structurally rather than behaviourally: neither the route module
    nor the store module references `vault_clips` at all, so no future code path
    in them can start synthesizing one by accident.
    """
    import ast
    import io
    import tokenize
    from pathlib import Path

    def executable_source(path: str) -> str:
        """The file with comments and docstrings stripped.

        The forbidden identifiers appear all over these modules' PROSE — the
        comments are what explain the prohibition, and a naive substring search
        would flag the explanation as the violation. Tokenizing strips comments;
        walking the AST finds the docstrings to drop as well.
        """
        source = Path(path).read_text()
        docstrings = {
            node.body[0].value.value
            for node in ast.walk(ast.parse(source))
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef))
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        }
        kept = []
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.COMMENT:
                continue
            if tok.type == tokenize.STRING and ast.literal_eval(tok.string) in docstrings:
                continue
            kept.append(tok.string)
        return " ".join(kept)

    for module in ("app/routes/voice.py", "app/voice_store.py", "app/voice.py"):
        code = executable_source(module)
        assert "vault_clips" not in code, f"{module} reads the vault table"
        assert "list_vault_clips" not in code, f"{module} reads vault clips"
        assert "get_vault_clip" not in code, f"{module} reads a vault clip"
