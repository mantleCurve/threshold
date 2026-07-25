"""Bounded request bodies for every public HTTP endpoint.

What this module does:
    Declares one Pydantic model per mutating endpoint so FastAPI validates and coerces
    the body *before* a handler runs. Every field carries an explicit bound — a maximum
    length, a numeric range, or a closed set of literals.

Why it exists (c_s.md, Code Quality P1 #1 and #6):
    `app/main.py` historically accepted raw `dict` bodies and hand-cast values. Two
    concrete defects came out of that:

      - `int(body.get("silent_seconds", 0))` raises `ValueError` on `"abc"` and on
        `None`, which FastAPI turns into a 500. A malformed request from a stranger's
        browser should be a clean, self-describing 422, never a server error on a
        product whose whole premise is that it stays up.
      - `bool(body.get("still"))` accepts the *string* `"false"` and evaluates it True,
        because every non-empty string is truthy in Python. That silently inverts a
        signal on the escalation path.

    Both classes of bug disappear when the body is a typed model: Pydantic parses
    `"20"` into an int, rejects `"abc"` with a field-level message, and refuses
    `"false"` for a bool unless it is a recognised boolean spelling.

    Bounding is also a security and cost control, not just tidiness. Utterance text
    reaches a paid model and the on-disk fallback cache; usernames and contact messages
    reach the log and the filesystem. An unbounded string on any of those paths is an
    unbounded bill, an unbounded log line, or a full disk.

What this module deliberately does NOT do:
    - It does not perform business validation. "Is this username taken", "is this the
      right password", "does this user own this profile" are decisions for
      `app/auth.py` and the route layer, which have the database and the session. This
      module only answers "is this body structurally plausible and within bounds".
    - It does not decide a tier, and it does not import `app.triage` or `app.genai`.
      It imports only `app.models`, which is the single source of truth for shared
      types (CONTRACT.md) and itself imports nothing from the app.
    - It does not define *response* models. Responses are already typed by
      `app/models.py`; duplicating them here would create two sources of truth.

Error contract:
    A body that fails any constraint here produces FastAPI's standard 422 with a
    per-field `loc`/`msg`/`type`. Routes keep raising 400/401/409 for the semantic
    failures they alone can detect. That split is deliberate and is what "consistent
    422/400 errors" in the review means: 422 = shape, 4xx = meaning.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool

from app.models import Tier

# --------------------------------------------------------------------------------------
# Shared bounds
#
# Named rather than inlined so a reviewer can see every public input ceiling in one
# screen, and so a change to a limit cannot be applied to one endpoint and forgotten on
# another. Values are chosen against real use, not round numbers for their own sake.
# --------------------------------------------------------------------------------------

# Long enough for a genuinely rambling spoken check-in transcribed by the browser's
# speech API, short enough that a single utterance cannot dominate a model context
# window or a cache entry. Roughly a spoken minute and a half.
MAX_UTTERANCE_CHARS = 2000

# Usernames are display identifiers here, not email addresses.
MIN_USERNAME_CHARS = 2
MAX_USERNAME_CHARS = 64

# The floor matches the existing registration rule so behaviour does not change. The
# ceiling exists because password verification is a deliberately expensive scrypt hash:
# an unbounded password is a free CPU-exhaustion primitive against an endpoint that
# must stay responsive (c_s.md Efficiency #1 names the same hazard).
MIN_PASSWORD_CHARS = 6
MAX_PASSWORD_CHARS = 256

# Free-text context passed to `GET /api/vault/select`. Bounded because it is
# attacker-controlled, unauthenticated-reachable, and lands in a prompt.
MAX_CONTEXT_CHARS = 500

# Profile free-text fields (address, unit, entry code, cross street). Matches the
# `[:200]` truncation main.py already applied by hand — stated once, here, instead.
MAX_PROFILE_FIELD_CHARS = 200

# Contact form. Mirrors the limits the route already enforced, so the only change is
# that a violation is now a structured 422 instead of a hand-rolled 413.
MAX_CONTACT_NAME_CHARS = 200
MAX_CONTACT_EMAIL_CHARS = 320  # RFC 5321 maximum path length
MAX_CONTACT_TOPIC_CHARS = 64
MAX_CONTACT_MESSAGE_CHARS = 5000

# Sensor bounds. A silence longer than an hour is not a live signal, it is a stale
# client or a forged request; either way the ladder has nothing sensible to do with it.
MAX_SILENT_SECONDS = 3600

# Ladder tuning bounds. Also enforced in `app/triage.py`; repeated here so a bad value
# is rejected at the edge rather than clamped silently in the middle of the system.
MIN_SILENCE_ESCALATE_SECONDS = 5
MAX_SILENCE_ESCALATE_SECONDS = 300
MIN_MISSED_CHECKINS = 1
MAX_MISSED_CHECKINS = 10


class _StrictBody(BaseModel):
    """Base for every request model: unknown keys are rejected, not ignored.

    `extra="forbid"` is a deliberate choice with a real cost — a frontend that sends a
    stray field gets a 422 instead of being quietly tolerated. It is worth it here
    because the alternative failure is worse and silent: a client that sends
    `{"silentSeconds": 30}` against a server reading `silent_seconds` would otherwise
    escalate on a default of 0 and nobody would ever see an error. Loud is correct on
    the escalation path.

    `str_strip_whitespace` removes the `(body.get("x") or "").strip()` dance from every
    handler, and makes a whitespace-only value fail a `min_length` check rather than
    passing validation and failing later as a mystery empty string.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# --------------------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------------------


