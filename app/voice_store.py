"""Persistence for consented supporter voice models.

WHAT THIS MODULE DOES
    Owns the `supporter_voices` table: one row per voice model a caregiver
    built from recordings of their own voice, in their own account, having
    ticked an explicit consent statement. It records the exact wording they
    agreed to, when they agreed, and whether they have separately chosen to
    share the result with the member they are linked to.

WHAT THIS MODULE DELIBERATELY DOES NOT DO
    * It performs NO authorization. It will happily return any row you ask for
      by id. Deciding who may read, share or delete a row is the route layer's
      job (`app/routes/voice.py`), and duplicating that judgement here would
      create two places where the consent gate could be got wrong.
    * It makes no network call. Creating and deleting the model UPSTREAM is
      `app/voice.py`'s job; this module only remembers that it exists. That
      split is why revocation deletes the provider-side model first and the row
      second — see the delete route.
    * It never touches Memory Vault clips. Those are real recordings of a real
      person saying a real thing and are never synthesized (PRD §7.2, and the
      module docstring of `app/voice.py`). Nothing in this file reads or writes
      `vault_clips`.

WHY A SEPARATE MODULE RATHER THAN A BLOCK IN app/store.py
    Two reasons, one architectural and one practical. Architecturally this is a
    self-contained, optional feature: the app boots, triages and escalates with
    this table absent, and keeping it out of the core schema makes that
    independence visible. Practically, `app/store.py` is the safety-critical
    persistence path and every addition to it is a chance to disturb the
    append-only event log; a feature that is off by default does not earn that
    risk. The table lives in the same SQLite file and reuses
    `store.connection()`, so there is still exactly one database.

WHY THERE IS NO FOREIGN KEY ON `caregiver_user_id`
    Deliberate, and not an oversight. `store.drop_all()` (behind POST
    /api/reset) drops `users` while `PRAGMA foreign_keys = ON`, which SQLite
    implements as an implicit DELETE — a child table holding referencing rows
    would abort the reset. Since this table is created outside that module's
    drop ordering, a hard reference would make an optional feature able to break
    the core demo reset. Referential integrity is instead maintained explicitly
    by `delete_voices_for_user()`, which the account-deletion path calls, and
    which is also the only place that can honour the harder half of the promise:
    removing the model from the provider, not just the row from our database.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app import store

# ---------------------------------------------------------------------------
# The consent statement
# ---------------------------------------------------------------------------
# THIS EXACT STRING IS STORED ON EVERY ROW, not a boolean and not a version
# number. An audit six months from now should be able to answer "what did this
# person actually agree to?" from the database alone, without archaeology
# through git history to find what the checkbox said on the day they ticked it.
# If this wording is ever changed, existing rows keep the wording their owner
# actually saw — which is the entire point of storing it rather than referencing
# it.
#
# Each clause maps to a mitigation named in app/voice.py's module docstring:
# the supporter consents in their own account; the copy is labelled every time
# it speaks; it never claims presence (PRD P5); and revocation deletes the model
# upstream rather than merely hiding it.
CONSENT_TEXT = (
    "I am the person speaking in these recordings. I am recording my own voice, "
    "in my own account, by my own choice, and I am not recording anyone else. "
    "I understand that Threshold will use these recordings to build a synthetic "
    "copy of my voice; that the copy is not me; that it will be labelled as an "
    "AI recreation every single time it speaks; that it will never be used to "
    "say I am present, listening, or on the line; and that I can delete it at "
    "any time, which removes the voice model itself and not just this app's "
    "link to it."
)

# The passages a supporter is asked to read. Written to be warm and ordinary.
#
# CONSTRAINTS ON THIS SCRIPT, and why each one is here:
#   * Nothing distressing to READ. A supporter is often someone's mother or
#     sponsor. Asking them to speak overdose language aloud into a microphone
#     would be a small cruelty in exchange for nothing.
#   * Nothing that sounds like a CRISIS LINE if it is ever replayed out of
#     context — no "are you still with me", no "stay awake", no "help is coming".
#     The recordings are ordinary speech about weather, toast and a dog.
#   * Nothing that CLAIMS PRESENCE. Not one line asserts the speaker is here,
#     watching, or on the line (PRD P5).
#   * Broad phonetic coverage, because a clone built on thin phonetics
#     mispronounces, and a voice people love sounding wrong is its own harm.
#     Between them these passages cover the English plosives, the fricatives
#     /f v θ ð s z ʃ h/, the affricates /tʃ dʒ/, nasals including /ŋ/, both
#     liquids, the glides, all the common diphthongs, and spoken digits.
# Three short passages rather than one long one: it lets someone re-record a
# single fluffed passage instead of the whole take, which is the difference
# between a two-minute task and an abandoned one.
SAMPLE_SCRIPT: tuple[str, ...] = (
    "Morning light comes through the kitchen window, and the kettle takes its "
    "time about it. This is the quiet part of the day, before the phone starts "
    "up. There is toast, there is coffee, and there is nothing much to decide "
    "yet.",
    "We walked the long way around the park on Thursday, past the blue bench in "
    "the shade, the six oak trees, and that generous, noisy little dog at number "
    "forty-one. The rain held off. It was about eleven degrees, which counts as "
    "a good March.",
    "Take your time with it. There is no rush on any of this, and whatever the "
    "answer turns out to be, it will keep until tomorrow.",
)

# Schema for this feature only. `IF NOT EXISTS` so `_ensure()` is safe to call
# on every operation, which is what lets the table appear without a migration
# step and without an edit to the boot sequence.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS supporter_voices (
    id                TEXT PRIMARY KEY,
    -- The account that recorded and consented. Set SERVER-SIDE from the
    -- session, never from the request body: a caregiver_user_id accepted from a
    -- client would let anyone attribute a voice model to anybody, which is the
    -- precise abuse this feature makes possible. See app/routes/voice.py.
    -- No REFERENCES clause: see the module docstring.
    caregiver_user_id TEXT NOT NULL,
    -- The provider-side model id. Deleting a row without deleting this upstream
    -- would make revocation a lie.
    voice_id          TEXT NOT NULL,
    display_name      TEXT NOT NULL,
    -- The exact wording consented to, stored verbatim. Not a flag. An audit
    -- shows WHAT was agreed, not merely THAT something was.
    consent_text      TEXT NOT NULL,
    consented_at      TEXT NOT NULL,   -- ISO-8601
    -- Recording and sharing are two separate decisions (see the route layer).
    -- DEFAULT 0: a freshly cloned voice is private to its owner until they act.
    shared            INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL    -- ISO-8601
);
CREATE INDEX IF NOT EXISTS idx_supporter_voices_caregiver
    ON supporter_voices(caregiver_user_id);
"""

