/* ============================================================================
   THRESHOLD — ladder.js
   Behaviour for the event log (/ladder).

   WHAT THIS FILE DOES
     Renders every event from GET /api/state `events[]` — newest first — with
     its tier, the auditable reason, the timestamp, the actions taken, and the
     trigger source. Then keeps the list live over SSE, so an escalation that
     happens while this page is open appears at the top of it.

   WHAT THIS FILE DELIBERATELY DOES NOT DO
     PRD §11: EVERY EVENT IS VISIBLE TO THE USER. NO HIDDEN LOG.

     So there is no filtering here. No severity threshold, no de-duplication, no
     "collapse similar rows", no cap. Every event the server returns is rendered.
     Any of those would be a defensible UI decision in a normal product and an
     indefensible one here: this page exists to make a single claim, and code
     that quietly drops a row makes that claim false. If the list is long, it is
     long — that is what an append-only record looks like.

     It never decides a tier, and never rewrites a reason. The reason strings
     come from the deterministic triage engine, which means they are the exact
     rule that fired and the user can check the system's work against their own
     memory of the day. Passing them through a model to make them read better
     would destroy the only property that makes them worth showing (PRD P4).

   NO BUILD STEP
     Plain ES module, no bundler, no framework, no CDN.
   ========================================================================= */

'use strict';

/* -------------------------------------------------------------------------- */
/* API helpers                                                                 */
/* -------------------------------------------------------------------------- */

/**
 * Fetch JSON with a hard timeout.
 * @param {string} path
 * @param {object} options   Passed through to fetch.
 * @param {number} timeoutMs Abort ceiling.
 * @returns {Promise<object>} Parsed JSON.
 */
