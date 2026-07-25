# PromptWars Codebase Evaluation & Optimization Report: Threshold

This report evaluates the **Threshold** codebase against the official **Google for Developers | H2S PromptWars** judging parameters. Each parameter has been thoroughly reviewed through static analysis, architectural inspection, and test execution.

---

## Executive Summary & Parameter Impact Breakdown

| Impact Level | Parameter | Status / Score Potential | Primary Recommendation |
| :--- | :--- | :--- | :--- |
| 🔴 **High Impact** | **Code Quality** | Strong Foundation | Modularize `main.py`, clean up frontend JS global states & cache disk reads. |
| 🔴 **High Impact** | **Problem Statement Alignment** | Excellent | Expand slang dictionary for phrase matching & implement real Web Push / SMS webhooks. |
| 🟡 **Medium Impact** | **Security** | Robust Auth & Hashing | Restrict demo session fallbacks on private endpoints & add CORS/CSRF middleware + rate limiting. |
| 🟡 **Medium Impact** | **Efficiency** | High (Async & WAL) | Offload synchronous `sqlite3` calls to worker threads & cache static legal dataset in memory. |
| 🟢 **Low Impact** | **Testing** | 262 Backend Tests Pass | Add Frontend JS unit tests & Playwright E2E automation for full test coverage. |
| 🟢 **Low Impact** | **Accessibility** | High Contrast & ARIA | Auto-focus crisis takeover container & add visual captions for spoken audio responses. |

---

## 1. 🔴 High Impact: Code Quality
> **Criterion:** Clean, readable & well-structured code.

### Current Strengths
1. **Comprehensive Documentation & PRD Traceability:**
   - Modules (`app/main.py`, `app/triage.py`, `app/genai.py`, `app/store.py`, `app/auth.py`, `app/models.py`) include top-level docstrings detailing responsibility boundaries, non-goals, and PRD section citations (e.g., `# PRD P4: model never decides a tier`).
2. **Strict Architectural Separation of Concerns:**
   - Pure, deterministic state machine (`app/triage.py`) operates with zero network or AI dependencies.
   - Network interactions are strictly isolated inside `app/genai.py`.
   - Data transfer schemas are defined in `app/models.py` using Pydantic V2 with strict type hints (`from __future__ import annotations`).
3. **Resilient Error Handling & Degraded Execution:**
   - `app/main.py` uses lazy imports for `genai` inside API handlers, allowing non-AI endpoints and emergency surfaces to operate seamlessly even if OpenRouter/Gemini keys are absent.
4. **Declarative CSS State Engine:**
   - UI transformations rely on `[data-tier]` state attributes on `<body>`, ensuring layout transitions are atomic and immune to script interrupts.

### Key Suggestions for Improvement
1. **Modularize `app/main.py`:**
   - **Issue:** `main.py` (~931 lines) currently handles routing, session resolution, SSE streaming, static asset serving, legal file reading, and crawler routes.
   - **Recommendation:** Split `main.py` using `fastapi.APIRouter` into focused sub-modules under `app/routers/`:
     - `app/routers/auth.py`
     - `app/routers/triage.py`
     - `app/routers/script.py`
     - `app/routers/legal.py`
2. **Encapsulate Frontend JavaScript State:**
   - **Issue:** `web/js/app.js` and `web/js/caregiver.js` manage key variables (`currentTier`, `emergencyTimers`) in script scope without encapsulation.
   - **Recommendation:** Wrap state and actions into modular ES objects or standard state stores (e.g., `const AppState = { tier: 0, timers: [] }`) to prevent variable collisions or leakage across page lifecycle events.
3. **In-Memory Caching of Static Legal Dataset:**
   - **Issue:** In `app/main.py` (`get_legal`), `good_samaritan.json` is read and parsed from disk synchronously on every request.
   - **Recommendation:** Load and parse `good_samaritan.json` into a global dictionary at application startup (lifespan hook) for instant O(1) in-memory lookups.

---

## 2. 🔴 High Impact: Problem Statement Alignment
> **Criterion:** Targets core challenge, user needs & objectives.

### Current Strengths
1. **Clinical Safety Architecture:**
   - Addresses the core challenge of opioid/substance overdose by keeping the AI generative layer strictly out of the safety-critical path (PRD P4). Triage is 100% deterministic and auditable.
2. **Frictionless Bystander Mode (`/bystander`):**
   - Unauthenticated access ensures bystanders acting during active overdoses are never blocked by login or sign-up screens.
3. **Good Samaritan Statutory Integrity:**
   - Legal immunity guidance (`data/legal/good_samaritan.json`) is served strictly from static, human-reviewed data, eliminating dangerous AI hallucinations regarding legal protection.
4. **Honest AI State Communication:**
   - All AI output carries a `live: bool` flag and latency metric. Fallbacks are explicitly labeled in the UI (`live=False`), adhering strictly to competition ground rules.
