"""Cross-account isolation: two users, two caregivers, and no path between them.

WHAT THESE TESTS PROTECT AGAINST
    The privacy failure this product cannot survive. Everything a Threshold
    account contains — a home address, a door entry code, which substances a
    person uses, the prose reason they escalated at 3am — is exactly what
    somebody would want in order to find and harm a person who is using. An
    authorization gap here is not a data-protection technicality; it is the
    thing the app exists to make safe.

    The suite is built as a matrix rather than a list of one-off checks. Four
    accounts exist for every test:

        sam    (user)      watched by sarah
        dana   (user)      watched by kim
        sarah  (caregiver) linked to sam,  NOT to dana
        kim    (caregiver) linked to dana, NOT to sam

    Every assertion then asks the same question from a different angle: can any
    of these four — or an anonymous caller — READ, MUTATE, SUBSCRIBE TO, or
    GENERATE AGAINST a resource belonging to somebody they hold no consented
    link to? The answer must be no in every cell, and the tests are named for the
    cell they cover so a failure says immediately which one opened.

    The four verbs matter separately because they failed separately:
      * READ      — /api/state used to resolve a caregiver to their own empty
                    account, and an anonymous caller to the demo profile.
      * MUTATE    — /api/profile accepted writes from anyone the session helper
                    could resolve, including anonymously.
      * SUBSCRIBE — /api/events broadcast EVERY user's events to EVERY listener
                    and let the browser decide what to show. This is the one that
                    leaked across accounts in normal operation, with no attacker
                    required.
      * GENERATE  — the 911 script and the caregiver brief are model calls over
                    an address, an entry code, and an event log.

WHAT THEY DELIBERATELY DO NOT DO
    They assert no model prose and require no API key. Whether a generation is
    live is `test_genai`'s question; whether it was permitted at all is this
    file's, and those must not be able to fail for each other's reasons.

    They also do not re-test the invite mechanics (see `test_invites.py`). Links
    are created directly through the store here, because the subject under test
    is what a link PERMITS, not how one comes to exist.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app import auth, deps, security, store
from app.main import app
from app.models import Event, LadderConfig, Tier, UserProfile, VaultClip

PASSWORD = "threshold"


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    """Fresh database per test, and empty live state to go with it.

    Autouse, so no test here can read or write the developer's real database.
    The in-memory ladder cursor and the SSE listener list are module-level and
    would otherwise leak between tests — a listener left registered by one test
    would receive another test's broadcasts and make a privacy assertion pass or
    fail for an unrelated reason.
    """
    monkeypatch.setattr(store, "_db_path", tmp_path / "test.db")
    # `_initialised` memoises which database paths have had their schema built,
    # so another test file that already ran init_db() leaves this path looking
    # done and the tables are never created. These tests pass alone and fail in
    # a full run for exactly that reason — clear the memo, not just the path.
    monkeypatch.setattr(store, "_initialised", set())
    store.init_db()
    deps._tiers.clear()
    deps._listeners.clear()
    # Login rate-limit buckets are process-global. These tests sign in as four
    # accounts in quick succession from one address, which legitimately trips the
    # brute-force limiter; clearing it keeps a 429 from masquerading as an
    # authorization failure and inverting what a red test means.
    security._hits.clear()
    yield
    deps._tiers.clear()
    deps._listeners.clear()
    security._hits.clear()


@pytest.fixture
def world():
    """The four accounts and the two consented links between them.

    Returns a dict rather than four fixtures so every test starts from the same
    fully-populated matrix. Each user gets a real profile with an address and an
    entry code, because "was the boundary crossed" is only answerable if there is
    something recognisable on the other side of it.
    """
    sam = auth.register("sam-user", PASSWORD, role="user")
    dana = auth.register("dana-user", PASSWORD, role="user")
    sarah = auth.register("sarah-care", PASSWORD, role="caregiver")
    kim = auth.register("kim-care", PASSWORD, role="caregiver")

    store.put_profile(
        sam.id,
        UserProfile(
            id=uuid.uuid4().hex,
            name="Sam",
            address="1412 Highland Avenue",
            unit="Apartment 4B",
            entry_code="SAM-ENTRY-1180",
            substances=["opioids"],
            ladder=LadderConfig(tier_3_visible_to_caregiver=False),
        ),
    )
    store.put_profile(
        dana.id,
        UserProfile(
            id=uuid.uuid4().hex,
            name="Dana",
            address="99 Elsewhere Road",
            unit="Unit 7",
            entry_code="DANA-ENTRY-4242",
            substances=["stimulants"],
            ladder=LadderConfig(tier_3_visible_to_caregiver=False),
        ),
    )

    # The consent matrix. Note what is absent: sarah has no link to dana, and kim
    # has none to sam. Those two gaps are what most of this file asserts.
    store.link_caregiver(sarah.id, sam.id)
    store.link_caregiver(kim.id, dana.id)

    return {"sam": sam, "dana": dana, "sarah": sarah, "kim": kim}


@pytest.fixture
def client():
    """A client with NO lifespan, so the temp database is never seeded.

    Running the lifespan would create the real sam/sarah demo accounts and their
    link, which would let an isolation test pass because the wrong pair happened
    to be connected.
    """
    return TestClient(app)


def sign_in(client: TestClient, username: str) -> None:
    """Authenticate the client as `username` for subsequent requests."""
    res = client.post(
        "/api/auth/login", json={"username": username, "password": PASSWORD}
    )
    assert res.status_code == 200, res.text


def sign_out(client: TestClient) -> None:
    """Drop the session cookie, returning the client to anonymous."""
    client.post("/api/auth/logout")
    client.cookies.clear()


def add_event(user_id: str, tier: Tier, reason: str) -> Event:
    """Append one event directly, bypassing the API.

    Written through the store so a test can plant a specific tier and a
    recognisable reason string without first having to drive triage into
    producing one.
    """
    return store.append_event(
        Event(
            id=uuid.uuid4().hex,
            user_id=user_id,
            at=datetime.now(),
            tier=tier,
            trigger_source="test",
            reason=reason,
        )
    )


# ---------------------------------------------------------------------------
# READ — /api/state
# ---------------------------------------------------------------------------


def test_each_user_reads_only_their_own_state(client, world):
    """Two signed-in users see their own profile and never each other's.

    The most basic cell in the matrix. If this fails nothing else in the file
    matters.
    """
    sign_in(client, "sam-user")
    sam_state = client.get("/api/state").json()
    assert sam_state["profile"]["entry_code"] == "SAM-ENTRY-1180"
    assert "DANA-ENTRY-4242" not in json.dumps(sam_state)

    sign_out(client)
    sign_in(client, "dana-user")
    dana_state = client.get("/api/state").json()
    assert dana_state["profile"]["entry_code"] == "DANA-ENTRY-4242"
    assert "SAM-ENTRY-1180" not in json.dumps(dana_state)


def test_a_caregiver_reads_the_person_they_are_linked_to(client, world):
    """The positive case, asserted so a total lockout cannot pass as security.

    Sarah is linked to Sam, so signing in as Sarah shows Sam's ladder — resolved
    server-side from the link (PRD §8), with no id supplied by the client.
    """
    sign_in(client, "sarah-care")
    state = client.get("/api/state").json()
    assert state["profile"]["name"] == "Sam"


def test_a_caregiver_cannot_read_the_other_users_state(client, world):
    """THE CENTRAL ASSERTION. Sarah watches Sam and must never see Dana.

    Sarah holds a real, valid caregiver session — she is not an outsider — which
    is exactly why this matters: authentication is not authorization. There must
    be no request she can make that returns Dana's ladder, and there is no
    parameter to try, because the subject is resolved from the link rather than
    from anything she sends.
    """
    sign_in(client, "sarah-care")
    body = client.get("/api/state").text

    assert "Dana" not in body
    assert "DANA-ENTRY-4242" not in body
    assert "99 Elsewhere Road" not in body


def test_a_caregiver_cannot_name_the_user_they_want_to_read(client, world):
    """A client-supplied id is a request, not a permission.

    The obvious attack once links exist: pass the other user's id and see what
    comes back. The route accepts no such parameter, so the attempt is inert —
    Sarah gets Sam either way, never Dana. Asserted rather than assumed, because
    "we don't read that parameter" is one careless convenience away from false.
    """
    sign_in(client, "sarah-care")
    for attempt in (
        f"/api/state?user_id={world['dana'].id}",
        f"/api/state?watched_user_id={world['dana'].id}",
        f"/api/state?user={world['dana'].id}",
    ):
        body = client.get(attempt).text
        assert "DANA-ENTRY-4242" not in body, f"{attempt} leaked Dana"
        assert "Dana" not in body, f"{attempt} leaked Dana"


def test_an_unlinked_caregiver_is_refused_rather_than_served(client, world):
    """A caregiver nobody has added gets an honest 403, not a fallback.

    Falling back to any other account — the demo fixture included — is how the
    original bug put one person's data on another's screen. Failing closed is the
    only correct answer, and the refusal must not name anybody either.
    """
    orphan = auth.register("orphan-care", PASSWORD, role="caregiver")
    assert orphan.role == "caregiver"

    sign_in(client, "orphan-care")
    res = client.get("/api/state")

    assert res.status_code == 403
    assert "Sam" not in res.text and "Dana" not in res.text
    assert "ENTRY" not in res.text


def test_revoking_a_link_removes_access_immediately(client, world):
    """Consent that cannot be withdrawn is not consent.

    Access is re-derived from the link on every request rather than captured at
    login, so unlinking takes effect on Sarah's next call — not at her next
    sign-in, and not whenever a cached permission happens to expire.
    """
    sign_in(client, "sarah-care")
    assert client.get("/api/state").json()["profile"]["name"] == "Sam"

    store.unlink_caregiver(world["sarah"].id, world["sam"].id)

    assert client.get("/api/state").status_code == 403


# ---------------------------------------------------------------------------
# MUTATE — /api/profile, /api/tier, /api/rescind
# ---------------------------------------------------------------------------


def test_a_caregiver_cannot_rewrite_the_watched_users_thresholds(client, world):
    """PRD P3: the escalation thresholds belong to the person they describe.

    Sarah may WATCH Sam's ladder. She may not edit it. A caregiver able to raise
    the silence threshold or hide a tier from themselves is a caregiver able to
    reshape the safety net around somebody else, which inverts the whole consent
    model.
    """
    sign_in(client, "sarah-care")
    res = client.post("/api/profile", json={"entry_code": "CAREGIVER-WAS-HERE"})

    assert res.status_code == 403
    assert store.get_profile(world["sam"].id).entry_code == "SAM-ENTRY-1180"


def test_an_anonymous_caller_cannot_write_a_profile(client, world):
    """The demo read-fallback must never become a write-fallback.

    Anonymous reads resolve to the published demo fixture so an evaluator sees a
    working product. Applying that convenience to a WRITE would let a passing
    stranger rewrite a home address and a door entry code.
    """
    sign_out(client)
    res = client.post("/api/profile", json={"entry_code": "ANONYMOUS-WAS-HERE"})

    assert res.status_code == 401
    assert store.get_profile(world["sam"].id).entry_code == "SAM-ENTRY-1180"
    assert store.get_profile(world["dana"].id).entry_code == "DANA-ENTRY-4242"


def test_one_user_cannot_write_another_users_profile(client, world):
    """There is no id parameter, so a write always lands on the caller's own row.

    Dana submits a profile body while signed in as herself; Sam's record is
    untouched no matter what she puts in it.
    """
    sign_in(client, "dana-user")
    res = client.post(
        "/api/profile",
        json={"id": store.get_profile(world["sam"].id).id, "entry_code": "DANA-WROTE-THIS"},
    )
    assert res.status_code == 200

    assert store.get_profile(world["sam"].id).entry_code == "SAM-ENTRY-1180"
    assert store.get_profile(world["dana"].id).entry_code == "DANA-WROTE-THIS"


def test_a_users_tier_change_does_not_move_another_users_ladder(client, world):
    """Live ladder state is per-account.

    Sam going to Tier 4 must leave Dana at Baseline. A shared cursor would show a
    caregiver an emergency that belongs to somebody else entirely.
    """
    sign_in(client, "sam-user")
    assert client.post("/api/tier", json={"tier": 4}).json()["tier"] == 4

    sign_out(client)
    sign_in(client, "dana-user")
    assert client.get("/api/state").json()["tier"] == 0


# ---------------------------------------------------------------------------
# SUBSCRIBE — /api/events
# ---------------------------------------------------------------------------
# These call `deps._broadcast` directly against registered listeners rather than
# holding an HTTP stream open. The filter under test is the one that decides
# whether a payload is ever WRITTEN to a queue, so inspecting the queues is a
# more precise instrument than reading a socket — and it cannot pass merely
# because a slow test read the stream before the event arrived.


def register_listener(user_id):
    """Attach a listener tagged with `user_id` (None for anonymous)."""
    import asyncio

    listener = deps.Listener(user_id=user_id, queue=asyncio.Queue())
    deps._listeners.append(listener)
    return listener


def drain(listener):
    """Return every payload delivered to a listener so far."""
    items = []
    while not listener.queue.empty():
        items.append(listener.queue.get_nowait())
    return items


@pytest.mark.anyio
async def test_an_event_reaches_its_own_user(world):
    """PRD §11: a user's own log is never hidden from them."""
    mine = register_listener(world["sam"].id)
    await deps._broadcast(
        {"type": "tier", "tier": int(Tier.EMERGENCY), "reason": "x", "user_id": world["sam"].id}
    )
    assert len(drain(mine)) == 1


