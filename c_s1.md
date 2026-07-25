# Threshold code review suggestions

Reviewed against the six PromptWars parameters on 25 July 2026. This is a
read-only review of the current working tree. No application source code was
changed.

## Executive summary

The project has a strong, well-tested deterministic triage core and unusually
thoughtful safety documentation. The main judging risk is that several runtime
boundaries do not yet enforce the promises made by that documentation.

| Parameter | Assessment | Main reason |
|---|---|---|
| Code Quality — high impact | Good foundations, significant drift | Clear domain models and documentation, but `app/main.py` is a 1,553-line duplicate of several dormant router modules, and defined request schemas are not used by the live routes. |
| Problem Statement Alignment — high impact | At risk | The ladder works, but “alert someone” currently means an SSE update to an already-open caregiver page; planned emergency steps are also recorded as completed actions. |
| Security — medium impact | At risk | A linked caregiver receives the member's complete profile and event history even where the UI promises tier-based redaction. Voice cloning also exposes an arbitrary-text impersonation surface. |
| Efficiency — medium impact | Fair to good | The dependency footprint is small and GenAI caching is thoughtful, but synchronous database/auth work runs in async handlers, SSE queues are unbounded, and voice requests create new HTTP clients. |
| Testing — low impact | Strong backend, weak browser coverage | 448 tests pass with 76% application coverage, but the most important privacy and emergency-interaction failures are not asserted end to end. |
| Accessibility — low impact | Good intent, one critical interaction issue | Semantic HTML, focus styles, reduced motion, captions, and touch targets are strong; the document-wide bystander click handler can hijack the 911 action. |

The highest-value order is:

1. Enforce a redacted caregiver response at the API boundary.
2. Separate planned actions from confirmed execution and add real alert delivery.
3. Resolve the voice-cloning contradiction and arbitrary-text synthesis risk.
4. Make the emergency 911 action impossible for other click handlers to intercept.
5. Then consolidate architecture, activate typed schemas, and add browser tests.

## Validation performed

- `pytest -q` against an isolated temporary database: **448 passed**.
- Coverage: **76% overall**.
- `app/triage.py`: **100% coverage**.
- Python compilation: passed.
- JavaScript syntax checks: passed.
- Installed dependency consistency check: passed.
- Static HTML checks found one `<main>` and one `<h1>` on each page, no
  duplicate IDs, no missing referenced script files, and no `<img>` missing
  `alt`.
- One warning remains: Starlette's current `TestClient`/`httpx` integration is
  deprecated and recommends `httpx2`.

These checks are strong evidence for the deterministic backend. They do not
exercise browser event propagation, caregiver response redaction, actual push
delivery, or third-party voice behavior.

## P0 — fix before judging

### 1. Redact caregiver state on the server

**Parameters:** Security, Problem Statement Alignment  
**Locations:** `app/main.py:get_state`, `app/deps.py:resolve_subject`,
`web/js/caregiver.js:renderVisibility`, `app/models.py:UserProfile`

`resolve_subject()` correctly proves that the caregiver has a consented link.
However, `get_state()` then returns the linked member's complete
`UserProfile.model_dump()` and the last 50 unfiltered events. That includes:

- address, unit, cross street, and entry code;
- substances;
- tolerance history;
- contact tree;
- every returned low-tier event and its reason.

The caregiver UI simultaneously promises that substances are never visible,
location is visible only at tier 4+, low-tier events depend on the member's
settings, and nothing remains visible after the member stands down. Client-side
copy does not protect data already present in the network response.

**Suggestion:** Return role-specific response models. A member may receive the
full owner view; a caregiver should receive a server-built projection containing
only fields authorized for the current tier and ladder settings. Filter events
through the same policy used by SSE. Release address/entry information only at
the explicitly authorized emergency tier, and remove it again on stand-down.

Add tests that seed unique secret values, sign in as the linked caregiver, and
assert those values are absent from both response JSON and generated briefs at
every tier where they are not allowed.

### 2. Do not record scheduled actions as completed actions

**Parameters:** Problem Statement Alignment, Code Quality, Security  
**Locations:** `app/main.py:_record`, `app/deps.py:_record`,
`app/models.py:Action`, `web/js/ladder.js:ACTION_LABELS`,
`web/js/caregiver.js:renderAlreadyDid`,
`app/prompts/caregiver_brief.py`