5. **Proactive Prevention (Tolerance Guard):**
   - Implements a 90-day post-abstinence/discharge window recognizing that physical tolerance collapses rapidly while dose memory persists.

### Key Suggestions for Improvement
1. **Expand Phrase Matching & Slang Vocabulary in `app/triage.py`:**
   - **Issue:** The `SIGNALS` regex table catches standard phrases but lacks broader regional colloquialisms, slang, and typo tolerance (e.g., "gonna get well", "dirty 30s", "subbie", "fixin to").
   - **Recommendation:** Expand `SIGNALS` in `app/triage.py` to include a wider taxonomy of recovery and relapse terminology, or integrate fuzzy matching for common transcription misspellings.
2. **Production-Ready Emergency Dispatch Webhooks:**
   - **Issue:** Emergency contact notifications currently trigger an action payload (`fire_contact_tree`), but external notifications are marked as "display only in this build".
   - **Recommendation:** Add pluggable webhook handlers for real Web Push (VAPID/FCM) or SMS integrations (e.g., Twilio) so emergency contact alerts send real notifications out-of-band.

---

## 3. 🟡 Medium Impact: Security
> **Criterion:** Safe practices, avoids common vulnerabilities.

### Current Strengths
1. **Robust Password Hashing & Salt Discipline:**
   - Passwords are hashed using `scrypt` (`n=2**14, r=8, p=1`) with unique 16-byte random salts per user (`app/auth.py`). Plaintext passwords are never persisted or logged.
2. **Constant-Time Comparison:**
   - `hmac.compare_digest` is used for password verification and session token validation to prevent timing side-channel attacks.
3. **API Key & Token Redaction Net:**
   - `app/genai.py` contains `_redact()` to sanitize OpenRouter API keys and Bearer tokens from exception messages before logging or returning to clients.
4. **Protective Database Triggers:**
   - SQLite `BEFORE UPDATE` and `BEFORE DELETE` triggers protect the append-only event log (`events` table) against modification or deletion.
5. **Secure Cookie Standards:**
   - Cookies use `HttpOnly` and `SameSite=Lax` flags to mitigate XSS session theft and CSRF attacks.

### Key Suggestions for Improvement
1. **Restrict Anonymous Fallback on Sensitive Endpoints:**
   - **Issue:** `_session_user()` in `app/main.py` falls back to the demo user (`sam`) for unauthenticated requests. While `_require_own_profile()` protects profile/911 routes, endpoints like `/api/utterance` and `/api/tier` allow unauthenticated clients to mutate `sam`'s state.
   - **Recommendation:** Ensure endpoints modifying user state or streaming personal ladder changes strictly enforce active session authentication and return HTTP 401 for unauthenticated calls.
2. **Explicit CORS & Security Headers:**
   - **Issue:** `app/main.py` lacks explicit CORS configuration (`CORSMiddleware`) and standard HTTP security headers (`Content-Security-Policy`, `X-Frame-Options`, `X-Content-Type-Options`).
   - **Recommendation:** Add FastAPI middleware for security headers and restrict CORS origins to prevent unauthorized cross-origin framing or request forgery.
3. **Rate Limiting on Authentication & Public Endpoints:**
   - **Issue:** Endpoints like `/api/auth/login`, `/api/auth/register`, `/api/contact`, and `/api/utterance` do not have rate limits.
   - **Recommendation:** Implement IP-based rate limiting (using libraries like `slowapi` or leaky-bucket algorithms) to prevent brute-force credential attempts and API denial-of-service.

---

## 4. 🟡 Medium Impact: Efficiency
> **Criterion:** Optimal use of time & memory.

### Current Strengths
1. **Lightweight Technology Stack:**
   - Pure FastAPI backend with standard library utilities, SQLite WAL mode (`PRAGMA journal_mode = WAL`), and zero heavy ORM framework overhead.
2. **Zero-Build Vanilla Frontend:**
   - Dependency-free static HTML/CSS/JS frontend eliminates bundler overhead and ensures immediate initial page rendering.
3. **Tiered Model Routing:**
   - Dual-model strategy routes fast user check-ins to `google/gemini-2.5-flash` (~300-500ms latency) and deep artefact generation to `google/gemini-2.5-pro`.
4. **Non-Blocking SSE Streaming:**
   - SSE listener queues use non-blocking `put_nowait()` so slow or disconnected event listeners never block triage execution.

### Key Suggestions for Improvement
1. **Asynchronous Thread Offloading for SQLite I/O:**
   - **Issue:** Database operations in `app/store.py` use synchronous `sqlite3` calls directly within FastAPI's `async def` route handlers. Under high concurrency, synchronous I/O blocks the main Python event loop.
   - **Recommendation:** Wrap database calls in `asyncio.to_thread()` (or adopt an async driver like `aiosqlite`) to keep the FastAPI event loop unblocked during DB reads and writes.
