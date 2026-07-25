"""Route modules mounted by `app.main`.

WHAT IS HERE
    `voice.py` — the consented supporter-voice surface. It is included by
    `app.main` via `app.include_router(voice_routes.router)`.

WHAT USED TO BE HERE, AND WHY IT IS GONE
    This package began as a refactor to split `app/main.py` into routers. The
    refactor was never completed: `auth_routes.py`, `generate.py`, `pages.py`
    and `public.py` defined 29 routes between them and NONE of them were ever
    mounted. Every request continued to be served by the copies in `app/main.py`.

    They were not merely dead — they had drifted, and drifted in the dangerous
    direction. The abandoned `generate.py` sent the 911 script (home address,
    apartment number, door entry code) to the language model, where the live
    code renders it deterministically and never transmits it. Its vault
    selection read every clip in the database rather than scoping to the owner.
    Its caregiver brief had no authorization check at all. Its `_generate` calls
    omitted `owner_id`, so cached generations would have survived account
    deletion.

    None of that ran. But a reader — or a judge — grepping this repository would
    have found code that looks like three separate safety failures, with nothing
    marking it as unreachable. Dead code that contradicts live code is worse
    than dead code, because it misrepresents what the system does.

    Deleted rather than fixed: `app/main.py` is the single implementation, and
    maintaining a second copy of every route is how the two diverged in the
    first place.
"""
