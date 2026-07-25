# Threshold code review suggestions

Reviewed against the six PromptWars parameters on 25 July 2026. This is a read-only
review of the current working tree; no application source was changed.

## Executive summary

The project has a strong core idea and unusually good deterministic triage tests, but
several end-to-end paths currently contradict the UI and documentation. Fix the P0
items first: they affect the two high-impact judging categories and include safety and
privacy risks.

| Parameter | Current assessment | Main reason |
|---|---|---|
| Code Quality (high) | Needs work | Clear architecture and documentation, but contracts, DOM IDs, event ordering, and behavior have drifted apart. |
| Problem Statement Alignment (high) | At risk | Core user, caregiver, escalation, settings, and contact flows are not end-to-end yet. |
| Security (medium) | At risk | Global unauthenticated SSE/reset paths and missing per-user authorization create cross-account exposure and destructive access. |
| Efficiency (medium) | Fair | Small dependency footprint, but synchronous work blocks async handlers and SSE queues are unbounded/global. |
| Testing (low) | Good unit depth, weak integration depth | 262 tests pass, but browser wiring and authorization boundaries are largely untested. |
| Accessibility (low) | Promising but incomplete | Good semantic intent and CSS support, but the emergency focus model and primary input currently fail in practice. |

Validation performed:

- `pytest -q`: **262 passed**, with one Starlette/httpx deprecation warning.
- Python compilation: passed.
- Dependency check: passed.
- JavaScript syntax checks: passed.
- Static HTML checks found no duplicate IDs and no missing `alt` attributes (the pages
  currently use inline SVG rather than `<img>`).

Passing syntax and unit tests should not be treated as proof that the product works
end-to-end; most of the P0 findings are integration gaps that those checks do not cover.

## P0 — fix before judging

### 1. Make the primary check-in interaction actually submit

**Locations:** `web/index.html:142-163`, `web/js/app.js:243-325`

`index.html` names the push-to-talk button `talk`, while `app.js` searches for `ptt`.
The script therefore returns before wiring speech recognition. The always-visible
`#utterance-form` also has no submit listener; the only typed-form listener is created
inside `enableTypedFallback()`, which also searches for the nonexistent `#ptt`.

**Impact:** The product's primary voice and typed interaction does nothing, so the core
triage loop cannot be reached from the main UI.

**Suggestion:** Use one stable ID, wire the existing form unconditionally, update
`aria-pressed` during recording, and add a browser test that types a phrase, submits it,
and verifies the tier and transcript update.

### 2. Remove false claims that emergency actions already happened

**Locations:** `app/main.py:90-107`, `app/triage.py:733-808`,
`web/index.html:287-331`, `web/js/caregiver.js:375-436`,
`web/js/ladder.js:79-103`, `web/terms.html:32-38`

`_record()` immediately writes every planned `Action.kind` into `actions_taken`,
including delayed actions. The UI then states “Location sent,” “Contacts called,”
“Door code shared,” and “Started calling the contact list.” No code sends location,
calls a contact, shares a door code, or summons an ambulance. `acquireLocation()` only
shows coordinates on the same device. The Terms correctly say the app cannot summon an
ambulance, which makes the emergency UI internally contradictory.

**Impact:** A user or caregiver can reasonably believe help was dispatched and wait
instead of calling 911. It also violates the build rule that features must be real,
not simulated or implied.

**Suggestion:** Separate `planned_actions` from execution receipts. Record an action as
completed only after the responsible adapter confirms it. Until outbound delivery
exists, use exact language such as “911 button shown” and “contact call not sent,” and
remove all “help is coming/already called/sent” claims.

### 3. Implement real user-to-caregiver relationships and per-user event delivery

**Locations:** `app/main.py:55-59`, `app/main.py:76-87`,
`app/main.py:309-340`, `app/main.py:472-504`, `app/main.py:622-640`,
`app/seed.py:137-157`, `web/js/caregiver.js:647-689`

There is no relationship table connecting Sarah's caregiver account to Sam's account.
After Sarah signs in, `/api/state` resolves Sarah's own user ID, which has no profile or
events. Meanwhile, `/api/events` broadcasts every user's event to every listener and
the caregiver client applies every received tier without checking the `user_id`.

**Impact:** The demo caregiver cannot reliably watch Sam, while a caregiver or anonymous
listener can receive unrelated users' tier reasons and identifiers.

**Suggestion:** Add an explicit consented caregiver relationship with a watched-user ID,
authorize it server-side, scope `/api/state`, `/api/caregiver/brief`, and SSE to that
relationship, and filter visibility by the watched user's ladder configuration. Never
rely on a client-side `user_id` check as the privacy boundary.

### 4. Protect the global reset endpoint

**Locations:** `app/main.py:643-654`, `app/seed.py:304-323`,
`app/store.py:283-309`

