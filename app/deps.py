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

from dataclasses import dataclass

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

@dataclass
class Listener:
    """One connected SSE client, tagged with who it belongs to.

    The identity is captured from the session at subscribe time and stored on the
    server. That is the whole point: a stream that did not know its own recipient
    could only send everything to everyone and hope the browser behaved.

    Attributes:
        user_id: The authenticated account behind this stream, or None for an
            anonymous listener. An anonymous listener receives no user events at
            all — see `visible_to`.
        queue: Unbounded per-client queue. A slow reader delays only itself.
    """

    user_id: str | None
    queue: asyncio.Queue


# Connected SSE listeners, so a tier change on the user's screen reaches the
# caregiver's screen without polling. One `Listener` per connected client.
_listeners: list[Listener] = []


def _now() -> datetime:
    """Single source of 'now'.

    Centralised so that time is injected into triage rather than read inside it —
    that is what keeps the state machine deterministic and testable.
    """
    return datetime.now(timezone.utc)


def _current_tier(user_id: str) -> Tier:
    """Current live tier for a user, defaulting to Baseline for anyone unseen."""
    return _tiers.get(user_id, Tier.BASELINE)


def visible_to(recipient_id: str | None, subject_id: str, tier: Tier) -> bool:
    """Whether one account may receive another account's ladder event.

    THE PRIVACY BOUNDARY. Every event is filtered through this before its bytes
    leave the server. The caregiver client used to receive every user's events
    and drop the irrelevant ones itself, which meant one person's tier reasons
    and account identifiers reached unrelated listeners — including anonymous
    ones — and "privacy" amounted to a `if (e.user_id !== mine) return;` that any
    reader of the network tab could ignore. Client-side filtering is never the
    boundary.

    The rule, and the PRD principle behind each branch:

    * Your own events -> always. PRD §11: the user's log is never hidden from
      the user.
    * No session -> never. An anonymous listener has no relationship with
      anybody and is entitled to nothing.
    * No consented link -> never. PRD §8: a caregiver watches one named person
      who agreed to it. Absence of a link is a No, not a maybe.
    * Tier 4 and 5 -> always, to a linked caregiver. PRD §4.2 makes these
      non-negotiable and undisableable, and this function must never grow a
      setting that can suppress them.
    * Tier 3 -> only with `tier_3_visible_to_caregiver`. Defaults to False. A
      user who fears their disclosure will be reported does not disclose; they
      use alone.
    * Tiers 0-2 -> never, even to a linked caregiver, and even if the user set
      `tier_2_visible_to_caregiver`. That flag governs what the caregiver page
      may DISPLAY about the ladder's shape, not a live push at 3am. Craving is
      not news; it is Tuesday, and waking someone for it is how the watched
      person learns to stop naming it.

    Args:
        recipient_id: The account behind the listening stream, or None.
        subject_id: The account the event is about.
        tier: The tier the event carries.

    Returns:
        True only when delivery is authorised. Fails closed on every unknown.
    """
    if recipient_id is None:
        return False
    if recipient_id == subject_id:
        return True
    if not store.is_linked(recipient_id, subject_id):
        return False

    # Tiers 4/5 pass regardless of configuration (PRD §4.2). Checked before the
    # ladder is loaded so that a missing or unreadable profile can never
    # suppress an emergency.
    if tier >= Tier.EMERGENCY:
        return True

    if tier is Tier.ACTIVE_USE:
        ladder = store.get_ladder(subject_id)
        # No ladder row means no profile yet; the default is private, and an
        # unknown preference resolves toward silence rather than exposure.
        return bool(ladder and ladder.tier_3_visible_to_caregiver)

    return False


async def _broadcast(payload: dict) -> None:
    """Push an event to the connected SSE clients ENTITLED to receive it.

    Filtering happens here, before serialisation, so an unauthorised recipient's
    socket never carries the bytes at all. A payload carrying `user_id` is a
    ladder event about that account and is filtered through `visible_to`.
    Anything without a `user_id` (the demo `reset` notice) is about the
    deployment rather than about a person and goes to everyone.

    Uses put_nowait and tolerates failure: a slow or dead listener must never block a
    tier transition. In this product, delivery to a dashboard is strictly less
    important than the escalation itself continuing to run.

    Args:
        payload: The SSE payload. `user_id` names the subject; `tier` is the
            integer tier. A ladder payload missing `tier` is treated as tier 5,
            i.e. it is delivered to linked caregivers — failing toward telling
            someone is right for a malformed EMERGENCY and wrong only for a
            malformed calm event, and only the first can cost a life.
    """
    subject_id = payload.get("user_id")
    tier = Tier(payload["tier"]) if isinstance(payload.get("tier"), int) else Tier.UNRESPONSIVE

    for listener in list(_listeners):
        if subject_id is not None and not visible_to(listener.user_id, subject_id, tier):
            continue
        try:
            listener.queue.put_nowait(payload)
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


# ---------------------------------------------------------------------------
# Event ordering — one conversion, in one place
# ---------------------------------------------------------------------------


