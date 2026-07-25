# Threshold — build contract

Every agent codes against this file. Do not change it unilaterally; if something here
is wrong, say so rather than diverging.

## Ground rules (from the judging slides — these are disqualifiers)

1. **No mock or fake data.** No hardcoded prose presented as model output.
2. **Every AI response is a real API call.** If the call fails, the UI must say so
   visibly. A fallback is labelled a fallback, always.
3. **Every feature must work end-to-end**, in any order, clicked by a stranger with
   no instructions. Not just along the demo path.
4. **Auth exists, but must never block an evaluator.** New accounts use full
   name + email + phone + password, with email verification and no phone OTP.
   Demo credentials are printed on the login screen AND pre-filled in the form,
   and repeated in the README. One-click "sign in as demo user" / "sign in as
   caregiver" buttons. Registration works end-to-end so an evaluator can make
   their own account. **Bystander mode is deliberately outside the auth wall** —
   PRD §3 says a bystander has no account and must never be asked to create one.
5. Scored: Code Quality (high), Problem Statement Alignment (high), Security (med),
   Efficiency (med), Testing (low), Accessibility (low).

## Commenting standard (applies to every file, no exceptions)

Code Quality is a high-impact scored category and a human evaluator reads this repo.
Comment thoroughly and in detail:

- **Every module** opens with a docstring: what it does, what it deliberately does
  NOT do, and how it fits the architecture.
- **Every function and class** gets a docstring: purpose, parameters, return value,
  and any non-obvious failure modes.
- **Every non-trivial block** gets an inline comment explaining the *why*, not the
  *what*. `# increment i` is noise; `# tolerance collapses within days, so the
  window is deliberately short` is the standard.
- **Every clinical or safety decision** cites the PRD principle or section that
  drives it (e.g. `# PRD P4: the model never decides a tier`).
- **Every deliberate omission** is commented, so a reader knows it was a choice and
  not an oversight.
- CSS: comment each token group and each tier state. HTML: comment each region.
- Prefer comments that would help a judge understand the reasoning in 10 seconds.

## Architecture invariants

- `app/triage.py` is a **pure, deterministic state machine**. It never imports
  `genai`. It never makes a network call. It is the safety-critical path.
- `app/genai.py` is the only module that calls OpenRouter. `app/voice.py` calls
  ElevenLabs and `app/email.py` calls Resend; all provider keys remain server-side.
- The model does language work only: composing, selecting, summarising, translating.
  It never decides a tier.
- Good Samaritan legal text is a **static reviewed dataset** in `data/legal/`.
  Never model-generated. This is the worst possible hallucination in this product.

## Shared types

`app/models.py` is the single source of truth. Import from it; do not redefine.

## GenAI access

Provider: OpenRouter. Model: `google/gemini-3.1-flash-lite` for fast/interactive
paths and `google/gemini-3.1-flash-lite-preview` for prepared scripts and
caregiver summaries.
Key comes from `OPENROUTER_API_KEY` env var. **It is not set yet.** Code must:
- start cleanly without it,
- surface a clear "AI offline — no API key" state in the UI,
- work fully the moment it is exported, with no code change.

## HTTP API (frozen — frontend and backend both depend on this)

```
POST /api/auth/register  {full_name, email, phone, password, role} -> sends email code
POST /api/auth/register/verify {pending_id, code} -> creates account + sets session
POST /api/auth/login     {username, password}        -> sets session cookie
POST /api/auth/logout
GET  /api/auth/me                   -> {username, role} or 401
GET  /api/state                     -> {tier, tier_name, reason, profile, events[], ai_online}
POST /api/utterance  {text}         -> TriageResult + generated reply (SSE for stream)
POST /api/sensor     {still, secs}  -> TriageResult
POST /api/tier       {tier}         -> manual override (demo control), returns TriageResult
POST /api/rescind                   -> one-tap false-alarm rescind, drops to Tier 1
GET  /api/script/112                -> Generation (live gen, cached fallback)
GET  /api/script/refusal            -> Generation
GET  /api/tolerance                 -> Generation (proactive prevention message)
GET  /api/legal/{state_code}        -> static statute record, never generated
GET  /api/vault/select?context=     -> chosen VaultClip + Generation (why this clip)
GET  /api/caregiver/brief           -> Generation (situation summary + next 60s)
GET  /api/events                    -> SSE stream of ladder changes
POST /api/reset                     -> restore seeded demo state
```

## Frontend

Vanilla JS, no build step, no framework, no CDN dependency at runtime.
Routes: `/login`, `/register`, `/` (user), `/caregiver`, `/bystander` (NO auth),
`/onboarding`, `/ladder`.

Demo credentials (printed on the login screen, pre-filled, and in the README):
- user:      `sam` / `threshold`
- caregiver: `sarah` / `threshold`
Design: near-black canvas, one accent, huge type in crisis states, one button at Tier 4.
Accessibility is scored: semantic HTML, real focus states, aria-live on the ladder,
prefers-reduced-motion respected, 4.5:1 contrast minimum.
