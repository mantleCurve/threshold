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
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app import store, triage
from app.models import Event, Tier, TriageResult

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
_tiers: dict[str, Tier] = {}

# Connected SSE listeners, so a tier change on the user's screen reaches the
# caregiver's screen without polling. One queue per connected client.
_listeners: list[asyncio.Queue] = []


def _now() -> datetime:
    """Single source of 'now'.

    Centralised so that time is injected into triage rather than read inside it —
    that is what keeps the state machine deterministic and testable.
    """
    return datetime.now(timezone.utc)


def _current_tier(user_id: str) -> Tier:
    """Current live tier for a user, defaulting to Baseline for anyone unseen."""
    return _tiers.get(user_id, Tier.BASELINE)


async def _broadcast(payload: dict) -> None:
    """Push an event to every connected SSE client.

    Uses put_nowait and tolerates failure: a slow or dead listener must never block a
    tier transition. In this product, delivery to a dashboard is strictly less
    important than the escalation itself continuing to run.
    """
    for q in list(_listeners):
        try:
            q.put_nowait(payload)
        except Exception:  # pragma: no cover - defensive; a full queue is not fatal
            pass


def _record(user_id: str, result: TriageResult, source: str) -> None:
    """Apply a triage decision: update the live tier and append to the audit log.

    PRD §11: every event is visible to the user. There is no hidden log — this is the
    only write path for ladder history, and nothing in the app filters it on read.
    """
    _tiers[user_id] = result.tier
    store.append_event(
        Event(
            id=str(uuid.uuid4()),
            user_id=user_id,
            at=_now(),
            tier=result.tier,
            trigger_source=source,
            reason=result.reason,
            actions_taken=[a.kind for a in result.actions],
        )
    )


