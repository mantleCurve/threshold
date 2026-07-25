/* ============================================================================
   THRESHOLD — caregiver.js
   Behaviour for the caregiver surface (/caregiver).

   WHAT THIS FILE DOES
     Holds an EventSource open on /api/events so a tier change on Sam's phone
     appears on this page without a refresh, and — when the ladder crosses into
     tier 4 or 5 — pulls GET /api/caregiver/brief and lays it out in the order
     caregiver.html is built around:

       1. what is happening            (tier word + AI situation summary)
       2. what Threshold already did   (deterministic, from the event log)
       3. the next 60 seconds          (one instruction at a time)
       4. what NOT to say              (CRAFT-grounded, easiest to skip, so
                                        given its own visual language)
       5. what this caregiver can and cannot see (the trust boundary, rendered
                                        from the user's OWN ladder config)

   WHAT THIS FILE DELIBERATELY DOES NOT DO
     It never decides a tier. Every tier here arrives from the server's
     deterministic engine, over SSE or from /api/state (PRD P4). A caregiver
     page that inferred severity from the words in a summary would be a model
     making a clinical call by the back door.

     It never presents a fallback as live. `/api/caregiver/brief` returns a
     Generation with a `live` flag; section 2's badge is switched to "offline
     fallback" whenever that flag is false, exactly as app.js does. A caregiver
     acting on cached text at 3am must know it is cached.

     It never invents the visibility statement. Section 5's "you cannot see
     tier 2 and 3" line is only true if Sam left those switches off, so it is
     rendered from profile.ladder rather than hardcoded (PRD P3).

     It never renders model output as markup. Every generated string goes
     through escapeHtml before it touches innerHTML.

   NO BUILD STEP
     Plain ES module, no bundler, no framework, no CDN.
   ========================================================================= */

'use strict';

/* -------------------------------------------------------------------------- */
/* API helpers                                                                 */
/* -------------------------------------------------------------------------- */

/**
 * Fetch JSON with a hard timeout.
 *
 * Same ceiling as app.js and for the same reason: a request that hangs forever
 * is worse than one that fails fast, because the caregiver sits looking at a
 * spinner during the minutes that matter.
 *
 * @param {string} path
 * @param {object} options   Passed through to fetch.
 * @param {number} timeoutMs Abort ceiling in milliseconds.
 * @returns {Promise<object>} Parsed JSON. Rejects on network failure or timeout.
 */
async function api(path, options = {}, timeoutMs = 20000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(path, {
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      signal: controller.signal,
      ...options,
    });
    return await res.json();
  } finally {
    clearTimeout(timer);
  }
}

const post = (path, body) =>
  api(path, { method: 'POST', body: body ? JSON.stringify(body) : undefined });

/** Escape before inserting model output into the DOM. Never trust a generation. */
function escapeHtml(s) {
  const d = document.createElement('div');
  d.textContent = String(s ?? '');
  return d.innerHTML;
}

function setText(id, text) {
  const el = document.getElementById(id);
  if (el) el.textContent = text;
}

/* -------------------------------------------------------------------------- */
/* Tier rendering                                                              */
/* -------------------------------------------------------------------------- */

const TIER_NAMES = [
  'Baseline', 'Elevated', 'Craving', 'Active use', 'Medical emergency', 'Unresponsive',
];

/** The tier this page last heard from the server. Never computed locally. */
let currentTier = 0;

/** Guards against two overlapping brief requests when events arrive in a burst. */
let briefInFlight = false;

/**
 * Apply a tier to the caregiver page.
 *
 * As in app.js, one attribute on <body> drives every colour and type change via
 * CSS, so the emergency presentation cannot end up half-applied because a script
 * error interrupted a sequence of style writes.
 *
 * @param {number} tier   Server-decided tier, 0–5.
 * @param {string} reason The auditable reason string from the triage engine.
 *                        Written by app/triage.py, never by a model.
 */