# Database paths this process has already created the table in. Purely an
# optimisation — the statements are idempotent regardless — so that a hot path
# like /api/voice/speak does not pay a DDL round trip per request.
_ensured: set[str] = set()


def _ensure() -> None:
    """Create the table if this process has not yet done so for the current DB.

    Called at the top of every public function rather than from application
    startup. That keeps the feature entirely self-contained: no boot-order
    dependency to forget, and a test that points `store._db_path` at a temp file
    gets a working table without any extra fixture.
    """
    key = str(store.db_path())
    if key in _ensured:
        return
    with store.connection() as conn:
        conn.executescript(_SCHEMA)
    _ensured.add(key)


@dataclass(frozen=True)
class SupporterVoice:
    """One consented voice model.

    Frozen, and deliberately NOT a Pydantic model in `app/models.py`. The same
    reasoning as `store.UserRecord`: a plain dataclass has no `.model_dump()`,
    so it cannot be casually serialised straight onto the wire. Routes build
    their own explicit projections, which is what keeps `consent_text` and the
    provider-side `voice_id` from leaking into responses that do not need them.

    Attributes:
        id: Our opaque row id, and the id used in the DELETE route. Distinct
            from `voice_id` on purpose — a client that only ever learns our id
            cannot address the provider's model directly.
        caregiver_user_id: The account that recorded and consented.
        voice_id: The provider-side model id, used for synthesis and deletion.
        display_name: What the member sees, e.g. "Sarah's voice (AI)".
        consent_text: The exact statement agreed to, verbatim.
        consented_at: When they agreed.
        shared: Whether the caregiver has separately chosen to share it with
            their linked member. False until they act; recording is not sharing.
        created_at: When the model was built.
    """

    id: str
    caregiver_user_id: str
    voice_id: str
    display_name: str
    consent_text: str
    consented_at: datetime
    shared: bool
    created_at: datetime