async function api(path, options = {}, timeoutMs = 12000) {
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

/** Escape before inserting anything server-supplied into the DOM. */
function escapeHtml(s) {
  const d = document.createElement('div');
  d.textContent = String(s ?? '');
  return d.innerHTML;
}

function setText(id, text) {
  const el = document.getElementById(id);
  if (el) el.textContent = text;
}

const TIER_NAMES = [
  'Baseline', 'Elevated', 'Craving', 'Active use', 'Medical emergency', 'Unresponsive',
];

/**
 * Human labels for the Action kinds in app/models.py.
 *
 * The raw kinds are schema identifiers. A user auditing their own log should
 * read "Played a recorded message from someone they trust", not
 * "play_vault_clip". Unknown kinds fall through to the raw string rather than
 * being dropped — an action the UI does not recognise is exactly the one the
 * user most needs to see, and silently swallowing it would be a hidden log by
 * another name (PRD §11).
 */
const ACTION_LABELS = {
  speak: 'Spoke to you',
  play_vault_clip: 'Played a voice you know',
  offer_contact: 'Offered to reach one person',
  fire_contact_tree: 'Contact-tree delivery scheduled',
  show_911_script: 'Showed your 911 script',
  show_good_samaritan: 'Showed your Good Samaritan protection',
  naloxone_prompt: 'Told you to use naloxone',
  bystander_hail: 'Hailed anyone nearby, out loud',
  arm_bystander_mode: 'Armed bystander mode',
  rescue_breathing: 'Started the rescue-breathing rhythm',
  start_grounding: 'Started a grounding exercise',
  acquire_location: 'Location display scheduled',
  keep_awake: 'Screen wake-lock request scheduled',
};

const COMPLETED_ACTION_LABELS = {
  caregiver_screen_notified: 'Alert delivered to a connected caregiver screen',
  location_displayed: 'Coordinates displayed on this device',
  bystander_hail_started: 'Phone called out for a nearby person',
  wake_lock_acquired: 'Screen wake lock acquired',
  '911_script_displayed': 'Local personalised 911 script displayed',
  vault_clip_played: 'Consented Memory Vault recording started',
  grounding_started: 'Grounding exercise started',
  rescue_breathing_started: 'Rescue-breathing rhythm started',
  naloxone_prompt_displayed: 'Naloxone prompt displayed and spoken',
  good_samaritan_displayed: 'Reviewed state legal summary displayed',
};

/* -------------------------------------------------------------------------- */
/* Log state                                                                   */
/* -------------------------------------------------------------------------- */

/**
 * The full log, oldest first — the same order the server returns.
 *
 * Held in memory so an SSE event can be appended without a refetch, and so the
 * export button writes exactly what is on screen. Reversal to newest-first
 * happens at render time only; keeping the canonical order server-shaped means
 * there is one place (renderLog) where the display order is decided.
 *
 * @type {Array<object>}
 */
let allEvents = [];

/* -------------------------------------------------------------------------- */
/* Rendering                                                                   */
/* -------------------------------------------------------------------------- */

/**
 * Render one event as a log row.
 *
 * Each row carries its OWN historical tier via data-tier rather than inheriting
 * the live --accent, because the log spans many tiers at once and a tier-4 row
 * from last night must not be recoloured calm because the ladder is at 0 now.
 *
 * @param {object} e An Event from the API (see app/models.py).
 * @returns {string} HTML for one row. All interpolated values are escaped.
 */
function renderRow(e) {
  const at = new Date(e.at);
  const tier = Number(e.tier);

  const completed = (e.actions_taken || [])
    .map((k) => `<li>Completed: ${escapeHtml(COMPLETED_ACTION_LABELS[k] || k)}</li>`);
  const planned = (e.actions_planned || [])
    .map((k) => `<li>Planned: ${escapeHtml(ACTION_LABELS[k] || k)}</li>`);
  const actions = [...completed, ...planned]
    .join('');

  return `
    <article class="log__row" data-tier="${tier}">
      <time class="log__time" datetime="${escapeHtml(e.at)}">${at.toLocaleTimeString()}</time>
      <span class="log__tier">${tier} · ${escapeHtml(TIER_NAMES[tier] || '')}</span>
      <div>
        <p class="log__reason">${escapeHtml(e.reason)}</p>
        ${actions ? `<ul class="log__actions">${actions}</ul>` : ''}
        <p class="log__source">${escapeHtml(at.toLocaleDateString())} · via ${escapeHtml(e.trigger_source || 'system')}</p>
      </div>
    </article>`;
}

/**
 * Render the whole log, newest first.
 *
 * A full re-render rather than an incremental append: the list is bounded by
 * what a person's day actually produces, so the simpler code is also the
 * correct code, and there is no reconciliation logic here to drift out of sync
 * with the server's ordering.
 *
 * The container is aria-live="polite" with aria-relevant="additions" in the
 * markup. Polite, because the assertive tier announcer at the top of the
 * document already interrupts for the escalation itself and announcing the same
 * change twice is worse than announcing it once.
 */
function renderLog() {
  const host = document.getElementById('event-log');
  const empty = document.getElementById('log-empty');
  if (!host) return;

  if (!allEvents.length) {
    host.innerHTML = '';
    if (empty) empty.hidden = false;
    return;
  }
  if (empty) empty.hidden = true;

  // Newest first. slice() so the reverse does not mutate the canonical order.
  host.innerHTML = allEvents.slice().reverse().map(renderRow).join('');
}

/**
 * Apply a tier to the rail and the announcer.
 *
 * The tier shown here always came from the server — over SSE or from
 * /api/state. Nothing on this page computes one (PRD P4).
 *
 * @param {number} tier
 * @param {string} reason
 */
function renderTier(tier, reason) {
  document.body.dataset.tier = String(tier);

  document.querySelectorAll('[data-tier-step]').forEach((el) => {
    const step = Number(el.dataset.tierStep);
    el.setAttribute('data-state', step === tier ? 'active' : step < tier ? 'passed' : 'ahead');
    el.setAttribute('aria-current', step === tier ? 'step' : 'false');
  });

  if (reason) setText('tier-reason', reason);
  setText('tier-announcer', `${TIER_NAMES[tier] || 'Tier ' + tier}. ${reason || ''}`);
}

/**
 * Show a degraded-mode strip. role="status" / aria-live="polite" in the markup.
 * @param {string} text Empty string hides it.
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
 * A tier event over SSE carries the tier and the reason but not the persisted
 * Event row, so we refetch /api/state rather than synthesising a row from the
 * broadcast payload. Synthesising would risk this page showing an event that
 * differs in any detail from the one actually written to the log — and a log
 * that disagrees with the record is worse than one that lags by 200ms.
 */
function initStream() {
  let es;
  try {
    es = new EventSource('/api/events');
  } catch {
    notice('Live updates are not supported in this browser. Reload to see new events.');
    return;
  }

  es.addEventListener('message', async (e) => {
    let data;
    try { data = JSON.parse(e.data); } catch { return; }

    if (data.type === 'reset') { window.location.reload(); return; }
    if (data.type !== 'tier') return;

    renderTier(data.tier, data.reason);

    try {
      const state = await api('/api/state');
      allEvents = state.events || [];
      renderLog();
    } catch {
      // The tier moved but we could not refetch. Say so rather than leaving a
      // stale list that silently omits the event that just fired — silence on
      // this page reads as "nothing happened", which is the one thing it must
      // never be able to say falsely.
      notice('An event just fired but the log could not be refreshed. Reload to see it.');
    }
  });

  es.addEventListener('error', () => {
    notice('Live connection dropped. Reconnecting — reload if this persists.');
  });

  // The server's immediate hello on connect. Clearing the notice here is how
  // "connected and quiet" is distinguished from "never connected".
  es.addEventListener('ping', () => notice(''));
}

/* -------------------------------------------------------------------------- */
/* Controls                                                                    */
/* -------------------------------------------------------------------------- */

/**
 * Download the log as JSON.
 *
 * Part of the same promise the page makes: a log you can see but not take is a
 * log someone else still controls. Built as a Blob and revoked immediately, so
 * nothing about the user's record touches a server to be exported.
 */
function exportLog() {
  const payload = JSON.stringify(allEvents, null, 2);
  const blob = new Blob([payload], { type: 'application/json' });
  const url = URL.createObjectURL(blob);

  const a = document.createElement('a');
  a.href = url;
  a.download = `threshold-log-${new Date().toISOString().slice(0, 10)}.json`;
  a.click();
  URL.revokeObjectURL(url);
}

/** Wire the footer and rail controls. */
function initControls() {
  document.getElementById('export-log')?.addEventListener('click', exportLog);

  document.getElementById('reset-demo')?.addEventListener('click', async () => {
    // No confirmation dialog: this is a labelled demo control on a seeded
    // account, and the server broadcasts a reset that reloads every open page.
    await post('/api/reset').catch(() => {});
    window.location.reload();
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
 * Entry point. The stream is wired before the first fetch so an event arriving
 * during load is not lost between the two.
 */
async function boot() {
  initControls();
  initStream();

  let state;
  try {
    state = await api('/api/state');
  } catch {
    notice('Offline — this is the log as of your last connection, and it may be incomplete.');
    return;
  }

  allEvents = state.events || [];
  renderLog();
  renderTier(state.tier, '');

  // Report AI availability honestly, even here where nothing on the page is
  // generated. An evaluator reading the log should be able to see at a glance
  // whether the generative surfaces they just used were live (CONTRACT).
  const ai = document.getElementById('ai-status');
  if (ai) {
    ai.textContent = state.ai_online ? 'AI online' : 'AI offline — no API key';
    ai.classList.add(state.ai_online ? 'badge--live' : 'badge--offline');
    ai.dataset.tone = state.ai_online ? 'ok' : 'warn';
  }
}

document.addEventListener('DOMContentLoaded', boot);