@pytest.mark.anyio
async def test_an_emergency_reaches_the_linked_caregiver_only(world):
    """Tier 4 always reaches a LINKED caregiver — and nobody else.

    PRD §4.2 makes tiers 4 and 5 non-negotiable, which is precisely why the
    "and nobody else" half has to be asserted alongside it: the fact that an
    event cannot be suppressed must not become a reason it is broadcast widely.
    """
    linked = register_listener(world["sarah"].id)     # watches sam
    unlinked = register_listener(world["kim"].id)     # watches dana, not sam
    stranger = register_listener(world["dana"].id)    # unrelated user
    anonymous = register_listener(None)

    await deps._broadcast(
        {
            "type": "tier",
            "tier": int(Tier.EMERGENCY),
            "reason": "not breathing normally",
            "user_id": world["sam"].id,
        }
    )

    assert len(drain(linked)) == 1, "tier 4 must reach the linked caregiver"
    assert drain(unlinked) == [], "an unlinked caregiver received another user's emergency"
    assert drain(stranger) == [], "an unrelated user received someone else's emergency"
    assert drain(anonymous) == [], "an anonymous listener received a user's event"


@pytest.mark.anyio
async def test_tier_three_is_withheld_unless_the_user_opted_in(world):
    """Tier 3 is active use, and it is the user's to disclose.

    Sam's ladder has `tier_3_visible_to_caregiver` False, so Sarah must not be
    told. A user who fears their disclosure will be reported does not disclose;
    they use alone, which is the outcome this restraint exists to prevent.
    """
    sarah = register_listener(world["sarah"].id)

    await deps._broadcast(
        {
            "type": "tier",
            "tier": int(Tier.ACTIVE_USE),
            "reason": "said they used tonight",
            "user_id": world["sam"].id,
        }
    )
    assert drain(sarah) == [], "active use leaked to a caregiver the user did not tell"

    # And the same event DOES arrive once Sam opts in — proving the flag is read
    # live, and that the withholding above is the setting working rather than the
    # stream being broken.
    store.put_ladder(world["sam"].id, LadderConfig(tier_3_visible_to_caregiver=True))
    await deps._broadcast(
        {
            "type": "tier",
            "tier": int(Tier.ACTIVE_USE),
            "reason": "said they used tonight",
            "user_id": world["sam"].id,
        }
    )
    assert len(drain(sarah)) == 1


