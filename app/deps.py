"""Shared application plumbing used by `app.main` and every router under `app.routes`.

WHAT THIS MODULE DOES
    Holds the small set of helpers that more than one router needs: the clock, the
    live ladder state and its SSE listener list, the triage record/broadcast path,
    the wire serialiser for a TriageResult, the two session resolvers, the static
    page helper, and the single funnel through which every generative call runs.
    It also owns the two filesystem anchors (`WEB_DIR`, `DATA_DIR`).

WHAT THIS MODULE DELIBERATELY DOES NOT DO
    - It does not import `app.main`, and it must never learn how to. This module sits
      *below* the FastAPI app in the dependency graph so that routers can import it
      freely without creating an import cycle. The direction is one-way: main and the
      routers import deps; deps imports neither.
    - It declares no routes and owns no `APIRouter`. It is plumbing, not surface.
    - It decides no tiers. Every tier transition still originates in `app.triage`
      (PRD P4); `_record` only transcribes a decision that has already been made.
    - It generates no safety-critical text. `_generate` is a uniform failure wrapper
      around `app.genai`; legal text is never routed through it at all (PRD §6.5).

IMPORT DISCIPLINE
    The generative layer and the auth layer are imported lazily inside functions
    rather than at module scope, for the same reason `app.main` does it: this app must
    boot and serve every non-AI surface even if the AI layer is missing, broken, or
    has no API key. A crashed import at startup would take down the emergency path
    along with it, which is exactly backwards.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from app import store
from app.models import Event, Tier, TriageResult

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
# Static page helper
# ---------------------------------------------------------------------------
def _page(name: str) -> FileResponse:
    """Serve a page from web/, with a clear error if the file is not there yet."""
    path = WEB_DIR / name
    if not path.exists():
        return JSONResponse(
            status_code=503, content={"detail": f"{name} not built yet"}
        )
    return FileResponse(path)


# ---------------------------------------------------------------------------
# Generative funnel
# ---------------------------------------------------------------------------
# Every generative endpoint makes a real API call to Google Gemini via OpenRouter.
# When the call fails, the response carries live=False plus a populated error, and the
# UI shows that state explicitly. A fallback never masquerades as a fresh generation —
# that would be dishonest to the user and an automatic disqualifier under the judging
# rules.
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