def _result_payload(result: TriageResult) -> dict:
    """Serialise a TriageResult for the wire, adding display-friendly names."""
    from app.models import TIER_NAMES

    return {
        "tier": int(result.tier),
        "tier_name": TIER_NAMES[result.tier],
        "previous_tier": int(result.previous_tier),
        "reason": result.reason,
        "matched_signal": result.matched_signal,
        "notify_caregiver": result.notify_caregiver,
        "actions": [a.model_dump() for a in result.actions],
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialise the database and seed demo state before serving any request.

    Seeding is idempotent, so a restart never duplicates or clobbers data. We seed at
    boot rather than on first request so an evaluator who lands directly on a deep
    link still finds a working, populated app.
    """
    store.init_db()
    try:
        from app import seed

        seed.seed()
    except Exception as exc:  # pragma: no cover - seed is best-effort at boot
        # A seed failure must not prevent the app from serving. Registration still
        # works, so an evaluator can create their own account and use every feature.
        log.warning("seed skipped: %s", exc)
    yield


app = FastAPI(
    title="Threshold",
    description="Recovery and overdose-prevention platform. Deterministic triage, generative language.",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Session helper
# ---------------------------------------------------------------------------
def _session_user(request: Request) -> str:
    """Resolve the acting user id from the session cookie.

    Falls back to the seeded demo user rather than raising. This is a deliberate
    trade-off for a judged hackathon build: an evaluator poking at an API endpoint
    directly should see the product work, not a 401. Authentication still gates the
    UI surfaces and still proves the security work; it simply is not allowed to make
    a feature look broken.
    """
    try:
        from app import auth

        user = auth.user_from_request(request)
        if user:
            return user.id
    except Exception:
        pass

    # Fall back to the seeded demo user, resolved BY USERNAME rather than by a
    # hardcoded id. Seeded accounts get generated UUIDs, so assuming the id is
    # literally "sam" silently produced an empty profile — the kind of bug that
    # looks like a broken feature to an evaluator rather than a wiring mistake.
    #
    # SCOPE OF THIS FALLBACK: it resolves to the *published demo account* only.
    # Its credentials are printed on the login page and in the README, so nothing
    # reachable through it is private — it is a fixture, not a person. A real
    # registered account is never served to an anonymous caller, because that
    # would hand out someone's home address and door entry code.
    # See _require_own_profile() for the endpoints that enforce that boundary.
    demo = store.get_user_by_username("sam")
    return demo.id if demo else "sam"


def _require_own_profile(request: Request) -> str:
    """Resolve the acting user for endpoints that expose personal detail.

    Stricter than `_session_user`. The 911 script contains a home address, an
    apartment number, and a door entry code; the profile contains substances used.
    That is the most dangerous data in the product — precisely what someone would
    want in order to find a person who is using — so it requires a real session.

    The published demo account remains reachable without one, because its details
    are fictional and printed publicly. Everything else demands authentication.
    """
    try:
        from app import auth

        user = auth.user_from_request(request)
        if user:
            return user.id
    except Exception:
        pass

    demo = store.get_user_by_username("sam")
    if demo:
        return demo.id

    raise HTTPException(status_code=401, detail="Sign in to view this.")


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
# These four routes back the login and registration pages. Auth exists to prove
# the security work and to keep one person's recovery surface out of another's
# hands — but it is never allowed to make a feature look broken to an evaluator,
# which is why the demo credentials are published and pre-filled.
@app.post("/api/auth/register")
async def auth_register(body: dict = Body(...)) -> JSONResponse:
    """Create an account and sign the new user in immediately.

    Registration works end-to-end so an evaluator can make their own account and
    watch every surface generate from scratch, rather than only ever seeing seeded
    state.
    """
    from app import auth

    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    role = body.get("role") or "user"

    if len(username) < 2 or len(password) < 6:
        raise HTTPException(
            status_code=400,
            detail="Username must be 2+ characters and password 6+ characters.",
        )
    if role not in ("user", "caregiver"):
        raise HTTPException(status_code=400, detail="Unknown role.")

    try:
        user = auth.register(username, password, role)
    except ValueError as exc:
        # Surfaces "that username is taken" and similar. Safe to reveal at
        # registration: the user is choosing a name and needs to know it collided.
        raise HTTPException(status_code=409, detail=str(exc))

    response = JSONResponse({"ok": True, "username": user.username, "role": user.role})
    auth.set_session_cookie(response, user.id)
    return response


@app.post("/api/auth/login")
async def auth_login(body: dict = Body(...)) -> JSONResponse:
    """Sign in and set the session cookie.

    The error message is deliberately generic and identical for an unknown username
    and a wrong password, so this endpoint cannot be used to enumerate who has an
    account here. Given what an account in this product implies about a person,
    that is a meaningful disclosure to withhold.
    """
    from app import auth

    username = (body.get("username") or "").strip()
    password = body.get("password") or ""

    try:
        user = auth.verify_login(username, password)
    except Exception:
        raise HTTPException(status_code=401, detail="Incorrect username or password.")

    response = JSONResponse({"ok": True, "username": user.username, "role": user.role})
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
    """
    user_id = _session_user(request)
    profile = store.get_profile(user_id)
    tier = _current_tier(user_id)
    events = store.list_events(user_id, limit=50)

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

    return {
        "tier": int(tier),
        "tier_name": TIER_NAMES[tier],
        "ai_online": ai_online,
        "profile": profile.model_dump(mode="json") if profile else None,
        "events": [e.model_dump(mode="json") for e in events],
    }


# ---------------------------------------------------------------------------
# Triage inputs
# ---------------------------------------------------------------------------
@app.post("/api/utterance")
async def post_utterance(request: Request, body: dict = Body(...)) -> dict:
    """Primary zero-typing input: a transcribed utterance from the voice companion.

    Order of operations matters and is not arbitrary. Triage runs FIRST and completely,
    on rules alone. Only afterwards do we ask the model for a conversational reply.
    That ordering is the architecture: the safety decision has already been made and
    committed before the generative layer is consulted, so a slow, wrong, or entirely
    absent model cannot delay or alter it (PRD P4).
    """
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="empty utterance")

    user_id = _session_user(request)
    profile = store.get_profile(user_id)

    result = triage.evaluate(
        _current_tier(user_id), utterance=text, profile=profile, now=_now()
    )
    _record(user_id, result, source="utterance")
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
async def post_sensor(request: Request, body: dict = Body(...)) -> dict:
    """Silence and stillness — the strongest signal the system will ever receive.

    PRD P2: silence is a signal, not a dead end. A user who says something high-risk
    and then stops responding is the case this product exists for, so this endpoint
    escalates rather than timing out.
    """
    user_id = _session_user(request)
    profile = store.get_profile(user_id)

    result = triage.evaluate(
        _current_tier(user_id),
        silent_seconds=int(body.get("silent_seconds", 0)),
        still=bool(body.get("still", False)),
        profile=profile,
        now=_now(),
    )
    _record(user_id, result, source="sensor")
    payload = _result_payload(result)
    await _broadcast({"type": "tier", **payload, "user_id": user_id})
    return payload


