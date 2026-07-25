# Threshold

A multi-modal, GenAI-powered recovery and prevention platform for people navigating
substance use disorders, and for the people who care for them.

Built for PromptWars (Google for Developers / Build with AI), Thiruvananthapuram.

---

## Demo credentials

Printed on the login screen and pre-filled in the form. No evaluator should ever be
locked out of a feature.

| Role | Username | Password |
|---|---|---|
| Person in recovery | `sam` | `threshold` |
| Caregiver | `sarah` | `threshold` |

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
export THRESHOLD_DEMO_MODE=true           # enables POST /api/reset
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

### What we deliberately did not build

- **No caregiver voice cloning.** Consent is obtained in calm and spent in crisis;
  a disclosure label is doing legal work, not cognitive work, on someone who is
  intoxicated or panicking. The Memory Vault plays real recordings instead.
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

One endpoint is deliberately NOT generative: `/api/script/911` renders the
emergency script from a deterministic template. It contains a home address, an
apartment number and a door entry code, and those facts must be reproduced
character-for-character and must work with the network down. A model is the
wrong tool for reciting an address, so it is not used there — and the response
says so, carrying `deterministic: true` rather than pretending to be a
generation.

When a generative call fails, the UI says so explicitly and labels any cached
fallback as a fallback. A fallback never masquerades as a live generation — look
for the `live` flag on every `Generation` object in `app/models.py`.
