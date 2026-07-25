"""Security controls: headers, rate limiting, and data-exposure boundaries.

WHAT THESE TESTS PROTECT AGAINST
    Security controls are invisible when they work, which makes them easy to
    break silently during a refactor. Each test here pins one control and states
    the attack it defeats, so a regression fails loudly rather than quietly
    reopening a hole.

WHAT THEY DELIBERATELY DO NOT DO
    They do not test the cryptography itself — scrypt and hmac are stdlib and
    testing them would be testing CPython. They test that we *use* those
    primitives correctly and that the boundaries around them hold.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app import security


@pytest.fixture
def client():
    """A fresh client per test.

    Function-scoped, and the rate-limit buckets are cleared alongside it: a
    module-scoped client would let one test's login attempts exhaust the quota
    and fail an unrelated test downstream.
    """
    security._hits.clear()
    with TestClient(app) as c:
        yield c
    security._hits.clear()


# ---------------------------------------------------------------------------
# Security headers
# ---------------------------------------------------------------------------
def test_clickjacking_headers_present(client):
    """The app must not be embeddable in a frame.

    An attacker who can iframe this app can overlay it and trick a user into
    clicking controls they cannot see — including, here, a control that cancels
    a real emergency alert.
    """
    r = client.get("/home")
    assert r.headers.get("X-Frame-Options") == "DENY"
    assert "frame-ancestors 'none'" in r.headers.get("Content-Security-Policy", "")


def test_csp_blocks_third_party_connections(client):
    """connect-src must stay same-origin.

    The browser never talks to OpenRouter directly, because the API key lives on
    the server and must never reach a client. A CSP that allowed arbitrary
    connect-src would also make key exfiltration trivial if XSS were ever found.
    """
    csp = client.get("/home").headers.get("Content-Security-Policy", "")
    assert "connect-src 'self'" in csp
    assert "object-src 'none'" in csp


def test_no_mime_sniffing(client):
    """Stops a browser reinterpreting a response as a script it was not."""
    assert client.get("/home").headers.get("X-Content-Type-Options") == "nosniff"


def test_referrer_is_never_leaked(client):
    """A URL from this product can disclose that someone is in recovery.

    Following an outbound link must not hand that fact to a third-party server
    in the Referer header.
    """
    assert client.get("/home").headers.get("Referrer-Policy") == "no-referrer"


def test_camera_denied_microphone_allowed(client):
    """Permissions are denied by default and re-granted only where used.

    Push-to-talk needs the microphone and the emergency flow needs geolocation.
    Nothing in this product uses the camera, so it is denied outright rather
    than left to a permission prompt.
    """
    pp = client.get("/home").headers.get("Permissions-Policy", "")
    assert "camera=()" in pp
    assert "microphone=(self)" in pp
    assert "geolocation=(self)" in pp


def test_api_responses_are_never_cached(client):
    """Profile, script, and event data must not sit in a shared cache.

    An API response here can contain a home address and a door entry code.
    """
    cc = client.get("/api/state").headers.get("Cache-Control", "")
    assert "no-store" in cc


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------
def test_login_is_rate_limited(client):
    """Online password guessing must become expensive.

    scrypt makes offline cracking slow, but nothing in the hash stops an
    attacker simply trying passwords against the live form. This is that limit.
    """
    codes = [
        client.post(
            "/api/auth/login", json={"username": "sam", "password": f"guess{i}"}
        ).status_code
        for i in range(12)
    ]
    assert 429 in codes, "login accepted 12 rapid attempts without throttling"


def test_rate_limited_response_says_when_to_retry(client):
    """A throttled client needs Retry-After, or it can only guess and hammer."""
    last = None
    for i in range(12):
        last = client.post(
            "/api/auth/login", json={"username": "nobody", "password": f"x{i}"}
        )
    if last.status_code == 429:
        assert last.headers.get("Retry-After"), "429 without Retry-After"


def test_emergency_path_is_never_rate_limited(client):
    """THE MOST IMPORTANT TEST IN THIS FILE.

    A person mid-overdose may hit the same endpoint repeatedly, and a bystander
    hammering a button is exactly the behaviour we expect. Throttling that to
    protect a server would be an indefensible trade. If someone ever adds a
    blanket limiter, this test is what stops it reaching production.
    """
    for path in ("/api/utterance", "/api/sensor", "/api/tier", "/api/rescind"):
        assert path not in security._LIMITS, f"{path} must never be rate limited"

    codes = [
        client.post("/api/tier", json={"tier": 4}).status_code for _ in range(25)
    ]
    assert 429 not in codes, "the emergency path was throttled"


# ---------------------------------------------------------------------------
# Credential handling
# ---------------------------------------------------------------------------
def test_login_does_not_reveal_whether_a_user_exists(client):
    """Whether someone HAS an account here is itself sensitive.

    An account in this product implies something about a person's health. A
    login form that distinguishes "no such user" from "wrong password" leaks
    that, and hands an attacker half a credential for free.
    """
    security._hits.clear()
    a = client.post("/api/auth/login", json={"username": "sam", "password": "wrong"})
    security._hits.clear()
    b = client.post(
        "/api/auth/login", json={"username": "definitely-not-a-user", "password": "wrong"}
    )
    assert a.status_code == b.status_code
    assert a.json().get("detail") == b.json().get("detail")


def test_no_password_or_hash_in_any_response(client):
    """No credential material may appear in a response body, ever."""
    security._hits.clear()
    r = client.post("/api/auth/login", json={"username": "sam", "password": "threshold"})
    body = r.text.lower()
    for leak in ("threshold_hash", "password_hash", "salt", "scrypt"):
        assert leak not in body, f"{leak} leaked in a login response"


def test_session_cookie_is_httponly(client):
    """HttpOnly means an XSS bug cannot exfiltrate a live session."""
    security._hits.clear()
    r = client.post("/api/auth/login", json={"username": "sam", "password": "threshold"})
    cookie = r.headers.get("set-cookie", "").lower()
    if cookie:
        assert "httponly" in cookie
        assert "samesite" in cookie


def test_registration_rejects_a_duplicate_username_cleanly(client):
    """A taken username is a 409, not a 500.

    This was a real bug: auth.AuthError does not subclass ValueError, so the
    409 handler never fired and an evaluator picking a taken name got an
    Internal Server Error.
    """
    security._hits.clear()
    r = client.post(
        "/api/auth/register", json={"username": "sam", "password": "another-pw"}
    )
    assert r.status_code == 409, f"expected 409 for duplicate, got {r.status_code}"


# ---------------------------------------------------------------------------
# Input bounds
# ---------------------------------------------------------------------------
def test_contact_form_bounds_its_input(client):
    """An unauthenticated write endpoint must not accept an unbounded body.

    Without a ceiling this is a disk-exhaustion vector: the endpoint appends to
    a file and requires no account.
    """
    security._hits.clear()
    r = client.post(
        "/api/contact",
        json={"name": "x", "email": "a@b.c", "message": "y" * 20000},
    )
    assert r.status_code == 413


def test_profile_update_ignores_unknown_fields(client):
    """Only whitelisted fields may be written from the onboarding form.

    A crafted request must not be able to rewrite parts of a record that this
    form has no business touching.
    """
    security._hits.clear()
    client.post("/api/auth/login", json={"username": "sam", "password": "threshold"})
    r = client.post("/api/profile", json={"id": "hijacked", "name": "attacker"})
    if r.status_code == 200:
        p = r.json().get("profile", {})
        assert p.get("id") != "hijacked", "profile id was writable from the client"
        assert p.get("name") != "attacker", "profile name was writable from the client"


def test_tier_4_visibility_cannot_be_disabled_by_a_client(client):
    """The one promise the user cannot switch off must not be writable.

    Tier 4/5 caregiver notification is disclosed at onboarding and stated in the
    Terms. If a crafted request could disable it, that disclosure becomes a lie.
    """
    security._hits.clear()
    client.post("/api/auth/login", json={"username": "sam", "password": "threshold"})
    client.post(
        "/api/profile",
        json={"ladder": {"tier_4_visible_to_caregiver": False, "notify_on_emergency": False}},
    )
    r = client.post("/api/tier", json={"tier": 4}).json()
    assert r["notify_caregiver"] is True, "Tier 4 notification was disabled by a client"