function renderTier(tier, reason) {
  currentTier = tier;
  document.body.dataset.tier = String(tier);

  // The ladder rail: mark the active rung for sighted users and for AT.
  document.querySelectorAll('[data-tier-step]').forEach((el) => {
    const step = Number(el.dataset.tierStep);
    el.setAttribute('data-state', step === tier ? 'active' : step < tier ? 'passed' : 'ahead');
    el.setAttribute('aria-current', step === tier ? 'step' : 'false');
  });

  setText('alert-tier', TIER_NAMES[tier] || `Tier ${tier}`);
  if (reason) setText('tier-reason', reason);

  // A11Y: the announcer is aria-live="assertive". A caregiver reading this page
  // when the tier changes under them must be interrupted — that is the entire
  // purpose of this surface, and a polite announcement would queue behind the
  // summary text they are already being read.
  setText('tier-announcer', `${TIER_NAMES[tier] || 'Tier ' + tier}. ${reason || ''}`);
}

/* -------------------------------------------------------------------------- */
/* The brief (section 1, 3, 4)                                                 */
/* -------------------------------------------------------------------------- */

/*
  The generative prompt (app/prompts/caregiver_brief.py) is specified to return
  exactly four labelled sections, each label on its own line:

    WHAT HAPPENED / WHAT THE SYSTEM ALREADY DID / NEXT 60 SECONDS /
    WHAT NOT TO DO RIGHT NOW

  We parse against those labels rather than against position, because a model
  that drops a section must produce a visibly missing section here rather than
  silently shifting "what not to say" into the "next 60 seconds" slot. Getting
  that wrong would put CRAFT de-escalation advice under an imperative heading.
*/
const BRIEF_SECTIONS = [
  { key: 'happened', label: 'WHAT HAPPENED' },
  { key: 'did',      label: 'WHAT THE SYSTEM ALREADY DID' },
  { key: 'next',     label: 'NEXT 60 SECONDS' },
  { key: 'notsay',   label: 'WHAT NOT TO DO RIGHT NOW' },
];

/**
 * Split the generated brief into its four labelled sections.
 *
 * @param {string} text Raw generation text.
 * @returns {{happened: string, did: string, next: string, notsay: string}}
 *          Missing sections come back as empty strings — never as filler. An
 *          empty section is rendered as an honest gap, because inventing the
 *          missing paragraph is exactly the failure mode this product cannot have.
 */
function parseBrief(text) {
  const out = { happened: '', did: '', next: '', notsay: '' };
  if (!text) return out;

  // Find each label's offset, tolerating case and stray punctuation around it.
  const marks = BRIEF_SECTIONS
    .map((s) => {
      const idx = text.toUpperCase().indexOf(s.label);
      return { ...s, idx };
    })
    .filter((s) => s.idx >= 0)
    .sort((a, b) => a.idx - b.idx);

  // No labels at all: the model ignored the format. Rather than discard a
  // possibly useful paragraph, show all of it under "what happened" — the one
  // section where undifferentiated prose is still honest.
  if (!marks.length) {
    out.happened = text.trim();
    return out;
  }

  marks.forEach((mark, i) => {
    const start = mark.idx + mark.label.length;
    const end = i + 1 < marks.length ? marks[i + 1].idx : text.length;
    out[mark.key] = text.slice(start, end).trim();
  });
  return out;
}

/**
 * Split a paragraph into sentences.
 *
 * Used for the two sections that must be read one instruction at a time. A
 * frightened person reading a paragraph reads nothing, so the paragraph is
 * broken up before it reaches the screen rather than being styled smaller.
 *
 * @param {string} text
 * @returns {string[]} Trimmed sentences, empties removed.
 */
function sentences(text) {
  return String(text || '')
    .split(/(?<=[.!?])\s+/)
    .map((s) => s.trim())
    .filter(Boolean);
}

/**
 * Set a live/fallback badge from a Generation's `live` flag.
 *
 * This is the single place the caregiver page decides how to present model
 * provenance, so "a fallback never masquerades as live" holds by construction
 * across all three badged sections (CONTRACT ground rule 2).
 *
 * @param {string}  id   Badge element id.
 * @param {boolean} live The Generation's live flag.
 */
