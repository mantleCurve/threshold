"""Static page routes plus the crawler surface and the liveness probe.

WHAT THIS MODULE DOES
    Maps every human-visitable URL to a file in `web/`, and serves the three
    machine-readable endpoints that sit alongside them: `/robots.txt`, `/sitemap.xml`
    and `/healthz`. Every handler here is deliberately trivial — the routing decision
    is the whole content — with the single exception of `/`, which branches on whether
    the visitor has a session.

WHAT THIS MODULE DELIBERATELY DOES NOT DO
    - It renders nothing. There is no template engine and no server-side HTML; the
      frontend is vanilla JS with no build step (CONTRACT.md), so these handlers hand
      back a file and stop.
    - It gates nothing. Page routes never require authentication: the pages fetch
      their own data and handle the anonymous case themselves. Bystander mode in
      particular is outside the auth wall by design (PRD §3), and gating it here
      would quietly break that guarantee.
    - It does not mount the static asset directory. That mount must be registered
      last, after every API route, so it cannot shadow them — so it stays in
      `app.main` where the ordering is visible.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response

from app.deps import _now, _page

router = APIRouter()


# ---------------------------------------------------------------------------
# Application pages
# ---------------------------------------------------------------------------
@router.get("/")
async def page_root(request: Request):
    """The front door.

    Serves the public homepage to a visitor with no session, and the app itself
    to someone signed in. A stranger — a judge, a family member deciding whether
    to trust this, someone who followed a link — should land on a page that
    explains what this is, not inside another person's live recovery surface with
    their tier, their event history, and their contact tree on screen.

    Signing in then lands you straight in the app, because someone who has an
    account does not need the pitch.
    """
    try:
        from app import auth

        if auth.user_from_request(request):
            return _page("index.html")
    except Exception:
        pass
    return _page("home.html")


@router.get("/app")
async def page_app():
    """The app surface at a stable URL.

    `/` is conditional, which makes it a poor thing to link to or bookmark. This
    route always serves the app regardless of session, so the homepage's
    "Open the app" button and the post-login redirect have somewhere fixed to go.
    """
    return _page("index.html")


@router.get("/caregiver")
async def page_caregiver():
    return _page("caregiver.html")


@router.get("/bystander")
async def page_bystander():
    """Bystander mode is intentionally reachable with no session and no account.

    PRD §3: this person may not know the user, may be using themselves, and is
    terrified of arrest. Asking them to register would cost the exact minutes that
    decide whether someone breathes.
    """
    return _page("bystander.html")


@router.get("/onboarding")
async def page_onboarding():
    return _page("onboarding.html")


@router.get("/ladder")
async def page_ladder():
    return _page("ladder.html")


@router.get("/home")
async def page_home():
    """Public marketing homepage. Deliberately separate from `/`, which is the app."""
    return _page("home.html")


@router.get("/emergency")
async def page_emergency():
    """Public emergency numbers. No auth, no account, no cookie wall.

    Someone may arrive here from a search engine while standing over a person who
    is not breathing. Nothing may stand between them and a dialable number.
    """
    return _page("emergency.html")


@router.get("/contact")
async def page_contact():
    return _page("contact.html")


@router.get("/terms")
async def page_terms():
    return _page("terms.html")


@router.get("/privacy")
async def page_privacy():
    return _page("privacy.html")


@router.get("/data-deletion")
async def page_data_deletion():
    return _page("data-deletion.html")


@router.get("/login")
async def page_login():
    return _page("login.html")


@router.get("/register")
async def page_register():
    return _page("register.html")


# ---------------------------------------------------------------------------
# Liveness
# ---------------------------------------------------------------------------
@router.get("/healthz")
async def healthz() -> dict:
    """Liveness probe, also handy for confirming the server is up during a demo."""
    return {"ok": True}


# ---------------------------------------------------------------------------
# Crawler surface
# ---------------------------------------------------------------------------
@router.get("/robots.txt")
async def robots() -> Response:
    """Crawler policy.

    The public pages SHOULD be indexed: someone searching "what to do if someone
    overdoses" should be able to find the bystander guide and the emergency numbers.
    Everything behind authentication is disallowed — not as a security measure
    (that is what the session check is for) but so that no fragment of a person's
    recovery surface can end up in a search index.
    """
    body = (
        "# Threshold — https://threshold.local\n"
        "# Public help pages are open to crawlers on purpose: someone searching\n"
        "# for overdose guidance should be able to find them.\n"
        "User-agent: *\n"
        "Allow: /home\n"
        "Allow: /emergency\n"
        "Allow: /bystander\n"
        "Allow: /contact\n"
        "Allow: /terms\n"
        "Allow: /privacy\n"
        "Allow: /data-deletion\n"
        "\n"
        "# Everything below is a person's private recovery surface.\n"
        "Disallow: /api/\n"
        "Disallow: /caregiver\n"
        "Disallow: /onboarding\n"
        "Disallow: /ladder\n"
        "Disallow: /login\n"
        "Disallow: /register\n"
        "\n"
        "Sitemap: /sitemap.xml\n"
    )
    return Response(content=body, media_type="text/plain")


@router.get("/sitemap.xml")
async def sitemap() -> Response:
    """Sitemap listing only the public, indexable pages.

    Priorities are set by how urgently a stranger might need the page, not by
    marketing value: the emergency numbers and the bystander guide outrank the
    homepage on purpose.
    """
    today = _now().date().isoformat()
    pages = [
        ("/emergency", "1.0", "weekly"),
        ("/bystander", "1.0", "weekly"),
        ("/home", "0.9", "weekly"),
        ("/contact", "0.5", "monthly"),
        ("/privacy", "0.4", "monthly"),
        ("/terms", "0.4", "monthly"),
        ("/data-deletion", "0.4", "monthly"),
    ]
    entries = "\n".join(
        f"  <url>\n"
        f"    <loc>{loc}</loc>\n"
        f"    <lastmod>{today}</lastmod>\n"
        f"    <changefreq>{freq}</changefreq>\n"
        f"    <priority>{pri}</priority>\n"
        f"  </url>"
        for loc, pri, freq in pages
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{entries}\n"
        "</urlset>\n"
    )
    return Response(content=xml, media_type="application/xml")
