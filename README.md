# Threshold

A multi-modal, GenAI-powered recovery and prevention platform for people navigating
substance use disorders, and for the people who care for them.

Built for PromptWars (Google for Developers / Build with AI), Thiruvananthapuram.

---

## Submission overview

### Chosen vertical

**Recovery and overdose prevention** for people navigating substance use
disorders, their invited caregivers, and bystanders responding to a possible
overdose.

### Approach and logic

Threshold is a multimodal GenAI assistant wrapped in deterministic medical
safety guardrails. Speech input, Gemini-generated interventions, ElevenLabs
spoken output, caregiver context, and visual emergency controls provide a
zero-typing path when cognitive load is high. A six-state escalation ladder
turns explicit phrases, missed check-ins, sensor events, and member actions into
an auditable state. Gemini personalizes what the assistant says; deterministic
rules decide when emergency controls must appear.

### How the solution works

1. A member completes a verified-email account and emergency profile.
2. Push-to-talk captures an utterance; deterministic triage updates the visible
   ladder without waiting for a model.
3. Gemini 3.1 Flash Lite generates the contextual response, prevention guidance,
   refusal script, validated 911 script, Memory Vault selection, or caregiver
   brief appropriate to that moment.
4. ElevenLabs v3 speaks normal interventions; ElevenLabs Flash v2.5 is reserved
   for low-latency urgent guidance. A labelled device-voice fallback prevents
   silence when cloud speech fails.
5. At a medical emergency the interface removes secondary choices, exposes
   one-tap 911 and naloxone guidance, and emails verified linked caregivers.

### Assumptions

- The judged deployment is US-focused, so emergency calling uses 911 and legal
  summaries are selected by US state.
- Phone number is required contact information but is not OTP-verified; email
  ownership is verified through Resend.
- Microphone, speech synthesis, location, and network access may be unavailable,
  so every critical interaction has a visual or local fallback.
- Threshold supports recovery and overdose response but does not diagnose,
  replace clinicians, or confirm that emergency services completed an action.
- Caregiver access exists only after an expiring member-generated invitation;
  permissions are enforced server-side.

**Bystander Mode at `/bystander` requires no account at all.** That is a deliberate
product decision, not an oversight: someone standing over an overdosing stranger must
never be asked to register.

## Run it

```bash
cd threshold
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# The generative layer needs an OpenRouter key. Without it the app runs fully,
# but every AI surface reports "AI offline" rather than inventing text.
export OPENROUTER_API_KEY=sk-or-...       # generative surfaces
export RESEND_API_KEY=re_...              # registration email codes
export ELEVENLABS_API_KEY=sk_...          # optional: cloud + supporter voices
export THRESHOLD_SECRET=$(openssl rand -hex 32)   # stable session signing
export THRESHOLD_HTTPS=true               # only when served over HTTPS

python3 -m uvicorn app.main:app --reload --port 8600
```

Open http://localhost:8600

## Tests

```bash
python3 -m pytest -q
```

---

## What this is

Most crisis apps are panic buttons: they activate after something has already gone
wrong. That describes maybe one percent of the runtime of a person's recovery. The
other ninety-nine percent is an ordinary Tuesday — which is where this product spends
its attention.

The spine of the system is a six-state **escalation ladder** (Baseline → Elevated →
Craving → Active use → Medical emergency → Unresponsive). The ladder is visible to the
user at all times, and the user tunes it themselves.

### The two design decisions that matter

**The model never decides a tier.** Triage is a deterministic, fully-tested state
machine in `app/triage.py` with no network access and no import of the AI layer. The
generative layer does language work only — composing, selecting, summarising,
adapting. When a judge asks "what happens when the model is wrong?", the answer is
that the model was never in the safety-critical path.

**The user owns the escalation thresholds.** Using alone is the single largest risk
factor in a fatal overdose. Any system that pushes people toward concealment is
net-negative no matter how good its alerting is. So the user decides which tiers a
caregiver can see — with exactly one exception, disclosed at onboarding and not
overridable: if the system believes you are in medical danger, it alerts someone.

### A distinctive multimodal feature

- **Consented caregiver voice cloning.** The caregiver records and consents in
  their own account, sharing is a separate action, and the member explicitly
  opts in. Every utterance is labelled as an AI recreation, it may never claim
  the real person is present, and either side can revoke it. Memory Vault
  messages remain real recordings.

### What we deliberately did not build

- **No model-generated legal text.** Good Samaritan immunity varies substantially by
  state, and hallucinated legal protection is the single worst failure mode this
  product could have. `data/legal/` is a static, human-reviewed dataset.
- **No sobriety scoring, streaks, or gamification.** A user who reports honestly
  while using is a user who can still be protected. A user who hides is not.

## Architecture

```
Client (browser)          Voice I/O, stillness/idle detection, offline cache
      │
Triage  app/triage.py     DETERMINISTIC. No model. No network. Auditable.
      │
GenAI   app/genai.py      Language work only. Real OpenRouter/Gemini calls.
      │
Data    app/store.py      SQLite. Append-only event log, fully user-visible.
```

## Honesty about AI calls

The judging rules disqualify canned responses presented as model output. Every
generative surface in this app makes a real API call to Google Gemini via
OpenRouter.

`/api/script/911` uses Gemini to personalize a short dispatcher script ahead of
need. Address, unit, cross-street, and entry facts are validated
character-for-character before the result can be shown. If generation is
offline or changes a critical fact, the endpoint returns a clearly labelled
verified local template instead.

When a generative call fails, the UI says so explicitly and labels any cached
fallback as a fallback. A fallback never masquerades as a live generation — look
for the `live` flag on every `Generation` object in `app/models.py`.