function setBadge(id, live) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = live ? 'live' : 'offline fallback';
  el.classList.toggle('badge--live', !!live);
  el.classList.toggle('badge--fallback', !live);
}

/**
 * Fetch and render the caregiver brief.
 *
 * Ordering note: this is never allowed to gate the tier. renderTier has already
 * run by the time this is called, so the severity, the ladder rail and the 911
 * button are all on screen before the model is consulted. If the generation is
 * slow, wrong, or entirely absent, the caregiver still has the actionable page
 * (PRD P4).
 *
 * @param {boolean} manual True when triggered by the "Regenerate summary"
 *                         button, which is allowed to re-run at any tier.
 */
async function loadBrief(manual = false) {
  if (briefInFlight) return;
  briefInFlight = true;

  const textEl = document.getElementById('brief-text');
  if (textEl && manual) textEl.textContent = 'Rewriting the situation summary…';

  try {
    const gen = await api('/api/caregiver/brief');

    // An outright failure is stated, not smoothed over. "AI unavailable: …"
    // from the server is more useful to a caregiver than a blank panel.
    if (gen.error && !gen.text) {
      if (textEl) textEl.textContent = gen.error;
      setBadge('brief-badge', false);
      setBadge('next-badge', false);
      setBadge('notsay-badge', false);
      notice('The written summary is unavailable. Everything below it is from ' +
             'the event log and is still accurate.');
      return;
    }

    const parts = parseBrief(gen.text);

    // 1 — the situation summary.
    if (textEl) textEl.textContent = parts.happened || gen.text || '';
    setBadge('brief-badge', gen.live);
    setText('brief-meta', gen.model
      ? `${gen.model} · ${gen.latency_ms}ms`
      : '');

    // 3 — the next 60 seconds, one imperative per row.
    renderNext60(parts.next);
    setBadge('next-badge', gen.live);

    // 4 — what not to say.
    renderNotToSay(parts.notsay);
    setBadge('notsay-badge', gen.live);

    // The model's own "what the system already did" paragraph is deliberately
    // NOT used to fill section 2. That section is rendered from the event log
    // instead (see renderAlreadyDid): a caregiver deciding whether 911 has been
    // called must read the record, not a summary of the record.
  } catch {
    if (textEl) {
      textEl.textContent =
        'Could not reach the server for a summary. The tier and the event log ' +
        'below were loaded before the connection dropped.';
    }
    setBadge('brief-badge', false);
  } finally {
    briefInFlight = false;
  }
}

/**
 * Render the "next 60 seconds" list.
 *
 * One sentence per <li>. The list is aria-live="polite" in the markup, so a
 * regenerated brief is announced without cutting across the assertive tier
 * announcement that may have just fired.
 *
 * @param {string} text The NEXT 60 SECONDS section body.
 */
function renderNext60(text) {
  const host = document.getElementById('next-60');
  if (!host) return;

  const items = sentences(text);
  if (!items.length) {
    // An honest gap. The static fallback is the one instruction that is true at
    // every tier and needs no model to produce it.
    host.innerHTML =
      '<li><p class="step__title">Call them.</p>' +
      '<p class="step__body">The written steps are unavailable. If they do not ' +
      'answer, or they answer and sound wrong, call 911.</p></li>';
    return;
  }

  host.innerHTML = items.map((s) => `
    <li><p class="step__title">${escapeHtml(s)}</p></li>`).join('');
}

/**
 * Render "what not to say".
 *
 * This is the highest-value content on the page and the easiest to skim past,
 * so caregiver.html gives it its own visual language: <del class="dont"> for the
 * thing not to do and <span class="why"> for the reason. Those elements also
 * carry the meaning to a screen reader, which cannot perceive a strike-through.
 *
 * The prompt asks for "the instruction, then the reason in half a sentence", so
 * we split on the first em dash, colon, or "because". When no reason clause is
 * present we render the instruction alone rather than fabricating a rationale —
 * an <ins class="do"> replacement is emitted ONLY when the model actually
 * supplied one, because a caregiver being told to say a specific sentence that
 * no model wrote is the product putting words in its own mouth.
 *
 * @param {string} text The WHAT NOT TO DO RIGHT NOW section body.
 */