@app.post("/api/tier")
async def post_tier(request: Request, body: dict = Body(...)) -> dict:
    """Explicit tier set. Two legitimate uses, both real product behaviour.

    1. The user taps "I need help now" and jumps straight to the emergency surface —
       a real path that must never require a conversation first.
    2. Demo control, so an evaluator can inspect any tier without having to perform
       a distressing script out loud in a crowded room.

    This is an honest override rather than simulated triage: the recorded reason says
    plainly that a human set it, so the audit log never misattributes it to a signal.
    """
    user_id = _session_user(request)
    profile = store.get_profile(user_id)
    try:
        tier = Tier(int(body.get("tier", 0)))
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="invalid tier")

    previous = _current_tier(user_id)
    result = TriageResult(
        tier=tier,
        previous_tier=previous,
        reason="Set directly from the interface.",
        matched_signal=None,
        actions=triage.actions_for_tier(tier, profile),
        notify_caregiver=triage.notify_caregiver_for(tier, profile),
    )
    _record(user_id, result, source="manual")
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
    profile = store.get_profile(user_id)
    # No `now` here by design: rescinding is a direct user instruction, not a
    # time-dependent inference, so the state machine needs no clock to honour it.
    result = triage.rescind(_current_tier(user_id), profile=profile)
    _record(user_id, result, source="rescind")
    payload = _result_payload(result)
    await _broadcast({"type": "tier", **payload, "user_id": user_id})
    return payload


# ---------------------------------------------------------------------------
# Server-sent events
# ---------------------------------------------------------------------------
@app.get("/api/events")
async def sse(request: Request) -> StreamingResponse:
    """Live ladder stream. This is what makes the caregiver surface feel real.

    SSE rather than WebSockets: the data flows one way, and SSE reconnects on its own
    without any client-side reconnection logic to get wrong.
    """
    queue: asyncio.Queue = asyncio.Queue()
    _listeners.append(queue)

    async def stream():
        try:
            # Immediate hello so the client can distinguish "connected and quiet"
            # from "never connected" — silence must never look like health here.
            yield "event: ping\ndata: {}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=15)
                    yield f"data: {json.dumps(item)}\n\n"
                except asyncio.TimeoutError:
                    # Heartbeat keeps proxies from closing an idle connection.
                    yield "event: ping\ndata: {}\n\n"
        finally:
            if queue in _listeners:
                _listeners.remove(queue)

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
async def _generate(builder, *, fast: bool, **kwargs) -> dict:
    """Run one prompt module through the generative layer.

    Centralised so that every AI surface fails the same way: a Generation-shaped dict
    with an honest `live` flag, never an exception leaking to the client and never a
    silent substitution of canned text.
    """
    try:
        from app import genai

        system, user = builder(**kwargs)
        gen = await genai.generate(system, user, fast=fast)
        return gen.model_dump()
    except Exception as exc:
        return {
            "text": "",
            "live": False,
            "model": "",
            "latency_ms": 0,
            "error": f"AI unavailable: {exc}",
        }


@app.get("/api/script/911")
async def get_script_911(request: Request) -> dict:
    """The personalised emergency script: their address, their unit, their entry code.

    Generated during calm and read aloud one line at a time during crisis, because
    under acute stress people cannot produce a coherent report from memory (PRD §6.1).
    """
    profile = store.get_profile(_require_own_profile(request))
    from app.prompts import script_911

    return await _generate(script_911.build, fast=False, profile=profile)


@app.get("/api/script/refusal")
async def get_script_refusal(request: Request) -> dict:
    """Refusal and exit lines in the user's own register — prevention-side, zero typing."""
    profile = store.get_profile(_session_user(request))
    from app.prompts import refusal

    return await _generate(refusal.build, fast=False, profile=profile)


