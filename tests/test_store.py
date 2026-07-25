"""Tests for `app.store` and `app.seed` — persistence and demo state.

WHAT THESE TESTS PROTECT AGAINST
    Three classes of failure, each named at the test that guards it:

    1. Lossy round-trips. A profile that loses its entry code, or a contact
       tree that comes back in the wrong order, is a person whose paramedics
       stand outside the wrong door. The round-trip tests assert every field.
    2. A mutable event log. PRD §11 promises the user a log nobody can quietly
       edit. That is enforced by SQLite triggers, and the tests here prove the
       triggers actually fire rather than trusting that they were installed.
    3. A non-idempotent seed. Startup calls `seed()` unconditionally; if it
       duplicated Sarah into the contact tree on every boot, the demo would
       degrade with each restart.

WHAT THESE TESTS DELIBERATELY DO NOT DO
    They do not test triage or generation. The store is intentionally logic-free
    — it will happily persist a nonsensical tier — and asserting behaviour here
    that belongs to `app/triage.py` would create a second, weaker source of
    truth for the safety-critical path.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta

import pytest

from app import seed as seed_module
from app import store
from app.models import (
    Contact,
    Event,
    LadderConfig,
    Tier,
    ToleranceEvent,
    UserProfile,
    VaultClip,
)


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    """Fresh database file per test, so no test can see another's rows."""
    monkeypatch.setattr(store, "_db_path", tmp_path / "test.db")
    store.init_db()
    yield


@pytest.fixture
def user():
    """A stored account to hang profiles and events off.

    Writes the credential columns directly rather than going through
    `app.auth`, because these tests are about persistence and paying the scrypt
    cost in every fixture would make the suite needlessly slow.
    """
    return store.create_user(
        id=uuid.uuid4().hex,
        username="tester",
        password_hash="deadbeef" * 16,
        salt="00112233445566778899aabbccddeeff",
        role="user",
    )


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_init_db_is_idempotent():
    """Running schema creation repeatedly is safe.

    `init_db()` runs on every boot with no migration step, so a second call
    must not raise on an existing table, trigger, or index.
    """
    store.init_db()
    store.init_db()
    with store.connection() as conn:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert {
        "users",
        "profiles",
        "ladder_config",
        "contacts",
        "tolerance_events",
        "vault_clips",
        "events",
    } <= tables


def test_foreign_keys_are_enforced():
    """A child row cannot reference a missing parent.

    Without `PRAGMA foreign_keys = ON` (off by default in SQLite) a deleted
    profile would leave its contacts behind — meaning a caregiver a user
    removed from the tree could still be reachable. That is a privacy failure.
    """
    with pytest.raises(sqlite3.IntegrityError):
        with store.connection() as conn:
            conn.execute(
                "INSERT INTO contacts (profile_id, name, relation, channel, position, tiers)"
                " VALUES ('no-such-profile', 'Ghost', 'None', 'phone', 1, '[]')"
            )


def test_role_check_constraint_rejects_unknown_roles():
    """The database refuses a role outside {user, caregiver}.

    A storage-level CHECK means a future code path that skips validation still
    cannot introduce a role the authorization checks do not understand.
    """
    with pytest.raises(sqlite3.IntegrityError):
        store.create_user(
            id="x",
            username="x",
            password_hash="x",
            salt="x",
            role="superadmin",
        )


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


def test_user_round_trip(user):
    """Every stored user field survives a write/read cycle."""
    by_id = store.get_user(user.id)
    by_name = store.get_user_by_username("tester")
    assert by_id == by_name == user
    assert by_id.role == "user"
    assert isinstance(by_id.created_at, datetime)


def test_get_user_by_username_is_case_insensitive(user):
    """Lookup matches regardless of case, matching the UNIQUE collation."""
    assert store.get_user_by_username("TESTER").id == user.id
    assert store.get_user_by_username("TeStEr").id == user.id


def test_missing_users_return_none():
    """Absent accounts return None rather than raising."""
    assert store.get_user("nope") is None
    assert store.get_user_by_username("nope") is None