def _row_to_voice(row) -> SupporterVoice:
    """Map a `supporter_voices` row to a `SupporterVoice`.

    Args:
        row: A row from `SELECT * FROM supporter_voices`.

    Returns:
        The equivalent `SupporterVoice`.
    """
    return SupporterVoice(
        id=row["id"],
        caregiver_user_id=row["caregiver_user_id"],
        voice_id=row["voice_id"],
        display_name=row["display_name"],
        consent_text=row["consent_text"],
        consented_at=datetime.fromisoformat(row["consented_at"]),
        shared=bool(row["shared"]),
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def create_supporter_voice(
    *,
    id: str,
    caregiver_user_id: str,
    voice_id: str,
    display_name: str,
    consent_text: str,
    consented_at: datetime | None = None,
    created_at: datetime | None = None,
) -> SupporterVoice:
    """Record a newly built voice model.

    Keyword-only: five same-typed string arguments in a row is exactly the shape
    where a positional swap silently files a voice under the wrong account.

    `shared` is not a parameter. A row is ALWAYS created unshared, because
    recording a voice and sharing it are two distinct decisions and collapsing
    them into one would mean the caregiver never made the second one. Sharing
    happens later, through `set_shared()`, from its own endpoint.

    Args:
        id: Our opaque row id.
        caregiver_user_id: The consenting account, resolved from the session by
            the caller. Never accepted from a request body.
        voice_id: The provider-side model id.
        display_name: Member-facing label.
        consent_text: The exact statement agreed to. Stored verbatim.
        consented_at: When they ticked it. Defaults to now.
        created_at: When the model was built. Defaults to now.

    Returns:
        The stored `SupporterVoice`, always with `shared=False`.
    """
    _ensure()
    when = consented_at or datetime.now()
    made = created_at or datetime.now()
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO supporter_voices"
            " (id, caregiver_user_id, voice_id, display_name, consent_text,"
            "  consented_at, shared, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
            (
                id,
                caregiver_user_id,
                voice_id,
                display_name,
                consent_text,
                when.isoformat(),
                made.isoformat(),
            ),
        )
    return SupporterVoice(
        id=id,
        caregiver_user_id=caregiver_user_id,
        voice_id=voice_id,
        display_name=display_name,
        consent_text=consent_text,
        consented_at=when,
        shared=False,
        created_at=made,
    )


def get_supporter_voice(voice_row_id: str) -> SupporterVoice | None:
    """Fetch one row by our id.

    Performs no authorization — the caller must check ownership or sharing.

    Args:
        voice_row_id: Our opaque row id.

    Returns:
        The `SupporterVoice`, or None if it does not exist (or has already been
        revoked, which is the same observable state and deliberately so).
    """
    _ensure()
    with store.connection() as conn:
        row = conn.execute(
            "SELECT * FROM supporter_voices WHERE id = ?", (voice_row_id,)
        ).fetchone()
    return _row_to_voice(row) if row else None


def list_for_caregiver(caregiver_user_id: str) -> list[SupporterVoice]:
    """Every voice this caregiver has recorded, oldest first.

    Args:
        caregiver_user_id: The owning account.

    Returns:
        Their own rows only. A caregiver sees the voices they made and nobody
        else's, including the consent text they agreed to, so the caregiver page
        can show them exactly what they signed up for rather than a reassurance.
    """
    _ensure()
    with store.connection() as conn:
        rows = conn.execute(
            "SELECT * FROM supporter_voices WHERE caregiver_user_id = ?"
            " ORDER BY created_at, id",
            (caregiver_user_id,),
        ).fetchall()
    return [_row_to_voice(r) for r in rows]


def list_shared_with_member(member_user_id: str) -> list[SupporterVoice]:
    """Voices this member is permitted to hear.

    TWO conditions, both required, and this is the join that enforces the
    consent chain end to end:
      1. `shared = 1` — the caregiver made the separate decision to share.
      2. a row in `caregiver_links` — the member consented to that caregiver
         watching them in the first place (the privacy boundary described in
         `app/store.py`).

    Revoking either one removes the voice from this list on the very next
    request. That is why the check is a live join rather than a copied flag: a
    deleted caregiver link takes the voice with it without any cleanup step
    needing to have run correctly.

    Args:
        member_user_id: The account that would do the listening.

    Returns:
        Shared voices from linked caregivers. Empty for a member with no links,
        which is the correct answer rather than an error.
    """
    _ensure()
    with store.connection() as conn:
        rows = conn.execute(
            "SELECT sv.* FROM supporter_voices AS sv"
            " JOIN caregiver_links AS cl"
            "   ON cl.caregiver_user_id = sv.caregiver_user_id"
            " WHERE cl.watched_user_id = ? AND sv.shared = 1"
            " ORDER BY sv.created_at, sv.id",
            (member_user_id,),
        ).fetchall()
    return [_row_to_voice(r) for r in rows]


