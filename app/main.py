"""FastAPI application: the seam where triage, persistence, and the generative layer meet.

WHAT THIS MODULE DOES
    Exposes the HTTP API defined in CONTRACT.md, serves the static frontend, holds the
    per-user in-memory ladder state, and streams ladder changes to connected clients
    (notably the caregiver surface) over server-sent events.

WHAT THIS MODULE DELIBERATELY DOES NOT DO
    - It does not decide tiers. Every tier transition comes from `app.triage`, which is
      a pure deterministic state machine (PRD P4). This module transcribes those
      decisions; it never second-guesses them.
    - It does not generate safety-critical text itself. All generation is delegated to
      `app.genai`, and legal text is never generated at all (PRD §6.5).
    - It holds no business rules. If you find yourself writing a clinical decision in
      this file, it belongs in triage.py instead.

IMPORT DISCIPLINE
    The generative layer is imported lazily inside handlers rather than at module
    scope. That is deliberate: this app must boot and serve every non-AI surface even
    if the AI layer is missing, broken, or has no API key. A crashed import at startup
    would take down the emergency path along with it, which is exactly backwards.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

from app import deps, email as email_delivery, legal, registration, store, triage, voice
from app.security import SecurityMiddleware
from app.models import Contact, Event, Tier, TriageResult, UserProfile
from app.schemas import (
    ActionReceiptRequest,
    ContactRequest,
    InviteCreateRequest,
    InviteResendRequest,
    LoginRequest,
    ProfileUpdateRequest,
    RegisterRequest,
    SensorRequest,
    TierRequest,
    UtteranceRequest,
    VerifyRegistrationRequest,
)

# The authorization primitives live in `app.deps` so that this module, the routers
# under `app.routes`, and any future surface all make the SAME privacy decision.
# A boundary that is reimplemented per file is not a boundary; it is a set of
# opinions that will disagree under maintenance.
from app.deps import (
    Listener,
    _broadcast,          # the ONE broadcaster — see the note below
    _current_tier,
    _generate,
    _now,
    _record,
    _require_own_profile,
    _result_payload,
    _session_user,
    authenticated_user_id,
    caregiver_event_projection,
    caregiver_profile_projection,
    events_for_wire,
    resolve_subject,
    visible_to,
)

log = logging.getLogger("threshold")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"


# ---------------------------------------------------------------------------
# Ladder state
# ---------------------------------------------------------------------------
# The current tier is intentionally in-memory rather than persisted. The ladder
# describes a *live* situation ("right now, is this person in danger?"), not a
# durable fact about them. Persisting it would mean a server restart could leave a
# user pinned at Tier 4 forever, or — worse — silently resurrect a stale emergency.
# The append-only Event log in SQLite is the durable record; this is the live cursor.
#
# BOUND TO `app.deps`, NOT REDECLARED. Both objects are aliases of the ones in
# that module, so this file and every router under `app.routes` read and write
# ONE dict and ONE listener list. Declaring fresh containers here (which is what
# this file used to do) gave the app a split brain: a tier set through a route in
# `app.routes` was invisible to a stream served from here, and — far worse — a
# listener registered on one list could never be filtered by a broadcast walking
# the other. Deletion clearing "the" live state would clear only one of two.
_tiers = deps._tiers
_listeners = deps._listeners
_delivery_tasks: set[asyncio.Task] = set()


def _now_naive() -> datetime:
    """Naive local 'now', for the invite subsystem only.

    Deliberately NOT `_now()`. Invite timestamps are stored by `app/store.py`
    with a naive `datetime.now()` default, and Python refuses to compare a naive
    datetime with an aware one — so passing UTC-aware time into an expiry check
    would raise TypeError the first time a code was redeemed, rather than
    failing a test. One clock per subsystem, matched at the boundary.

    Kept separate rather than converting the store to aware time, because the
    store's timestamps are already written naive throughout (events, consent)
    and changing that is a migration, not a fix for this feature.
    """
    return datetime.now()


# `_broadcast` is deliberately NOT defined here.
#
# It used to be, and that duplicate was a live privacy leak: this module's copy
# called visible_to() WITHOUT live=True, while deps.py passed it. Every real SSE
# push runs through whatever this module binds, so the deps.py fix — Tier 2
# cravings are never pushed to a caregiver — never reached production, and the
# test guarding it passed because it exercised the deps.py function that nothing
# called. A green suite over an open leak.
#
# There is now exactly one broadcaster, imported from deps below. If you find
# yourself about to redefine it here, that is the bug.

async def _deliver_emergency_alerts(user_id: str, event: Event) -> None:
    """Email verified linked caregivers and member-entered emergency contacts."""
    from app.models import TIER_NAMES

    if event.tier < Tier.EMERGENCY:
        return
    profile, member = await asyncio.gather(
        asyncio.to_thread(store.get_profile, user_id),
        asyncio.to_thread(store.get_user, user_id),
    )
    member_name = (
        profile.name if profile else (member.full_name if member else "A member")
    )
    delivered_to: set[str] = set()
    caregiver_ids = await asyncio.to_thread(store.caregivers_for, user_id)
    for caregiver_id in caregiver_ids:
        caregiver = await asyncio.to_thread(store.get_user, caregiver_id)
        if not caregiver or not caregiver.email or not caregiver.email_verified:
            continue
        delivered, _error = await email_delivery.send_caregiver_alert(
            caregiver.email,
            member_name,
            tier_name=TIER_NAMES[event.tier],
            idempotency_key=f"threshold-alert/{event.id}/{caregiver.id}",
        )
        if delivered:
            delivered_to.add(caregiver.email.lower())
            await asyncio.to_thread(
                store.append_event,
                Event(
                    id=str(uuid.uuid4()),
                    user_id=user_id,
                    at=_now(),
                    tier=event.tier,
                    trigger_source="delivery_receipt",
                    reason="A linked caregiver emergency email was delivered.",
                    actions_planned=[],
                    actions_taken=["caregiver_email_delivered"],
                ),
            )

    # A member can add an emergency email before the recipient has accepted a
    # caregiver account invitation. These addresses receive the minimum alert
    # only at tiers explicitly selected by the member. Linked caregivers above
    # are deduplicated so one person never receives two alerts for one event.
    for index, contact in enumerate(profile.contacts if profile else [], start=1):
        destination = contact.destination.strip().lower()
        if (
            contact.channel != "email"
            or not destination
            or event.tier not in contact.tiers
            or destination in delivered_to
        ):
            continue
        delivered, _error = await email_delivery.send_caregiver_alert(
            destination,
            member_name,
            tier_name=TIER_NAMES[event.tier],
            idempotency_key=f"threshold-alert/{event.id}/contact-{index}",
        )
        if delivered:
            delivered_to.add(destination)
            await asyncio.to_thread(
                store.append_event,
                Event(
                    id=str(uuid.uuid4()),
                    user_id=user_id,
                    at=_now(),
                    tier=event.tier,
                    trigger_source="delivery_receipt",
                    reason=f"Emergency email delivered to {contact.name}.",
                    actions_planned=[],
                    actions_taken=["emergency_contact_email_delivered"],
                ),
            )


def _schedule_emergency_alerts(user_id: str, event: Event) -> None:
    """Retain background delivery tasks until completion."""
    if event.tier < Tier.EMERGENCY:
        return
    task = asyncio.create_task(_deliver_emergency_alerts(user_id, event))
    _delivery_tasks.add(task)
    task.add_done_callback(_delivery_tasks.discard)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialise production services before serving requests."""
    store.init_db()
    legal.load()
    await voice.startup()
    try:
        yield
    finally:
        await email_delivery.close()
        await voice.close()


