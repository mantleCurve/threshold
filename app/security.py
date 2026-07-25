"""Transport and abuse-resistance controls: security headers and rate limiting.

WHAT THIS MODULE DOES
    Adds the HTTP response headers a browser needs in order to enforce our own
    security assumptions, and applies a small in-process rate limiter to the
    endpoints where repetition is an attack rather than normal use.

WHAT THIS MODULE DELIBERATELY DOES NOT DO
    It is not an authentication or authorisation layer — that is `app.auth`. It
    makes no clinical or triage decision. And it never rate-limits the emergency
    path: a person in an overdose may legitimately hammer the same endpoint, and
    throttling them to protect a server would be an indefensible trade.

WHY IN-PROCESS RATE LIMITING RATHER THAN REDIS
    A single-process deployment behind one nginx does not need shared state, and
    an extra network dependency is an extra thing that can fail at 3am. If this
    ever runs multi-worker, this must move to a shared store — the limiter is
    written behind a small interface so that swap is contained. That limitation
    is documented here rather than left for someone to discover.
"""

from __future__ import annotations

import os
import time
from collections import defaultdict, deque

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------
# Endpoint -> (max requests, window seconds).
#
# Only endpoints where repetition is inherently suspicious appear here. Login and
# register are brute-force targets; contact is an unauthenticated write and
# therefore a spam target; account deletion is destructive and no human deletes
# their account eight times a minute.
#
# NOTHING on the emergency path is listed, deliberately: /api/utterance,
# /api/sensor, /api/tier, /api/rescind and /api/events are never throttled,
# because the cost of being wrong there is measured in lives rather than CPU.
_LIMITS: dict[str, tuple[int, int]] = {
    "/api/auth/login": (8, 60),       # 8 attempts per minute per IP
    "/api/auth/register": (5, 300),   # 5 new accounts per 5 minutes per IP
    "/api/contact": (5, 300),         # 5 messages per 5 minutes per IP
    "/api/account/delete": (5, 60),
    # Server-side speech synthesis. Listed for a different reason from the rest:
    # it is not an abuse target, it is a BILLING one — every call is a paid
    # request to the speech provider, and a signed-in client looping it is an
    # invoice rather than an outage. 30/minute is far above any human reading
    # pace and far below a runaway loop.
    #
    # Safe to limit even though the app speaks during an emergency, because a
    # throttled response degrades to the browser's own speechSynthesis in the
    # client (see `speak()` in web/js/app.js and app/routes/voice.py). Nobody is
    # left in silence, which is the test every entry in this table has to pass.
    # Cloning is limited harder: it is a slow, expensive upload and no supporter
    # legitimately builds four voice models in five minutes.
    "/api/voice/speak": (30, 60),
    "/api/voice/clone": (3, 300),
}

# client key -> deque of request timestamps, oldest first.
_hits: dict[str, deque[float]] = defaultdict(deque)

# Hard ceiling on tracked clients. Without this the dict is itself a memory-
# exhaustion vector: an attacker rotating source addresses would grow it without
# bound. On overflow we clear rather than evict cleverly — the window is 60-300
# seconds, so the cost of forgetting is one extra allowed attempt.
_MAX_TRACKED_CLIENTS = 10_000


# Proxies whose X-Forwarded-For we are willing to believe.
#
# Only the loopback addresses by default, because that is where our own nginx
# sits. Anything else must be named explicitly via THRESHOLD_TRUSTED_PROXIES.
_TRUSTED_PROXIES: frozenset[str] = frozenset(
    p.strip()
    for p in os.getenv("THRESHOLD_TRUSTED_PROXIES", "127.0.0.1,::1").split(",")
    if p.strip()
)


def _client_key(request: Request) -> str:
    """Identify the caller for rate-limiting purposes.

    X-Forwarded-For is honoured ONLY when the immediate peer is a proxy we
    trust. It used to be honoured unconditionally, and a security review proved
    that made every limit in this table decorative: fourteen wrong passwords
    with a rotating `X-Forwarded-For: 10.1.1.$i` were all accepted, where the
    same requests without the header throttled correctly at eight.

    The old comment defended taking only the first entry because "the rest are
    attacker-controllable". The first entry is the one an attacker sets — when
    the request did not come through our proxy, the whole header is theirs.

    Falling back to the peer address is the safe direction: behind our nginx
    every request genuinely originates from loopback, so the header is real;
    anywhere else the peer is the true source.
    """
    peer = request.client.host if request.client else "unknown"

    if peer in _TRUSTED_PROXIES:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            # Leftmost entry is the originating client as recorded by our own
            # proxy, which we have just established is the one that set it.
            return forwarded.split(",")[0].strip()

    return peer