def member_may_use(member_user_id: str, voice_id: str) -> bool:
    """Whether this member may be spoken to in this provider-side voice.

    The single predicate behind the synthesis gate, kept as one function so
    there is one place to audit and no route can invent its own weaker version —
    the same discipline `store.is_linked` applies to the caregiver boundary.

    Note it takes the PROVIDER voice id, not our row id: the question being
    asked at synthesis time is "may this caller cause this model to speak?",
    and the model is what the provider knows about.

    Args:
        member_user_id: The account requesting speech.
        voice_id: The provider-side model id they asked for.

    Returns:
        True only if a shared voice from a linked caregiver carries that id.
        Fails closed on everything else, including an unshared voice the member
        happens to know the id of.
    """
    return any(v.voice_id == voice_id for v in list_shared_with_member(member_user_id))


def get_supporter_voice_ids_all() -> set[str]:
    """Every provider-side voice id this app has ever cloned.

    Exists so the synthesis route can tell "a stock narrator id" apart from
    "someone else's clone that this caller has no right to". Without it, a
    member who obtained another supporter's voice id would be indistinguishable
    from a member picking a legitimate stock voice, and the request would go
    through — an impersonation of someone who never consented to speak to THIS
    person.

    Returns ids only, never rows: this is a membership test and the caller has
    no business seeing whose voice each id belongs to.

    Returns:
        The set of provider-side voice ids under our management.
    """
    _ensure()
    with store.connection() as conn:
        rows = conn.execute("SELECT voice_id FROM supporter_voices").fetchall()
    return {r["voice_id"] for r in rows}


def set_shared(voice_row_id: str, shared: bool) -> bool:
    """Turn sharing on or off for one voice.

    The caregiver's second decision, and the one they can take back without
    destroying the model. Un-sharing is deliberately NOT the same operation as
    deletion: a supporter may want to stop the member hearing it today without
    throwing away a recording session, and forcing those to be the same action
    would push people toward keeping something shared that they would rather
    pause.

    Args:
        voice_row_id: Our opaque row id.
        shared: Desired state.

    Returns:
        True if a row changed, False if there was no such row.
    """
    _ensure()
    with store.connection() as conn:
        cursor = conn.execute(
            "UPDATE supporter_voices SET shared = ? WHERE id = ?",
            (int(shared), voice_row_id),
        )
    return cursor.rowcount > 0


def delete_supporter_voice(voice_row_id: str) -> bool:
    """Remove the local row.

    HALF OF REVOCATION, and the less important half. The caller must delete the
    provider-side model via `voice.delete_voice()` as well; a row removed while
    the model lives on upstream would mean this app had merely stopped looking
    at something it promised to destroy. The route does the upstream call first
    and this second, so a failed upstream delete leaves a row we can retry from
    rather than an orphan nobody can find.

    Args:
        voice_row_id: Our opaque row id.

    Returns:
        True if a row was removed, False if there was none — so a
        double-submitted revoke button is a no-op rather than a 500.
    """
    _ensure()
    with store.connection() as conn:
        cursor = conn.execute(
            "DELETE FROM supporter_voices WHERE id = ?", (voice_row_id,)
        )
    return cursor.rowcount > 0


def voices_for_user(user_id: str) -> list[SupporterVoice]:
    """Every voice that must die with this account or this relationship.

    Backs two cleanup paths named as hard constraints in the brief:
      * deleting an account deletes the voice models it owns;
      * deleting a caregiver link deletes the voice shared through it.

    Only the OWNER's rows are returned, never rows merely shared WITH this
    user. A member closing their account must not destroy their supporter's
    recording of their own voice — that is the supporter's property and their
    decision, and erasing it on someone else's behalf would be its own consent
    failure. The member's link disappears with their account, so the voice stops
    being reachable by them regardless.

    Args:
        user_id: The account being deleted.

    Returns:
        The rows they own, so the caller can delete each one upstream before
        removing it locally.
    """
    return list_for_caregiver(user_id)
