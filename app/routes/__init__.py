"""HTTP surface split into focused routers, one module per cohesive group of routes.

WHAT THIS PACKAGE DOES
    Holds the parts of the HTTP API that are self-contained enough to live away from
    `app.main`: the static page routes, authentication, the generative endpoints, and
    the public-facing endpoints behind the marketing pages. Each module exposes a
    single `router: APIRouter` which `app.main` includes at import time.

WHAT THIS PACKAGE DELIBERATELY DOES NOT DO
    - It does not contain the ladder core. `/api/state`, `/api/utterance`,
      `/api/sensor`, `/api/tier`, `/api/rescind`, `/api/events`, `/api/profile` and
      `/api/reset` stay in `app.main` on purpose: they are the heart of the product
      and a reader looking for the safety path should find it in the main module
      rather than hunting through a package.
    - It does not own shared state or helpers. Those live in `app.deps`, which both
      this package and `app.main` import. Nothing here imports `app.main` — that
      would be a cycle.
    - No router applies a URL prefix. Every path is written out in full at the
      decorator, so grepping for a URL finds it exactly as the client requests it.
"""