class RegisterRequest(_StrictBody):
    """Body of `POST /api/auth/register`.

    The length rules that `main.py` checked by hand (`len(username) < 2 or
    len(password) < 6`) are now declarative, so they appear in the OpenAPI schema and
    the frontend can read them instead of restating them.

    Role is a `Literal` union rather than a free string: an unknown role previously
    produced a hand-written 400, and encoding it in the type means the set of roles
    cannot drift between this file, the seed, and the auth module.
    """

    email: str = Field(
        min_length=3,
        max_length=320,
        pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
    )
    full_name: str = Field(
        min_length=2,
        max_length=100,
        description="The person's name; no separate username is required.",
    )
    phone: str = Field(
        min_length=7,
        max_length=24,
        pattern=r"^\+?[0-9().\-\s]{7,24}$",
        description="Contact number only. Threshold does not phone-verify it.",
    )
    password: str = Field(
        min_length=MIN_PASSWORD_CHARS,
        max_length=MAX_PASSWORD_CHARS,
        description="Bounded above to cap scrypt cost per request.",
    )
    role: str = Field(
        default="user",
        pattern="^(user|caregiver)$",
        description="Closed set. Anything else is a 422, not a silently-defaulted user.",
    )
    invite_code: str = Field(default="", max_length=32)


class VerifyRegistrationRequest(_StrictBody):
    """Body of the second registration step."""

    email: str = Field(
        min_length=3,
        max_length=320,
        pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
    )
    code: str = Field(pattern=r"^\d{6}$")


class LoginRequest(_StrictBody):
    """Body of `POST /api/auth/login`.

    Deliberately looser than `RegisterRequest` on the lower bound: a `min_length` of 6
    on the password here would let an attacker distinguish "too short to be a real
    password" (422) from "wrong password" (401), which is a small but free oracle. Only
    the upper bounds are enforced, because those exist to protect the server, not to
    grade the credential. The 401 is identical for every wrong input, as before.
    """

    username: str = Field(min_length=1, max_length=MAX_USERNAME_CHARS)
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_CHARS)


# --------------------------------------------------------------------------------------
# Triage inputs
# --------------------------------------------------------------------------------------


class UtteranceRequest(_StrictBody):
    """Body of `POST /api/utterance` — the product's primary input.

    `min_length=1` combined with the base class's whitespace stripping replaces the
    handler's `if not text: raise HTTPException(400, "empty utterance")`. A body of
    `{"text": "   "}` now fails validation instead of reaching triage.
    """

    text: str = Field(
        min_length=1,
        max_length=MAX_UTTERANCE_CHARS,
        description="Transcribed or typed check-in. Bounded: this text reaches a paid "
        "model and is hashed into the on-disk fallback cache.",
    )


class SensorRequest(_StrictBody):
    """Body of `POST /api/sensor` — silence and stillness.

    This is the model that fixes both defects named in the review. `silent_seconds` is
    a bounded `int`, so `"abc"` is a 422 rather than a `ValueError` inside the handler,
    and `still` is a strict JSON boolean, so the string `"false"` is rejected outright
    instead of being coerced to True by Python truthiness.

    `ge=0` matters on the escalation path: a negative duration is not a shorter silence,
    it is a nonsense value, and clamping it silently would hide a broken client.
    """

    silent_seconds: int = Field(
        default=0,
        ge=0,
        le=MAX_SILENT_SECONDS,
        description="Seconds of continuous silence observed by the client.",
    )
    still: StrictBool = Field(
        default=False,
        description="Whether the device has also been motionless. Strict bool — the "
        "string \"false\" is rejected, not silently treated as True.",
    )


class TierRequest(_StrictBody):
    """Body of `POST /api/tier` — explicit tier set by a signed-in member.

    Typed as `Tier`, so an out-of-range integer is a 422 naming the valid values rather
    than the handler's hand-rolled `Tier(int(...))` / `except (ValueError, TypeError)`
    pair. Note this endpoint does not perform triage: it records an honest human
    override, and the model still never decides a tier (CONTRACT.md / PRD P4).
    """

    tier: Tier = Field(description="Target tier, 0-5. Values outside the ladder are 422.")


class ActionReceiptRequest(_StrictBody):
    """A client-confirmed action that actually completed.

    The closed set prevents a caller from writing arbitrary prose into the
    immutable audit log. These are execution facts, not triage plans.
    """

    action: Literal[
        "caregiver_screen_notified",
        "location_displayed",
        "bystander_hail_started",
        "wake_lock_acquired",
        "911_script_displayed",
        "vault_clip_played",
        "grounding_started",
        "rescue_breathing_started",
        "naloxone_prompt_displayed",
        "good_samaritan_displayed",
    ]
    detail: str = Field(default="", max_length=160)