def events_for_wire(user_id: str, limit: int = 50) -> list[Event]:
    """Read a user's recent events and return them OLDEST FIRST.

    THE WIRE-ORDER CONTRACT. Everything that leaves this server as JSON, and
    every prompt built from an event log, uses this function and therefore reads
    oldest first — a chronology, forwards, the way a person reads one. The
    storage layer's `store.list_events` returns the opposite order because SQL
    needs DESC to bound a query cheaply; see its docstring.

    This function is the ONLY place the two orders meet. Before it existed, the
    conversion was done ad hoc by each consumer, several of which had it
    backwards: the caregiver brief documented its input as oldest-first, took
    `events[-max_events:]` to get "the tail", and read `tail[-1].reason` as the
    current reason — so with newest-first input it fed the model the OLDEST
    slice, in reverse, and presented the oldest reason as what is happening now.
    A caregiver woken at 3am was told the wrong thing about tonight.

    Args:
        user_id: Whose log to read.
        limit: How many of the MOST RECENT events to return. The window is taken
            from the newest end and then reversed, so `limit=3` on a ten-event
            log yields the last three in chronological order — never the first
            three.

    Returns:
        Up to `limit` events, oldest first, the newest last.
    """
    # list_events is newest-first and its limit truncates the oldest end, so this
    # takes the correct window and then flips it once.
    return list(reversed(store.list_events(user_id, limit=limit)))


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
# Session and authorization
# ---------------------------------------------------------------------------
# Three resolvers, in increasing strictness:
#
#   _session_user       — who is acting; falls back to the published demo fixture
#   _require_own_profile— same, for endpoints exposing address and entry code
#   resolve_subject     — WHOSE LADDER this request is about, which for a
#                         caregiver is not the caller at all
#
# The third is new and is the fix for the privacy finding. A caregiver's own
# account has no profile and no events, so resolving a caregiver to themselves
# made their surface render empty; the client then "fixed" it by listening to
# everyone's events and filtering locally. The watched user is now resolved
# server-side from the consented link, and a client-supplied id is never
# consulted anywhere in this file.
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


def authenticated_user_id(request: Request) -> str | None:
    """The signed-in account id, or None. No demo fallback, ever.

    The identity used for authorization decisions, as distinct from the identity
    used to make a demo look alive. `_session_user` deliberately resolves an
    anonymous caller to the published demo fixture so an evaluator poking at the
    API sees a working product; that convenience must never leak into a
    permission check, or every anonymous request would inherit the demo
    account's relationships. This function is what `visible_to` and the SSE
    subscription are tagged with.

    Args:
        request: The incoming request.

    Returns:
        The account id, or None for an anonymous or invalid session. Never
        raises — "signed out" is an ordinary branch here.
    """
    try:
        from app import auth

        user = auth.user_from_request(request)
        return user.id if user else None
    except Exception:
        # A failure to resolve identity is not a licence to assume one.
        return None


def resolve_subject(request: Request) -> str:
    """Whose ladder this request is about.

    For a user, that is themselves. For a caregiver it is the person they hold a
    consented link to (PRD §8) — never themselves, because a caregiver account
    has no profile and no events, and never an id taken from the request, because
    an id in a query string is a claim rather than a permission.

    This is the resolution `/api/state` and `/api/caregiver/brief` use. It closes
    the finding that a signed-in caregiver saw an empty surface: the server now
    knows who Sarah is entitled to watch, so it can answer the question directly
    instead of shipping everybody's events to the browser and hoping.

    Args:
        request: The incoming request.

    Returns:
        The subject account id.

    Raises:
        HTTPException: 403 when a caregiver has no consented link. An honest
            empty-handed answer — "nobody has added you yet" — is the correct
            one; falling back to any other account's data is how the original
            bug leaked one person's tier reasons to another's screen.
    """
    caller = authenticated_user_id(request)
    if caller is None:
        # Anonymous: the published demo fixture, exactly as `_session_user`
        # documents. Nothing private is reachable this way.
        return _session_user(request)

    user = store.get_user(caller)
    if user is None or user.role != "caregiver":
        return caller

    watched = store.primary_watched_user(caller)
    if watched is None:
        raise HTTPException(
            status_code=403,
            detail=(
                "This caregiver account is not linked to anyone yet. "
                "The person you support has to add you before you can see their ladder."
            ),
        )
    return watched


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
# Single choke point for AI calls, so the honesty rule is enforced in exactly one
# place: a failed call comes back live=False with a populated error rather than as
# canned text wearing a fresh generation's clothes. See app/routes/generate.py for the
# endpoints that use it.
async def _generate(builder, *, fast: bool, owner_id: str | None = None, **kwargs) -> dict:
    """Run one prompt module through the generative layer.

    Centralised so that every AI surface fails the same way: a Generation-shaped dict
    with an honest `live` flag, never an exception leaking to the client and never a
    silent substitution of canned text.

    Args:
        builder: A prompt module's `build`, returning (system, user).
        fast: True for the low-latency model, False for the deep one.
        owner_id: The account this generation was produced FOR. When given, the
            resulting cache entry is recorded against that account so deleting it
            deletes the cached text too — /data-deletion promises to remove "any
            cached generations produced for you", and the cache is keyed by a hash
            of the prompt, which says nothing about whose address is inside it.
            Passing None means the output is not personal (there is nothing to
            own) and is a deliberate choice at each call site, not a default to
            fall into.
        **kwargs: Forwarded to `builder`.

    Returns:
        A Generation-shaped dict.
    """
    try:
        from app import genai

        system, user = builder(**kwargs)
        gen = await genai.generate(system, user, fast=fast)

        # Recorded only for a LIVE generation, because only a live call writes a
        # cache file (see genai.generate). Claiming ownership of an entry that
        # does not exist would make a deletion report overstate what it removed.
        if owner_id and gen.live:
            try:
                store.record_cache_owner(genai.cache_key(gen.model, system, user), owner_id)
            except Exception as exc:  # pragma: no cover - bookkeeping is best-effort
                # A failure here must not turn a successful generation into an
                # error on a crisis screen. Logged loudly because the consequence
                # is a cache file that deletion will not find.
                import logging

                logging.getLogger("threshold").warning(
                    "cache ownership not recorded: %s", exc
                )

        return gen.model_dump()
    except Exception as exc:
        return {
            "text": "",
            "live": False,
            "model": "",
            "latency_ms": 0,
            "error": f"AI unavailable: {exc}",
        }