def test_duplicate_username_is_rejected(user):
    """The UNIQUE constraint is the authority on username collisions."""
    with pytest.raises(sqlite3.IntegrityError):
        store.create_user(
            id="other",
            username="TESTER",
            password_hash="x",
            salt="y",
            role="user",
        )


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------


def _sample_profile() -> UserProfile:
    """A profile exercising every field, including the ones easiest to drop."""
    return UserProfile(
        id="profile-1",
        name="Sam",
        address="1412 Highland Avenue, Louisville, KY 40204",
        unit="Apartment 4B",
        entry_code="1180",
        cross_street="Corner of Barret Avenue",
        state_code="KY",
        substances=["opioids"],
        naloxone_on_hand=True,
        ladder=LadderConfig(
            tier_3_visible_to_caregiver=False,
            tier_2_visible_to_caregiver=True,
            missed_checkins_to_elevate=3,
            silence_seconds_to_escalate=45,
        ),
        contacts=[
            Contact(
                name="Sarah",
                relation="Sister",
                channel="phone",
                order=1,
                tiers=[Tier.EMERGENCY, Tier.UNRESPONSIVE],
            ),
            Contact(
                name="Marcus",
                relation="Sponsor",
                channel="phone",
                order=2,
                tiers=[Tier.UNRESPONSIVE],
            ),
        ],
        tolerance_events=[
            ToleranceEvent(
                kind="hospital_discharge",
                date=datetime(2026, 7, 14, 9, 30),
                note="Discharged after an inpatient stay.",
            )
        ],
    )


def test_profile_round_trip_preserves_every_field(user):
    """A profile reads back exactly as written.

    The address components are asserted individually on purpose: `unit` and
    `entry_code` are the fields a naive schema silently drops, and they are
    literally the difference between paramedics reaching a door and standing
    outside a locked lobby (PRD §5).
    """
    original = _sample_profile()
    store.put_profile(user.id, original)
    loaded = store.get_profile(user.id)

    assert loaded is not None
    assert loaded.name == "Sam"
    assert loaded.address == original.address
    assert loaded.unit == "Apartment 4B"
    assert loaded.entry_code == "1180"
    assert loaded.cross_street == "Corner of Barret Avenue"
    assert loaded.state_code == "KY"
    assert loaded.substances == ["opioids"]
    assert loaded.naloxone_on_hand is True
    assert loaded == original


def test_contacts_keep_their_order_and_tiers(user):
    """The contact tree reads back in fire order with tiers intact.

    Order is who gets called first; tiers are whether they get called at all.
    A tier list that came back empty would mean nobody is contacted at Tier 5.
    """
    store.put_profile(user.id, _sample_profile())
    contacts = store.get_profile(user.id).contacts

    assert [c.name for c in contacts] == ["Sarah", "Marcus"]
    assert [c.order for c in contacts] == [1, 2]
    assert contacts[0].tiers == [Tier.EMERGENCY, Tier.UNRESPONSIVE]
    assert contacts[1].tiers == [Tier.UNRESPONSIVE]
    # Tiers must survive as Tier enum members, not bare ints, or comparisons
    # in the notification path would silently stop matching.
    assert all(isinstance(t, Tier) for c in contacts for t in c.tiers)


def test_put_profile_replaces_children_rather_than_appending(user):
    """Re-saving a profile does not duplicate or resurrect contacts.

    Two regressions in one: an append-only write would double Sarah on every
    save, and a diff-based write could leave a removed contact reachable — the
    exact thing a user relies on when they take someone off the tree.
    """
    profile = _sample_profile()
    store.put_profile(user.id, profile)
    store.put_profile(user.id, profile)
    assert len(store.get_profile(user.id).contacts) == 2

    profile.contacts = [profile.contacts[0]]
    store.put_profile(user.id, profile)
    remaining = store.get_profile(user.id).contacts
    assert [c.name for c in remaining] == ["Sarah"]


def test_tolerance_events_round_trip(user):
    """Tolerance events keep their kind, timestamp and note."""
    store.put_profile(user.id, _sample_profile())
    events = store.get_profile(user.id).tolerance_events
    assert len(events) == 1
    assert events[0].kind == "hospital_discharge"
    assert events[0].date == datetime(2026, 7, 14, 9, 30)
    assert events[0].note.startswith("Discharged")