app = FastAPI(
    title="Threshold",
    description="Recovery and overdose-prevention platform. Deterministic triage, generative language.",
    lifespan=lifespan,
)

# Security headers and abuse-resistance. Mounted first so it wraps every route,
# including the static mount. HTTPS-only headers are enabled by environment flag
# so a local http:// dev server does not pin the browser to a scheme it cannot
# answer on. See app/security.py for why the emergency path is never throttled.
app.add_middleware(
    SecurityMiddleware,
    https=os.getenv("THRESHOLD_HTTPS", "").lower() in ("1", "true", "yes"),
)

# The consented supporter-voice surface. Included as a router rather than
# inlined here because it is an optional, self-contained feature with its own
# consent gate, and a reader auditing that gate should find the whole of it in
# one file (app/routes/voice.py) rather than interleaved with the ladder core.
from app.routes import voice as voice_routes  # noqa: E402

app.include_router(voice_routes.router)


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
# These routes back production login and email-verified registration.
@app.post("/api/auth/register")
async def auth_register(body: RegisterRequest) -> JSONResponse:
    """Start a two-step signup and email a short-lived verification code."""
    try:
        pending = await registration.begin(
            email=body.email,
            full_name=body.full_name,
            phone=body.phone,
            password=body.password,
            role=body.role,
            invite_code=body.invite_code,
        )
    except registration.RegistrationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(
        {
            "ok": True,
            "verification_required": True,
            "email": pending.email,
            "expires_in_seconds": int(registration.CODE_TTL.total_seconds()),
        },
        status_code=202,
    )


@app.post("/api/auth/register/verify")
async def auth_register_verify(body: VerifyRegistrationRequest) -> JSONResponse:
    """Verify the emailed code, create the account, and start its session."""
    from app import auth

    try:
        completed = registration.complete(body.email, body.code)
    except registration.RegistrationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    payload: dict[str, object] = {
        "ok": True,
        "full_name": completed.user.full_name,
        "role": completed.user.role,
    }
    if completed.user.role == "caregiver":
        payload["linked"] = completed.link_error is None
        payload["watching"] = completed.watching
        if completed.link_error:
            payload["link_error"] = completed.link_error
    response = JSONResponse(payload)
    auth.set_session_cookie(response, completed.user.id)
    return response


# ---------------------------------------------------------------------------
# Invite codes — consent as structure, not as policy (PRD P3)
# ---------------------------------------------------------------------------
# These two endpoints are the ONLY way a caregiver link is created outside the
# seeded demo fixture. Note what is missing from both of them: nowhere can a
# caregiver name the account they would like to watch. The member generates a
# code on their own screen and hands it over; the caregiver can only present a
# code they were given.
#
# That asymmetry is the answer to "isn't this surveillance?". It is not a
# promise in a privacy policy that we ask permission first — it is an API in
# which the unconsented case cannot be expressed. Nobody can attach themselves
# to a person who did not invite them, because there is no parameter for it.
@app.post("/api/invite")
async def post_invite(
    request: Request,
    body: InviteCreateRequest | None = Body(default=None),
) -> dict:
    """Generate a single-use, 24-hour invite code. Called by the member.

    A REAL SESSION IS REQUIRED — `authenticated_user_id`, which has no demo
    fallback, rather than `_session_user`, which resolves an anonymous caller to
    the published demo fixture. That fallback exists so an evaluator poking at
    read endpoints sees a working product, and it must not reach this one: a code
    is a live permission to watch whoever issued it, so letting a stranger mint
    one against Sam's account would make the consent story a fiction on the
    single endpoint where it has to be literally true.

    Returns:
        `{code, expires_at, expires_in_hours}`. The code is shown once on the
        member's own screen; we do not email or message it anywhere, because
        the handover is the consent act and it belongs to the member.

    Raises:
        HTTPException 401: No session. Anonymous callers cannot issue permissions.
        HTTPException 403: A caregiver account calling this. Caregivers do not
            issue invitations to be watched; only members do.
    """
    user_id = authenticated_user_id(request)
    if user_id is None:
        raise HTTPException(
            status_code=401,
            detail="Sign in to create an invite code. Credentials are on the login page.",
        )

    # Role check, not just an auth check. A caregiver issuing an invite would
    # invert the direction of consent — someone redeeming it would end up
    # watching the caregiver, which is not a relationship this product models.
    user = await asyncio.to_thread(store.get_user, user_id)
    if user and user.role != "user":
        raise HTTPException(
            status_code=403,
            detail="Only the person being supported can create an invite code.",
        )

    email = (body.email if body else "").strip().lower()
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise HTTPException(status_code=422, detail="Enter a valid caregiver email.")

    invite = await asyncio.to_thread(
        store.create_invite,
        user_id,
        now=_now_naive(),
        invited_email=email,
    )
    delivered, delivery_error = False, None
    if email:
        delivered, delivery_error = await email_delivery.send_caregiver_invitation(
            email,
            user.full_name or "Someone you care about",
            invite.code,
            idempotency_key=f"invite-{invite.code}",
        )
    return {
        "code": invite.code,
        "email": email,
        "email_sent": delivered,
        "email_error": delivery_error,
        "expires_at": invite.expires_at.isoformat(),
        "expires_in_hours": store.INVITE_TTL_HOURS,
    }


@app.get("/api/invites")
async def get_invites(request: Request) -> dict:
    """Show a member every issued invitation and every accepted caregiver."""
    user_id = authenticated_user_id(request)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Sign in to view invitations.")
    user = await asyncio.to_thread(store.get_user, user_id)
    if not user or user.role != "user":
        raise HTTPException(status_code=403, detail="Only members have invitations.")

    now = _now_naive()
    invites = await asyncio.to_thread(store.list_invites, user_id)
    caregiver_ids = await asyncio.to_thread(store.caregivers_for, user_id)
    caregivers = await asyncio.gather(
        *(asyncio.to_thread(store.get_user, caregiver_id) for caregiver_id in caregiver_ids)
    )
    return {
        "invitations": [
            {
                "code": invite.code,
                "email": invite.invited_email,
                "created_at": invite.created_at.isoformat(),
                "expires_at": invite.expires_at.isoformat(),
                "expired": invite.is_expired(now),
                "redeemed": invite.is_spent,
            }
            for invite in invites
        ],
        "caregivers": [
            {
                "full_name": caregiver.full_name,
                "email": caregiver.email,
            }
            for caregiver in caregivers
            if caregiver
        ],
    }


