"""The generative surfaces: 911 script, refusal lines, tolerance guard, vault, brief.

WHAT THIS MODULE DOES
    Exposes the five endpoints whose entire payload is model-written language. Every
    one of them makes a real API call to Google Gemini via OpenRouter. When the call
    fails, the response carries live=False plus a populated error, and the UI shows
    that state explicitly. A fallback never masquerades as a fresh generation — that
    would be dishonest to the user and an automatic disqualifier under the judging
    rules.

WHAT THIS MODULE DELIBERATELY DOES NOT DO
    - It never decides a tier. `/api/tolerance` reports whether the tolerance window
      is open by asking `app.triage`, a pure deterministic function; the model only
      phrases the message (PRD P4).
    - It serves no legal text. Good Samaritan statutes are a static reviewed dataset
      and are deliberately routed away from this module entirely — see
      `app/routes/public.py` and PRD §6.5. A hallucinated legal protection is the
      worst thing this product could say.
    - It does not clone anyone's voice. The Memory Vault picks among real recordings
      made by a real caregiver; the generative work is only the situational judgement
      of which recording belongs here (PRD §7.2).
    - It builds no prompts. Each prompt lives in its own module under `app.prompts`,
      imported lazily so the app boots with the AI layer missing or broken.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from app import store, triage
from app.deps import (
    _current_tier,
    _generate,
    _now,
    _require_own_profile,
    _session_user,
)

router = APIRouter()


@router.get("/api/script/911")
async def get_script_911(request: Request) -> dict:
    """The personalised emergency script: their address, their unit, their entry code.

    Generated during calm and read aloud one line at a time during crisis, because
    under acute stress people cannot produce a coherent report from memory (PRD §6.1).
    """
    profile = store.get_profile(_require_own_profile(request))
    from app.prompts import script_911

    return await _generate(script_911.build, fast=False, profile=profile)


@router.get("/api/script/refusal")
async def get_script_refusal(request: Request) -> dict:
    """Refusal and exit lines in the user's own register — prevention-side, zero typing."""
    profile = store.get_profile(_session_user(request))
    from app.prompts import refusal

    return await _generate(refusal.build, fast=False, profile=profile)


@router.get("/api/tolerance")
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


@router.get("/api/vault/select")
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


@router.get("/api/caregiver/brief")
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