@pytest.mark.anyio
async def test_low_tiers_never_reach_a_caregiver(world):
    """Tiers 0-2 are never pushed, even to a linked caregiver.

    Craving is not news; it is Tuesday. Waking someone for it teaches the watched
    person to stop naming it, which removes the signal the product runs on.
    """
    sarah = register_listener(world["sarah"].id)

    for tier in (Tier.BASELINE, Tier.ELEVATED, Tier.CRAVING):
        await deps._broadcast(
            {"type": "tier", "tier": int(tier), "reason": "r", "user_id": world["sam"].id}
        )

    assert drain(sarah) == []


@pytest.mark.anyio
async def test_tier_two_visibility_does_not_open_the_live_stream(world):
    """`tier_2_visible_to_caregiver` governs display, not a 3am push.

    The flag lets the caregiver page describe the shape of the ladder. It is not
    a subscription to craving events, and conflating the two would turn a
    disclosure preference into an alerting one without the user asking for it.
    """
    store.put_ladder(world["sam"].id, LadderConfig(tier_2_visible_to_caregiver=True))
    sarah = register_listener(world["sarah"].id)

    await deps._broadcast(
        {"type": "tier", "tier": int(Tier.CRAVING), "reason": "r", "user_id": world["sam"].id}
    )
    assert drain(sarah) == []


