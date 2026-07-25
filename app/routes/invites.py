"""Caregiver invitation lifecycle.

This router is the only HTTP surface that can create a caregiver relationship.
Consent flows outward from the member: a caregiver can redeem a code they were
given, but cannot name or search for an account to watch.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from datetime import datetime

from fastapi import APIRouter, Body, HTTPException, Request

from app import email as email_delivery, store
from app.deps import authenticated_user_id
from app.schemas import (
    InviteCreateRequest,
    InviteRedeemRequest,
    InviteResendRequest,
)

router = APIRouter(tags=["caregiver invitations"])
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


def _now() -> datetime:
    """Return a naive timestamp matching the existing SQLite invite records."""
    return datetime.now()


async def _member(request: Request):
    """Return the authenticated member or fail without leaking account details."""
    user_id = authenticated_user_id(request)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Sign in to manage invitations.")
    user = await asyncio.to_thread(store.get_user, user_id)
    if not user or user.role != "user":
        raise HTTPException(
            status_code=403,
            detail="Only the person being supported can manage invitations.",
        )
    return user


@router.post("/api/invite")
async def create_invite(
    request: Request,
    body: InviteCreateRequest | None = Body(default=None),
) -> dict:
    """Create a one-use, 24-hour code and optionally deliver it through Resend."""
    member = await _member(request)
    email = (body.email if body else "").strip().lower()
    if email and not _EMAIL.fullmatch(email):
        raise HTTPException(status_code=422, detail="Enter a valid caregiver email.")

    invite = await asyncio.to_thread(
        store.create_invite,
        member.id,
        now=_now(),
        invited_email=email,
    )
    delivered, delivery_error = False, None
    if email:
        delivered, delivery_error = await email_delivery.send_caregiver_invitation(
            email,
            member.full_name or "Someone you care about",
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


@router.get("/api/invites")
async def list_invites(request: Request) -> dict:
    """Return issued codes and caregivers who completed the consent flow."""
    member = await _member(request)
    now = _now()
    invites = await asyncio.to_thread(store.list_invites, member.id)
    caregiver_ids = await asyncio.to_thread(store.caregivers_for, member.id)
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
            {"full_name": caregiver.full_name, "email": caregiver.email}
            for caregiver in caregivers
            if caregiver
        ],
    }


@router.post("/api/invite/resend")
async def resend_invite(request: Request, body: InviteResendRequest) -> dict:
    """Redeliver an active code while retaining the same expiry and consent."""
    member = await _member(request)
    invite = await asyncio.to_thread(store.get_invite, body.code)
    if not invite or invite.user_id != member.id:
        raise HTTPException(status_code=404, detail="Invitation not found.")
    if invite.is_spent:
        raise HTTPException(status_code=409, detail="This invitation has already been used.")
    if invite.is_expired(_now()):
        raise HTTPException(
            status_code=410,
            detail="This invitation has expired. Create a new one.",
        )

    email = (body.email or invite.invited_email).strip().lower()
    if not _EMAIL.fullmatch(email):
        raise HTTPException(status_code=422, detail="Enter a valid caregiver email.")
    await asyncio.to_thread(store.set_invite_email, invite.code, member.id, email)
    delivered, error = await email_delivery.send_caregiver_invitation(
        email,
        member.full_name or "Someone you care about",
        invite.code,
        idempotency_key=f"invite-resend-{invite.code}-{uuid.uuid4().hex}",
    )
    if not delivered:
        raise HTTPException(status_code=503, detail=error or "Invitation could not be sent.")
    return {"ok": True, "code": invite.code, "email": email, "email_sent": True}


@router.post("/api/invite/redeem")
async def redeem_invite(request: Request, body: InviteRedeemRequest) -> dict:
    """Redeem a member-issued code as the authenticated caregiver."""
    caller = authenticated_user_id(request)
    if caller is None:
        raise HTTPException(status_code=401, detail="Sign in to redeem an invite code.")
    try:
        watched_id = await asyncio.to_thread(
            store.redeem_invite,
            body.code,
            caller,
            _now(),
        )
    except store.InviteError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    watched = await asyncio.to_thread(store.get_user, watched_id)
    return {"ok": True, "watching": watched.username if watched else None}