@app.post("/api/invite/resend")
async def resend_invite(
    request: Request,
    body: InviteResendRequest,
) -> dict:
    """Resend an unspent, unexpired invitation owned by the signed-in member."""
    user_id = authenticated_user_id(request)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Sign in to resend invitations.")
    user = await asyncio.to_thread(store.get_user, user_id)
    if not user or user.role != "user":
        raise HTTPException(status_code=403, detail="Only members have invitations.")

    invite = await asyncio.to_thread(store.get_invite, body.code)
    if not invite or invite.user_id != user_id:
        raise HTTPException(status_code=404, detail="Invitation not found.")
    if invite.is_spent:
        raise HTTPException(status_code=409, detail="This invitation has already been used.")
    if invite.is_expired(_now_naive()):
        raise HTTPException(status_code=410, detail="This invitation has expired. Create a new one.")

    email = (body.email or invite.invited_email).strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise HTTPException(status_code=422, detail="Enter a valid caregiver email.")
    await asyncio.to_thread(store.set_invite_email, invite.code, user_id, email)
    delivered, error = await email_delivery.send_caregiver_invitation(
        email,
        user.full_name or "Someone you care about",
        invite.code,
        idempotency_key=f"invite-resend-{invite.code}-{uuid.uuid4().hex}",
    )
    if not delivered:
        raise HTTPException(status_code=503, detail=error or "Invitation could not be sent.")
    return {"ok": True, "code": invite.code, "email": email, "email_sent": True}