@pytest.mark.anyio
async def test_revoking_a_link_silences_an_already_open_stream(world):
    """An open subscription is not a permanent grant.

    The link is re-read per event, so withdrawing consent stops delivery on the
    next event rather than at the caregiver's next reconnect — which they control
    and might never do.
    """
    sarah = register_listener(world["sarah"].id)
    store.unlink_caregiver(world["sarah"].id, world["sam"].id)

    await deps._broadcast(
        {
            "type": "tier",
            "tier": int(Tier.UNRESPONSIVE),
            "reason": "no response",
            "user_id": world["sam"].id,
        }
    )
    assert drain(sarah) == []


def test_a_subscriber_is_tagged_with_its_session_identity(world):
    """The listener's identity comes from the SIGNED COOKIE at subscribe time.

    Covers the wiring between the route and the filter: the tests above prove
    `visible_to` is correct, and this one proves the identity handed to it is
    derived from the session rather than from anything the subscriber sends.

    Exercised through `authenticated_user_id` — the exact call the SSE route
    makes — against a synthetic request, rather than by opening a real stream.
    `/api/events` never ends by design, so a TestClient stream would block the
    suite forever waiting for a response body that is not coming.
    """
    from starlette.requests import Request as StarletteRequest

    def request_with(cookie_value: str | None) -> StarletteRequest:
        headers = []
        if cookie_value is not None:
            headers.append(
                (b"cookie", f"{auth.SESSION_COOKIE}={cookie_value}".encode())
            )
        return StarletteRequest({"type": "http", "headers": headers})

    # A real signed token resolves to its account.
    token = auth.issue_token(world["sarah"].id)
    assert deps.authenticated_user_id(request_with(token)) == world["sarah"].id

    # No cookie at all: anonymous, and therefore entitled to nothing. The stream
    # still CONNECTS — the bystander surface is deliberately outside the auth
    # wall (PRD §3) and an error banner on a page someone reads while standing
    # over an unconscious person is its own harm — it simply receives no events.
    assert deps.authenticated_user_id(request_with(None)) is None

    # A forged or tampered token resolves to nobody rather than to a guess. This
    # is what stops a subscriber naming themselves: the id lives inside an
    # HMAC-signed payload, so editing it invalidates the signature.
    assert deps.authenticated_user_id(request_with("not.a.real.token")) is None
    forged = auth.issue_token(world["sarah"].id).split(".")[0] + ".forgedsignature"
    assert deps.authenticated_user_id(request_with(forged)) is None