`Action.at_second` describes a plan, but `_record()` immediately copies every
action kind into `Event.actions_taken`. This happens before delayed steps execute
and even for capabilities this build does not implement.

The consequence is visible:

- the owner log maps `fire_contact_tree` to “Called your contacts, in order”;
- the caregiver log can claim location was acquired, a recording was played, or
  the screen was kept awake without a success receipt;
- the caregiver prompt is instructed to describe these records as “what the
  system already did,” so the model can amplify the incorrect record.

The emergency screen itself has improved wording, but the durable audit log still
conflates intent and completion.

**Suggestion:** Store `planned_actions` separately from immutable execution
receipts. Append a completion only after the responsible adapter succeeds, with
status, time, destination class, and a safe failure reason. Render “scheduled,”
“attempted,” “delivered,” and “failed” distinctly. Never infer completion from a
triage result.

### 3. Make “alert someone” a real delivery path

**Parameters:** Problem Statement Alignment, Security, Efficiency  
**Locations:** `app/triage.py:notify_caregiver_for`,
`app/deps.py:_broadcast`, `app/main.py:sse`,
`web/js/app.js:runEmergencySequence`, `app/models.py:Contact`,
`web/js/caregiver.js:boot`

`notify_caregiver=True` currently produces only an in-process SSE event. It
reaches a caregiver only if their page is already open, signed in, connected to
the same process, and not interrupted by a restart or proxy failure. There is no
durable notification queue, Web Push, SMS, call, or confirmed receipt.

The “Call [member]” control is also not end to end. It reads the first contact's
`channel`, although `Contact.channel` is documented as a display-only value such
as `phone`, `sms`, or `push`, not the member's verified telephone number.

**Suggestion:** Add at least one real, consented delivery adapter with a verified
destination and delivery receipt. Keep retries bounded and idempotent. Model the
member's phone separately from their escalation contact tree. Until delivery
exists, keep all product and demo claims at the exact level of truth:
“shown on already-open caregiver screens,” not “alerted someone.”

### 4. Resolve the voice-cloning product and safety contradiction

**Parameters:** Problem Statement Alignment, Security, Code Quality  
**Locations:** `README.md:73-80`, `CONTRACT.md:41-49`,
`docs/TECHNICAL-DECISIONS.md:145-155`, `web/onboarding.html`,
`app/voice.py`, `app/routes/voice.py`, `web/js/voice.js`

The repository's declared product decision is “No caregiver voice cloning,” and
the contract says `app/genai.py` is the only networked module. The implementation
now clones caregiver voices through ElevenLabs, while onboarding and other copy
still tell users that voice cloning is deliberately absent. The code itself
acknowledges that the original objections were not answered, only mitigated.

There is also a concrete control failure. `/api/voice/speak` accepts arbitrary
client-supplied text for an authorized clone. `_refuse_presence_claim()` blocks a
short substring list and explicitly admits that paraphrases bypass it. The
comment says the text is composed by the app, but the API does not enforce that.
A member can therefore make the caregiver clone say arbitrary statements.

**Suggestion:** The safest judging path is to remove the cloning feature and keep
the real-recording Memory Vault promised by the product documents. If cloning is
an intentional product change, update the PRD, contract, onboarding, privacy
copy, data-deletion promises, and threat model together. Do not accept arbitrary
speech text: issue server-side, capability-scoped utterance IDs from an approved
message set or a caregiver-reviewed workflow. Add strong provider deletion
reconciliation and do not claim identity proof merely because a caregiver
account uploaded audio.

### 5. Prevent the bystander handler from intercepting 911

**Parameters:** Accessibility, Problem Statement Alignment  
**Location:** `web/js/app.js:hailBystander`

Ten seconds into an emergency, `hailBystander()` installs a one-shot click
handler on the entire `document`. The next click—including activation of the
plain `tel:911` link, the rescind control, or another emergency action—navigates
to `/bystander`.

This is especially risky for keyboard and assistive-technology users because
activating a focused link also produces a click event. A safety-critical action
must not depend on browser ordering between the document handler, navigation,
and the `tel:` default action.

**Suggestion:** Scope bystander navigation to the explicit takeover/button
surface. At minimum, ignore clicks originating in links, buttons, form controls,
or elements with an emergency action role. Add a browser test that advances the
timer, activates “Call 911” by pointer and keyboard, and proves the `tel:911`
action is not replaced.