2. **In-Memory Cache for Legal Files:**
   - **Issue:** `/api/legal/{state_code}` parses `good_samaritan.json` from disk on every invocation.
   - **Recommendation:** Pre-load the JSON file into a dictionary during startup to achieve $O(1)$ response time.
3. **Prompt Token Optimization:**
   - **Issue:** Prompts in `app/prompts/caregiver_brief.py` pass the 10 most recent events and full profile schemas to the model.
   - **Recommendation:** Compress event summaries and strip unused profile fields before passing to OpenRouter to reduce prompt token consumption and lower LLM response latency.

---

## 5. 🟢 Low Impact: Testing
> **Criterion:** Easily testable & maintainable code.

### Current Strengths
1. **Extensive Backend Test Suite:**
   - 262 backend tests pass in ~2.1 seconds across 5 test suites (`test_triage.py`, `test_genai.py`, `test_store.py`, `test_auth.py`, `test_api.py`).
2. **Isolated State Machine Testing:**
   - `test_triage.py` contains 136 standalone tests evaluating signal precedence, negation windows, tolerance windows, and tier overrides with zero network or DB dependencies.
3. **Deterministic Time Injection:**
   - All time-dependent methods accept an explicit `now` datetime parameter, enabling deterministic time testing.
4. **Mocked Offline AI Test Mode:**
   - `test_genai.py` tests API retry loops, fallback cache reads, and redaction logic without calling OpenRouter.

### Key Suggestions for Improvement
1. **Introduce Frontend JavaScript / UI Tests:**
   - **Issue:** The repository contains no unit or component tests for `web/js/app.js`, `web/js/caregiver.js`, or `web/js/auth.js`.
   - **Recommendation:** Add lightweight JS tests (using Vitest or Jest) to verify `renderTier()`, SSE payload parsing, and timer handling.
2. **Implement End-to-End (E2E) Automation:**
   - **Issue:** No automated browser tests exist to verify complete user flows (e.g. login -> voice check-in -> tier 4 takeover -> rescind).
   - **Recommendation:** Add a Playwright or Cypress suite covering critical paths to guarantee non-breaking UI updates.
3. **Track Line Coverage with `pytest-cov`:**
   - **Issue:** `pytest-cov` is not configured in `requirements.txt` or CI scripts.
   - **Recommendation:** Add `pytest-cov` to track exact line/branch coverage metrics across `app/`.

---

## 6. 🟢 Low Impact: Accessibility
> **Criterion:** Usable for diverse users & environments.

### Current Strengths
1. **High-Contrast Emergency Interface:**
   - Near-black canvas with bold high-contrast accent colors and large typography on crisis takeover states.
2. **ARIA Live & Navigation Isolation:**
   - `aria-current="step"` marks active ladder rungs. `aria-hidden="true"` is applied to main page content during Tier 4/5 takeover so screen readers prioritize emergency actions.
3. **Semantic HTML Structure:**
   - HTML documents use proper `<main>`, `<nav>`, `<header>`, and `<article>` tags with clean sectioning.

### Key Suggestions for Improvement
1. **Automated Focus Trapping & Management on Emergency Takeover:**
   - **Issue:** When Tier 4/5 takeover occurs in `web/js/app.js`, focus remains on the triggering element rather than moving automatically to the takeover container or primary action button.
   - **Recommendation:** Call `takeoverElement.focus()` upon emergency takeover activation and trap keyboard focus inside the takeover container while active.
2. **Visual Captions for Web Speech Output:**
   - **Issue:** Spoken prompt audio using Web Speech API (`speak()`) does not render real-time on-screen text captions.
   - **Recommendation:** Display visible text captions for all spoken audio responses to ensure deaf or hard-of-hearing users receive identical guidance.
3. **Explicit `:focus-visible` Indicators:**
   - **Issue:** Certain custom interactive elements rely on standard browser focus outlines which may blend into dark backgrounds.
   - **Recommendation:** Add high-visibility custom `:focus-visible` outlines (e.g. 3px solid `#38bdf8` with 2px offset) across all interactive rungs and buttons in `web/css/base.css`.

---

## Summary Checklist for Competitors / Evaluators

| Area | Recommended Action | Impact |
| :--- | :--- | :--- |
| **Code Quality** | Refactor `app/main.py` into FastAPI Routers | High |
| **Problem Alignment** | Expand slang dictionary in `triage.py` & add real Web Push | High |
| **Security** | Require sessions for state-changing routes & add rate limiting | Medium |
| **Efficiency** | Offload SQLite calls via `asyncio.to_thread` & cache `good_samaritan.json` | Medium |
| **Testing** | Add Playwright E2E tests for crisis takeover | Low |
| **Accessibility** | Add auto-focus to takeover container & visual speech captions | Low |