# ---------------------------------------------------------------------------
# GENERATE — the model-backed surfaces
# ---------------------------------------------------------------------------
# No API key is required. These assert WHO the generation was built for, by
# checking which profile the route resolved, never what the model said.


def test_a_caregiver_brief_is_about_the_linked_user_only(client, world):
    """Sarah's brief is about Sam; Kim's is about Dana. Neither crosses.

    The brief is built from the watched person's profile and event log, so
    resolving the wrong subject would put one person's incident chronology in
    another caregiver's hands.
    """
    add_event(world["sam"].id, Tier.EMERGENCY, "sam-specific-reason")
    add_event(world["dana"].id, Tier.EMERGENCY, "dana-specific-reason")

    sign_in(client, "sarah-care")
    assert "dana-specific-reason" not in client.get("/api/caregiver/brief").text

    sign_out(client)
    sign_in(client, "kim-care")
    assert "sam-specific-reason" not in client.get("/api/caregiver/brief").text


def test_an_unlinked_caregiver_cannot_generate_a_brief(client, world):
    """No link, no generation. Refused before the model is ever consulted."""
    auth.register("nobody-care", PASSWORD, role="caregiver")
    sign_in(client, "nobody-care")

    assert client.get("/api/caregiver/brief").status_code == 403


def test_the_911_script_never_carries_another_users_address(client, world):
    """The single most dangerous artefact in the product.

    A 911 script contains a home address, an apartment number and a door entry
    code. Generating one against the wrong profile would hand a stranger exactly
    what they would need to find a person who is using.
    """
    sign_in(client, "dana-user")
    body = client.get("/api/script/911").text

    assert "SAM-ENTRY-1180" not in body
    assert "1412 Highland Avenue" not in body


