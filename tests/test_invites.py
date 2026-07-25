"""Tests for invite codes and the caregiver privacy boundary they create.

WHAT THESE TESTS PROTECT
    The product's answer to "isn't this surveillance?" is that consent is
    STRUCTURAL rather than a policy claim: a caregiver link can only come into
    existence when the watched member generated a code and handed it over. That
    claim is only worth making if the properties it rests on are enforced rather
    than intended, so each is asserted here:

    1. A code is SINGLE USE. A leaked or forwarded code cannot attach a second
       watcher. `test_a_code_cannot_be_redeemed_twice`.
    2. A code EXPIRES after 24 hours. Consent is a moment, not a standing state
       that quietly stays open. `test_an_expired_code_is_refused`.
    3. A code is UNGUESSABLE and UNAMBIGUOUS. Generated with `secrets`, from an
       alphabet with no O/0 or I/1 to mistype.
    4. No API accepts the NAME of the person to watch. The permission travels
       outward from the member; there is no inward path.
       `test_a_caregiver_cannot_read_a_members_state_without_a_link`.

WHAT THEY DELIBERATELY DO NOT DO
    They do not test the ladder, triage, or generation. An invite decides WHO may
    look, never WHAT is shown at a given tier — that second question belongs to
    the ladder config and is tested with it. Keeping them apart means a failure
    here always means "the consent boundary broke", with no ambiguity.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import auth, store
from app.main import app


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    """Fresh database per test.

    Autouse so no test can read or write the developer's real demo database. A
    test that mints invite codes against someone's real account would be its own
    security bug.
    """
    monkeypatch.setattr(store, "_db_path", tmp_path / "test.db")
    store.init_db()
    yield


@pytest.fixture
def member():
    """A member account — the role that issues invitations."""
    return auth.register("member-one", "threshold", role="user")


@pytest.fixture
def caregiver():
    """A caregiver account — the role that redeems them."""
    return auth.register("caregiver-one", "threshold", role="caregiver")


# ---------------------------------------------------------------------------
# Code generation
# ---------------------------------------------------------------------------


def test_generated_codes_avoid_ambiguous_characters():
    """No O/0 or I/1/L anywhere in the alphabet.

    This code is read aloud down a phone or squinted at on a scrap of paper by
    an exhausted family member at midnight. A character pair that cannot be
    told apart turns a thirty-second handover into the member regenerating a
    code while their sister waits — friction on precisely the step that has to
    be effortless.
    """
    forbidden = set("O0I1L")
    assert not (set(store.INVITE_ALPHABET) & forbidden)

    # Sampled rather than reasoned about, so a future change to the generator
    # that stopped using the alphabet constant would still be caught.
    for _ in range(200):
        assert not (set(store.generate_invite_code()) & forbidden)


def test_codes_are_the_documented_length_and_not_repeated():
    """Six characters, and different every time.

    A generator returning a constant would pass every functional test in this
    file — every redemption would still work — while making every member's
    invite the same string. That is the failure this asserts against.
    """
    codes = {store.generate_invite_code() for _ in range(200)}
    assert len(codes) > 190, "codes are colliding far more than chance allows"
    assert all(len(c) == store.INVITE_CODE_LENGTH for c in codes)


def test_create_invite_stores_an_unredeemed_code_with_a_24h_window(member):
    """A fresh invite is live, unspent, and expires a day out."""
    now = datetime(2026, 1, 1, 12, 0, 0)
    invite = store.create_invite(member.id, now=now)

    assert invite.user_id == member.id
    assert invite.is_spent is False
    assert invite.is_expired(now) is False
    assert invite.expires_at == now + timedelta(hours=store.INVITE_TTL_HOURS)

    # Readable back by code, which is how the redeem path finds it.
    assert store.get_invite(invite.code).user_id == member.id


# ---------------------------------------------------------------------------
# Redemption
# ---------------------------------------------------------------------------


def test_redeeming_a_code_creates_the_consented_link(member, caregiver):
    """The happy path, and the ONLY path: a code becomes a link.

    Asserts the relationship is visible from both directions, because the two
    are read by different surfaces — `watched_user_for` by the caregiver page,
    `caregivers_for` by the member's own view of who can see them.
    """
    invite = store.create_invite(member.id)
    watched = store.redeem_invite(invite.code, caregiver.id)

    assert watched == member.id
    assert store.watched_user_for(caregiver.id) == member.id
    assert store.caregivers_for(member.id) == [caregiver.id]
    assert store.is_linked(caregiver.id, member.id) is True


def test_a_code_is_accepted_in_any_case_or_with_dashes(member, caregiver):
    """A retyped code is normalised before lookup.

    Someone copying "K7QW-2M" out of a text message must not be told their code
    is invalid over a difference the alphabet cannot even represent.
    """
    invite = store.create_invite(member.id)
    messy = f"  {invite.code[:3].lower()}-{invite.code[3:].lower()}  "

    assert store.redeem_invite(messy, caregiver.id) == member.id


def test_a_code_cannot_be_redeemed_twice(member, caregiver):
    """SINGLE USE. A leaked code cannot attach a second watcher.

    The property that makes handing a code to one person safe: even if it is
    then forwarded, screenshotted, or overheard, it is already spent. Asserts
    the second caregiver ends up linked to NOBODY, not merely that an exception
    was raised — a refusal that still wrote the link would pass a weaker test.
    """
    invite = store.create_invite(member.id)
    store.redeem_invite(invite.code, caregiver.id)

    other = auth.register("caregiver-two", "threshold", role="caregiver")
    with pytest.raises(store.InviteError):
        store.redeem_invite(invite.code, other.id)

    assert store.watched_user_for(other.id) is None
    assert store.caregivers_for(member.id) == [caregiver.id]


def test_an_expired_code_is_refused(member, caregiver):
    """EXPIRING. Consent is a moment, not a standing state.

    A code found in a drawer next month is not permission the member would
    recognise giving, so it stops working on its own rather than waiting to be
    revoked by someone who has long since forgotten it exists.
    """
    issued = datetime(2026, 1, 1, 12, 0, 0)
    invite = store.create_invite(member.id, now=issued)

    just_inside = issued + timedelta(hours=store.INVITE_TTL_HOURS) - timedelta(minutes=1)
    just_outside = issued + timedelta(hours=store.INVITE_TTL_HOURS, minutes=1)

    assert invite.is_expired(just_inside) is False
    assert invite.is_expired(just_outside) is True

    with pytest.raises(store.InviteError):
        store.redeem_invite(invite.code, caregiver.id, now=just_outside)

    assert store.watched_user_for(caregiver.id) is None


def test_an_expired_code_still_works_inside_its_window(member, caregiver):
    """The boundary in the other direction.

    Guards an off-by-one that would expire codes the moment they were issued —
    a failure that the expiry test above would not catch on its own.
    """
    issued = datetime(2026, 1, 1, 12, 0, 0)
    invite = store.create_invite(member.id, now=issued)
    late = issued + timedelta(hours=store.INVITE_TTL_HOURS - 1)

    assert store.redeem_invite(invite.code, caregiver.id, now=late) == member.id


def test_an_unknown_code_is_refused(caregiver):
    """A guessed or mistyped code links nobody."""
    with pytest.raises(store.InviteError):
        store.redeem_invite("ZZZZZZ", caregiver.id)
    assert store.watched_user_for(caregiver.id) is None


def test_a_member_cannot_redeem_their_own_code(member):
    """Self-redemption is refused BEFORE the code is spent.

    `link_caregiver` rejects self-links anyway, so allowing this through would
    burn the member's only code on an error and leave them wondering why the
    invite they just generated no longer works.
    """
    invite = store.create_invite(member.id)
    with pytest.raises(store.InviteError):
        store.redeem_invite(invite.code, member.id)

    # Still live, and still usable by an actual caregiver.
    assert store.get_invite(invite.code).is_spent is False


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------


@pytest.fixture
def client():
    """A test client WITHOUT the lifespan, so the temp_db fixture is not reseeded.

    Running the lifespan here would call `seed()` against the temporary database
    and create sam/sarah, which would make "this caregiver has no link" tests
    accidentally pass for the wrong reason.
    """
    return TestClient(app)


def _sign_in(client: TestClient, username: str) -> None:
    """Sign the client in, so subsequent requests carry a real session cookie."""
    res = client.post(
        "/api/auth/login", json={"username": username, "password": "threshold"}
    )
    assert res.status_code == 200, res.text


def test_invite_endpoint_requires_a_real_session(client):
    """ANONYMOUS CALLERS CANNOT MINT PERMISSIONS.

    Most read endpoints deliberately fall back to the published demo account so
    an evaluator poking at the API sees a working product. That convenience must
    not reach this endpoint: a code is a live permission to watch whoever issued
    it, so an anonymous caller minting one against Sam's account would make the
    consent story a fiction on the single endpoint where it must be literally
    true.
    """
    assert client.post("/api/invite").status_code == 401


def test_a_caregiver_cannot_issue_an_invite(client, member, caregiver):
    """Only a member invites. Consent travels outward from the watched person.

    A caregiver issuing an invitation would invert the direction of the
    relationship — whoever redeemed it would end up watching the caregiver.
    """
    _sign_in(client, "caregiver-one")
    assert client.post("/api/invite").status_code == 403


def test_member_creates_and_caregiver_redeems_over_http(client, member, caregiver):
    """The full round trip an evaluator performs by hand.

    The response names the member being watched, so a caregiver who typed a
    valid-but-wrong code sees whose ladder they just attached to before relying
    on it.
    """
    _sign_in(client, "member-one")
    created = client.post("/api/invite")
    assert created.status_code == 200
    code = created.json()["code"]
    assert len(code) == store.INVITE_CODE_LENGTH

    client.post("/api/auth/logout")
    _sign_in(client, "caregiver-one")
    redeemed = client.post("/api/invite/redeem", json={"code": code})

    assert redeemed.status_code == 200
    assert redeemed.json()["watching"] == "member-one"
    assert store.is_linked(caregiver.id, member.id)


def test_redeeming_an_invalid_code_over_http_says_why(client, caregiver):
    """A refusal is a 400 with a sentence someone can act on.

    The store distinguishes unknown / expired / already-used deliberately and
    the endpoint passes that through, because a single opaque "invalid" sends an
    exhausted person round a loop they cannot debug. Safe to disclose: they
    already hold the code.
    """
    _sign_in(client, "caregiver-one")
    res = client.post("/api/invite/redeem", json={"code": "ZZZZZZ"})

    assert res.status_code == 400
    assert isinstance(res.json()["detail"], str)
    assert res.json()["detail"]


def test_registration_redeems_a_code_atomically(client, member, monkeypatch):
    """A caregiver arrives with a code and lands on a working surface.

    Registering and redeeming in one request is what stops a new caregiver
    landing on an empty page with no explanation. Note what the request does NOT
    contain: any field naming the member. The code is the only thing that
    decides who this account is attached to.
    """
    invite = store.create_invite(member.id)

    sent: dict[str, str] = {}

    async def fake_send(email, code, **_kwargs):
        sent["code"] = code
        return True, None

    monkeypatch.setenv("THRESHOLD_SECRET", "test-registration-secret")
    monkeypatch.setattr("app.registration.email_delivery.send_verification_code", fake_send)
    res = client.post(
        "/api/auth/register",
        json={
            "email": "sister@example.com",
            "full_name": "Sister Person",
            "phone": "+1 502 555 0142",
            "password": "threshold",
            "role": "caregiver",
            "invite_code": invite.code,
        },
    )

    assert res.status_code == 202, res.text
    verified = client.post(
        "/api/auth/register/verify",
        json={"email": "sister@example.com", "code": sent["code"]},
    )
    assert verified.status_code == 200, verified.text
    assert verified.json()["linked"] is True
    assert verified.json()["watching"] == "member-one"

    new_caregiver = store.get_user_by_email("sister@example.com")
    assert store.watched_user_for(new_caregiver.id) == member.id


def test_registration_with_a_dead_code_creates_no_account(client, member, caregiver):
    """A bad code leaves NO half-registered orphan behind.

    The code is validated before the account is created, so someone who mistypes
    it can simply try again with the same username rather than discovering it is
    now taken by an account they cannot use.
    """
    invite = store.create_invite(member.id)
    store.redeem_invite(invite.code, caregiver.id)  # spend it

    res = client.post(
        "/api/auth/register",
        json={
            "email": "sister@example.com",
            "full_name": "Sister Person",
            "phone": "+1 502 555 0142",
            "password": "threshold",
            "role": "caregiver",
            "invite_code": invite.code,
        },
    )

    assert res.status_code == 400
    assert store.get_user_by_email("sister@example.com") is None


def test_a_member_may_not_register_with_an_invite_code(client, member):
    """An invite code on a member registration is refused, not ignored.

    Silently discarding it would let someone believe a link was made. A member
    does not attach themselves to anyone.
    """
    invite = store.create_invite(member.id)
    res = client.post(
        "/api/auth/register",
        json={
            "email": "someone@example.com",
            "full_name": "Someone Person",
            "phone": "+1 502 555 0143",
            "password": "threshold",
            "role": "user",
            "invite_code": invite.code,
        },
    )
    assert res.status_code == 400


# ---------------------------------------------------------------------------
# The boundary itself
# ---------------------------------------------------------------------------


def test_a_caregiver_cannot_read_a_members_state_without_a_link(client, member, caregiver):
    """THE CORE SECURITY ASSERTION OF THIS FEATURE.

    An unlinked caregiver gets nothing — not an error page with the data behind
    it, not another account's ladder, and specifically NOT a fallback to the
    demo user's profile, which is what the pre-link code did. `resolve_subject`
    fails closed, and "nobody has invited you yet" is the honest answer.

    This is the test that makes the consent story real. If it ever passes
    because the caregiver saw *something*, the product's central claim is false
    regardless of what the privacy page says.
    """
    _sign_in(client, "caregiver-one")
    res = client.get("/api/state")

    assert res.status_code == 403, "an unlinked caregiver must be refused, not served"

    # And nothing about the member leaked into the refusal body.
    assert "member-one" not in res.text


def test_the_same_caregiver_can_read_state_once_linked(client, member, caregiver):
    """The other half of the boundary: a consented link genuinely grants access.

    Asserted alongside the refusal above so a change that simply broke the
    caregiver surface entirely cannot masquerade as good security.
    """
    invite = store.create_invite(member.id)
    store.redeem_invite(invite.code, caregiver.id)

    _sign_in(client, "caregiver-one")
    res = client.get("/api/state")

    assert res.status_code == 200, res.text


def test_revoking_a_link_immediately_closes_the_surface(client, member, caregiver):
    """Consent that cannot be withdrawn is not consent.

    Every authorization check reads the link table live, so the caregiver's very
    next request is refused. No cached grant, no session to expire, no window in
    which a revoked watcher is still watching.
    """
    invite = store.create_invite(member.id)
    store.redeem_invite(invite.code, caregiver.id)
    _sign_in(client, "caregiver-one")
    assert client.get("/api/state").status_code == 200

    store.unlink_caregiver(caregiver.id, member.id)

    assert client.get("/api/state").status_code == 403


def test_deleting_a_member_takes_their_invites_with_them(member):
    """A dead account leaves no live codes behind.

    Enforced by ON DELETE CASCADE rather than by cleanup code, so a future
    deletion path that forgets about invites still cannot leave a code that
    would attach a caregiver to a ghost.
    """
    invite = store.create_invite(member.id)
    store.delete_user_data(member.id)

    assert store.get_invite(invite.code) is None