`POST /api/reset` is unauthenticated and calls `drop_all()`, deleting every account,
profile, event, and credential before reseeding the demo.

**Impact:** Any visitor can erase the entire deployment. This is the highest-severity
security issue in the repository.

**Suggestion:** Disable reset outside an explicit demo environment. In demo mode,
require an admin/demo-only capability and reset only the seeded fixture, never shared
tables or evaluator-created accounts. Add a test proving anonymous and ordinary users
receive 403.

### 5. Complete the profile/ladder onboarding path

**Locations:** `app/main.py:643-683`, `web/js/onboarding.js:284-344`,
`web/js/onboarding.js:416-424`

`POST /api/profile` now updates an existing profile, but it resolves anonymous callers
to the public demo profile, cannot create a profile for a newly registered user, ignores
the submitted `missed_checkins_to_elevate` field, and has no contact-editing path. The
contact button explicitly reports that contacts are not editable.

**Impact:** The seeded demo can update some fields, but the product's central
promise—“the user owns the escalation thresholds”—is not end-to-end for a new account
and is not protected by a real owner-only boundary.

**Suggestion:** Make the profile endpoint authenticated and owner-only, validate a
complete typed payload, create the initial profile during onboarding, persist every
visible editable field, implement contact editing, and test save/reload persistence.
Continue enforcing server-side that tiers 4 and 5 cannot be hidden.

### 6. Correct event order everywhere

**Locations:** `app/store.py:849-880`, `app/prompts/caregiver_brief.py:127-152`,
`web/js/app.js:612-621`, `web/js/caregiver.js:408-436`,
`web/js/ladder.js:109-181`

`store.list_events()` returns newest first. Multiple consumers document the list as
oldest first, slice the wrong end, and reverse it. The caregiver prompt can therefore
identify the oldest event as the latest reason and describe events in reverse order.
The “newest first” event-log page currently reverses a newest-first response.

**Impact:** The caregiver can receive a misleading incident chronology, and the audit
log does not match its label.

**Suggestion:** Choose one wire-order contract, encode it in the response schema, and
normalize once at the API boundary. Add integration tests with three timestamped events
that assert API order, visual order, prompt order, and which reason is considered
current.

### 7. Replace universal, unverified legal assurances

**Locations:** `web/bystander.html:46-55`,
`web/js/bystander.js:113-161`, `data/legal/good_samaritan.json`

The bystander page states “You will not be arrested” and says the caller and person who
overdosed are protected from possession charges. At the same time, all eight legal
records are `verified: false`, coverage differs by state, and the repository explicitly
calls these summaries unconfirmed.

**Impact:** The strongest legal claim bypasses the state-specific cautious dataset and
can be wrong even when the dynamic record is correctly labelled unverified.

**Suggestion:** Keep the unconditional message to “Call 911 and stay with them,” make
all legal scope state-specific and qualified, and do not show substantive legal
summaries until a named human has checked the current primary source. Verify the demo
state first and record reviewer/date.

## P1 — major scoring and release issues

### Code Quality

1. **Replace raw request dictionaries with schemas.**  
   `app/main.py` accepts `dict` bodies and manually casts values. For example,
   `int(body.get("silent_seconds", 0))` can produce a 500, while
   `bool("false")` becomes `True`. Define bounded Pydantic request models for auth,
   utterance, sensor, tier, contact, and profile updates; return consistent 422/400
   errors.

2. **Reconcile `CONTRACT.md` with the implementation.**  
   Examples: the contract says sensor input is `{still, secs}` but the server expects
   `silent_seconds`; it describes an SSE utterance response while the route returns
   JSON; it says anonymous `/api/auth/me` returns 401 while the implementation returns
   200; and `/api/profile` exists outside the frozen contract. Treat the contract
   as executable: add contract tests or generate OpenAPI clients from typed schemas.

3. **Make comments describe executable truth.**  
   Several detailed comments claim `inert` and focus movement exist, that scripts are
   generated in calm, that real recordings play, or that actions ran when the code does
   none of those things. These comments make review harder because they conceal drift.
   Prefer a shorter verified invariant plus a test over a long aspirational comment.

4. **Reduce duplicated frontend protocol code.**  
   Tier names, action labels, request helpers, escaping, AI badges, auth/logout, and SSE
   handling are repeated across several large JS files. Move shared constants/helpers
   to ES modules and keep page modules focused on page behavior. This will reduce the
   order and wording drift already visible.

5. **Narrow broad exception handling.**  
   Login converts any exception—including a database failure—into “incorrect
   password”; seed/auth/profile helpers silently swallow unrelated failures. Catch
   expected domain exceptions, log unexpected failures with a request ID, and return a
   safe 5xx instead of misdiagnosing infrastructure as user error.

6. **Bound all public inputs.**  
   Add maximum lengths for utterances, usernames, passwords, context query strings, and
   JSON request size. This controls memory, model cost, cache growth, and log size.