# --------------------------------------------------------------------------------------
# Profile
# --------------------------------------------------------------------------------------


class LadderUpdate(_StrictBody):
    """The user-tunable part of `LadderConfig`, as sent by onboarding.

    Every field is optional so the form can send a partial update, and the route applies
    only what is present. This mirrors the existing whitelist behaviour but makes the
    whitelist a *type* instead of a loop over field-name strings.

    Tier 4 and Tier 5 caregiver visibility are deliberately absent. They are the one
    thing the user cannot switch off — disclosed at onboarding and stated in the Terms
    (PRD §4.2, `USER_CONTROLLABLE_TIERS` in app/models.py). Omitting them from the
    schema means a crafted request cannot even express the change, which is a stronger
    guarantee than a handler remembering not to read the field.
    """

    tier_2_visible_to_caregiver: bool | None = None
    tier_3_visible_to_caregiver: bool | None = None
    silence_seconds_to_escalate: int | None = Field(
        default=None,
        ge=MIN_SILENCE_ESCALATE_SECONDS,
        le=MAX_SILENCE_ESCALATE_SECONDS,
        description="Bounded so the user can tune the silence window but cannot "
        "effectively disable it by setting a value that never fires.",
    )
    missed_checkins_to_elevate: int | None = Field(
        default=None,
        ge=MIN_MISSED_CHECKINS,
        le=MAX_MISSED_CHECKINS,
    )


class ContactUpdate(_StrictBody):
    """One ordered caregiver contact submitted during onboarding."""

    name: str = Field(min_length=1, max_length=100)
    relation: str = Field(default="", max_length=100)
    channel: Literal["phone", "sms", "email"] = "phone"
    destination: str = Field(default="", max_length=320)
    tiers: list[Tier] = Field(default_factory=list, max_length=6)


class ProfileUpdateRequest(_StrictBody):
    """Body of `POST /api/profile` — onboarding and ladder settings.

    Every field is optional: onboarding saves in stages and a partial body is normal.
    `None` means "not sent, leave it alone", which is distinguishable from `""` meaning
    "the user cleared this field" — a distinction the previous `if field in body` check
    could make only by inspecting the raw dict.

    The address block is bounded at 200 characters each, replacing main.py's silent
    `[:200]` truncation. Truncating an address is worse than rejecting it: a half-
    written address is what gets read aloud to a 911 dispatcher.
    """

    address: str | None = Field(default=None, max_length=MAX_PROFILE_FIELD_CHARS)
    unit: str | None = Field(default=None, max_length=MAX_PROFILE_FIELD_CHARS)
    entry_code: str | None = Field(default=None, max_length=MAX_PROFILE_FIELD_CHARS)
    cross_street: str | None = Field(default=None, max_length=MAX_PROFILE_FIELD_CHARS)
    state_code: str | None = Field(
        default=None,
        pattern="^[A-Za-z]{2}$",
        description="Two-letter code driving the Good Samaritan lookup. Constrained "
        "because a malformed value here silently yields the 'no reviewed summary "
        "for this state' response, which reads as a data gap rather than a typo.",
    )
    naloxone_on_hand: bool | None = None
    ladder: LadderUpdate | None = None
    contacts: list[ContactUpdate] | None = Field(default=None, max_length=10)


# --------------------------------------------------------------------------------------
# Public (unauthenticated) surfaces
# --------------------------------------------------------------------------------------


class ContactRequest(_StrictBody):
    """Body of `POST /api/contact` — the public contact form.

    Unauthenticated and world-reachable, so the bounds here are load-bearing rather
    than cosmetic: the handler appends each submission to a JSONL file on disk. The
    previous hand-rolled 413 became a 422 with a field name, which is both more
    accurate (the body is well-formed but out of range) and more useful to a client.
    """

    name: str = Field(min_length=1, max_length=MAX_CONTACT_NAME_CHARS)
    email: str = Field(min_length=3, max_length=MAX_CONTACT_EMAIL_CHARS)
    message: str = Field(min_length=1, max_length=MAX_CONTACT_MESSAGE_CHARS)
    topic: str = Field(default="general", max_length=MAX_CONTACT_TOPIC_CHARS)


class VaultSelectQuery(BaseModel):
    """Query parameters for `GET /api/vault/select`.

    A query model rather than a body model, so it does not inherit `_StrictBody` —
    `extra="forbid"` on a query string would reject harmless tracking parameters and
    break ordinary links. The bound is what matters: `context` is unauthenticated,
    attacker-controlled, and is interpolated into a prompt sent to a paid model
    (c_s.md Code Quality #6 names context query strings explicitly).
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    context: str = Field(
        default="",
        max_length=MAX_CONTEXT_CHARS,
        description="Plain description of the current moment. Never a tier name.",
    )