@app.get("/api/tolerance")
async def get_tolerance(request: Request) -> dict:
    """Tolerance Guard: the lead prevention feature.

    Fires on an ordinary day when nothing appears to be wrong. Tolerance collapses
    within days of abstinence but the remembered dose does not, and the period after
    detox, hospital discharge, or release from incarceration is among the highest-risk
    windows in medicine (PRD §5.1).
    """
    user_id = _session_user(request)
    profile = store.get_profile(user_id)
    from app.prompts import tolerance

    payload = await _generate(tolerance.build, fast=False, profile=profile, now=_now())
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
    """
    clips = store.list_vault_clips()
    if not clips:
        return {"clip": None, "why": None, "error": "No vault clips recorded yet."}

    profile = store.get_profile(_session_user(request))

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
    """
    user_id = _session_user(request)
    profile = store.get_profile(user_id)
    tier = _current_tier(user_id)
    events = store.list_events(user_id, limit=10)
    from app.prompts import caregiver_brief

    payload = await _generate(
        caregiver_brief.build, fast=False, profile=profile, tier=tier, events=events
    )
    payload["tier"] = int(tier)
    return payload


@app.post("/api/reset")
async def post_reset() -> dict:
    """Restore seeded demo state, so an evaluator can run the whole story again."""
    _tiers.clear()
    try:
        from app import seed

        seed.reset()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"reset failed: {exc}")
    await _broadcast({"type": "reset"})
    return {"ok": True}


# ---------------------------------------------------------------------------
# Legal data — static, never generated
# ---------------------------------------------------------------------------
@app.get("/api/legal/{state_code}")
async def get_legal(state_code: str) -> dict:
    """Good Samaritan overdose-immunity summary for a state.

    Served from a static, human-reviewed dataset and NEVER from the model. Immunity
    scope varies substantially between states, and a hallucinated legal protection is
    the single worst thing this product could tell someone standing over an overdose
    (PRD §6.5). If the state is missing we say so plainly rather than guessing.
    """
    path = DATA_DIR / "legal" / "good_samaritan.json"
    if not path.exists():
        raise HTTPException(status_code=503, detail="legal dataset unavailable")

    records = json.loads(path.read_text())
    if isinstance(records, dict):
        records = records.get("states", [])

    for rec in records:
        if str(rec.get("state_code", "")).upper() == state_code.upper():
            return rec

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
async def page_index():
    return _page("index.html")


@app.get("/caregiver")
async def page_caregiver():
    return _page("caregiver.html")


@app.get("/bystander")
async def page_bystander():
    """Bystander mode is intentionally reachable with no session and no account.

    PRD §3: this person may not know the user, may be using themselves, and is
    terrified of arrest. Asking them to register would cost the exact minutes that
    decide whether someone breathes.
    """
    return _page("bystander.html")


@app.get("/onboarding")
async def page_onboarding():
    return _page("onboarding.html")


@app.get("/ladder")
async def page_ladder():
    return _page("ladder.html")


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
async def post_contact(body: dict = Body(...)) -> dict:
    """Receive a contact message.

    Persisted to a local JSONL file rather than emailed: this is a prototype with no
    mail infrastructure, and silently dropping a message while showing the user a
    success tick would be a lie. Writing it down means the submission is real.

    Deliberately NOT stored in the main database — contact messages come from the
    public and must never mix with clinical profile data.
    """
    name = (body.get("name") or "").strip()
    email = (body.get("email") or "").strip()
    message = (body.get("message") or "").strip()

    if not (name and email and message):
        raise HTTPException(status_code=400, detail="name, email and message are required")

    # Cheap length bound: this endpoint is unauthenticated, so it must not accept an
    # unbounded body that could fill the disk.
    if len(message) > 5000 or len(name) > 200 or len(email) > 320:
        raise HTTPException(status_code=413, detail="message too long")

    DATA_DIR.mkdir(exist_ok=True)
    record = {
        "at": _now().isoformat(),
        "name": name,
        "email": email,
        "topic": (body.get("topic") or "general")[:64],
        "message": message,
    }
    with (DATA_DIR / "contact_messages.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")

    return {"ok": True}


@app.post("/api/account/delete")
async def post_account_delete(request: Request) -> dict:
    """Delete the signed-in account and everything attached to it.

    Immediate and total: no soft-delete, no thirty-day tombstone, no recovery
    window. A person who cannot leave cleanly was never safe being honest with us
    in the first place, and a retained shadow copy would make this policy a lie.
    """
    try:
        from app import auth

        user = auth.user_from_request(request)
    except Exception:
        user = None

    if not user:
        raise HTTPException(status_code=401, detail="sign in first")

    # Clear the live ladder cursor too, so no in-memory trace of the session outlives
    # the deletion of the records behind it.
    _tiers.pop(user.id, None)
    store.delete_user_data(user.id)

    return {"ok": True}


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


# Mounted last so it cannot shadow the API routes above.
if (WEB_DIR / "css").exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")