def test_get_profile_returns_none_for_a_user_without_one(user):
    """A freshly registered account has no profile, and that is not an error."""
    assert store.get_profile(user.id) is None


# ---------------------------------------------------------------------------
# Ladder config
# ---------------------------------------------------------------------------


def test_ladder_round_trip_and_targeted_update(user):
    """Ladder config saves with the profile and can be updated on its own.

    `tier_3_visible_to_caregiver` is the flag the whole demo turns on: the
    system choosing NOT to escalate at Tier 3 is the point. A boolean that
    round-tripped as an int, or defaulted to True, would invert that.
    """
    store.put_profile(user.id, _sample_profile())
    ladder = store.get_ladder(user.id)
    assert ladder.tier_3_visible_to_caregiver is False
    assert ladder.tier_2_visible_to_caregiver is True
    assert ladder.missed_checkins_to_elevate == 3
    assert ladder.silence_seconds_to_escalate == 45

    store.put_ladder(user.id, LadderConfig(tier_3_visible_to_caregiver=True))
    updated = store.get_ladder(user.id)
    assert updated.tier_3_visible_to_caregiver is True
    # The rest of the profile is untouched by a ladder-only write.
    assert store.get_profile(user.id).entry_code == "1180"


def test_ladder_defaults_to_private_when_no_row_exists(user):
    """A missing ladder row yields the private defaults, not exposure.

    Failing toward privacy: if the config were ever lost, tiers 2 and 3 must
    stay hidden rather than start broadcasting to a caregiver.
    """
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO profiles (id, user_id, name) VALUES ('bare', ?, 'Bare')",
            (user.id,),
        )
    ladder = store.get_ladder(user.id)
    assert ladder.tier_3_visible_to_caregiver is False
    assert ladder.tier_2_visible_to_caregiver is False


def test_put_ladder_returns_none_without_a_profile(user):
    """Saving ladder config for a profile-less user is a no-op, not a crash."""
    assert store.put_ladder(user.id, LadderConfig()) is None
    assert store.get_ladder(user.id) is None


# ---------------------------------------------------------------------------
# Vault clips
# ---------------------------------------------------------------------------


def test_vault_clip_round_trip():
    """A clip keeps its transcript, tags and null audio path.

    `audio_path` stays None because no audio file is shipped. If it round-
    tripped as the string "None", the UI would try to play a missing file.
    """
    clip = VaultClip(
        id="clip-1",
        recorded_by="Sarah",
        relation="Sister",
        transcript="You can call me. There's no number of times.",
        tags=["isolation", "tier_2"],
    )
    store.put_vault_clip(clip)
    loaded = store.get_vault_clip("clip-1")
    assert loaded == clip
    assert loaded.audio_path is None
    assert loaded.tags == ["isolation", "tier_2"]


def test_vault_clip_upsert_does_not_duplicate():
    """Storing the same clip id twice updates rather than duplicates.

    This is what makes `seed()` safe to run on every boot.
    """
    clip = VaultClip(id="clip-1", recorded_by="Sarah", relation="Sister", transcript="v1")
    store.put_vault_clip(clip)
    store.put_vault_clip(clip.model_copy(update={"transcript": "v2"}))
    clips = store.list_vault_clips()
    assert len(clips) == 1
    assert clips[0].transcript == "v2"


def test_list_vault_clips_is_stably_ordered():
    """Clips come back in a deterministic order.

    The list is the candidate set handed to clip selection; a shuffling order
    would make the demo unreproducible.
    """
    for cid in ["clip-c", "clip-a", "clip-b"]:
        store.put_vault_clip(
            VaultClip(id=cid, recorded_by="Sarah", relation="Sister", transcript="x")
        )
    assert [c.id for c in store.list_vault_clips()] == ["clip-a", "clip-b", "clip-c"]


