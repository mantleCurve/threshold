# Technical decisions

Source material for the write-up site (single-page, Vercel — **later task, not yet built**).
Each entry is a decision, the reason behind it, and what it cost.

---

## Infrastructure

### nginx: SSE buffering explicitly off

```nginx
location /api/events {
    proxy_buffering off;
    proxy_cache off;
    chunked_transfer_encoding off;
    proxy_read_timeout 3600s;
}
```

**Why.** nginx buffers proxied responses by default: it accumulates output until
its buffer fills or the upstream closes, then flushes. For an ordinary HTML page
that is a performance win. For a server-sent event stream it is a correctness
bug — each ladder change is a few dozen bytes, so events sit in the buffer until
enough of them pile up to justify a flush.

On this product that failure has a specific shape. `/api/events` is how a tier
change on the user's screen reaches the **caregiver's** screen. Buffered, a Tier 4
emergency alert arrives late, batched behind other events, or — if the stream
stays quiet after it — not at all. The caregiver sees nothing and reads that
silence as everything being fine.

`proxy_read_timeout 3600s` is the companion setting: a healthy SSE stream is
*mostly idle*, and the default 60s timeout would kill a working connection
roughly once a minute. `chunked_transfer_encoding off` stops nginx re-chunking
a stream the client is already parsing incrementally.

**Cost.** A persistent connection per client, held open, unbuffered. Trivial at
this scale, and the correct trade regardless: a dashboard that updates instantly
is the entire point of the caregiver surface.

### Cloudflare proxy disabled for this subdomain

The A record for `threshold-warmup.mntlcrv.com` is **grey-clouded** (DNS-only),
not proxied.

**Why.** With the orange cloud on, requests never reached our nginx. A
pre-existing Cloudflare Worker or Pages project on the zone was intercepting the
hostname and answering with a different app entirely — `/` returned someone
else's HTML, and every route we actually own 404'd. The response carried a
`permissions-policy` header we never set, which is what identified it. The API
token is zone-scoped and cannot enumerate or unbind Workers, so removing the
interception was not possible with the access available.

Turning the proxy off routes DNS straight to origin, past the Worker entirely.
TLS is then terminated at our own nginx with a Let's Encrypt certificate
(certbot, auto-renewing) rather than at Cloudflare's edge.

**Cost.** No CDN, no edge DDoS absorption, and the origin IP is public. All
acceptable for a hackathon deployment; on a real deployment the right fix is
unbinding the Worker route and putting the proxy back.

### systemd rather than a process manager or container

`Restart=always`, `NoNewPrivileges=true`, `PrivateTmp=true`, `ProtectSystem=full`.
`THRESHOLD_SECRET` lives in a `0600` env file generated **on the server** and
never committed — a hardcoded default in a public repo would let anyone forge a
session cookie for any account.

The secret must also be *stable across restarts*: regenerating it per boot
silently invalidates every existing session on each deploy.

---

## Architecture

### The model is not in the safety-critical path

`app/triage.py` is a pure state machine. It does not import `app/genai.py`, makes
no network call, and reads no wall clock (time is injected, which is what makes
it deterministic and testable). The generative layer only ever does language
work: composing, selecting, summarising, adapting reading level.

**Why.** It answers "what happens when the model is wrong?" in one sentence: the
model was never asked. A hallucinated tier would mean either a missed overdose or
a false alarm that teaches someone to stop talking to the app. 171 tests cover
the state machine specifically; 490 across the suite.

**Cost.** Keyword triage is less capable than an LLM at understanding intent, and
we document that limit honestly in the module docstring rather than hiding it.

### Legal text is a static human-reviewed dataset, never generated

`data/legal/good_samaritan.json`. Every record carries `verified: false` until a
human reads the current statute, and the UI renders that flag as a visible
"unverified — confirm locally" badge.

**Why.** Good Samaritan immunity varies substantially between states and is
amended frequently. Hallucinated legal protection is the worst failure this
product could produce: it could convince someone it is safe to call when it is
not, or that it is unsafe when protection exists. `does_not_cover` is written
harder than `summary`, because overstating immunity is the dangerous direction
and understating it is the survivable one.

### Honest AI status, never a silent fallback

Every `Generation` carries `live`, `model`, `latency_ms`, and `error`. A cached
fallback is always labelled as one, in the UI. With no API key the app reports
"AI offline" rather than substituting canned text.

**Why.** Two reasons that point the same way: the competition rules disqualify
canned output presented as model output, and — more importantly — a user in
crisis must always be able to tell what is real.

### In-memory tier, persisted event log

The live tier is deliberately **not** persisted; the append-only event log is.

**Why.** The ladder describes a live situation ("right now, is this person in
danger?"), not a durable fact about a person. Persisting it means a server
restart could pin someone at Tier 4 forever, or silently resurrect a stale
emergency. The log is the durable record and every entry is user-visible — there
is no hidden log.

---

## Security

- Passwords: `hashlib.scrypt`, per-user 16-byte salt, `hmac.compare_digest`.
- Login returns **one generic error** for unknown-username and wrong-password
  alike, and burns a dummy hash on the unknown path so response timing is not a
  username oracle. Given what an account here implies about a person, whether
  someone *has* one is itself sensitive.
- Session cookie: HMAC-signed, HttpOnly, SameSite=Lax; signature verified before
  the expiry is trusted.
- `_require_own_profile()` gates endpoints exposing home address, apartment
  number, and door entry code. **A bug found by review:** the anonymous fallback
  originally resolved *every* unauthenticated caller to the seeded demo user, so
  `/api/script/911` would hand a real user's address to anyone. Now only the
  published demo fixture is anonymous-readable.
- All model output is escaped before entering the DOM. Never trust a generation.

---

## Deliberate omissions

- **Caregiver voice cloning uses a two-party consent chain.** The *person being
  cloned* consents in their own session with wording stored verbatim; recording,
  sharing, and member selection are separate revocable decisions. Every
  utterance is visibly labelled, presence claims are prohibited, and revocation
  deletes the provider-side model. Memory Vault clips remain real recordings.
- **No always-on listening.** It would materially improve Tier 5 detection and is
  the single largest trust cost available — an always-open microphone is a reason
  to leave the phone in another room, and a phone in another room protects nobody.
- **No sobriety scores, streaks, or gamification.**
- **No build step on the frontend.** Vanilla ES modules, system fonts, no CDN. It
  works with the network down, which is exactly when it matters.

---

## Bugs that testing caught

1. **`help me find my keys` dispatched an ambulance.** A bare `\bhelp\b` at Tier 4
   was the lowest-precision rule in the table. Narrowed to exclude the transitive
   sense (`help me find/with/understand…`) while every real cry — "help", "help
   me", "I need help" — still fires. Routine false alarms are what teach someone
   to stop talking to the app.
2. **Seeded users get UUIDs**, but the demo fallback looked up the literal id
   `"sam"` and silently returned an empty profile.
3. **Two agents wrote conflicting `delete_user_data()`s**; one violated the
   append-only trigger and made account deletion a guaranteed 500.
4. **A test asserted the wrong thing.** It pinned "I used to drink" to Tier 0,
   but the seeded user is 11 days post-discharge, so Tolerance Guard correctly
   holds them at Tier 1. Fixed the test, not the behaviour.
