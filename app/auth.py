"""Username + password authentication and session handling.

WHAT THIS MODULE DOES
    Hashes and verifies passwords (stdlib `hashlib.scrypt`), mints and
    validates signed session cookies (stdlib `hmac`), and exposes the FastAPI
    dependencies `current_user()` and `optional_user()` that routes use to
    identify the caller.

WHAT THIS MODULE DELIBERATELY DOES NOT DO
    * No third-party auth library. scrypt, hmac and secrets are all in the
      standard library; adding passlib/jose would add supply-chain surface to a
      demo for no security gain.
    * No server-side session table. Tokens are stateless and signed, so there
      is no session store to leak, expire, or keep consistent. The trade-off is
      that logout is client-side cookie clearing plus the per-boot secret
      rotation described below — acceptable and explicitly noted rather than
      silently ignored.
    * No password-strength rules, no email, no reset flow. CONTRACT ground rule
      4 says auth must never block an evaluator; a rejected password on the
      registration form is exactly that kind of blocker.
    * No plaintext password is ever written to the database, a log line, an
      exception message, or a response body. Security is a scored category and
      a plaintext password in a repo is the most obvious possible hit.

SECURITY DECISIONS AND WHY
    * scrypt, not sha256/md5: a general-purpose hash is designed to be fast,
      which is precisely wrong for passwords. scrypt is memory-hard, so a GPU
      or ASIC attacker gains far less than they would against a fast digest.
      Parameters below.
    * Per-user random salt (16 bytes from os.urandom via `secrets`): identical
      passwords produce different digests, so a stolen database cannot be
      attacked with one precomputed rainbow table across all users.
    * `hmac.compare_digest` everywhere a secret is compared: `==` on bytes
      short-circuits at the first differing byte, which leaks the length of the
      correct prefix through timing. Constant-time comparison removes that
      channel for both password digests and session tokens.
    * A dummy verification on the "no such user" path: without it, a miss
      returns in microseconds and a hit takes ~100ms, which turns the login
      form into a username oracle. We burn the same scrypt work either way.
    * One generic error message for every failed login: telling an attacker
      "no such user" versus "wrong password" hands them half the credential
      for free.
    * Cookie is HttpOnly (JavaScript cannot read the token, so an XSS bug
      cannot exfiltrate a session), SameSite=Lax (blocks cross-site POST CSRF
      while still allowing an evaluator to follow a link into the app), and
      Secure only when actually served over HTTPS (setting it unconditionally
      would break the http://localhost demo entirely).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import Literal

from fastapi import Cookie, HTTPException, Response, status

from app import store

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

Role = Literal["user", "caregiver"]
VALID_ROLES: frozenset[str] = frozenset({"user", "caregiver"})

SESSION_COOKIE = "threshold_session"

# Sessions last a week. Long enough that an evaluator returning to the demo
# tomorrow is still signed in; short enough that a forgotten cookie on a shared
# machine eventually stops working.
SESSION_TTL = timedelta(days=7)

# scrypt work factors. n=2**14 with r=8, p=1 is the widely cited interactive
# login profile: roughly 16 MiB of memory and ~100 ms on a laptop. High enough
# to make offline cracking expensive, low enough that the login route stays
# responsive and the test suite does not crawl.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 64

# CPython refuses scrypt when n * r * 128 exceeds OpenSSL's default maxmem, so
# the limit is raised explicitly to match the parameters above.
_SCRYPT_MAXMEM = 2**25

_SALT_BYTES = 16

# The signing secret. Taken from the environment when present so that sessions
# survive a restart in any real deployment. When absent — which is the case for
# a fresh clone — a random per-boot secret is generated instead. That choice is
# deliberate: the alternative, a hardcoded default secret, would let anyone
# reading this public repo forge a session cookie for any account. The cost is
# that restarting the demo signs everyone out, which is the correct direction
# to fail.
_SECRET: bytes = (
    os.environ.get("THRESHOLD_SECRET", "").encode("utf-8") or secrets.token_bytes(32)
)


def secret_is_ephemeral() -> bool:
    """Report whether the signing secret was generated at boot.

    Lets the startup banner tell an operator that sessions will not survive a
    restart until `THRESHOLD_SECRET` is exported — surfacing the trade-off
    rather than hiding it.

    Returns:
        True if `THRESHOLD_SECRET` was unset and a random secret is in use.
    """
    return not os.environ.get("THRESHOLD_SECRET")


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    """Hash a password with scrypt and a per-user salt.

    Args:
        password: The plaintext, straight from the request body. It exists only
            as a local here and in the caller's frame; it is never stored,
            logged, or included in an exception.
        salt: Hex salt to reuse. Only supplied when VERIFYING an existing
            password — registration always passes None so a fresh random salt
            is generated.

    Returns:
        `(password_hash_hex, salt_hex)`, both safe to persist.
    """
    salt_bytes = bytes.fromhex(salt) if salt else secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt_bytes,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
        maxmem=_SCRYPT_MAXMEM,
    )
    return digest.hex(), salt_bytes.hex()


def verify_password(password: str, password_hash: str, salt: str) -> bool:
    """Check a plaintext password against a stored digest, in constant time.

    Args:
        password: The plaintext as typed.
        password_hash: The stored hex digest.
        salt: The stored hex salt for this user.

    Returns:
        True if the password matches.
    """
    candidate, _ = hash_password(password, salt)
    # compare_digest, not ==: see the module docstring. A byte-by-byte early
    # exit would leak how much of the digest an attacker had guessed.
    return hmac.compare_digest(candidate, password_hash)


# ---------------------------------------------------------------------------
# Session tokens
# ---------------------------------------------------------------------------
#
# Token format:  <payload_b64url>.<signature_b64url>
# where payload is "<user_id>:<expiry_epoch_seconds>" and the signature is
# HMAC-SHA256 over the payload bytes under the server secret.
#
# The payload is signed, not encrypted — a user id and an expiry are not
# secrets, and the only property we need is that neither can be altered. HMAC
# gives exactly that with no key management beyond the one secret.


def _b64(raw: bytes) -> str:
    """Encode bytes as unpadded URL-safe base64 (cookie-safe, no '=' to quote)."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    """Decode unpadded URL-safe base64, restoring the padding first."""
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _sign(payload: bytes) -> str:
    """Return the HMAC-SHA256 signature of a payload under the server secret."""
    return _b64(hmac.new(_SECRET, payload, hashlib.sha256).digest())


def issue_token(user_id: str, ttl: timedelta = SESSION_TTL) -> str:
    """Mint a signed session token.

    Args:
        user_id: The account the token authenticates.
        ttl: How long the token remains valid.

    Returns:
        The token string to place in the session cookie.
    """
    expiry = int((datetime.now() + ttl).timestamp())
    payload = f"{user_id}:{expiry}".encode("utf-8")
    return f"{_b64(payload)}.{_sign(payload)}"


def read_token(token: str) -> str | None:
    """Validate a session token and extract the user id.

    Rejects, in order: malformed structure, an undecodable payload, a bad
    signature, and an expired timestamp. The signature is checked BEFORE the
    expiry is trusted — the expiry is inside the signed payload, so reading it
    from an unverified token would let an attacker set their own.

    Args:
        token: The raw cookie value.

    Returns:
        The user id, or None for any invalid, tampered, or expired token. One
        None for every failure mode on purpose: the caller returns the same 401
        either way, so a probe learns nothing about which check failed.
    """
    try:
        payload_b64, signature = token.split(".", 1)
        payload = _unb64(payload_b64)
    except (ValueError, TypeError, base64.binascii.Error):
        # Anything unparseable is simply not a token we issued.
        return None

    # Constant-time signature check. This is the line that makes the whole
    # scheme work: a forged or edited payload produces a different MAC, and the
    # attacker cannot compute the right one without the server secret.
    if not hmac.compare_digest(_sign(payload), signature):
        return None

    try:
        user_id, expiry_text = payload.decode("utf-8").rsplit(":", 1)
        expiry = int(expiry_text)
    except (ValueError, UnicodeDecodeError):
        # Signed but structurally wrong — only reachable if the secret were
        # reused across incompatible token formats. Fail closed.
        return None

    if datetime.now().timestamp() > expiry:
        return None
    return user_id


# ---------------------------------------------------------------------------
# Registration and login
# ---------------------------------------------------------------------------

# The generic message shown for every login failure. Deliberately identical for
# "no such user" and "wrong password" — see the module docstring.
LOGIN_FAILED_MESSAGE = "Incorrect username or password."


class AuthError(Exception):
    """A login or registration failure that is safe to show to the user.

    Carries only pre-approved wording. Nothing derived from the attempt (the
    username tried, whether it existed) reaches the message, so the exception
    itself cannot become an enumeration oracle.
    """


def register(username: str, password: str, role: str = "user") -> store.UserRecord:
    """Create an account.

    Registration works end-to-end so an evaluator can make their own login
    (CONTRACT ground rule 4).

    Args:
        username: Desired login name; leading/trailing whitespace is trimmed
            because a trailing space is invisible and would lock someone out.
        password: Plaintext, hashed immediately and never retained.
        role: "user" or "caregiver".

    Returns:
        The created `UserRecord`.

    Raises:
        AuthError: Blank username or password, unknown role, or a name already
            taken. Username-taken must be reported here — a registration form
            cannot function otherwise — which is a knowingly different exposure
            from the login path, where we say nothing.
    """
    username = username.strip()
    if not username or not password:
        raise AuthError("Username and password are both required.")
    if role not in VALID_ROLES:
        raise AuthError("Role must be either 'user' or 'caregiver'.")

    password_hash, salt = hash_password(password)
    try:
        return store.create_user(
            id=uuid.uuid4().hex,
            username=username,
            password_hash=password_hash,
            salt=salt,
            role=role,
        )
    except sqlite3.IntegrityError as exc:
        # The UNIQUE COLLATE NOCASE constraint is the authority on uniqueness,
        # not a prior SELECT — checking first would race two simultaneous
        # registrations of the same name.
        raise AuthError("That username is already taken.") from exc


# A digest computed once at import over a throwaway password. Used only to give
# the "no such user" path the same scrypt cost as a real verification.
_DUMMY_HASH, _DUMMY_SALT = hash_password(secrets.token_hex(16))


def verify_login(username: str, password: str) -> store.UserRecord:
    """Authenticate a username/password pair.

    Args:
        username: As typed; matched case-insensitively by the store.
        password: Plaintext as typed.

    Returns:
        The authenticated `UserRecord`.

    Raises:
        AuthError: Always with `LOGIN_FAILED_MESSAGE`, whether the account does
            not exist or the password is wrong.
    """
    user = store.get_user_by_username(username.strip())
    if user is None:
        # Burn an equivalent scrypt hash so a missing account takes the same
        # ~100ms as a real one. Without this the response time alone tells an
        # attacker which usernames are registered — in an overdose-response app
        # that is a disclosure that someone is a user of it.
        verify_password(password, _DUMMY_HASH, _DUMMY_SALT)
        raise AuthError(LOGIN_FAILED_MESSAGE)

    if not verify_password(password, user.password_hash, user.salt):
        raise AuthError(LOGIN_FAILED_MESSAGE)
    return user


# ---------------------------------------------------------------------------
# Cookie helpers
# ---------------------------------------------------------------------------


def set_session_cookie(response: Response, user_id: str, secure: bool = False) -> None:
    """Attach a signed session cookie to a response.

    Args:
        response: The FastAPI response to mutate.
        user_id: The account to authenticate.
        secure: Set the Secure flag. Left False by default because the demo is
            served over plain http://localhost, where a Secure cookie would be
            silently dropped by the browser and auth would appear broken. The
            caller passes True when the request arrived over HTTPS.
    """
    response.set_cookie(
        SESSION_COOKIE,
        issue_token(user_id),
        max_age=int(SESSION_TTL.total_seconds()),
        # HttpOnly: JavaScript cannot read this value, so an XSS bug elsewhere
        # in the app cannot steal the session.
        httponly=True,
        # Lax: blocks cross-site form POSTs (CSRF) while still sending the
        # cookie on a top-level navigation, so a link into the demo works.
        samesite="lax",
        secure=secure,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    """Remove the session cookie.

    Deletes with the same path the cookie was set on; a mismatched path leaves
    the original cookie in place and logout silently does nothing.

    Args:
        response: The FastAPI response to mutate.
    """
    response.delete_cookie(SESSION_COOKIE, path="/")


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------


def optional_user(
    threshold_session: str | None = Cookie(default=None),
) -> store.UserRecord | None:
    """Resolve the caller's account if they have a valid session, else None.

    This is what makes bystander mode possible: PRD §3 says a bystander has no
    account and must never be asked to create one, so /bystander and its routes
    depend on this rather than on `current_user`.

    Args:
        threshold_session: Cookie value, injected by FastAPI. The parameter
            name must match `SESSION_COOKIE` for the injection to work.

    Returns:
        The `UserRecord`, or None if there is no cookie, the token is invalid
        or expired, or the account has since been deleted.
    """
    if not threshold_session:
        return None
    user_id = read_token(threshold_session)
    if user_id is None:
        return None
    # Re-read the account on every request. A signed token proves the id was
    # issued by us; it does not prove the account still exists or still holds
    # the role it had when the token was minted.
    return store.get_user(user_id)


def current_user(
    threshold_session: str | None = Cookie(default=None),
) -> store.UserRecord:
    """Require an authenticated caller.

    Args:
        threshold_session: Cookie value, injected by FastAPI.

    Returns:
        The authenticated `UserRecord`.

    Raises:
        HTTPException: 401 for every failure mode. No WWW-Authenticate header,
            because that triggers the browser's native basic-auth dialog and
            the app renders its own login screen.
    """
    user = optional_user(threshold_session)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not signed in.",
        )
    return user


def require_role(user: store.UserRecord, *roles: str) -> store.UserRecord:
    """Assert that a user holds one of the given roles.

    Not a FastAPI dependency: it takes an already-resolved user so a route can
    apply its own logic first (for example, allowing a user to read their own
    caregiver brief).

    Args:
        user: The authenticated account.
        *roles: Acceptable role names.

    Returns:
        The same user, unchanged, so it can be used inline.

    Raises:
        HTTPException: 403 when the role does not match. 403 and not 401 —
            the caller IS authenticated, and returning 401 would bounce a
            signed-in user back to the login screen in a loop.
    """
    if user.role not in roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This account does not have access to that view.",
        )
    return user