### 6. Make the emergency script deterministic for location facts

**Parameters:** Problem Statement Alignment, Security, Efficiency  
**Locations:** `app/main.py:get_script_911`,
`app/prompts/script_911.py`, `web/js/app.js:runEmergencySequence`

The route documentation says the script is generated during calm, but the
frontend first requests it 15 seconds into the emergency sequence. On a cache
miss this places a slow network model directly between the user and their
address/entry instructions. A prompt that asks a model to preserve the address
character-for-character is not an enforcement mechanism.

**Suggestion:** Render the dispatcher-critical lines—address, unit, cross street,
entry instruction, breathing, responsiveness, naloxone—using deterministic code
from validated fields. Precompute any optional noncritical phrasing when the
profile changes. Cache it by profile version and keep a fully local fallback.
Add exact-string tests for punctuation, apartment numbers, Unicode addresses,
missing fields, and entry codes.

## P1 — major scoring improvements

### Code Quality

1. **Finish or remove the router migration.**  
   `app/main.py` contains nearly every live endpoint, while
   `app/routes/auth_routes.py`, `generate.py`, `pages.py`, and `public.py`
   duplicate many of them but are never included. Coverage reports all four at
   0%. `app/routes/__init__.py` says routers are included at import time, which is
   not true. Keep one implementation and one dependency layer.

2. **Use the request schemas already written.**  
   `app/schemas.py` has strict, bounded models for registration, login,
   utterances, sensors, tiers, and profiles, but the live routes still accept
   `dict = Body(...)`. This leaves manual coercion such as
   `int(silent_seconds)` and `bool(still)`: malformed numbers can become 500s,
   while the string `"false"` evaluates to `True`. Type the route parameters and
   delete the parallel validation paths.

3. **Remove duplicated infrastructure helpers.**  
   Tier state, broadcasting, recording, session resolution, page helpers, and
   endpoint implementations exist in both `main.py` and extracted modules. The
   planned/completed action bug already exists in both `_record()` copies.
   Consolidation will prevent fixes from landing in only one inactive copy.

4. **Keep comments shorter than the invariant they protect.**  
   The reasoning is excellent, but several comments describe aspirational
   behavior that runtime code does not enforce. Prefer a concise invariant plus a
   test. This makes the high-impact code-quality score easier for a judge to
   verify quickly.

5. **Bring the executable API and `CONTRACT.md` back together.**  
   The contract is marked frozen but omits new invite, profile, deletion, and
   voice surfaces; it also says only `genai.py` touches the network. Either revise
   the contract through an explicit product decision or make the implementation
   conform to it.

### Security

1. **Replace anonymous demo fallback with a real demo session.**  
   `_session_user()` maps unsigned callers to Sam. That allows anonymous callers
   to mutate the demo tier/log and invoke AI-backed routes. A one-click demo login
   satisfies the evaluator requirement without bypassing authentication.

2. **Narrow reset authorization.**  
   `THRESHOLD_DEMO_MODE` is a good gate, but any signed-in account can still call
   a reset that drops the entire database, including evaluator-created accounts.
   Require an explicit demo/admin capability and reset only seeded fixture rows.
   Hide or disable the reset UI when the server reports that demo mode is off.

3. **Do not trust arbitrary `X-Forwarded-For`.**  
   The rate limiter uses its first value without proving the request came through
   a trusted proxy that strips client-supplied values. Configure trusted proxy
   hops or use the socket address; otherwise login and paid-voice limits are easy
   to bypass. Add per-account limits and concurrency caps for GenAI endpoints,
   which currently are not in the limiter.

4. **Tighten the CSP.**  
   The header coverage is otherwise good, but `script-src` and `style-src`
   include `'unsafe-inline'`. Move inline script/style content to static files or
   use nonces/hashes so the CSP provides meaningful XSS containment.

5. **Validate multipart audio before provider upload.**  
   The clone route reads each upload fully into memory and does not verify media
   type, decodability, duration, or that the authenticated person owns the voice.
   Apply a proxy/request-body ceiling, stream bounded chunks, validate supported
   audio, and document the remaining identity limitation honestly.

### Efficiency

1. **Bound SSE queues and document the process model.**  
   Every listener gets an unbounded `asyncio.Queue`; a slow client can accumulate
   events indefinitely. Use a small bounded queue with explicit overflow
   behavior, coalesce superseded tier updates, and cap connections per account.