def test_missing_clip_returns_none():
    """An unknown clip id returns None.

    Callers must handle this: the model picks a clip id, and a model can pick
    one that does not exist.
    """
    assert store.get_vault_clip("no-such-clip") is None


# ---------------------------------------------------------------------------
# Event log — append-only
# ---------------------------------------------------------------------------


def _event(user_id: str, tier: Tier, reason: str) -> Event:
    """Build an event with a unique id."""
    return Event(
        id=uuid.uuid4().hex,
        user_id=user_id,
        at=datetime.now(),
        tier=tier,
        trigger_source="test",
        reason=reason,
        actions_taken=["speak"],
    )


def test_event_round_trip_and_newest_first(user):
    """Events read back intact, ordered newest first."""
    store.append_event(_event(user.id, Tier.ELEVATED, "first"))
    store.append_event(_event(user.id, Tier.CRAVING, "second"))
    store.append_event(_event(user.id, Tier.EMERGENCY, "third"))

    events = store.list_events(user.id)
    assert [e.reason for e in events] == ["third", "second", "first"]
    assert events[0].tier is Tier.EMERGENCY
    assert events[0].trigger_source == "test"
    assert events[0].actions_taken == ["speak"]
    assert events[0].user_visible is True


def test_events_cannot_be_updated(user):
    """The database aborts any UPDATE against the event log.

    PRD §11 promises a log nobody can quietly edit — including us. Enforced by
    trigger rather than convention, because a convention is one refactor away
    from being broken. This test proves the trigger is actually installed.
    """
    store.append_event(_event(user.id, Tier.ACTIVE_USE, "the truth"))
    with pytest.raises(sqlite3.IntegrityError):
        with store.connection() as conn:
            conn.execute("UPDATE events SET reason = 'a nicer story'")

    assert store.list_events(user.id)[0].reason == "the truth"


def test_events_cannot_be_deleted(user):
    """The database aborts any DELETE against the event log."""
    store.append_event(_event(user.id, Tier.EMERGENCY, "this happened"))
    with pytest.raises(sqlite3.IntegrityError):
        with store.connection() as conn:
            conn.execute("DELETE FROM events")

    assert len(store.list_events(user.id)) == 1


def test_store_exposes_no_event_mutation_functions():
    """There is no update/delete helper for events in the module's API.

    Guards the human-facing half of the guarantee: even though the triggers
    would abort, a tempting `delete_event()` in the module would invite someone
    to work around them.
    """
    for forbidden in ("update_event", "delete_event", "clear_events", "edit_event"):
        assert not hasattr(store, forbidden)


def test_events_are_scoped_to_their_user(user):
    """One user's log never contains another's events.

    Ladder history is among the most sensitive data in the product; leaking it
    across accounts would be a serious privacy failure.
    """
    other = store.create_user(
        id="other", username="other", password_hash="x", salt="y", role="user"
    )
    store.append_event(_event(user.id, Tier.CRAVING, "mine"))
    store.append_event(_event(other.id, Tier.CRAVING, "theirs"))

    assert [e.reason for e in store.list_events(user.id)] == ["mine"]
    assert [e.reason for e in store.list_events(other.id)] == ["theirs"]


def test_events_ordered_by_seq_not_timestamp(user):
    """Ordering survives identical timestamps.

    Escalations can land in the same microsecond. Ordering by `at` alone would
    make a fast Tier 3 -> 4 -> 5 sequence display in an arbitrary order, which
    is exactly when the user most needs to see what happened first.
    """
    same_moment = datetime.now()
    for reason in ["one", "two", "three"]:
        store.append_event(
            Event(
                id=uuid.uuid4().hex,
                user_id=user.id,
                at=same_moment,
                tier=Tier.EMERGENCY,
                trigger_source="test",
                reason=reason,
            )
        )
    assert [e.reason for e in store.list_events(user.id)] == ["three", "two", "one"]


def test_latest_event_and_empty_log(user):
    """`latest_event` returns the newest entry, or None for a fresh account."""
    assert store.latest_event(user.id) is None
    store.append_event(_event(user.id, Tier.ELEVATED, "first"))
    store.append_event(_event(user.id, Tier.UNRESPONSIVE, "latest"))
    assert store.latest_event(user.id).reason == "latest"


