"""Endpoints backing the public pages: contact, account deletion, and legal lookup.

WHAT THIS MODULE DOES
    Serves the three routes that the unauthenticated marketing and policy pages call:
    the contact form, the account self-deletion action, and the Good Samaritan
    statute lookup that the bystander surface reads.

WHAT THIS MODULE DELIBERATELY DOES NOT DO
    - It never generates legal text. `/api/legal/{state_code}` reads a static,
      human-reviewed dataset and says plainly that it does not know when a state is
      missing. Immunity scope varies substantially between states, and a hallucinated
      legal protection is the single worst thing this product could tell someone
      standing over an overdose (PRD §6.5). This module does not import `app.genai`
      at all, so that guarantee is structural rather than a matter of care.
    - It does not soft-delete. Account deletion is immediate and total; there is no
      tombstone and no recovery window. See `post_account_delete`.
    - It does not put contact messages in the clinical database. Public submissions
      and profile data never share a store.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Body, HTTPException, Request

from app import store
from app.deps import DATA_DIR, _now, _tiers

router = APIRouter()


@router.post("/api/contact")
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


@router.post("/api/account/delete")
async def post_account_delete(request: Request) -> dict:
    """Delete the signed-in account and everything attached to it.

    Immediate and total: no soft-delete, no thirty-day tombstone, no recovery
    window. A person who cannot leave cleanly was never safe being honest with us
    in the first place, and a retained shadow copy would make this policy a lie.

    FOUR PLACES, because /data-deletion names four things and the page has to be
    true. Deleting the database rows alone left the two most sensitive artefacts
    behind:

      1. Database — profile, ladder, contacts, tolerance events, event log,
         caregiver links, and the user's own Memory Vault clips (`store`).
      2. Disk cache — cached generations produced for this account, including a
         911 script containing their home address and door entry code. The cache
         is keyed by a prompt hash, so an owner index is what makes this findable
         at all.
      3. Live in-memory state — the ladder cursor and any open SSE listener
         tagged with this account. A stream left attached to a deleted user would
         keep receiving their own events, and the tier cursor would survive the
         records behind it.
      4. The session cookie — cleared on the response, so the browser is not left
         holding a signed token naming an account that no longer exists.
    """
    try:
        from app import auth

        user = auth.user_from_request(request)
    except Exception:
        user = None

    if not user:
        raise HTTPException(status_code=401, detail="sign in first")

    # Read the cache keys BEFORE the rows go: `delete_user_data` removes the
    # ownership index, after which the files are unreachable orphans.
    from app import genai

    cache_keys = store.cache_keys_exclusively_owned_by(user.id)

    counts = store.delete_user_data(user.id)

    removed = 0
    for key in cache_keys:
        if genai.cache_delete(key):
            removed += 1

    # Live state, both halves. The cursor and the listener list are the app's only
    # in-memory trace of a person.
    _tiers.pop(user.id, None)
    for listener in [l for l in _listeners if l.user_id == user.id]:
        # Detached rather than closed: the generator owns its own lifecycle and
        # removing it here means the next broadcast cannot reach it. The stream
        # ends on its own when the client disconnects.
        _listeners.remove(listener)

    response = JSONResponse(
        {
            "ok": True,
            # Reported so the confirmation screen can state what was actually
            # removed rather than asserting it generically. A deletion the user
            # cannot verify is one they have to take on trust, which is the exact
            # thing this page exists to avoid asking of them.
            "deleted": {**counts, "generation_cache_files": removed},
        }
    )
    auth.clear_session_cookie(response)
    return response


# ---------------------------------------------------------------------------
# Legal data — static, never generated
# ---------------------------------------------------------------------------
@router.get("/api/legal/{state_code}")
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