### Problem Statement Alignment

1. **Wire every visible button to a real outcome.**  
   In the main app, `show-911` and `start-grounding` have no handlers. Refusal,
   Samaritan, tolerance, and 911 renderers target IDs that are absent from
   `index.html` (`refusal-panel`, `samaritan-panel`, `tolerance-msg`, `script-911`).
   Consolidate the harm panel or add the intended targets, and add one browser test per
   visible control.

2. **Implement actual sensing or label it entirely as a demo.**  
   The backend accepts stillness/silence, but the browser has no real idle/stillness
   monitor; only a “Simulate” button and an emergency timer call `/api/sensor`.
   Do not claim automatic unresponsiveness detection until signals are collected with
   explicit consent and tested on supported devices.

3. **Do not call seeded transcripts “real recordings.”**  
   Every seeded `VaultClip.audio_path` is `None`, yet README and UI copy say the Memory
   Vault plays real recordings. Either capture and play consented audio end-to-end or
   clearly label this demo as transcript-only. Also store clip ownership and consent.

4. **Precompute or deterministically template the 911 script.**  
   The code first requests it 15 seconds into an emergency. With no prior successful
   call, the cache is empty. Address, unit, and entry code should be rendered
   deterministically and validated character-for-character; a model may help with
   noncritical phrasing but should not be the source of location facts.

5. **Make “full event log” truthful.**  
   `/api/state` returns only 50 events while the page claims every event with no cap.
   Add a paginated owner-only events endpoint and an export path that retrieves all
   pages, or state the visible limit.

6. **Decide what a restart means during an incident.**  
   Live tier is process-local and resets to Baseline on restart. That avoids reviving a
   stale emergency, but it also silently clears an active one and breaks across
   multiple workers. Store a short-lived incident with expiry/acknowledgement, or
   explicitly constrain the deployment to one process and document/test restart
   behavior.

### Security

1. **Require authentication and ownership for private APIs.**  
   `_session_user()` silently maps anonymous requests to Sam, so anonymous callers can
   read the demo profile and mutate its tier/log. Demo convenience should happen via a
   real one-click login, not an authorization bypass. Separate public bystander/legal
   routes from authenticated user routes.

2. **Add role authorization.**  
   `auth.require_role()` exists but `main.py` does not use it. Enforce user/caregiver
   roles and resource ownership on state, briefs, profiles, logs, mutations, deletion,
   and streams.

3. **Add rate limits and abuse controls.**  
   Login, registration, contact submission, AI generation, SSE connections, and
   reset-sensitive operations are currently unlimited. Apply per-IP and per-account
   limits, concurrency caps for model calls, generic login responses, and audit events
   for abuse—not sensitive prompt content.

4. **Fix session cookie deployment behavior.**  
   Login and registration always use the default `secure=False`; neither route passes
   request scheme/configuration. Set `Secure` in deployed HTTPS environments, keep
   `HttpOnly`/`SameSite`, add HSTS at the TLS edge, and test proxy-header handling.

5. **Make account deletion match the published promise.**  
   The deletion page promises to remove Vault recordings/transcripts and cached
   generations. `delete_user_data()` deliberately leaves global vault clips, and the
   generation cache has no user ownership or deletion path. Add owner IDs and
   per-user cache metadata, then verify deletion across DB, disk cache, live state,
   and sessions.

6. **Protect sensitive generation cache files.**  
   Cached 911 output can contain address and entry code. Store it with restrictive
   permissions, encryption appropriate to deployment, expiry, owner association, and
   deletion support. Do not use a prompt hash as the only ownership mechanism.

7. **Validate outbound model data minimization.**  
   Caregiver prompts include substance data and event details; 911 prompts include
   address and entry code. Document exactly which endpoint sends which fields, require
   explicit user consent, and avoid sending deterministic facts that do not need model
   processing.

## P2 — efficiency, tests, and accessibility

### Efficiency

1. **Move blocking work off the async event loop.**  
   SQLite operations and scrypt hashing run synchronously inside async handlers.
   Use sync FastAPI handlers/threadpool boundaries or an async persistence strategy, and
   cap concurrent password work to prevent CPU starvation.

2. **Reuse one `httpx.AsyncClient`.**  
   The main routes do not pass a shared client, so each generation creates a new client
   and connection. Create it in the application lifespan and reuse pooled connections.

3. **Bound and isolate SSE queues.**  
   Each listener gets an unbounded queue, broadcasts are global, and state exists only
   inside one process. Use bounded per-user channels with a defined overflow policy,
   authorization, disconnect cleanup, and either a single-worker constraint or a real
   broker for multi-worker deployment.

4. **Cache immutable legal data after validated startup loading.**  
   The JSON file is read and parsed on every request. Load it once, validate its schema,
   index by state code, and fail visibly if malformed.