function renderNotToSay(text) {
  const host = document.getElementById('say-not');
  if (!host) return;

  const items = sentences(text);
  if (!items.length) {
    host.innerHTML =
      '<li><del class="dont">Do not accuse, interrogate, or demand a promise.</del>' +
      '<span class="why">Static guidance — the written version is unavailable. ' +
      'Pressure now makes them hide next time, and hidden use is the dangerous kind.</span></li>';
    return;
  }

  host.innerHTML = items.map((s) => {
    // Split instruction from reason on the first em dash / en dash / colon /
    // "because". Everything before is the thing to avoid; everything after is why.
    const m = s.match(/^(.*?)(?:\s+[—–:]\s+|\s+because\s+)(.+)$/i);
    const dont = m ? m[1].trim() : s;
    const why = m ? m[2].trim() : '';
    return `
      <li>
        <del class="dont">${escapeHtml(dont)}</del>
        ${why ? `<span class="why">${escapeHtml(why)}</span>` : ''}
      </li>`;
  }).join('');
}

/* -------------------------------------------------------------------------- */
/* What Threshold already did (section 2)                                      */
/* -------------------------------------------------------------------------- */

/**
 * Human labels for the Action kinds in app/models.py.
 *
 * Kept as a lookup rather than shown raw: "fire_contact_tree" is a schema name,
 * and a caregiver at 3am must read only what actually happened, not what
 * order". Anything not in this map falls back to the raw kind rather than being
 * dropped — a silently omitted action is the one that gets duplicated.
 */
const ACTION_LABELS = {
  caregiver_screen_notified: 'Delivered the alert to a connected caregiver screen',
  location_displayed: 'Displayed coordinates on their phone to read aloud',
  bystander_hail_started: 'Called out through the phone for a nearby person',
  wake_lock_acquired: 'Confirmed that the emergency screen will stay awake',
  '911_script_displayed': 'Displayed the local personalised 911 script',
  vault_clip_played: 'Started a consented Memory Vault recording',
  grounding_started: 'Started the grounding exercise',
  rescue_breathing_started: 'Started the rescue-breathing rhythm',
  naloxone_prompt_displayed: 'Displayed and spoke the naloxone prompt',
  good_samaritan_displayed: 'Displayed the reviewed state legal summary',
};

/**
 * Render the deterministic record of what the system already did.
 *
 * Placed before the instructions in the markup on purpose: a caregiver who does
 * does not know whether 911 was called must be told to call it themselves.
 * Sourced from the event log rather than from the generation — this section is
 * a claim about what happened, and only the record can make that claim
 * (CONTRACT: the model does language work only).
 *
 * @param {Array<object>} events Events from /api/state, oldest first.
 */
function renderAlreadyDid(events) {
  const host = document.getElementById('already-did');
  if (!host) return;

  // Only escalation events carry actions worth reporting; a row with no actions
  // would render as an empty numbered step that looks like a missing item.
  const rows = (events || [])
    .filter((e) => e.actions_taken?.length)
    .slice(-6);

  if (!rows.length) {
    host.innerHTML =
      '<li><p>No automatic action has a confirmed completion receipt yet. Call 911 if help may be needed.</p></li>';
    return;
  }

  host.innerHTML = rows.reverse().map((e) => {
    const at = new Date(e.at);
    const actions = e.actions_taken
      .map((k) => escapeHtml(ACTION_LABELS[k] || k))
      .join('. ');
    return `
      <li>
        <time datetime="${escapeHtml(e.at)}">${at.toLocaleTimeString()}</time>
        <p>${actions}.</p>
      </li>`;
  }).join('');
}

/* -------------------------------------------------------------------------- */
/* The trust boundary (section 5)                                              */
/* -------------------------------------------------------------------------- */

/**
 * Render exactly what this caregiver can and cannot see.
 *
 * caregiver.html ships a static, honest default in the markup. This function
 * replaces it with the truth for THIS user, because the sentence "you cannot
 * see tier 2 and 3" is only accurate while Sam has those switches off — and if
 * Sam turned tier 3 on, a caregiver who was told otherwise is being misled by
 * the one section of the product whose whole job is not to mislead them.
 *
 * PRD P3: the user owns the ladder config, and both sides see the same list.
 * There is no second view of the user that is not shown here.
 *
 * @param {object|null} profile The profile from /api/state (UserProfile shape).
 */
