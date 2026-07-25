"""Two-step, email-verified account registration."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from app import auth, email as email_delivery, store
from app.models import UserProfile

CODE_TTL = timedelta(minutes=10)
MAX_ATTEMPTS = 6


class RegistrationError(Exception):
    """A registration failure safe to display to the caller."""


@dataclass(frozen=True)
class RegistrationResult:
    """A verified account plus any non-fatal invite-link warning."""

    user: store.UserRecord
    watching: str | None = None
    link_error: str | None = None


def _code_digest(email: str, code: str) -> str:
    """Key a low-entropy numeric code so a database leak cannot brute-force it."""
    secret = os.getenv("THRESHOLD_SECRET", "").encode("utf-8")
    if not secret:
        raise RegistrationError(
            "Account verification requires THRESHOLD_SECRET on this deployment."
        )
    message = f"{email.strip().lower()}:{code}".encode("utf-8")
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


async def begin(
    *,
    email: str,
    full_name: str,
    phone: str,
    password: str,
    role: str,
    invite_code: str = "",
    now: datetime | None = None,
) -> store.PendingRegistration:
    """Hash credentials, save a pending row, and send its one-time code."""
    email = email.strip().lower()
    full_name = full_name.strip()
    phone = phone.strip()
    when = now or datetime.now()

    if store.get_user_by_email(email):
        raise RegistrationError("That email already has an account.")
    if role not in auth.VALID_ROLES:
        raise RegistrationError("Unknown account role.")
    if invite_code and role != "caregiver":
        raise RegistrationError("Invite codes are redeemed by caregivers.")
    if invite_code:
        invite = store.get_invite(invite_code)
        if invite is None or invite.is_spent or invite.is_expired(when):
            raise RegistrationError(
                "That invite code is not valid. Ask for a fresh code."
            )

    password_hash, salt = auth.hash_password(password)
    code = f"{secrets.randbelow(1_000_000):06d}"
    pending = store.PendingRegistration(
        id=uuid.uuid4().hex,
        email=email,
        # Kept only as an internal compatibility key for the existing schema.
        # New accounts authenticate with email and never see or choose this.
        username=email,
        password_hash=password_hash,
        salt=salt,
        role=role,
        invite_code=invite_code.strip(),
        full_name=full_name,
        phone=phone,
        code_digest=_code_digest(email, code),
        expires_at=when + CODE_TTL,
        attempts=0,
        created_at=when,
    )
    try:
        store.put_pending_registration(pending)
    except sqlite3.IntegrityError as exc:
        raise RegistrationError("That username or email is already pending.") from exc

    sent, error = await email_delivery.send_verification_code(
        email, code, idempotency_key=f"threshold-signup/{pending.id}"
    )
    if not sent:
        store.delete_pending_registration(email)
        raise RegistrationError(error or "The verification email could not be sent.")
    return pending


def complete(
    email: str, code: str, *, now: datetime | None = None
) -> RegistrationResult:
    """Verify a pending code, create the account, and consume the pending row."""
    email = email.strip().lower()
    pending = store.get_pending_registration(email)
    when = now or datetime.now()
    if pending is None:
        raise RegistrationError("No pending signup was found for that email.")
    if pending.expires_at < when:
        store.delete_pending_registration(email)
        raise RegistrationError("That code expired. Request a new one.")
    if pending.attempts >= MAX_ATTEMPTS:
        store.delete_pending_registration(email)
        raise RegistrationError("Too many attempts. Request a new code.")
    if not hmac.compare_digest(pending.code_digest, _code_digest(email, code)):
        attempts = store.increment_pending_attempts(email)
        remaining = max(0, MAX_ATTEMPTS - attempts)
        raise RegistrationError(
            f"That code is not correct. {remaining} attempt"
            f"{'s' if remaining != 1 else ''} remaining."
        )

    try:
        user = store.create_user(
            id=uuid.uuid4().hex,
            username=pending.username,
            password_hash=pending.password_hash,
            salt=pending.salt,
            role=pending.role,
            email=pending.email,
            email_verified=True,
            full_name=pending.full_name,
            phone=pending.phone,
        )
    except sqlite3.IntegrityError as exc:
        raise RegistrationError("That username or email is already registered.") from exc

    # A member needs a profile immediately so onboarding/state never lands on
    # a missing-record error. Sensitive fields start empty and are user-owned.
    if pending.role == "user":
        store.put_profile(
            user.id,
            UserProfile(
                id=f"profile-{user.id}",
                name=pending.full_name,
                address="",
            ),
        )

    watching: str | None = None
    link_error: str | None = None
    if pending.invite_code and pending.role == "caregiver":
        try:
            watched_id = store.redeem_invite(pending.invite_code, user.id, when)
            watched = store.get_user(watched_id)
            watching = watched.full_name or watched.username if watched else None
        except store.InviteError as exc:
            # The account is verified and usable; a code expiring during email
            # verification must not discard it. The caregiver can redeem a new
            # invite after signing in.
            link_error = f"Your account is verified, but the invite could not be used: {exc}"

    store.delete_pending_registration(email)
    return RegistrationResult(user=user, watching=watching, link_error=link_error)