def test_list_events_respects_its_limit(user):
    """The limit bounds the query, so a long demo cannot blow up a page render."""
    for i in range(10):
        store.append_event(_event(user.id, Tier.ELEVATED, f"event-{i}"))
    assert len(store.list_events(user.id, limit=3)) == 3


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


def test_seed_creates_the_demo_state():
    """Seeding produces both contract accounts, Sam's profile and three clips."""
    seed_module.seed()

    sam = store.get_user_by_username("sam")
    sarah = store.get_user_by_username("sarah")
    assert sam is not None and sam.role == "user"
    assert sarah is not None and sarah.role == "caregiver"

    profile = store.get_profile(sam.id)
    assert profile.name == "Sam"
    assert profile.state_code == "KY"
    assert profile.naloxone_on_hand is True
    assert profile.unit and profile.entry_code and profile.cross_street

    assert len(store.list_vault_clips()) == 3


def test_seeded_demo_credentials_work():
    """The credentials printed on the login screen actually sign in.

    Contract ground rule 4: auth must never block an evaluator. If this test
    fails, the demo is unusable by a judge.
    """
    from app import auth

    seed_module.seed()
    assert auth.verify_login("sam", "threshold").role == "user"
    assert auth.verify_login("sarah", "threshold").role == "caregiver"


def test_seed_stores_no_plaintext_demo_password():
    """Even the public demo password is hashed at rest.

    Being printed on the login screen does not license a shortcut that writes
    it directly — a demo-only credential path is how weak hashes reach a repo.
    """
    seed_module.seed()
    assert b"threshold" not in store.db_path().read_bytes()


def test_seed_is_idempotent():
    """Seeding repeatedly converges on the same state.

    Startup calls `seed()` unconditionally. If it appended, Sarah would appear
    in the contact tree twice after two boots and three times after three.
    """
    seed_module.seed()
    seed_module.seed()
    seed_module.seed()

    assert len(store.list_users()) == 2
    assert len(store.list_vault_clips()) == 3
    sam = store.get_user_by_username("sam")
    profile = store.get_profile(sam.id)
    assert [c.name for c in profile.contacts] == ["Sarah", "Marcus"]
    assert len(profile.tolerance_events) == 1


def test_seed_tier_3_is_hidden_from_the_caregiver():
    """The seeded ladder keeps Tier 3 private.

    The demo depends on the system CHOOSING not to escalate at active use. If
    this flag ever seeds True, the product's central argument — that restraint
    is what keeps someone from uninstalling it — disappears from the demo.
    """
    seed_module.seed()
    sam = store.get_user_by_username("sam")
    ladder = store.get_profile(sam.id).ladder
    assert ladder.tier_3_visible_to_caregiver is False
    assert ladder.tier_2_visible_to_caregiver is False


def test_seed_contact_tree_shape():
    """Sarah is first and tiers 4/5; the sponsor is second and tier 5 only.

    Keeping the sister off the lower tiers is a design decision, not an
    oversight: a caregiver pinged for every craving stops being someone the
    user wants in the app.
    """
    seed_module.seed()
    contacts = store.get_profile(store.get_user_by_username("sam").id).contacts

    sarah, sponsor = contacts
    assert sarah.name == "Sarah" and sarah.relation == "Sister" and sarah.order == 1
    assert sarah.tiers == [Tier.EMERGENCY, Tier.UNRESPONSIVE]
    assert sponsor.relation == "Sponsor" and sponsor.order == 2
    assert sponsor.tiers == [Tier.UNRESPONSIVE]


def test_seed_tolerance_event_is_computed_not_hardcoded():
    """The discharge is always 11 days before now, whenever the demo runs.

    A hardcoded date would rot: months later the demo would warn about a
    tolerance window that had long closed, making the single most important
    number on the screen wrong.
    """
    now = datetime(2030, 3, 15, 12, 0)
    seed_module.seed(now=now)
    events = store.get_profile(store.get_user_by_username("sam").id).tolerance_events

    assert len(events) == 1
    assert events[0].kind == "hospital_discharge"
    assert (now - events[0].date).days == seed_module.DAYS_SINCE_DISCHARGE == 11