function renderVisibility(profile) {
  const can = document.getElementById('can-see');
  const cannot = document.getElementById('cannot-see');
  if (!can || !cannot) return;

  const ladder = profile?.ladder || {};
  const t2 = !!ladder.tier_2_visible_to_caregiver;
  const t3 = !!ladder.tier_3_visible_to_caregiver;
  // COPY: "your member" is the product's noun when no real name is available.
  // Never "patient" or "client" — this is a relationship, not a caseload.
  const name = profile?.name || 'your member';

  // Name the person rather than saying "the user": the markup is written for
  // Sam, and a real caregiver of a different account should read their own name.
  document.querySelectorAll('[data-watched-name]').forEach((el) => {
    el.textContent = name;
  });

  const canItems = [
    'Tier 4 and tier 5, always — these cannot be switched off, and they know that.',
    'The reason the tier changed, in the system\'s own words.',
    'What the system did in response, and when.',
    `${escapeHtml(name)}'s address and entry code, at tier 4 and above only.`,
  ];
  const cannotItems = [
    `Anything ${escapeHtml(name)} said. Transcripts are never shared with you.`,
    'Which substances were involved.',
    'Location, at any tier below 4.',
    'Anything at all after they stand down — the alert closes with it.',
  ];

  // Tier 2 and 3 move between the two columns according to the actual config.
  // This is the entire reason this function exists rather than a static list.
  (t2 ? canItems : cannotItems).push(
    t2
      ? 'Tier 2, craving — they have turned this on for you.'
      : 'Tier 2, craving — they have not turned this on for you.'
  );
  (t3 ? canItems : cannotItems).push(
    t3
      ? 'Tier 3, active use — they have turned this on for you.'
      : 'Tier 3, active use — they have not turned this on for you.'
  );

  // Items are already escaped where they interpolate profile data; the literal
  // strings are authored here and contain no markup.
  can.innerHTML = canItems.map((t) => `<li>${t}</li>`).join('');
  cannot.innerHTML = cannotItems.map((t) => `<li>${t}</li>`).join('');
}

/* -------------------------------------------------------------------------- */
/* Recent events                                                               */
/* -------------------------------------------------------------------------- */

/**
 * Render the short "recent events you can see" log at the foot of the page.
 *
 * Deliberately a subset with a link to /ladder for the full record, rather than
 * a truncation with a "show more" that could imply rows are being withheld.
 *
 * @param {Array<object>} events Events from /api/state, oldest first.
 */
function renderRecent(events) {
  const host = document.getElementById('recent-log');
  if (!host) return;
  if (!events?.length) {
    host.innerHTML = '<p class="empty">Nothing has happened yet.</p>';
    return;
  }
  host.innerHTML = events.slice(-6).reverse().map((e) => `
    <article class="log__row" data-tier="${Number(e.tier)}">
      <time class="log__time" datetime="${escapeHtml(e.at)}">${new Date(e.at).toLocaleTimeString()}</time>
      <span class="log__tier">${Number(e.tier)} · ${escapeHtml(TIER_NAMES[e.tier] || '')}</span>
      <div><p class="log__reason">${escapeHtml(e.reason)}</p></div>
    </article>`).join('');
}

/* -------------------------------------------------------------------------- */
/* Notices                                                                     */
/* -------------------------------------------------------------------------- */

/**
 * Show a degraded-mode strip.
 *
 * The element is role="status" / aria-live="polite": a caregiver should be told
 * that something is degraded, but not have it cut across the tier announcement.
 *
 * @param {string} text Empty string hides the strip.
 */
function notice(text) {
  const el = document.getElementById('system-notice');
  if (!el) return;
  el.textContent = text || '';
  el.hidden = !text;
}

/* -------------------------------------------------------------------------- */
/* Live stream                                                                 */
/* -------------------------------------------------------------------------- */

