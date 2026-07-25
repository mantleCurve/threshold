"""Seeds the demo state, and provides the reset used by POST /api/reset.

WHAT THIS MODULE DOES
    Creates the two demo accounts from the build contract, Sam's profile
    (address, contact tree, ladder config, tolerance history) and the three
    recorded vault clips, on first boot. Idempotent: running it against an
    already-seeded database is a no-op, so it can be called unconditionally at
    startup. Also exposes `reset()`, which restores that exact state.

WHAT THIS MODULE DELIBERATELY DOES NOT DO
    * It does NOT seed any AI output. Contract ground rule 1 forbids hardcoded
      prose presented as model output, and this module writes none. Every
      Generation in the product comes from a live call or is visibly labelled a
      fallback by `app/genai.py`.
    * It does NOT seed ladder events. Sam starts at Tier 0 with an empty log,
      because a pre-populated escalation history would be exactly the fake data
      the contract prohibits. The log fills up as an evaluator uses the app.
    * It does NOT hardcode dates. See `_hospital_discharge` below — the
      tolerance window is computed from `now`, so the demo reads correctly
      whenever it is run.

ON THE VAULT TRANSCRIPTS
    The three clips carry real prose in their `transcript` field. This is NOT
    model output dressed up as data, and it is not a violation of ground rule 1.
    A vault clip is a recording of a thing a caregiver actually said — the
    transcript IS the data, in the same way a contact's phone number is data.
    They are written here as demo content standing in for what Sarah would have
    recorded in her own voice during onboarding. The model's only role is
    SELECTING among them; it never writes one. See the comment at
    `_vault_clips()`.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from app import auth, store
from app.models import (
    Contact,
    LadderConfig,
    Tier,
    ToleranceEvent,
    UserProfile,
    VaultClip,
)

# ---------------------------------------------------------------------------
# Demo identities (frozen by CONTRACT.md — the login screen prints these)
# ---------------------------------------------------------------------------

DEMO_USER_USERNAME = "sam"
DEMO_CAREGIVER_USERNAME = "sarah"

# The shared demo password. It is in the contract, printed on the login screen
# and pre-filled in the form, because ground rule 4 says auth must never block
# an evaluator. Being public does not make it plaintext-at-rest: it still goes
# through scrypt with a per-user salt like any other password, and the database
# never contains this string.
DEMO_PASSWORD = "threshold"

# Stable profile id so that re-seeding upserts the same row rather than
# accumulating duplicate profiles.
DEMO_PROFILE_ID = "profile-sam"

# Days since discharge. Tolerance falls fast after any period of abstinence and
# the risk window after a hospital discharge is measured in days, not months —
# 11 days puts Sam inside the period the prevention message is about, which is
# the entire point of the tolerance feature.
DAYS_SINCE_DISCHARGE = 11


def _hospital_discharge(now: datetime) -> ToleranceEvent:
    """Build Sam's discharge event relative to the current time.

    Computed at runtime rather than hardcoded. A fixed date would silently rot:
    a demo run three months from now would show a tolerance warning about an
    event long outside the risk window, and the single most important number on
    the screen would be wrong.

    Args:
        now: The reference time, injected so tests can pin it.

    Returns:
        A `ToleranceEvent` dated `DAYS_SINCE_DISCHARGE` days before `now`.
    """
    return ToleranceEvent(
        kind="hospital_discharge",
        date=now - timedelta(days=DAYS_SINCE_DISCHARGE),
        note=(
            "Discharged after an inpatient stay. Tolerance is substantially lower "
            "than before admission; a previously routine amount can be fatal."
        ),
    )


def _demo_profile(now: datetime) -> UserProfile:
    """Build Sam's complete profile.

    Args:
        now: Reference time for the tolerance event.

    Returns:
        A fully populated `UserProfile`.
    """
    return UserProfile(
        id=DEMO_PROFILE_ID,
        name="Sam",
        # Address is broken into parts because PRD §5 has these read out
        # separately to a dispatcher. "Apartment 4B, door code 1180, near the
        # corner of Barret" is what gets paramedics through the door instead of
        # leaving them in a lobby.
        address="1412 Highland Avenue, Louisville, KY 40204",
        unit="Apartment 4B",
        entry_code="1180",
        cross_street="Corner of Barret Avenue",
        # KY drives the Good Samaritan lookup. Kentucky is a high-burden state
        # and its record is the one verified first in data/legal/.
        state_code="KY",
        substances=["opioids"],
        # Naloxone on hand changes what the Tier 4 and bystander flows can
        # instruct. With it true, the bystander screen can say "there is
        # naloxone in the apartment" rather than only "wait for paramedics".
        naloxone_on_hand=True,
        ladder=LadderConfig(
            # THE LOAD-BEARING FLAG FOR THE DEMO. Tier 3 is active use. Sam has
            # chosen not to surface that to his caregiver, and the system
            # honours it: at Tier 3 it does NOT notify Sarah. That restraint is
            # the point — a system that escalates everything gets uninstalled,
            # and an uninstalled system saves nobody. PRD §4.2 makes tiers 4
            # and 5 non-negotiable precisely so this choice is safe to offer.
            tier_3_visible_to_caregiver=False,
            tier_2_visible_to_caregiver=False,
            missed_checkins_to_elevate=2,
            silence_seconds_to_escalate=20,
        ),
        contacts=[
            Contact(
                name="Sarah",
                relation="Sister",
                channel="phone",
                order=1,
                # Tiers 4 and 5 only. Sarah is not pinged for a craving; she is
                # called when Sam is in medical trouble. Keeping her off the
                # lower tiers is what keeps her a person Sam wants in the app.
                tiers=[Tier.EMERGENCY, Tier.UNRESPONSIVE],
            ),
            Contact(
                name="Marcus",
                relation="Sponsor",
                channel="phone",
                order=2,
                # Tier 5 only: second in the tree, reached when Sam is
                # unresponsive and the first contact may not have answered.
                tiers=[Tier.UNRESPONSIVE],
            ),
        ],
        tolerance_events=[_hospital_discharge(now)],
    )


def _vault_clips() -> list[VaultClip]:
    """Build the three seeded vault clips.

    IMPORTANT — why prose here is not fake AI data:

    A vault clip is a recording a caregiver made of themselves speaking. The
    `transcript` field is a transcription of that recording. It is real content
    of a real kind, exactly like a stored phone number or address — not model
    output, and never presented as model output. The application's AI touches
    these clips in one way only: at Tier 2/3 it SELECTS which existing clip
    best fits the moment and explains why. It cannot write one, and if it could
    that would be the second-worst hallucination in the product after bad legal
    text — a synthesised "message from your sister" is a betrayal of the exact
    relationship this feature depends on.

    In a real deployment Sarah records these during onboarding in her own
    voice. Here they stand in for that recording session, so `audio_path` is
    None: we do not ship a fake audio file, and the UI shows the transcript
    rather than pretending to play something.

    The three are deliberately different in kind, so clip selection is a real
    decision and not a coin flip: one is about identity, one is about the
    physical fact of tolerance, one is about permission to call.

    Returns:
        Three `VaultClip`s with stable ids.
    """
    return [
        VaultClip(
            id="clip-sarah-01",
            recorded_by="Sarah",
            relation="Sister",
            transcript=(
                "Hey. It's me. If you're hearing this it's because you asked me "
                "to record it, back when you were thinking clearly, and you told "
                "me to remind you of something. You said: tell me I'm not a bad "
                "person, I'm a person with a thing that's hard. So that's what "
                "I'm telling you. You're not a bad person. Whatever's happening "
                "right now doesn't change that, and it doesn't change me."
            ),
            tags=["shame", "craving", "identity", "tier_2"],
            # No audio file is shipped. The UI shows the transcript rather than
            # implying a recording exists that does not.
            audio_path=None,
        ),
        VaultClip(
            id="clip-sarah-02",
            recorded_by="Sarah",
            relation="Sister",
            transcript=(
                "I'm not going to tell you what to do. I want to say one thing "
                "and then I'll stop. You were in the hospital eleven days ago. "
                "Your body isn't where it was before. The amount that used to be "
                "your normal is not your normal anymore, and it isn't a "
                "willpower thing, it's just what your body is right now. If "
                "you're going to do this, please do less than you think, and "
                "please don't do it behind a locked door."
            ),
            # Tagged for the tolerance and active-use contexts: this is the clip
            # the selector should reach for when the risk is physiological
            # rather than emotional.
            tags=["tolerance", "post_discharge", "active_use", "tier_3"],
            audio_path=None,
        ),
        VaultClip(
            id="clip-sarah-03",
            recorded_by="Sarah",
            relation="Sister",
            transcript=(
                "You can call me. I know you think there's a version of this "
                "where you've used up all your chances with me and you can't "
                "call anymore. There isn't. There's no number of times. I would "
                "much rather get a call at four in the morning than find out "
                "later that you didn't want to bother me. Please just call."
            ),
            tags=["isolation", "permission", "reaching_out", "tier_2", "tier_3"],
            audio_path=None,
        ),
    ]


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


def is_seeded() -> bool:
    """Report whether the demo state already exists.

    Keyed on the demo user rather than on "any users at all", so an evaluator
    who registers their own account first does not accidentally suppress the
    seed and end up with an empty demo.

    Returns:
        True if the `sam` account exists.
    """
    return store.get_user_by_username(DEMO_USER_USERNAME) is not None


def seed(now: datetime | None = None) -> None:
    """Create the demo state if it is not already present.

    Idempotent, and called unconditionally at startup. Two layers of
    idempotency, because both matter:
      * the account check below skips creation entirely on a warm database;
      * every store write it performs is an upsert anyway, so even a partially
        seeded database (a crash mid-seed) converges to the right state rather
        than raising.

    Args:
        now: Reference time for the tolerance event. Defaults to the current
            time; injected by tests so the computed window can be asserted.
    """
    now = now or datetime.now()
    store.init_db()

    # --- Accounts ---------------------------------------------------------
    # Passwords go through the same scrypt path as any registration. There is
    # deliberately no shortcut that writes a precomputed digest: a demo-only
    # credential path is how plaintext or weak hashes end up in a repo.
    sam = store.get_user_by_username(DEMO_USER_USERNAME)
    if sam is None:
        sam = auth.register(DEMO_USER_USERNAME, DEMO_PASSWORD, role="user")

    sarah = store.get_user_by_username(DEMO_CAREGIVER_USERNAME)
    if sarah is None:
        sarah = auth.register(DEMO_CAREGIVER_USERNAME, DEMO_PASSWORD, role="caregiver")

    # --- The consented caregiver relationship ------------------------------
    # PRD §8: a caregiver watches ONE named person who agreed to be watched.
    # Without this row Sarah signs in and the server has no idea whose ladder
    # she is entitled to see — which is precisely why /api/state used to resolve
    # her own (empty) account and the event stream used to broadcast everybody's
    # events to everybody and let the browser decide what to hide. Client-side
    # filtering is a rendering preference, not a privacy boundary. This row is
    # the boundary.
    #
    # SEEDED THROUGH A REAL INVITE, NOT A BARE LINK ROW. Sarah watches Sam
    # because Sam invited her: a code was generated on his onboarding page, read
    # to his sister, and redeemed by her on /register/caregiver. Seeding it that
    # way means the demo's starting state is one an ordinary account could have
    # reached, with no privileged path a real user does not also have — and the
    # `invites` row is there in the database as the evidence of consent.
    #
    # PRD P3: consent is structural here, not a policy claim. There is no API
    # parameter anywhere that lets a caregiver name the account they want to
    # watch; the permission only ever travels from the watched person outward.
    #
    # Guarded on `is_linked` rather than unconditionally, because `create_invite`
    # writes a fresh row each call and re-running seed() on a warm database must
    # not accumulate spent codes.
    if not store.is_linked(sarah.id, sam.id):
        invite = store.create_invite(sam.id, now=now)
        store.redeem_invite(invite.code, sarah.id, now=now)

    # --- Profile ----------------------------------------------------------
    # Upsert on every call. `put_profile` replaces contacts and tolerance
    # events wholesale, so this cannot duplicate Sarah into the tree twice.
    store.put_profile(sam.id, _demo_profile(now))

    # --- Vault ------------------------------------------------------------
    # Owned by Sam, not by Sarah. The clip is a recording Sarah MADE, but it is
    # a thing in Sam's vault, played to Sam, and it names him and his situation.
    # Ownership follows the listener because that is what /data-deletion
    # promises to erase: "Memory Vault recordings and transcripts linked to
    # your account". If Sam leaves, the recordings about Sam go with him.
    for clip in _vault_clips():
        store.put_vault_clip(clip, owner_user_id=sam.id)

    # --- Event log --------------------------------------------------------
    # Deliberately empty. Sam starts at Tier 0. Seeding an escalation history
    # would be inventing events that never happened, which contract ground
    # rule 1 forbids, and it would make the ladder UI a fiction on first load.


def reset(now: datetime | None = None) -> None:
    """Restore the seeded demo state from scratch. Backs POST /api/reset.

    Drops every table and re-seeds, rather than deleting rows. Two reasons:
      * the append-only triggers on `events` would abort a row-level delete, and
        working around them here would undermine the guarantee they exist to
        provide (PRD §11);
      * a full drop leaves no residue — no orphaned event, no account an
        evaluator created mid-demo — so "reset" means the same thing every
        time, which is what makes the demo re-runnable by a stranger.

    Note that this also removes any account an evaluator registered themselves.
    That is the intended meaning of a reset button, and the UI says so before
    it fires.

    Args:
        now: Reference time, forwarded to `seed`.
    """
    store.drop_all()
    seed(now)