def test_the_vault_offers_only_the_callers_own_clips(client, world):
    """A recorded message was made for one listener.

    It names the speaker and the relationship. Offering Sam's clips as candidates
    for Dana's selection would leak both, even if the model never picked one —
    the transcripts are sent to the provider as candidates regardless.
    """
    store.put_vault_clip(
        VaultClip(
            id="clip-sam", recorded_by="Sarah", relation="Sister",
            transcript="sam-private-transcript",
        ),
        owner_user_id=world["sam"].id,
    )

    offered = store.list_vault_clips(for_user=world["dana"].id)
    assert all(c.id != "clip-sam" for c in offered)
    assert any(c.id == "clip-sam" for c in store.list_vault_clips(for_user=world["sam"].id))


# ---------------------------------------------------------------------------
# DELETE — the boundary at the end of the relationship
# ---------------------------------------------------------------------------


def test_deleting_an_account_leaves_the_other_user_intact(client, world):
    """One person leaving must not damage anybody else's record.

    The counterpart to completeness: a deletion broad enough to catch every trace
    of Sam is also broad enough to catch Dana if it is not scoped, and a blanket
    `DELETE FROM vault_clips` would have done exactly that.
    """
    store.put_vault_clip(
        VaultClip(id="clip-sam", recorded_by="Sarah", relation="Sister", transcript="s"),
        owner_user_id=world["sam"].id,
    )
    store.put_vault_clip(
        VaultClip(id="clip-dana", recorded_by="Kim", relation="Friend", transcript="d"),
        owner_user_id=world["dana"].id,
    )
    add_event(world["sam"].id, Tier.CRAVING, "sam event")
    add_event(world["dana"].id, Tier.CRAVING, "dana event")

    store.delete_user_data(world["sam"].id)

    assert store.get_user(world["sam"].id) is None
    assert store.get_vault_clip("clip-sam") is None
    assert store.list_events(world["sam"].id) == []

    assert store.get_user(world["dana"].id) is not None
    assert store.get_vault_clip("clip-dana") is not None
    assert len(store.list_events(world["dana"].id)) == 1