def test_seed_writes_no_events():
    """A freshly seeded demo has an empty ladder log.

    Contract ground rule 1 forbids fake data. A pre-populated escalation
    history would be inventing events that never happened, and would make the
    ladder UI a fiction on first load.
    """
    seed_module.seed()
    assert store.list_events(store.get_user_by_username("sam").id) == []


def test_seed_vault_clips_are_attributed_and_distinct():
    """The three clips are Sarah's, transcribed, with no fake audio file.

    A vault clip is a recording of something a caregiver actually said — real
    data, not model output. The AI selects among these; it never writes one.
    They are distinct in kind so that selection is a real decision.
    """
    seed_module.seed()
    clips = store.list_vault_clips()

    assert len(clips) == 3
    assert all(c.recorded_by == "Sarah" and c.relation == "Sister" for c in clips)
    assert all(c.transcript.strip() for c in clips)
    # No audio is shipped, so nothing should claim a playable file.
    assert all(c.audio_path is None for c in clips)
    # Distinct transcripts and distinct tag sets, so clip choice is meaningful.
    assert len({c.transcript for c in clips}) == 3
    assert len({tuple(c.tags) for c in clips}) == 3


def test_reset_restores_the_demo_state():
    """Reset clears user-created data and rebuilds the seeded state.

    Backs POST /api/reset. It must leave no residue — no stray event, no
    account an evaluator created mid-demo — so that "reset" means the same
    thing every time and the demo is re-runnable by a stranger.
    """
    seed_module.seed()
    sam = store.get_user_by_username("sam")
    store.append_event(_event(sam.id, Tier.EMERGENCY, "demo escalation"))
    store.create_user(
        id="evaluator", username="evaluator", password_hash="x", salt="y", role="user"
    )

    seed_module.reset()

    sam_again = store.get_user_by_username("sam")
    assert sam_again is not None
    assert store.list_events(sam_again.id) == []
    assert store.get_user_by_username("evaluator") is None
    assert len(store.list_vault_clips()) == 3
    assert store.get_profile(sam_again.id).ladder.tier_3_visible_to_caregiver is False


def test_reset_works_around_the_append_only_triggers():
    """Reset succeeds despite the event log being undeletable.

    `reset()` drops tables rather than deleting rows, precisely because the
    append-only triggers would abort a row-level delete. If someone changed
    reset to `DELETE FROM events`, this test fails.
    """
    seed_module.seed()
    sam = store.get_user_by_username("sam")
    store.append_event(_event(sam.id, Tier.UNRESPONSIVE, "cannot be deleted"))

    seed_module.reset()  # must not raise

    assert store.list_events(store.get_user_by_username("sam").id) == []


def test_is_seeded_reflects_the_demo_user_only():
    """`is_seeded` keys on the demo account, not on any account existing.

    Otherwise an evaluator who registers first would suppress the seed and land
    in an empty demo.
    """
    assert seed_module.is_seeded() is False
    store.create_user(
        id="evaluator", username="evaluator", password_hash="x", salt="y", role="user"
    )
    assert seed_module.is_seeded() is False

    seed_module.seed()
    assert seed_module.is_seeded() is True


# ---------------------------------------------------------------------------
# JSON column encoding
# ---------------------------------------------------------------------------


def test_json_columns_store_plain_values(user):
    """List columns are stored as readable JSON, not Python reprs.

    `str(["a"])` produces `['a']`, which is not valid JSON and would fail to
    parse on read. Storing plain numbers for tiers also keeps the column
    legible from the sqlite3 CLI during a demo.
    """
    store.put_profile(user.id, _sample_profile())
    with store.connection() as conn:
        row = conn.execute("SELECT tiers FROM contacts ORDER BY position").fetchone()
        assert json.loads(row["tiers"]) == [4, 5]

        substances = conn.execute("SELECT substances FROM profiles").fetchone()
        assert json.loads(substances["substances"]) == ["opioids"]