/**
 * Subscribe to server-sent ladder events.
 *
 * This is the mechanism that makes this page an alert rather than a dashboard:
 * a tier change on the user's phone lands here with no polling and no refresh.
 * EventSource reconnects on its own, so there is no hand-written reconnection
 * logic here to get subtly wrong.
 *
 * On a tier >= 4 event we pull a fresh brief immediately. Below that we update
 * the rail but leave the brief alone — regenerating a summary for a tier the
 * caregiver may not even be permitted to see would burn latency and confuse the
 * live/fallback badge state at the moment it matters most.
 *
 * THIS CODE DOES NOT FILTER BY USER, AND MUST NOT START.
 * Every event arriving on this stream has already been authorised for THIS
 * session by the server: the listener is tagged with the signed-in account when
 * it subscribes, and `app/deps.py::visible_to` decides per recipient whether an
 * event may be written to it — own events always, a linked caregiver's watched
   * user at tier 4/5 always (PRD §4.2), and at tiers 2/3 only with the watched
   * person's explicit setting. Tiers 0/1 and anonymous listeners receive none.
 *
 * This page used to receive EVERY user's events and drop the ones whose
 * `user_id` did not match, which meant other people's tier reasons and account
 * ids were delivered to this browser and discarded here. A filter in the client
 * is a rendering preference; anyone reading the network tab sees the data
 * regardless. Re-adding a check here would not restore privacy — it would only
 * hide the fact that the boundary had moved back to the wrong side of the wire.
 */
function initStream() {
  let es;
  try {
    es = new EventSource('/api/events');
  } catch {
    // SSE unsupported: /api/state on load still rendered a correct page.
    notice('Live updates are not supported in this browser. Reload to refresh.');
    return;
  }

  es.addEventListener('message', async (e) => {
    let data;
    try { data = JSON.parse(e.data); } catch { return; }

    if (data.type === 'reset') { window.location.reload(); return; }
    if (data.type === 'receipt') {
      try {
        const state = await api('/api/state');
        renderAlreadyDid(state.events);
        renderRecent(state.events);
      } catch { /* A receipt may appear on the next reconnect. */ }
      return;
    }
    if (data.type !== 'tier') return;

    renderTier(data.tier, data.reason);

    // Refresh the deterministic record on every change, so "what Threshold
    // already did" is never one escalation behind the tier word above it.
    try {
      const state = await api('/api/state');
      renderAlreadyDid(state.events);
      renderRecent(state.events);
    } catch { /* The tier is already on screen; the log can lag a beat. */ }

    // PRD §4.2: tier 4 and 5 always reach the caregiver, whatever the config
    // says. This is the one escalation the user cannot switch off, so it is the
    // one that unconditionally triggers a brief.
    if (data.tier >= 4) loadBrief();
  });

  es.addEventListener('error', () => {
    // EventSource retries by itself; we only report the gap so silence is never
    // mistaken for "nothing is happening".
    notice('Live connection dropped. Reconnecting — reload if this persists.');
  });

  // The server sends an immediate `ping` event on connect, which is how a
  // connected-and-quiet stream is told apart from one that never connected.
  es.addEventListener('ping', () => notice(''));
}

/* -------------------------------------------------------------------------- */
/* Controls                                                                    */
/* -------------------------------------------------------------------------- */

/** Wire the buttons in the alert header and the rail. */
function initControls() {
  document.getElementById('brief-refresh')?.addEventListener('click', () => loadBrief(true));

  // "Call Sam" is a button rather than a tel: link because the number comes
  // from the profile and is not known until /api/state resolves. The 911 link
  // next to it IS a plain <a href="tel:911"> in the markup, so the call that
  // matters most works even if this script never ran.
  document.getElementById('alert-call-user')?.addEventListener('click', () => {
    const tel = document.getElementById('alert-call-user')?.dataset.tel;
    if (tel) window.location.href = `tel:${tel}`;
    else notice('No phone number on file for them. Use 911 if you cannot reach them.');
  });

  document.getElementById('logout')?.addEventListener('click', async () => {
    await post('/api/auth/logout').catch(() => {});
    window.location.href = '/login';
  });
}