def test_deleting_a_user_revokes_their_caregivers_access(client, world):
    """The link goes when the account does, in both directions.

    A surviving row would name an account that no longer exists — and if that id
    were ever reissued, a stranger would inherit a caregiver they never consented
    to.
    """
    store.delete_user_data(world["sam"].id)

    assert store.watched_users(world["sarah"].id) == []
    assert store.caregivers_for(world["sam"].id) == []


def test_deletion_clears_live_state_and_the_session(client, world):
    """Deletion is not complete while an in-memory trace or a valid cookie remains.

    A ladder cursor outliving the records behind it, or a browser still holding a
    signed token for a deleted account, would both make /data-deletion's
    "immediate and total" claim false.
    """
    sign_in(client, "sam-user")
    client.post("/api/tier", json={"tier": 4})
    assert world["sam"].id in deps._tiers

    res = client.post("/api/account/delete")
    assert res.status_code == 200

    assert world["sam"].id not in deps._tiers, "live ladder cursor outlived the account"
    # The cookie is cleared, so the next call is anonymous rather than
    # authenticated-as-a-deleted-user.
    assert client.get("/api/auth/me").json()["signed_in"] is False


def test_deletion_reports_what_it_removed(client, world):
    """The confirmation states counts rather than asserting success generically.

    A deletion the person cannot verify is one they have to take on trust, which
    is the exact thing this page exists to avoid asking of them.
    """
    sign_in(client, "sam-user")
    deleted = client.post("/api/account/delete").json()["deleted"]

    assert deleted["users"] == 1
    assert deleted["profiles"] == 1
    assert deleted["caregiver_links"] == 1
    for key in ("vault_clips", "generation_cache_files"):
        assert key in deleted, f"deletion did not report on {key}"


@pytest.fixture
def anyio_backend():
    """Pin the async tests to asyncio; the app has no trio dependency."""
    return "asyncio"


# ---------------------------------------------------------------------------
# The broadcaster the app actually uses
# ---------------------------------------------------------------------------
def test_the_app_uses_exactly_one_broadcaster():
    """Guards against the duplicate that made every other push test meaningless.

    `app/main.py` once defined its own `_broadcast` that shadowed the one in
    `deps.py`. The two differed in one argument — main's omitted `live=True` —
    so Tier 2 cravings were pushed to caregivers in production while the tests
    below, which exercise `deps._broadcast`, went green. A passing suite over an
    open privacy leak.

    Asserting on the bound module rather than on behaviour is deliberate: the
    behavioural tests cannot detect this class of bug by construction, because
    they call the function directly rather than through a route.
    """
    from app import deps, main

    assert main._broadcast is deps._broadcast, (
        "main.py has rebound _broadcast — every SSE privacy test below is now "
        "testing a function the app does not call"
    )


@pytest.mark.anyio
async def test_a_craving_is_not_pushed_through_the_real_route(world):
    """End-to-end version of the Tier 2 rule, through the code path routes use.

    The sibling test calls `deps._broadcast` directly. This one goes through
    whatever `main` has bound, so it fails if the duplicate ever returns.
    """
    from app import main

    store.put_ladder(world["sam"].id, LadderConfig(tier_2_visible_to_caregiver=True))
    sarah = register_listener(world["sarah"].id)

    await main._broadcast(
        {
            "type": "tier",
            "tier": int(Tier.CRAVING),
            "reason": "r",
            "user_id": world["sam"].id,
        }
    )
    assert drain(sarah) == [], "a craving reached a caregiver's live stream"