5. **Use an atomic event sequence.**  
   `MAX(seq) + 1` is vulnerable to concurrent writers and `seq` is not unique. Use an
   integer primary key/autoincrement or another database-enforced ordering mechanism.

### Testing

1. **Add real browser integration tests.**  
   Cover login by both roles, typed check-in, voice fallback behavior, every tier button,
   emergency takeover, rescind, ladder save/reload, caregiver receipt, legal-state
   switching, deletion, and every visible action button. These would have caught the ID
   and missing-target issues immediately.

2. **Add authorization/isolation tests.**  
   Create two users and two caregivers. Prove each cannot read, mutate, subscribe to,
   delete, or generate against another user's resources. Include anonymous requests and
   an SSE event from the unrelated user.

3. **Add destructive-route tests.**  
   Prove reset is disabled in production, scoped in demo mode, and never deletes a
   non-demo account. Prove account deletion removes DB rows, cache files, sessions, and
   live-state entries.

4. **Isolate API tests from the developer database.**  
   `tests/test_api.py` imports the real app and does not visibly redirect
   `THRESHOLD_DB`; its tests mutate tiers and append events. Give every API test a
   temporary database and reset in-memory listeners/tiers between tests. Avoid
   module-scoped shared state so tests genuinely pass in any order.

5. **Test current provider integration separately.**  
   Keep deterministic mocked unit tests, but add an opt-in smoke test using a restricted
   key to verify the current OpenRouter request/response contract, model IDs, timeout,
   and `live` metadata. Never make it part of the default offline suite.

6. **Add automated accessibility and contract checks.**  
   Run axe on every route at calm and emergency tiers, keyboard-only smoke tests, HTML
   validation, a broken-link/static-asset check, and OpenAPI schema tests in CI.

### Accessibility

1. **Implement a real emergency focus trap.**  
   `index.html` says the background becomes `inert` and focus moves to the 911 action,
   but `app.js` only sets `aria-hidden` on `#main`. The focused control can remain inside
   an `aria-hidden` subtree, while rail controls remain keyboard-accessible behind the
   overlay. Set `inert` on the whole shell, focus the 911 link on entry, keep focus
   inside the alert dialog, and restore focus/inert state on rescind.

2. **Update the live tier announcement.**  
   The main page has `#tier-announcer`, but its `renderTier()` never writes to it.
   Update it once per meaningful tier transition with tier name and deterministic
   reason; avoid announcing unchanged tiers repeatedly.

3. **Expose recording state correctly.**  
   Even after the ID mismatch is fixed, the script changes `data-state` but not
   `aria-pressed`. Keep pointer, touch, keyboard, visible label, and ARIA state in sync,
   and handle pointer cancellation/lost capture so recording cannot remain stuck.

4. **Verify 44×44 CSS-pixel touch targets.**  
   Rail navigation and small quiet controls use compact padding and very small text.
   Measure all interactive targets on mobile, especially in the bystander and emergency
   flows, and increase hit areas without necessarily increasing visible size.

5. **Give critical pages a clear top-level heading.**  
   Bystander and caregiver pages start with lower-level headings or styled paragraphs.
   Add one descriptive `<h1>` per page while retaining the current visual scale.

6. **Test at 200% zoom and with screen readers.**  
   Validate the horizontal mobile ladder, emergency takeover, live regions, generated
   content, state picker, and rescue-breathing timer with VoiceOver/NVDA and keyboard
   only. Ensure assertive regions do not repeatedly interrupt each other.

## Positive findings to preserve

- The deterministic triage engine is isolated from model/network code and has extensive
  boundary, negation, purity, consent, timing, and tolerance-window tests.
- Password hashing uses scrypt with per-user salts and constant-time comparison;
  login errors avoid username enumeration.
- Model failures use explicit `live`/`error` metadata, outputs are escaped before
  `innerHTML`, output lengths are bounded, and provider error bodies are not exposed.
- The frontend uses semantic buttons/links, labels, skip links, visible focus styles,
  reduced-motion handling, responsive rules, and documented contrast-aware tokens.
- Bystander mode is public by design and the 911 link remains a plain `tel:` link that
  does not depend on JavaScript.
- The runtime dependency set is small, JavaScript needs no build step, and immutable
  legal data is kept outside the generative path.

## Recommended order

1. Fix the primary input and all dead/mismatched controls.
2. Remove every unconfirmed “called/sent/help is coming” statement.
3. Lock down reset, state, SSE, and all owner/role boundaries.
4. Build the real caregiver relationship and profile/ladder save path.
5. Correct event ordering and action execution records.
6. Replace unverified universal legal claims.
7. Add end-to-end browser, authorization, accessibility, and destructive-route tests.
8. Then address async blocking, connection reuse, queue bounds, and UI polish.
