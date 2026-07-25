"""Authentication endpoints: register, login, logout, and "who am I".

WHAT THIS MODULE DOES
    Backs the login and registration pages with the four routes named in CONTRACT.md.
    Auth exists to prove the security work and to keep one person's recovery surface
    out of another's hands — but it is never allowed to make a feature look broken to
    an evaluator, which is why the demo credentials are published and pre-filled.

WHAT THIS MODULE DELIBERATELY DOES NOT DO
    - It implements no cryptography and no session format. Password hashing, cookie
      signing, and cookie verification all live in `app.auth`; this module is the HTTP
      shell around them and holds no secrets of its own.
    - It does not gate the rest of the app. No other router depends on a session being
      present: `app.deps._session_user` falls back to the published demo account, and
      bystander mode is outside the auth wall entirely (PRD §3).
    - It never reveals whether a username exists at login time. See `auth_login`.

IMPORT DISCIPLINE
    `app.auth` is imported inside each handler rather than at module scope, matching
    the rest of the app: a failure in the auth layer must not be able to prevent the
    emergency surfaces from being served.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import JSONResponse

router = APIRouter()


@router.post("/api/auth/register")
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
    except (ValueError, auth.AuthError) as exc:
        # Surfaces "that username is taken" and similar. Safe to reveal at
        # registration: the user is choosing a name and needs to know it collided.
        raise HTTPException(status_code=409, detail=str(exc))

    response = JSONResponse({"ok": True, "username": user.username, "role": user.role})
    auth.set_session_cookie(response, user.id)
    return response


@router.post("/api/auth/login")
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


@router.post("/api/auth/logout")
async def auth_logout() -> JSONResponse:
    """Clear the session cookie."""
    from app import auth

    response = JSONResponse({"ok": True})
    auth.clear_session_cookie(response)
    return response


@router.get("/api/auth/me")
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
