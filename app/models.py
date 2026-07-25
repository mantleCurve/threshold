"""Shared schemas. Every module and every subagent codes against this file.

Nothing here imports from elsewhere in the app, so it can never create a cycle.
"""

from __future__ import annotations

from datetime import datetime
from enum import IntEnum
from typing import Literal

from pydantic import BaseModel, Field


class Tier(IntEnum):
    """The escalation ladder. Ordering is meaningful: higher is more dangerous."""

    BASELINE = 0
    ELEVATED = 1
    CRAVING = 2
    ACTIVE_USE = 3
    EMERGENCY = 4
    UNRESPONSIVE = 5


TIER_NAMES: dict[Tier, str] = {
    Tier.BASELINE: "Baseline",
    Tier.ELEVATED: "Elevated",
    Tier.CRAVING: "Craving",
    Tier.ACTIVE_USE: "Active use",
    Tier.EMERGENCY: "Medical emergency",
    Tier.UNRESPONSIVE: "Unresponsive",
}

# Tiers the user may hide from caregivers. 4 and 5 are non-negotiable (PRD §4.2).
USER_CONTROLLABLE_TIERS: frozenset[Tier] = frozenset(
    {Tier.BASELINE, Tier.ELEVATED, Tier.CRAVING, Tier.ACTIVE_USE}
)


class Action(BaseModel):
    """Something the system does in response to a tier. Emitted by triage, never by a model."""

    kind: Literal[
        "speak",
        "play_vault_clip",
        "offer_contact",
        "fire_contact_tree",
        "show_emergency_script",
        "show_good_samaritan",
        "naloxone_prompt",
        # Surfaced when the trigger was a suicidal statement rather than a
        # physical emergency. India-wide Tele-MANAS support is offered alongside
        # 112 rather than asking a language model to act as a crisis counsellor.
        "show_crisis_line",
        "bystander_hail",
        "arm_bystander_mode",
        "rescue_breathing",
        "start_grounding",
        "acquire_location",
        "keep_awake",
    ]
    detail: str = ""
    # Seconds after tier entry that this action fires. 0 = immediately, in parallel.
    at_second: int = 0


class TriageResult(BaseModel):
    """Return value of the deterministic triage engine."""

    tier: Tier
    previous_tier: Tier
    reason: str  # Human-readable, shown in the UI. Auditable, never model-written.
    matched_signal: str | None = None
    actions: list[Action] = Field(default_factory=list)
    notify_caregiver: bool = False


class Contact(BaseModel):
    name: str
    relation: str
    channel: str
    destination: str = ""
    order: int
    tiers: list[Tier]  # tiers at which this contact is reached


class ToleranceEvent(BaseModel):
    kind: Literal["detox", "hospital_discharge", "incarceration_release", "abstinence"]
    date: datetime
    note: str = ""


class VaultClip(BaseModel):
    id: str
    recorded_by: str
    relation: str
    transcript: str  # what the caregiver said; used for AI selection and captioning
    tags: list[str] = Field(default_factory=list)
    audio_path: str | None = None


class LadderConfig(BaseModel):
    """User-owned. PRD P3: the user sees and tunes this."""

    tier_3_visible_to_caregiver: bool = False
    tier_2_visible_to_caregiver: bool = False
    # Bounded at the MODEL, not only at the HTTP boundary.
    #
    # These were bare ints. silence_seconds_to_escalate=0 makes every Tier 3
    # evaluation escalate instantly to Tier 4 — a config value that inverts the
    # safety behaviour of the whole ladder. missed_checkins_to_elevate=0 pins
    # everyone at Tier 1 permanently. The HTTP route clamped both, but the store
    # did not, so any row written by seed, migration, or a future writer was
    # trusted. models.py is documented as the single source of truth, so the
    # constraint belongs here where every path gets it.
    missed_checkins_to_elevate: int = Field(default=2, ge=1, le=10)
    silence_seconds_to_escalate: int = Field(default=20, ge=5, le=300)


class UserProfile(BaseModel):
    id: str
    name: str
    address: str
    unit: str = ""
    entry_code: str = ""
    cross_street: str = ""
    state_code: str = "KA"  # Indian state code for regional emergency guidance
    substances: list[str] = Field(default_factory=list)
    naloxone_on_hand: bool = False
    ladder: LadderConfig = Field(default_factory=LadderConfig)
    contacts: list[Contact] = Field(default_factory=list)
    tolerance_events: list[ToleranceEvent] = Field(default_factory=list)


class Event(BaseModel):
    """Append-only log. PRD §11: every event is visible to the user. No hidden log."""

    id: str
    user_id: str
    at: datetime
    tier: Tier
    trigger_source: str
    reason: str
    actions_planned: list[str] = Field(default_factory=list)
    actions_taken: list[str] = Field(default_factory=list)
    user_visible: Literal[True] = True


class Generation(BaseModel):
    """Wrapper for anything the model produced.

    `live` distinguishes a real API call from a cached fallback. The UI renders
    this difference visibly — a fallback must never pass as a fresh generation.
    """

    text: str
    live: bool
    model: str
    latency_ms: int
    error: str | None = None