2. **Move live incident state and delivery out of process memory before scaling.**  
   `_tiers`, listeners, and the rate limiter are process-local. Multiple workers
   will disagree, and a restart silently resets an active incident to Baseline.
   For the current demo, explicitly enforce one worker. For deployment, use a
   shared incident store and pub/sub with expiry and acknowledgement semantics.

3. **Keep blocking work off the event loop.**  
   SQLite access and password hashing are synchronous inside async routes.
   Execute them in a worker thread or use synchronous route functions where
   appropriate. Emergency endpoints should not queue behind a slow password hash
   or large database operation.

4. **Reuse the voice HTTP client.**  
   `app/genai.py` now shares a client, but `app/voice.py` creates a new
   `httpx.AsyncClient` for provider operations. Manage one client in application
   lifespan with explicit connection limits and close it on shutdown.

5. **Bound all paid and user-controlled model inputs.**  
   Activate the existing schemas for utterance/context lengths, cap concurrent
   generations per user, and prevent cache stampedes for identical misses.

### Testing

The deterministic triage suite is the clearest technical strength:
`app/triage.py`, models, schemas, legal data, seed data, and voice persistence all
have excellent coverage. Keep that standard and add tests at the boundaries
where the current suite is weakest.

1. Add a linked-caregiver privacy matrix across tiers 0–5, including stand-down.
2. Assert that planned actions never appear as completed without a receipt.
3. Add Playwright browser flows for check-in, emergency escalation, 911,
   rescind, caregiver refresh/SSE, onboarding persistence, and voice fallback.
4. Run axe or equivalent on every page in dark and light themes and at mobile
   width.
5. Test SSE overflow, disconnect, reconnect, link revocation, restart behavior,
   and multiple workers.
6. Test real adapter contracts in opt-in staging smoke tests without depending on
   them in deterministic CI.
7. Add a repository-level `conftest.py` or app factory that assigns a temporary
   database before importing `app.main`. The current API tests otherwise use the
   configured/default database; the documented plain `pytest` command should
   never mutate development data.
8. Resolve the Starlette/httpx deprecation warning so warning-free CI remains a
   useful signal.

### Accessibility

The accessibility implementation is above average: skip links, visible focus,
semantic buttons/links, live regions, emergency captions, `aria-pressed`,
`aria-current`, focus transfer into the emergency dialog, background `inert`,
reduced-motion support, and large crisis controls are all present.

After fixing the P0 document-click issue:

1. Change the home-page section headings from `h3` to `h2`; the current outline
   jumps from `h1` directly to `h3`.
2. Keep the page `h1` before supporting `h2` content in login/registration DOM
   order where practical.
3. Test speech-recognition denial, no microphone, no speech synthesis,
   geolocation denial, slow network, zoom to 200%, and high-contrast mode.
4. Ensure cloned-voice disclosure is also announced nonvisually for the full
   duration of playback if the feature remains.
5. Give every timer-driven emergency state a persistent visible equivalent; do
   not rely on speech or transient live-region announcements alone.

## Strengths worth preserving

- Triage is deterministic, pure, comprehensively tested, and separated from
  model output.
- AI failure is represented honestly through live/offline state rather than
  disguised canned content.
- Good Samaritan content is static, validated, and not model-generated.
- Caregiver linking uses explicit invites, server-side subject resolution, and
  immediate revocation.
- The caregiver brief now minimizes provider-bound personal data.
- Profile saving is owner-only and enforces non-disableable emergency visibility.
- Account deletion covers database rows, cache ownership, sessions, listeners,
  and best-effort upstream voice deletion.
- The interface has strong semantic and crisis-focused accessibility intent.
- Security headers, password hashing, cookie signing, input ceilings in several
  public paths, and append-only event history are solid foundations.

## Suggested judging strategy

For the biggest score gain, demonstrate a narrow path that is completely true
end to end:

1. A member controls their ladder and creates a caregiver link.
2. A deterministic signal changes the tier.
3. The system delivers a real alert with a receipt.
4. The caregiver receives only data authorized for that tier.
5. The emergency UI preserves direct 911 access and shows only confirmed actions.
6. The model adds language, never safety state or dispatcher-critical facts.

That path directly addresses both high-impact categories while also improving
security, efficiency, testing, and accessibility.