@app.post("/api/invite/redeem")
async def post_invite_redeem(request: Request, body: dict = Body(...)) -> dict:
    """Redeem an invite code, linking the calling caregiver to its issuer.

    Auth is required: a link is attached to a real account, and an anonymous
    redemption would spend a member's code on nobody.

    Args:
        body: `{code}` — the code as typed. Case and dashes are normalised by
            the store, because this is retyped by a human from a screen.

    Returns:
        `{ok, watching}` where `watching` is the member's username, so the
        caregiver immediately sees WHO they are now connected to and can catch a
        mistyped-but-valid code before relying on it.

    Raises:
        HTTPException 400: Unknown, expired, already-used, or self-issued code.
            The store's message is passed through verbatim — it distinguishes
            those cases on purpose, because "invalid" sends an exhausted person
            round a loop they cannot debug.
    """
    caller = authenticated_user_id(request)
    if caller is None:
        raise HTTPException(status_code=401, detail="Sign in to redeem an invite code.")

    code = (body.get("code") or "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="Enter the code you were given.")

    try:
        watched_id = await asyncio.to_thread(
            store.redeem_invite, code, caller, _now_naive()
        )
    except store.InviteError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    watched = await asyncio.to_thread(store.get_user, watched_id)
    return {"ok": True, "watching": watched.username if watched else None}


@app.post("/api/auth/login")
async def auth_login(body: LoginRequest) -> JSONResponse:
    """Sign in and set the session cookie.

    The error message is deliberately generic and identical for an unknown username
    and a wrong password, so this endpoint cannot be used to enumerate who has an
    account here. Given what an account in this product implies about a person,
    that is a meaningful disclosure to withhold.
    """
    from app import auth

    try:
        user = auth.verify_login(body.username, body.password)
    except Exception:
        raise HTTPException(status_code=401, detail="Incorrect email or password.")

    response = JSONResponse(
        {
            "ok": True,
            "full_name": user.full_name or user.username,
            "role": user.role,
        }
    )
    auth.set_session_cookie(response, user.id)
    return response


@app.post("/api/auth/logout")
async def auth_logout() -> JSONResponse:
    """Clear the session cookie."""
    from app import auth

    response = JSONResponse({"ok": True})
    auth.clear_session_cookie(response)
    return response


@app.get("/api/auth/me")
async def auth_me(request: Request) -> dict:
    """Who is signed in, if anyone.

    Returns a 200 with signed_in:false rather than a 401 for an anonymous caller.
    The bystander surface asks this question and must never be handed an error for
    the entirely normal state of having no account (PRD §3).
    """
    from app import auth

    user = auth.user_from_request(request)
    if not user:
        return {"signed_in": False}
    return {"signed_in": True, "username": user.username, "role": user.role}


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
@app.get("/api/state")
async def get_state(request: Request) -> dict:
    """Everything a client needs to render itself on load.

    Deliberately one round trip: at Tier 4 the client must be able to paint the
    emergency surface without a waterfall of requests.

    WHOSE state? Resolved SERVER-SIDE by `resolve_subject`. A member sees their
    own; a caregiver sees the one person they hold a consented link to (PRD §8);
    a caregiver with no link gets an honest 403 rather than somebody else's data.
    The client never names the subject — there is no parameter for it, which is
    the strongest form the guarantee can take. Before the link table existed this
    route resolved a signed-in caregiver to their OWN empty account, which is why
    the caregiver surface rendered blank and the browser was left to reassemble
    it from an unfiltered event firehose.

    `events` are OLDEST FIRST. See `deps.events_for_wire` for the wire-order
    contract; every consumer, prompt and UI reads this order.
    """
    user_id = resolve_subject(request)
    profile = await asyncio.to_thread(store.get_profile, user_id)
    tier = _current_tier(user_id)
    events = await asyncio.to_thread(events_for_wire, user_id, limit=50)
    caller_id = authenticated_user_id(request)
    caller = await asyncio.to_thread(store.get_user, caller_id) if caller_id else None
    caregiver_view = bool(caller and caller.role == "caregiver")
    caregiver_visible = bool(
        caregiver_view and visible_to(caller_id, user_id, tier)
    )

    # Report AI availability honestly. The UI renders an explicit "AI offline" state
    # rather than silently substituting canned text — passing off a fallback as a
    # live generation is both dishonest and an automatic disqualifier here.
    ai_online = False
    try:
        from app import genai

        ai_online = genai.is_online()
    except Exception:
        ai_online = False

    from app.models import TIER_NAMES

    if caregiver_view:
        profile_payload = caregiver_profile_projection(
            profile, tier, visible=caregiver_visible
        )
        event_payload = caregiver_event_projection(
            events, caller_id, user_id, visible=caregiver_visible
        )
    else:
        profile_payload = profile.model_dump(mode="json") if profile else None
        event_payload = [e.model_dump(mode="json") for e in events]

    return {
        "visible": caregiver_visible if caregiver_view else True,
        "tier": int(tier) if not caregiver_view or caregiver_visible else None,
        "tier_name": (
            TIER_NAMES[tier]
            if not caregiver_view or caregiver_visible
            else "No shared alert"
        ),
        "ai_online": ai_online,
        "profile": profile_payload,
        "events": event_payload,
        # The order contract, stated on the wire rather than only in a docstring.
        # Consumers had drifted into disagreeing about this — one sliced the
        # wrong end, another reversed an already-reversed list — and a caregiver
        # was shown the oldest event as the current reason. A client can now
        # assert the order it is being given instead of assuming one.
        "events_order": "oldest_first",
        # How many of the most recent events this response carries. The ladder
        # page calls itself a "full event log"; it can only say that honestly if
        # the server discloses the cap it applied.
        "events_limit": 50,
    }


# ---------------------------------------------------------------------------
# Triage inputs
# ---------------------------------------------------------------------------
@app.post("/api/utterance")
async def post_utterance(request: Request, body: UtteranceRequest) -> dict:
    """Primary zero-typing input: a transcribed utterance from the voice companion.

    Order of operations matters and is not arbitrary. Triage runs FIRST and completely,
    on rules alone. Only afterwards do we ask the model for a conversational reply.
    That ordering is the architecture: the safety decision has already been made and
    committed before the generative layer is consulted, so a slow, wrong, or entirely
    absent model cannot delay or alter it (PRD P4).
    """
    text = body.text

    user_id = _session_user(request)
    profile = await asyncio.to_thread(store.get_profile, user_id)

    result = triage.evaluate(
        _current_tier(user_id), utterance=text, profile=profile, now=_now()
    )
    event = await _record(user_id, result, source="utterance")
    _schedule_emergency_alerts(user_id, event)
    payload = _result_payload(result)
    await _broadcast({"type": "tier", **payload, "user_id": user_id})

    # Now — and only now — the language layer. Failure here degrades the reply, never
    # the escalation, which has already been recorded above.
    reply = None
    try:
        from app import genai
        from app.prompts import checkin

        system, user = checkin.build(profile, text, result.tier)
        gen = await genai.generate(system, user, fast=True)
        reply = gen.model_dump()
    except Exception as exc:
        reply = {
            "text": "",
            "live": False,
            "model": "",
            "latency_ms": 0,
            "error": f"AI unavailable: {exc}",
        }

    return {"triage": payload, "reply": reply}


@app.post("/api/sensor")
async def post_sensor(request: Request, body: SensorRequest) -> dict:
    """Silence and stillness — the strongest signal the system will ever receive.

    PRD P2: silence is a signal, not a dead end. A user who says something high-risk
    and then stops responding is the case this product exists for, so this endpoint
    escalates rather than timing out.
    """
    user_id = _session_user(request)
    profile = await asyncio.to_thread(store.get_profile, user_id)

    result = triage.evaluate(
        _current_tier(user_id),
        silent_seconds=body.silent_seconds,
        still=body.still,
        profile=profile,
        now=_now(),
    )
    event = await _record(user_id, result, source="sensor")
    _schedule_emergency_alerts(user_id, event)
    payload = _result_payload(result)
    await _broadcast({"type": "tier", **payload, "user_id": user_id})
    return payload


@app.post("/api/tier")
async def post_tier(request: Request, body: TierRequest) -> dict:
    """Explicit tier set. Two legitimate uses, both real product behaviour.

    1. The user taps "I need help now" and jumps straight to the emergency surface —
       a real path that must never require a conversation first.
    This is an honest override rather than simulated triage: the recorded reason says
    plainly that a human set it, so the audit log never misattributes it to a signal.
    """
    user_id = _session_user(request)
    profile = await asyncio.to_thread(store.get_profile, user_id)
    tier = body.tier

    previous = _current_tier(user_id)
    result = TriageResult(
        tier=tier,
        previous_tier=previous,
        reason="Set directly from the interface.",
        matched_signal=None,
        actions=triage.actions_for_tier(tier, profile),
        notify_caregiver=triage.notify_caregiver_for(tier, profile),
    )
    event = await _record(user_id, result, source="manual")
    _schedule_emergency_alerts(user_id, event)
    payload = _result_payload(result)
    await _broadcast({"type": "tier", **payload, "user_id": user_id})
    return payload


@app.post("/api/rescind")
async def post_rescind(request: Request) -> dict:
    """One-tap false-alarm rescind.

    PRD §15 Q3: this must be instantaneous and single-tap. If cancelling a false
    positive is awkward, users disable the tier that protects them — so the cost of
    a mistake has to stay near zero.
    """
    user_id = _session_user(request)
    profile = await asyncio.to_thread(store.get_profile, user_id)
    # No `now` here by design: rescinding is a direct user instruction, not a
    # time-dependent inference, so the state machine needs no clock to honour it.
    result = triage.rescind(_current_tier(user_id), profile=profile)
    await _record(user_id, result, source="rescind")
    payload = _result_payload(result)
    await _broadcast({"type": "tier", **payload, "user_id": user_id})
    return payload


@app.post("/api/action-receipt")
async def post_action_receipt(
    request: Request, body: ActionReceiptRequest
) -> dict:
    """Append a client-confirmed execution receipt to the immutable log."""
    user_id = authenticated_user_id(request)
    actor = await asyncio.to_thread(store.get_user, user_id) if user_id else None
    if actor is None:
        raise HTTPException(status_code=401, detail="Sign in to record an action.")
    if actor.role != "user":
        raise HTTPException(
            status_code=403,
            detail="Only the member's own device may confirm member-side actions.",
        )

    event = Event(
        id=str(uuid.uuid4()),
        user_id=user_id,
        at=_now(),
        tier=_current_tier(user_id),
        trigger_source="execution_receipt",
        reason=body.detail or "The member's device confirmed an action completed.",
        actions_planned=[],
        actions_taken=[body.action],
    )
    await asyncio.to_thread(store.append_event, event)
    await _broadcast(
        {
            "type": "receipt",
            "tier": int(event.tier),
            "user_id": user_id,
            "at": event.at.isoformat(),
            "actions_taken": event.actions_taken,
        }
    )
    return {"ok": True, "event": event.model_dump(mode="json")}


# ---------------------------------------------------------------------------
# Server-sent events
# ---------------------------------------------------------------------------
@app.get("/api/events")
async def sse(request: Request) -> StreamingResponse:
    """Live ladder stream, scoped per recipient. This makes the caregiver surface real.

    SSE rather than WebSockets: the data flows one way, and SSE reconnects on its own
    without any client-side reconnection logic to get wrong.

    THE STREAM IS AUTHORIZED, NOT FILTERED DOWNSTREAM. The listener is tagged
    with the account behind the session at subscribe time, and `_broadcast` runs
    every event through `deps.visible_to` before it is written. A listener
    therefore receives an event only when:

      * it is their own event (PRD §11 — the user's log is never hidden from
        the user); or
      * they hold a consented caregiver link to that user AND the watched
        person's own ladder config permits visibility at that tier — tier 4/5
        always (PRD §4.2, non-negotiable and undisableable), tier 3 only with
        `tier_3_visible_to_caregiver`, tiers 0-2 never.

    An anonymous listener is entitled to nothing and receives only heartbeats. It
    is still allowed to CONNECT rather than being refused, because the bystander
    surface is deliberately outside the auth wall (PRD §3) and a 401 here would
    show a scary error on a page someone may be reading while standing over an
    unconscious person.

    This replaces a global fan-out in which every user's events reached every
    listener and the caregiver page discarded the ones that were not its own.
    That put one person's tier reasons, escalation prose and account id on a
    stranger's socket. Filtering in the browser is a rendering preference; the
    privacy boundary has to be the point where the bytes leave the server.
    """
    # Resolved ONCE, from the signed cookie, before the stream opens. Never from
    # a query parameter: a subscriber does not get to name who they are.
    #
    # A session that expires mid-stream does not retroactively widen access —
    # this id only ever narrows what `visible_to` will pass — but the LINK is
    # re-read from the database on every event, so revoking consent silences an
    # already-open stream on its next event rather than at reconnect.
    listener = Listener(
        user_id=authenticated_user_id(request),
        queue=asyncio.Queue(maxsize=64),
    )
    _listeners.append(listener)

    async def stream():
        try:
            # Immediate hello so the client can distinguish "connected and quiet"
            # from "never connected" — silence must never look like health here.
            yield "event: ping\ndata: {}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    item = await asyncio.wait_for(listener.queue.get(), timeout=15)
                    yield f"data: {json.dumps(item)}\n\n"
                except asyncio.TimeoutError:
                    # Heartbeat keeps proxies from closing an idle connection.
                    yield "event: ping\ndata: {}\n\n"
        finally:
            if listener in _listeners:
                _listeners.remove(listener)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Generative endpoints
# ---------------------------------------------------------------------------
# Every one of these makes a real API call to Google Gemini via OpenRouter. When the
# call fails, the response carries live=False plus a populated error, and the UI shows
# that state explicitly. A fallback never masquerades as a fresh generation — that
# would be dishonest to the user and an automatic disqualifier under the judging rules.
#
# The funnel itself lives in `app.deps` and is imported at the top of this file
# rather than redefined here. It grew a second responsibility — recording which
# account a cached generation belongs to, so account deletion can remove it — and
# a duplicate copy would have meant half the generative surfaces silently leaving
# a cached home address and door entry code behind after a user deleted
# themselves.


@app.get("/api/script/911")
async def get_script_911(request: Request) -> dict:
    """The personalised emergency script: their address, their unit, their entry code.

    Gemini personalizes the script ahead of need. Dispatcher facts are validated
    character-for-character; any missing or altered fact triggers the local
    template, so GenAI remains core without becoming a single point of failure.
    """
    owner_id = _require_own_profile(request)
    profile = await asyncio.to_thread(store.get_profile, owner_id)
    from app.prompts import script_911

    if profile is None:
        raise HTTPException(
            status_code=409,
            detail="Complete your emergency profile before generating this script.",
        )
    generated = await _generate(
        script_911.build,
        fast=False,
        owner_id=owner_id,
        profile=profile,
    )
    if generated.get("live") and script_911.preserves_dispatcher_facts(
        str(generated.get("text") or ""), profile
    ):
        generated["validated"] = True
        generated["deterministic"] = False
        return generated

    return {
        "text": script_911.render(profile),
        "live": False,
        "deterministic": True,
        "validated": True,
        "model": "local-template",
        "latency_ms": generated.get("latency_ms", 0),
        "error": generated.get("error")
        or "Generated script failed fact validation; using verified local fallback.",
    }


@app.get("/api/script/refusal")
async def get_script_refusal(request: Request) -> dict:
    """Refusal and exit lines in the user's own register — prevention-side, zero typing."""
    owner_id = _session_user(request)
    profile = await asyncio.to_thread(store.get_profile, owner_id)
    from app.prompts import refusal

    return await _generate(refusal.build, fast=False, owner_id=owner_id, profile=profile)


@app.get("/api/tolerance")
async def get_tolerance(request: Request) -> dict:
    """Tolerance Guard: the lead prevention feature.

    Fires on an ordinary day when nothing appears to be wrong. Tolerance collapses
    within days of abstinence but the remembered dose does not, and the period after
    detox, hospital discharge, or release from incarceration is among the highest-risk
    windows in medicine (PRD §5.1).
    """
    user_id = _session_user(request)
    profile = await asyncio.to_thread(store.get_profile, user_id)
    from app.prompts import tolerance

    payload = await _generate(
        tolerance.build, fast=False, owner_id=user_id, profile=profile, now=_now()
    )
    # Surface the window itself alongside the message so the UI can show *why* this
    # fired — the deterministic layer explains itself; the model only phrases it.
    payload["window_active"] = triage.tolerance_window_active(profile, _now())
    return payload


@app.get("/api/vault/select")
async def get_vault_select(request: Request, context: str = "") -> dict:
    """Memory Vault: choose the recording that fits this moment, and say why.

    The clips are real recordings made by a real caregiver. The generative work is the
    situational judgement of which one belongs here; the emotional payload stays
    authentically human. We do not clone the caregiver's voice (PRD §7.2).

    SCOPED TO THE CALLER'S VAULT. `for_user` restricts the candidate set to this
    account's own clips plus the unowned shared demo fixtures. A recorded message
    names the speaker, names the relationship, and was made for exactly one
    listener; offering somebody else's to the selector would be a privacy failure
    dressed up as a feature. Previously every clip in the database was a
    candidate for every caller.
    """
    user_id = _session_user(request)
    clips = await asyncio.to_thread(store.list_vault_clips, for_user=user_id)
    if not clips:
        return {"clip": None, "why": None, "error": "No vault clips recorded yet."}

    profile = await asyncio.to_thread(store.get_profile, user_id)

    # Delegate to genai.vault_select rather than parsing the model's JSON here.
    # That path validates the returned id against the clips we actually offered —
    # so a hallucinated clip id can never select a recording that does not exist —
    # and downgrades a salvaged-but-malformed parse to live=False rather than
    # presenting a guess as a confident choice.
    try:
        from app import genai

        chosen, gen = await genai.vault_select(profile, clips, context)
    except Exception as exc:
        chosen, gen = None, None
        error = f"AI unavailable: {exc}"
    else:
        error = gen.error

    # Whatever happens upstream, Tier 2 must not render an empty screen: a person
    # asking for a reason not to use is owed a real recording, even if the model
    # could not say which one fits best.
    if chosen is None:
        chosen = clips[0]

    return {
        "clip": chosen.model_dump(mode="json"),
        "why": gen.text if gen else "",
        "live": gen.live if gen else False,
        "error": error,
    }


@app.get("/api/caregiver/brief")
async def get_caregiver_brief(request: Request) -> dict:
    """The 3am alert, written for a terrified person with no context and no memory.

    A raw ping wakes someone into maximum panic and tells them nothing. This arrives
    with what happened, what the system already did automatically, what to do in the
    next sixty seconds, and — CRAFT-grounded — what not to say (PRD §8).

    ABOUT WHOM? `resolve_subject` again: the linked, consented watched user, or a
    403 for a caregiver nobody has added. A caregiver cannot brief against an
    account they do not hold a link to, and no request parameter can name one.

    The events go in OLDEST FIRST via `events_for_wire`, which is what the prompt
    documents and expects. It previously received the newest-first storage order,
    took `events[-max_events:]` believing that was the recent tail, and read
    `tail[-1].reason` as the CURRENT reason — so a caregiver woken at 3am was
    handed the oldest event as tonight's reason and the incident narrated
    backwards. The prompt now gets the order it was written for.
    """
    user_id = resolve_subject(request)
    profile = await asyncio.to_thread(store.get_profile, user_id)
    tier = _current_tier(user_id)
    caller_id = authenticated_user_id(request)
    caller = await asyncio.to_thread(store.get_user, caller_id) if caller_id else None
    if caller and caller.role == "caregiver" and not visible_to(caller_id, user_id, tier):
        raise HTTPException(
            status_code=403,
            detail="There is no alert the member has chosen to share right now.",
        )
    events = await asyncio.to_thread(events_for_wire, user_id, limit=10)
    from app.prompts import caregiver_brief

    # Owned by the WATCHED person, not the caregiver reading it. The brief is
    # about them and is built from their event log, so it goes when they go.
    payload = await _generate(
        caregiver_brief.build,
        fast=False,
        owner_id=user_id,
        profile=profile,
        tier=tier,
        events=events,
    )
    payload["tier"] = int(tier)
    return payload


@app.post("/api/profile")
async def post_profile(request: Request, body: ProfileUpdateRequest) -> dict:
    """Save profile and ladder settings from onboarding.

    This is how the user exercises PRD P3 — they own the escalation thresholds.
    Without it the ladder table is a display of a promise rather than the promise
    itself, so the endpoint is load-bearing for the product's central claim.

    Tier 4 and Tier 5 visibility are deliberately NOT writable here. They are the
    one thing the user cannot switch off, disclosed at onboarding and stated in
    the Terms. Accepting them from the client would make that disclosure a lie
    even if the UI never sent them.

    CREATES a profile when the account has none. A newly registered evaluator had
    no way to get one: this route required an existing row, and an anonymous
    caller was resolved to the published demo profile — so "the user owns the
    escalation thresholds", the product's central claim, was true only for a
    fixture that shipped pre-populated. It is now true end-to-end from an empty
    account.

    OWNER-ONLY. `authenticated_user_id` is used rather than the demo-resolving
    session helper: this route WRITES, and the demo fallback exists to make reads
    look alive, not to let an anonymous stranger edit a home address and door
    entry code. A caregiver is refused too — they may watch a ladder, never
    rewrite one (PRD P3: these thresholds belong to the person they describe).
    """
    user_id = authenticated_user_id(request)
    if user_id is None:
        raise HTTPException(
            status_code=401,
            detail="Sign in to save your settings.",
        )

    actor = await asyncio.to_thread(store.get_user, user_id)
    if actor is not None and actor.role == "caregiver":
        raise HTTPException(
            status_code=403,
            detail="A caregiver cannot change the thresholds of the person they support.",
        )

    profile = await asyncio.to_thread(store.get_profile, user_id)
    if profile is None:
        # First save for this account. The profile id is generated SERVER-SIDE and
        # the display name comes from the account, so neither is a client-writable
        # field — a crafted body cannot mint a profile with somebody else's id.
        profile = UserProfile(
            id=uuid.uuid4().hex,
            name=(actor.username if actor else "You"),
        )

    changes = body.model_dump(exclude_unset=True)

    # The request model is the whitelist. It rejects unknown fields before this
    # function runs and rejects overlong address facts instead of truncating them.
    for field in ("address", "unit", "entry_code", "cross_street", "state_code"):
        if field in changes:
            setattr(profile, field, changes[field])

    if "naloxone_on_hand" in changes:
        profile.naloxone_on_hand = changes["naloxone_on_hand"]

    ladder = changes.get("ladder") or {}
    for field in ("tier_2_visible_to_caregiver", "tier_3_visible_to_caregiver"):
        if isinstance(ladder.get(field), bool):
            setattr(profile.ladder, field, ladder[field])
    # `bool` is a subclass of `int` in Python, so `isinstance(True, int)` is True.
    # Excluding bool explicitly stops a stray `true` from being stored as a
    # threshold of 1 — a silently wrong safety number that no form would reveal.
    if isinstance(ladder.get("silence_seconds_to_escalate"), int) and not isinstance(
        ladder.get("silence_seconds_to_escalate"), bool
    ):
        # Bounded: a user may tune how long silence waits, but not disable it by
        # setting it to something that never fires.
        profile.ladder.silence_seconds_to_escalate = max(
            5, min(300, ladder["silence_seconds_to_escalate"])
        )
    if isinstance(ladder.get("missed_checkins_to_elevate"), int) and not isinstance(
        ladder.get("missed_checkins_to_elevate"), bool
    ):
        # Was accepted by the form and then silently dropped here, so the user
        # saw their choice reflected in the UI and stored nowhere. Bounded 1-10:
        # zero would elevate on a check-in that never happened, and a large
        # number is indistinguishable from switching the rung off — the ladder
        # is tunable, not disableable.
        profile.ladder.missed_checkins_to_elevate = max(
            1, min(10, ladder["missed_checkins_to_elevate"])
        )

    # --- Contacts ---------------------------------------------------------
    # Editable at last. The onboarding UI said outright that contacts were not
    # editable, which made the contact tree — who gets woken, and at which tier —
    # the one part of the escalation the user could not actually own.
    #
    # Absent key means "leave the tree alone"; an explicit empty list means
    # "I have no contacts", which is a legitimate and different statement. The
    # two must not collapse into each other, or a partial save would silently
    # delete everyone.
    if "contacts" in changes:
        profile.contacts = _parse_contacts(changes["contacts"])

    await asyncio.to_thread(store.put_profile, user_id, profile)
    return {"ok": True, "profile": profile.model_dump(mode="json")}


# Ceiling on the contact tree. A person has a handful of people; anything past
# this is either a mistake or an attempt to make one profile write expensive.
_MAX_CONTACTS = 10


def _parse_contacts(raw) -> list[Contact]:
    """Validate a client-supplied contact tree into typed `Contact` models.

    Every field is bounded and every tier is checked against the enum, because
    this list decides who is telephoned when someone stops breathing. A malformed
    entry is REJECTED with a 400 rather than dropped silently: a user who thinks
    they added their sister and did not is worse off than one who sees an error.

    Args:
        raw: The `contacts` value from the request body. Must be a list of
            objects.

    Returns:
        The parsed contacts, renumbered into a contiguous 1-based fire order so
        the stored tree cannot contain gaps or ties that make "who is called
        first" ambiguous.

    Raises:
        HTTPException: 400 for a non-list, an over-long list, a non-object entry,
            a missing name, or an unknown tier.
    """
    if not isinstance(raw, list):
        raise HTTPException(status_code=400, detail="contacts must be a list.")
    if len(raw) > _MAX_CONTACTS:
        raise HTTPException(
            status_code=400, detail=f"No more than {_MAX_CONTACTS} contacts."
        )

    parsed: list[Contact] = []
    for index, entry in enumerate(raw, start=1):
        if not isinstance(entry, dict):
            raise HTTPException(status_code=400, detail="Each contact must be an object.")

        name = str(entry.get("name") or "").strip()[:100]
        if not name:
            raise HTTPException(status_code=400, detail="Every contact needs a name.")
        channel = str(entry.get("channel") or "phone").strip()[:20] or "phone"
        destination = str(entry.get("destination") or "").strip()[:320]
        if channel == "email" and (
            "@" not in destination or "." not in destination.rsplit("@", 1)[-1]
        ):
            raise HTTPException(
                status_code=400,
                detail=f"{name} needs a valid emergency email address.",
            )

        tiers: list[Tier] = []
        for value in entry.get("tiers") or []:
            try:
                tiers.append(Tier(int(value)))
            except (ValueError, TypeError):
                raise HTTPException(status_code=400, detail=f"Unknown tier: {value!r}")

        parsed.append(
            Contact(
                name=name,
                relation=str(entry.get("relation") or "").strip()[:100],
                # Defaults to phone: an unreachable channel is worse than a
                # wrong-but-dialable one on the tier-5 path.
                channel=channel,
                destination=destination,
                # Position comes from the submitted ORDER, not from a
                # client-supplied `order` field. That makes ties and gaps
                # unrepresentable rather than merely unlikely.
                order=index,
                tiers=tiers,
            )
        )
    return parsed


@app.get("/api/legal/{state_code}")
async def get_legal(state_code: str) -> dict:
    """Good Samaritan overdose-immunity summary for a state.

    Served from a static, human-reviewed dataset and NEVER from the model. Immunity
    scope varies substantially between states, and a hallucinated legal protection is
    the single worst thing this product could tell someone standing over an overdose
    (PRD §6.5). If the state is missing we say so plainly rather than guessing.
    """
    record = legal.get(state_code)
    if record is not None:
        return record

    return {
        "state_code": state_code.upper(),
        "unknown": True,
        "summary": (
            "We do not have a reviewed summary for this state. Calling 911 is still "
            "the right thing to do. Do not rely on this app for legal advice."
        ),
    }


# ---------------------------------------------------------------------------
# Static frontend
# ---------------------------------------------------------------------------
@app.get("/healthz")
async def healthz() -> dict:
    """Liveness probe, also handy for confirming the server is up during a demo."""
    return {"ok": True}


def _page(name: str) -> FileResponse:
    """Serve a page from web/, with a clear error if the file is not there yet."""
    path = WEB_DIR / name
    if not path.exists():
        return JSONResponse(
            status_code=503, content={"detail": f"{name} not built yet"}
        )
    return FileResponse(path)


@app.get("/")
async def page_root(request: Request):
    """The front door.

    Serves the public homepage to a visitor with no session, and the app itself
    to someone signed in. A stranger — a judge, a family member deciding whether
    to trust this, someone who followed a link — should land on a page that
    explains what this is, not inside another person's live recovery surface with
    their tier, their event history, and their contact tree on screen.

    Signing in then lands you straight in the app, because someone who has an
    account does not need the pitch.
    """
    try:
        from app import auth

        if auth.user_from_request(request):
            return _page("index.html")
    except Exception:
        pass
    return _page("home.html")


def _private_page(request: Request, name: str):
    """Serve a private page only while its signed session is valid."""
    from app import auth

    if auth.user_from_request(request) is None:
        return RedirectResponse("/login", status_code=303)
    return _page(name)


@app.get("/app")
async def page_app(request: Request):
    """Member app, protected at the HTTP boundary."""
    return _private_page(request, "index.html")


@app.get("/caregiver")
async def page_caregiver(request: Request):
    return _private_page(request, "caregiver.html")


@app.get("/onboarding")
async def page_onboarding(request: Request):
    return _private_page(request, "onboarding.html")


@app.get("/ladder")
async def page_ladder(request: Request):
    return _private_page(request, "ladder.html")


@app.get("/legacy-app")
async def page_legacy_app(request: Request):
    """Compatibility redirect for previously bookmarked app links."""
    if authenticated_user_id(request) is None:
        return RedirectResponse("/login", status_code=303)
    return _page("index.html")


@app.get("/bystander")
async def page_bystander():
    """Bystander mode is intentionally reachable with no session and no account.

    PRD §3: this person may not know the user, may be using themselves, and is
    terrified of arrest. Asking them to register would cost the exact minutes that
    decide whether someone breathes.
    """
    return _page("bystander.html")




@app.get("/home")
async def page_home():
    """Public marketing homepage. Deliberately separate from `/`, which is the app."""
    return _page("home.html")


@app.get("/emergency")
async def page_emergency():
    """Public emergency numbers. No auth, no account, no cookie wall.

    Someone may arrive here from a search engine while standing over a person who
    is not breathing. Nothing may stand between them and a dialable number.
    """
    return _page("emergency.html")


@app.get("/contact")
async def page_contact():
    return _page("contact.html")


@app.get("/terms")
async def page_terms():
    return _page("terms.html")


@app.get("/privacy")
async def page_privacy():
    return _page("privacy.html")


@app.get("/data-deletion")
async def page_data_deletion():
    return _page("data-deletion.html")


# ---------------------------------------------------------------------------
# Public endpoints backing the public pages
# ---------------------------------------------------------------------------
@app.post("/api/contact")
async def post_contact(body: ContactRequest) -> dict:
    """Receive a contact message.

    Persisted to a local JSONL file rather than emailed: this is a prototype with no
    mail infrastructure, and silently dropping a message while showing the user a
    success tick would be a lie. Writing it down means the submission is real.

    Deliberately NOT stored in the main database — contact messages come from the
    public and must never mix with clinical profile data.
    """
    DATA_DIR.mkdir(exist_ok=True)
    record = {
        "at": _now().isoformat(),
        "name": body.name,
        "email": body.email,
        "topic": body.topic,
        "message": body.message,
    }
    with (DATA_DIR / "contact_messages.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")

    return {"ok": True}


@app.post("/api/account/delete")
async def post_account_delete(request: Request) -> JSONResponse:
    """Delete the signed-in account and everything attached to it.

    Immediate and total: no soft-delete, no thirty-day tombstone, no recovery
    window. A person who cannot leave cleanly was never safe being honest with us
    in the first place, and a retained shadow copy would make this policy a lie.

    FIVE PLACES, because /data-deletion names them and the page has to be true.
    Deleting the database rows alone left the two most sensitive artefacts
    behind — the vault clips and the cached 911 script:

      1. Provider-side voice models (below, before the rows they hang off).
      2. Database — profile, ladder, contacts, tolerance events, event log,
         caregiver links in both directions, and the user's own Memory Vault
         clips, which now carry an owner and so can finally be scoped.
      3. Disk cache — cached generations produced for this account, including a
         911 script carrying their home address and door entry code. The cache is
         keyed by a prompt hash, so the owner index is what makes those files
         findable at all; without it they simply survived.
      4. Live in-memory state — the ladder cursor AND any open SSE listener
         tagged with this account, so no stream keeps delivering events about
         records that no longer exist.
      5. The session cookie, cleared on the response, so the browser is not left
         holding a signed token naming a deleted account.
    """
    # Imported outside the try: this route CANNOT complete without `auth`, since
    # it must clear the session cookie at the end. Swallowing the import failure
    # here would leave a signed-out-looking user still holding a valid token.
    from app import auth

    try:
        user = auth.user_from_request(request)
    except Exception:
        user = None

    if not user:
        raise HTTPException(status_code=401, detail="sign in first")

    # Destroy any consented supporter voice models this account owns, BEFORE the
    # rows behind them go. A hard constraint of that feature: deleting an account
    # deletes the voice model, at the provider and not merely in our database.
    # `store.delete_user_data` cannot do this — it has no network access by
    # design — so it is done here, where a route can await the provider call.
    # Best-effort: a provider outage must never trap a person inside an account
    # they have asked to leave.
    try:
        from app.routes import voice as voice_routes

        await voice_routes.purge_voices_for_user(user.id)
    except Exception as exc:  # pragma: no cover - defensive; deletion still proceeds
        log.warning("supporter voice purge failed during account deletion: %s", exc)

    # Read the cache keys BEFORE the rows go: `delete_user_data` drops the
    # ownership index, after which the files on disk are unreachable orphans
    # holding a home address and a door entry code.
    from app import genai

    cache_keys = await asyncio.to_thread(
        store.cache_keys_exclusively_owned_by, user.id
    )
    counts = await asyncio.to_thread(store.delete_user_data, user.id)

    removed = 0
    for key in cache_keys:
        if genai.cache_delete(key):
            removed += 1

    # Clear the live in-memory trace too, so no ladder cursor and no open stream
    # outlives the deletion of the records behind it.
    _tiers.pop(user.id, None)
    for listener in [l for l in _listeners if l.user_id == user.id]:
        # Detached rather than closed: the stream generator owns its own
        # lifecycle and ends when the client disconnects. Removing it here means
        # the next broadcast cannot reach it.
        _listeners.remove(listener)

    response = JSONResponse(
        {
            "ok": True,
            # Reported so the confirmation screen can state what was actually
            # removed rather than asserting it generically. A deletion someone
            # cannot verify is one they have to take on trust, which is precisely
            # what this page exists to avoid asking of them.
            "deleted": {**counts, "generation_cache_files": removed},
        }
    )
    auth.clear_session_cookie(response)
    return response


# ---------------------------------------------------------------------------
# Crawler surface
# ---------------------------------------------------------------------------
@app.get("/robots.txt")
async def robots() -> Response:
    """Crawler policy.

    The public pages SHOULD be indexed: someone searching "what to do if someone
    overdoses" should be able to find the bystander guide and the emergency numbers.
    Everything behind authentication is disallowed — not as a security measure
    (that is what the session check is for) but so that no fragment of a person's
    recovery surface can end up in a search index.
    """
    body = (
        "# Threshold — https://threshold.local\n"
        "# Public help pages are open to crawlers on purpose: someone searching\n"
        "# for overdose guidance should be able to find them.\n"
        "User-agent: *\n"
        "Allow: /home\n"
        "Allow: /emergency\n"
        "Allow: /bystander\n"
        "Allow: /contact\n"
        "Allow: /terms\n"
        "Allow: /privacy\n"
        "Allow: /data-deletion\n"
        "\n"
        "# Everything below is a person's private recovery surface.\n"
        "Disallow: /api/\n"
        "Disallow: /caregiver\n"
        "Disallow: /onboarding\n"
        "Disallow: /ladder\n"
        "Disallow: /login\n"
        "Disallow: /register\n"
        "\n"
        "Sitemap: /sitemap.xml\n"
    )
    return Response(content=body, media_type="text/plain")


@app.get("/sitemap.xml")
async def sitemap() -> Response:
    """Sitemap listing only the public, indexable pages.

    Priorities are set by how urgently a stranger might need the page, not by
    marketing value: the emergency numbers and the bystander guide outrank the
    homepage on purpose.
    """
    today = _now().date().isoformat()
    pages = [
        ("/emergency", "1.0", "weekly"),
        ("/bystander", "1.0", "weekly"),
        ("/home", "0.9", "weekly"),
        ("/contact", "0.5", "monthly"),
        ("/privacy", "0.4", "monthly"),
        ("/terms", "0.4", "monthly"),
        ("/data-deletion", "0.4", "monthly"),
    ]
    entries = "\n".join(
        f"  <url>\n"
        f"    <loc>{loc}</loc>\n"
        f"    <lastmod>{today}</lastmod>\n"
        f"    <changefreq>{freq}</changefreq>\n"
        f"    <priority>{pri}</priority>\n"
        f"  </url>"
        for loc, pri, freq in pages
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{entries}\n"
        "</urlset>\n"
    )
    return Response(content=xml, media_type="application/xml")


@app.get("/login")
async def page_login():
    return _page("login.html")


@app.get("/register")
async def page_register():
    return _page("register.html")


@app.get("/register/caregiver")
async def page_register_caregiver():
    """The caregiver's own front door.

    A separate page rather than a radio button, because the two audiences arrive
    in completely different states. A member is signing up for themselves in a
    calm moment. A caregiver is usually here because someone they love asked them
    to be, and they are frightened. The copy on each page is written for the
    person reading it, which a single shared form cannot do.

    It is also where the invite code is entered, which makes the consent model
    visible at the front door rather than buried in a settings screen.
    """
    return _page("register-caregiver.html")


# Mounted last so it cannot shadow the API routes above.
if (WEB_DIR / "css").exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")
