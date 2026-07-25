"""Transactional email delivery through Resend.

Only verification email is sent here. The API key and sender stay in
environment variables, failures are returned as data, and no caller logs a
verification code.
"""

from __future__ import annotations

import os

import httpx

_API_URL = "https://api.resend.com/emails"
_TIMEOUT = httpx.Timeout(connect=4.0, read=10.0, write=10.0, pool=4.0)
_client: httpx.AsyncClient | None = None


def _sender() -> str:
    """Resolve the configured sender, accepting common deployment names."""
    return (
        os.getenv("THRESHOLD_EMAIL_FROM", "").strip()
        or os.getenv("RESEND_FROM_EMAIL", "").strip()
        or "Threshold <onboarding@resend.dev>"
    )


def is_online() -> bool:
    """Whether the Resend API key is present."""
    return bool(os.getenv("RESEND_API_KEY", "").strip())


def _http() -> httpx.AsyncClient:
    """Return the process-wide Resend client."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=_TIMEOUT)
    return _client


async def close() -> None:
    """Close the shared client during application shutdown."""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


async def send_verification_code(
    email: str, code: str, *, idempotency_key: str
) -> tuple[bool, str | None]:
    """Send one short-lived signup code without exposing provider details."""
    key = os.getenv("RESEND_API_KEY", "").strip()
    sender = _sender()
    if not key:
        return False, "Email verification is not configured on this deployment."

    try:
        response = await _http().post(
            _API_URL,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "Idempotency-Key": idempotency_key[:256],
                "User-Agent": "Threshold/1.0",
            },
            json={
                "from": sender,
                "to": [email],
                "subject": "Your Threshold verification code",
                "text": (
                    f"Your Threshold verification code is {code}. "
                    "It expires in 10 minutes. If you did not request this, "
                    "you can ignore this email."
                ),
            },
        )
        if response.is_success:
            return True, None
        return False, "The verification email provider refused the request."
    except httpx.TimeoutException:
        return False, "The verification email timed out. Please try again."
    except httpx.HTTPError:
        return False, "The verification email could not be sent. Please try again."


async def send_caregiver_alert(
    email: str,
    member_name: str,
    *,
    tier_name: str,
    idempotency_key: str,
) -> tuple[bool, str | None]:
    """Deliver a real emergency alert without including private clinical data."""
    key = os.getenv("RESEND_API_KEY", "").strip()
    if not key:
        return False, "Caregiver email delivery is not configured."
    try:
        response = await _http().post(
            _API_URL,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "Idempotency-Key": idempotency_key[:256],
                "User-Agent": "Threshold/1.0",
            },
            json={
                "from": _sender(),
                "to": [email],
                "subject": f"Threshold {tier_name.lower()} alert for {member_name}",
                "text": (
                    f"{member_name}'s Threshold ladder reached {tier_name}. "
                    "Open the caregiver page now. If they may be unresponsive "
                    "or not breathing normally, call emergency services."
                ),
            },
        )
        return (
            (True, None)
            if response.is_success
            else (False, "The caregiver alert provider refused the request.")
        )
    except httpx.HTTPError:
        return False, "The caregiver alert could not be delivered."