def check_rate_limit(request: Request) -> tuple[bool, int]:
    """Return (allowed, retry_after_seconds) for this request.

    Uses a sliding window rather than fixed buckets: fixed buckets let an
    attacker send a full quota at the end of one window and another at the start
    of the next, doubling the effective rate at the boundary.
    """
    limit = _LIMITS.get(request.url.path)
    if limit is None:
        return True, 0

    max_hits, window = limit
    now = time.monotonic()
    key = f"{_client_key(request)}:{request.url.path}"

    if len(_hits) > _MAX_TRACKED_CLIENTS:
        # Evict the OLDEST-IDLE entries, never clear the whole table.
        #
        # This used to call _hits.clear(), which reset every client's counter at
        # once. A review demonstrated the consequence: lock an address out of
        # login, then send ~10,500 requests with distinct spoofed keys in three
        # seconds, and the locked-out address is immediately able to guess
        # passwords again. A brute-forcer could clear its own lockout on demand.
        #
        # Evicting by last-activity keeps the entries that matter — a client
        # mid-attack is by definition the most recently active — and bounds
        # memory just as effectively.
        stale = sorted(_hits.items(), key=lambda kv: kv[1][-1] if kv[1] else 0.0)
        for key_to_drop, _ in stale[: len(_hits) // 2]:
            _hits.pop(key_to_drop, None)

    bucket = _hits[key]
    # Drop timestamps that have aged out of the window.
    while bucket and now - bucket[0] > window:
        bucket.popleft()

    if len(bucket) >= max_hits:
        return False, max(1, int(window - (now - bucket[0])))

    bucket.append(now)
    return True, 0


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------
class SecurityMiddleware(BaseHTTPMiddleware):
    """Apply rate limits, then attach security headers to every response."""

    def __init__(self, app, *, https: bool = False) -> None:
        super().__init__(app)
        # HSTS is only meaningful — and only safe — when the site genuinely
        # serves HTTPS. Sending it from a local http:// dev server would pin the
        # browser to a scheme that host cannot answer on.
        self.https = https

    async def dispatch(self, request: Request, call_next):
        allowed, retry_after = check_rate_limit(request)
        if not allowed:
            # A generic message: telling a caller precisely which limit they hit
            # helps them tune an attack around it.
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many requests. Please wait a moment."},
                headers={"Retry-After": str(retry_after)},
            )

        response = await call_next(request)

        # -- Content Security Policy ---------------------------------------
        # 'unsafe-inline' for style and script is required by the current
        # markup: several pages carry inline style attributes and two carry a
        # small inline script. That is a real weakening of the policy and it is
        # recorded here rather than quietly omitted. Removing it means moving
        # that inline content into the stylesheets and js files — worth doing,
        # and the reason the rest of the policy is kept tight in the meantime.
        #
        # connect-src stays 'self': the browser never talks to OpenRouter
        # directly, because the API key must never reach a client.
        response.headers["Content-Security-Policy"] = "; ".join([
            "default-src 'self'",
            "script-src 'self' 'unsafe-inline'",
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' data:",
            "font-src 'self'",
            "connect-src 'self'",
            "media-src 'self'",
            "form-action 'self'",
            "frame-ancestors 'none'",   # clickjacking: nothing may embed us
            "base-uri 'self'",
            "object-src 'none'",
        ])

        # Defence in depth for browsers that predate frame-ancestors.
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"

        # Do not leak the path a user was on when they follow an outbound link.
        # A URL from this product can disclose that someone is in recovery.
        response.headers["Referrer-Policy"] = "no-referrer"

        # Deny by default, then re-grant only what a surface actually needs.
        # Geolocation and microphone are granted to same-origin because the
        # emergency flow and push-to-talk depend on them; camera and payment are
        # denied outright because nothing here uses them.
        response.headers["Permissions-Policy"] = (
            "geolocation=(self), microphone=(self), camera=(), payment=(), "
            "interest-cohort=()"
        )

        if self.https:
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )

        # Never let a proxy or browser cache a response containing someone's
        # profile, script, or event history.
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store, private"

        return response