/* -------------------------------------------------------------------------- */
/* Boot                                                                        */
/* -------------------------------------------------------------------------- */

/**
 * Entry point.
 *
 * Order matters: controls and the stream are wired BEFORE the first fetch, so a
 * tier-4 event arriving during the initial load is not dropped on the floor
 * while /api/state is still in flight.
 */
async function boot() {
  initControls();
  initStream();

  let state;
  try {
    state = await api('/api/state');
  } catch {
    notice('Offline. Call 911 directly if you believe this is an emergency.');
    return;
  }

  renderVisibility(state.profile);

  if (state.visible === false) {
    currentTier = null;
    document.body.dataset.tier = '0';
    document.querySelectorAll('[data-tier-step]').forEach((el) => {
      el.setAttribute('data-state', 'ahead');
      el.setAttribute('aria-current', 'false');
    });
    setText('alert-tier', 'No shared alert');
    setText(
      'brief-text',
      'This member has no active tier that they chose to share. Their current state and history remain private.',
    );
    setText('brief-meta', 'Privacy boundary active');
    renderAlreadyDid([]);
    renderRecent([]);
    const refresh = document.getElementById('brief-refresh');
    if (refresh) refresh.disabled = true;
    const callMember = document.getElementById('alert-call-user');
    if (callMember) callMember.hidden = true;
    return;
  }

  renderTier(state.tier, '');
  renderAlreadyDid(state.events);
  renderRecent(state.events);

  // The watched person's name, from the profile rather than the markup default.
  if (state.profile?.name) setText('watched-name', state.profile.name);

  // The member's own number is a distinct field. A contact-tree destination
  // belongs to somebody else and must never be used as "Call the member".
  const tel = state.profile?.phone;
  const callBtn = document.getElementById('alert-call-user');
  if (callBtn && state.profile?.name) {
    callBtn.textContent = `Call ${state.profile.name}`;
    if (tel && /\d/.test(tel)) callBtn.dataset.tel = tel;
  }

  // Report AI availability honestly and visibly, rather than letting an offline
  // model look like a broken feature (CONTRACT: GenAI access).
  const ai = document.getElementById('ai-status');
  if (ai) {
    ai.textContent = state.ai_online ? 'AI online' : 'AI offline — no API key';
    ai.classList.add(state.ai_online ? 'badge--live' : 'badge--offline');
    ai.dataset.tone = state.ai_online ? 'ok' : 'warn';
  }

  // Load a brief on arrival whenever the ladder is already in an emergency — a
  // caregiver who opens this page mid-incident must not have to wait for the
  // next SSE event to find out what is happening.
  if (state.tier >= 4) {
    loadBrief();
  } else {
    setText('brief-text',
      'Nothing is escalating right now. If the ladder crosses into an emergency, ' +
      'this page will write the situation summary itself and announce it.');
    // No badge is claimed at a calm tier: the sentence above is authored markup,
    // not a generation, and badging it "live" would be claiming a model call
    // that never happened (CONTRACT ground rule 2).
    document.getElementById('brief-badge')?.remove();
    document.getElementById('next-badge')?.remove();
    document.getElementById('notsay-badge')?.remove();

    // The two generated sections say plainly that there is nothing to do,
    // rather than showing stale instructions from a resolved incident or an
    // empty list that reads as "failed to load".
    const nextHost = document.getElementById('next-60');
    if (nextHost) {
      nextHost.innerHTML =
        '<li><p class="step__title">Nothing to do right now.</p>' +
        '<p class="step__body">Instructions appear here the moment the ladder ' +
        'reaches an emergency. You do not need to watch this page.</p></li>';
    }
    const notHost = document.getElementById('say-not');
    if (notHost) {
      notHost.innerHTML =
        '<li><del class="dont">Do not check in to see if the alert was real.</del>' +
        '<span class="why">Being asked about a quiet day is still being watched. ' +
        'This page will tell you when there is something to know.</span></li>';
    }
  }
}

document.addEventListener('DOMContentLoaded', boot);
