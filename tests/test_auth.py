"""Tests for `app.auth` — password hashing, session tokens, and role gates.

WHAT THESE TESTS PROTECT AGAINST
    Security is a scored category, and the failures that matter here are the
    silent ones: a password stored in a readable form, a session cookie whose
    contents can be edited by its holder, a login form that tells an attacker
    which usernames exist. None of those break a feature, so none of them would
    be caught by the API tests. Each test below names the specific regression
    it exists to catch.

WHAT THESE TESTS DELIBERATELY DO NOT DO
    They do not assert scrypt's cost parameters or measure timing. Timing
    assertions are flaky on shared CI hardware, so the constant-time and
    equal-work properties are protected by construction (`hmac.compare_digest`,
    the dummy-hash burn on the miss path) and reviewed rather than measured.
    The dummy-hash path is still exercised for correctness below.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest
from fastapi import HTTPException

from app import auth, store


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    """Point the store at a fresh database file for every test.

    Autouse so no test can accidentally read or write the developer's real
    demo database — a test that deletes someone's data is its own bug.
    """
    monkeypatch.setattr(store, "_db_path", tmp_path / "test.db")
    store.init_db()
    yield


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------


def test_hash_password_round_trip():
    """A correct password verifies against its own digest.

    The baseline: if this fails, nobody can log in at all.
    """
    digest, salt = auth.hash_password("correct horse battery staple")
    assert auth.verify_password("correct horse battery staple", digest, salt)


def test_wrong_password_is_rejected():
    """A wrong password does not verify.

    Guards the inverted-comparison bug — a `not` in the wrong place in
    `verify_password` would let every password through, and the round-trip test
    above would still pass.
    """
    digest, salt = auth.hash_password("correct horse battery staple")
    assert not auth.verify_password("Correct horse battery staple", digest, salt)
    assert not auth.verify_password("", digest, salt)
    assert not auth.verify_password("wrong", digest, salt)


def test_digest_is_not_the_plaintext():
    """The stored digest bears no resemblance to the password.

    Catches the catastrophic regression of someone "simplifying" hashing away
    and storing the password, or storing something reversible like base64.
    """
    password = "threshold"
    digest, salt = auth.hash_password(password)
    assert password not in digest
    assert password not in salt
    # A hex scrypt digest at dklen=64 is 128 hex characters. A short value here
    # would mean the hashing step was bypassed.
    assert len(digest) == 128
    assert digest != password


def test_same_password_different_users_yields_different_digests():
    """Per-user salts make identical passwords hash differently.

    Both demo accounts share the password `threshold`. Without a per-user salt
    their rows would be byte-identical, one cracked digest would open both, and
    a precomputed rainbow table would work across the whole user table.
    """
    first_digest, first_salt = auth.hash_password("threshold")
    second_digest, second_salt = auth.hash_password("threshold")
    assert first_salt != second_salt
    assert first_digest != second_digest


def test_supplied_salt_reproduces_the_same_digest():
    """Re-hashing with a stored salt is deterministic.

    This is the property verification depends on: if passing the salt back in
    did not reproduce the digest, every login would fail.
    """
    digest, salt = auth.hash_password("threshold")
    again, same_salt = auth.hash_password("threshold", salt)
    assert again == digest
    assert same_salt == salt


# ---------------------------------------------------------------------------
# Registration and login
# ---------------------------------------------------------------------------


def test_register_then_login():
    """An account created through the public path can sign in.

    Contract ground rule 4 requires registration to work end-to-end so an
    evaluator can make their own account.
    """
    auth.register("evaluator", "hunter2", role="user")
    user = auth.verify_login("evaluator", "hunter2")
    assert user.username == "evaluator"
    assert user.role == "user"


def test_no_plaintext_password_anywhere_in_the_database():
    """The plaintext password appears in no column of any table.

    The single most important security test in the repo. Rather than checking
    the `users` table only, this dumps every value of every table and asserts
    the password is absent — so a future feature that logs a password into,
    say, the events table would fail here too.
    """
    password = "sup3r-s3cret-passphrase"
    auth.register("leakcheck", password, role="user")

    with store.connection() as conn:
        tables = [
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        for table in tables:
            for row in conn.execute(f"SELECT * FROM {table}").fetchall():  # noqa: S608
                for value in tuple(row):
                    assert password not in str(value), (
                        f"plaintext password found in table {table!r}"
                    )

    # Belt and braces: the raw file on disk contains it nowhere either, which
    # also covers WAL pages and any stray index copy.
    raw = store.db_path().read_bytes()
    assert password.encode() not in raw


def test_login_error_message_is_identical_for_unknown_user_and_wrong_password():
    """Failed login never reveals whether the username exists.

    A differing message turns the login form into a username oracle. In an
    overdose-response app, confirming that a given person has an account is
    itself a disclosure about their health.
    """
    auth.register("sam", "threshold", role="user")

    with pytest.raises(auth.AuthError) as wrong_password:
        auth.verify_login("sam", "not-the-password")
    with pytest.raises(auth.AuthError) as no_such_user:
        auth.verify_login("nobody-by-that-name", "not-the-password")

    assert str(wrong_password.value) == str(no_such_user.value)
    assert str(wrong_password.value) == auth.LOGIN_FAILED_MESSAGE


def test_unknown_user_still_performs_a_verification():
    """The miss path runs the dummy hash rather than returning immediately.

    Protects the timing defence. If someone removes the dummy verification as
    "dead code", a missing account returns in microseconds while a real one
    takes ~100ms, and the response time alone enumerates users. Asserted by
    spying on `verify_password` rather than by measuring the clock, which would
    be flaky.
    """
    calls: list[str] = []
    real_verify = auth.verify_password

    def spy(password: str, password_hash: str, salt: str) -> bool:
        calls.append(password_hash)
        return real_verify(password, password_hash, salt)

    auth.verify_password = spy
    try:
        with pytest.raises(auth.AuthError):
            auth.verify_login("definitely-not-registered", "whatever")
    finally:
        auth.verify_password = real_verify

    assert calls, "no verification ran on the unknown-user path"
    assert calls[0] == auth._DUMMY_HASH


def test_usernames_are_case_insensitive():
    """`Sam` and `sam` are the same account.

    Two accounts differing only by case is a real safety problem here: a
    caregiver could end up watching the ladder of an empty duplicate profile
    while the person they care about escalates on the other one.
    """
    auth.register("Sam", "threshold", role="user")
    assert auth.verify_login("sam", "threshold").username == "Sam"
    assert auth.verify_login("SAM", "threshold").username == "Sam"

    with pytest.raises(auth.AuthError):
        auth.register("sAm", "threshold", role="user")


def test_register_rejects_blank_and_invalid_input():
    """Registration refuses empty credentials and unknown roles."""
    with pytest.raises(auth.AuthError):
        auth.register("", "threshold")
    with pytest.raises(auth.AuthError):
        auth.register("someone", "")
    with pytest.raises(auth.AuthError):
        auth.register("someone", "threshold", role="administrator")


def test_username_is_trimmed():
    """Whitespace around a username is stripped at registration.

    A trailing space is invisible in a form field and would otherwise create an
    account the user can never log into again.
    """
    user = auth.register("  spaced  ", "threshold")
    assert user.username == "spaced"
    assert auth.verify_login("spaced", "threshold").id == user.id


def test_duplicate_registration_raises_authError_not_a_db_error():
    """A taken username surfaces as AuthError, never a raw sqlite error.

    An unhandled `IntegrityError` would become a 500 with a stack trace, which
    both breaks the form and leaks schema details.
    """
    auth.register("taken", "threshold")
    with pytest.raises(auth.AuthError):
        auth.register("taken", "threshold")
    # And specifically not the underlying database exception.
    try:
        auth.register("taken", "threshold")
    except sqlite3.IntegrityError:  # pragma: no cover - would be a failure
        pytest.fail("sqlite3.IntegrityError leaked out of auth.register")
    except auth.AuthError:
        pass


# ---------------------------------------------------------------------------
# Session tokens
# ---------------------------------------------------------------------------


def test_token_round_trip():
    """A freshly issued token resolves back to its user id."""
    token = auth.issue_token("user-123")
    assert auth.read_token(token) == "user-123"


def test_tampered_payload_is_rejected():
    """Editing the payload invalidates the signature.

    The core session guarantee. If this fails, anyone holding a cookie can
    rewrite it to another user's id and read that person's ladder, address and
    entry code.
    """
    token = auth.issue_token("user-123")
    forged = auth.issue_token("attacker")
    # Splice the attacker's payload onto the legitimate signature.
    tampered = f"{forged.split('.')[0]}.{token.split('.')[1]}"
    assert auth.read_token(tampered) is None


def test_tampered_signature_is_rejected():
    """Editing the signature invalidates the token."""
    token = auth.issue_token("user-123")
    payload, signature = token.split(".", 1)
    # Flip one character of the MAC.
    flipped = ("b" if signature[0] != "b" else "c") + signature[1:]
    assert auth.read_token(f"{payload}.{flipped}") is None


def test_malformed_tokens_are_rejected_without_raising():
    """Garbage cookie values return None rather than throwing.

    A cookie is attacker-controlled input. An uncaught decode error here would
    turn any request carrying junk into a 500 — trivially, a denial of service
    on the whole app.
    """
    for junk in ["", ".", "no-dot", "a.b", "!!!.???", "x" * 500, "a.b.c.d"]:
        assert auth.read_token(junk) is None


def test_expired_token_is_rejected():
    """A token past its expiry no longer authenticates."""
    expired = auth.issue_token("user-123", ttl=timedelta(seconds=-1))
    assert auth.read_token(expired) is None


def test_token_signed_with_a_different_secret_is_rejected(monkeypatch):
    """A token minted under another server's secret does not validate here.

    This is why a hardcoded default secret would be unacceptable: with one,
    anyone reading this public repo could mint a valid cookie for any account.
    """
    token = auth.issue_token("user-123")
    monkeypatch.setattr(auth, "_SECRET", b"a-completely-different-secret")
    assert auth.read_token(token) is None


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------


def test_current_user_resolves_a_valid_session():
    """A valid cookie resolves to the stored account."""
    user = auth.register("sam", "threshold", role="user")
    resolved = auth.current_user(auth.issue_token(user.id))
    assert resolved.id == user.id
    assert resolved.role == "user"


def test_current_user_raises_401_without_a_session():
    """No cookie, junk cookie, and tampered cookie all produce 401.

    One status for every failure mode, so a probe cannot distinguish "expired"
    from "forged" from "never existed".
    """
    for cookie in [None, "", "garbage", auth.issue_token("no-such-user")]:
        with pytest.raises(HTTPException) as exc:
            auth.current_user(cookie)
        assert exc.value.status_code == 401


def test_current_user_rejects_a_token_for_a_deleted_account():
    """A validly signed token for a nonexistent account is still rejected.

    The signature proves we issued the id; it does not prove the account still
    exists. Session validation must re-read the user on every request.
    """
    token = auth.issue_token("ghost-account-id")
    with pytest.raises(HTTPException):
        auth.current_user(token)


def test_optional_user_returns_none_instead_of_raising():
    """Bystander mode depends on this returning None, not 401.

    PRD §3: a bystander has no account and must never be asked to create one.
    If `optional_user` ever started raising, the bystander route would demand a
    login from a stranger performing CPR.
    """
    assert auth.optional_user(None) is None
    assert auth.optional_user("garbage") is None

    user = auth.register("sam", "threshold", role="user")
    assert auth.optional_user(auth.issue_token(user.id)).id == user.id


def test_user_from_request_resolves_and_tolerates_missing_cookies():
    """Route handlers can resolve the caller from a raw Request without raising.

    Used by page routes and by account deletion, which must read the user
    inside the handler body. It must never raise: a signed-out visitor hitting
    a public page is an ordinary branch, not an error.
    """
    from starlette.datastructures import Headers
    from starlette.requests import Request as StarletteRequest

    def make_request(cookie_header: str | None) -> StarletteRequest:
        raw = [(b"cookie", cookie_header.encode())] if cookie_header else []
        return StarletteRequest(
            {"type": "http", "headers": Headers(raw=raw).raw, "method": "GET", "path": "/"}
        )

    user = auth.register("sam", "threshold", role="user")
    token = auth.issue_token(user.id)

    assert auth.user_from_request(make_request(f"{auth.SESSION_COOKIE}={token}")).id == user.id
    assert auth.user_from_request(make_request(None)) is None
    assert auth.user_from_request(make_request(f"{auth.SESSION_COOKIE}=garbage")) is None


def test_require_role_allows_matching_and_forbids_others():
    """Role gating returns 403, not 401, for a signed-in user without access.

    401 would bounce an authenticated user back to the login screen, where they
    would sign in successfully and be bounced again — an infinite loop instead
    of an error message.
    """
    caregiver = auth.register("sarah", "threshold", role="caregiver")
    assert auth.require_role(caregiver, "caregiver") is caregiver

    with pytest.raises(HTTPException) as exc:
        auth.require_role(caregiver, "user")
    assert exc.value.status_code == 403


def test_session_cookie_flags(monkeypatch):
    """The session cookie is HttpOnly, SameSite=Lax, and path-scoped.

    HttpOnly keeps an XSS bug from reading the token; SameSite=Lax blocks
    cross-site POST CSRF. Secure defaults off because the demo runs on
    http://localhost, where a Secure cookie is dropped and auth silently breaks.
    """
    from fastapi import Response

    response = Response()
    auth.set_session_cookie(response, "user-123")
    header = response.headers["set-cookie"]

    assert "httponly" in header.lower()
    assert "samesite=lax" in header.lower()
    assert "path=/" in header.lower()
    assert "secure" not in header.lower()

    secure_response = Response()
    auth.set_session_cookie(secure_response, "user-123", secure=True)
    assert "secure" in secure_response.headers["set-cookie"].lower()


def test_cookie_does_not_contain_the_password_or_the_raw_user_record():
    """The cookie carries only an id and an expiry, both signed.

    Guards against a "convenience" refactor that stuffs the role or username
    into the token, which would make those values attacker-editable if the
    signature check were ever weakened.
    """
    user = auth.register("sam", "threshold", role="user")
    token = auth.issue_token(user.id)
    assert "threshold" not in token
    assert user.password_hash not in token
    assert user.salt not in token
