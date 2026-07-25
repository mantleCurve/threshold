/* ============================================================================
   THRESHOLD — app.js
   Behaviour for the person-in-recovery surface (/) and shared helpers.

   WHAT THIS FILE DOES
     Binds the DOM hooks in index.html to the API in CONTRACT.md, drives the
     tier transformation, runs push-to-talk voice input, and keeps the ladder
     in sync with the server.

   WHAT THIS FILE DELIBERATELY DOES NOT DO
     It never decides a tier. Every tier on screen came from the server's
     deterministic triage engine (PRD P4). The client renders state; it does not
     infer it. If you find yourself writing `if (text.includes(...)) tier = 4`
     in here, it belongs in app/triage.py instead.

     It also never fabricates AI text. When a generation comes back with
     live:false, the UI says so rather than showing the fallback as if it were
     a fresh answer.

   NO BUILD STEP
     Plain ES modules, no bundler, no framework, no CDN. One less thing that can
     fail on an evaluator's machine, and it works with the network down — which
     is exactly when this product matters most.
   ========================================================================= */

'use strict';

/* -------------------------------------------------------------------------- */
/* API helpers                                                                 */
/* -------------------------------------------------------------------------- */

/**
 * Fetch JSON with a hard timeout.
 *
 * Every network call in a crisis UI needs a ceiling: a request that hangs
 * forever is worse than one that fails fast, because the user sits looking at a
 * spinner during the minutes that matter.
 */
async function api(path, options = {}, timeoutMs = 12000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(path, {
      headers: { 'Content-Type': 'application/json' },
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

/* -------------------------------------------------------------------------- */
/* Tier rendering                                                              */
/* -------------------------------------------------------------------------- */

const TIER_NAMES = [
  'Baseline', 'Elevated', 'Craving', 'Active use', 'Medical emergency', 'Unresponsive',
];

/** Current tier, mirrored from the server. Never computed locally. */
let currentTier = 0;

/**
 * Apply a tier to the whole document.
 *
 * Setting one attribute on <body> is the entire transformation mechanism: every
 * colour, density, and type-scale change is CSS reacting to [data-tier]. Keeping
 * it declarative means the emergency layout cannot be half-applied because a
 * JavaScript error interrupted a sequence of style writes partway through.
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

  // Tier 4/5 replace the interface with a single action. The takeover is a real
  // element that is shown, not a new page: navigation during an emergency risks
  // losing the session, and the phone may be about to leave the user's hand.
  const takeover = document.getElementById('takeover');
  if (takeover) {
    const emergency = tier >= 4;
    takeover.hidden = !emergency;
    // aria-hidden on the rest of the page so a screen reader user in an emergency
    // is not read the navigation before the one thing that matters.
    document.getElementById('main')?.setAttribute('aria-hidden', String(emergency));
    if (emergency) {
      setText('takeover-tier', TIER_NAMES[tier]);
      setText('takeover-reason', reason || '');
      if (tier === 4) runEmergencySequence();
      // Keep the screen awake: a locked screen mid-overdose is a dead phone to a
      // bystander who picks it up. Best-effort — not supported everywhere.
      requestWakeLock();
    }
  }

  // Reveal or hide anything gated on a tier threshold.
  document.querySelectorAll('[data-when-tier]').forEach((el) => {
    const min = Number(el.dataset.whenTier);
    el.hidden = tier < min;
  });
}

function setText(id, text) {
  const el = document.getElementById(id);
  if (el) el.textContent = text;
}

/* -------------------------------------------------------------------------- */
/* Emergency sequence (PRD §4.3)                                               */
/* -------------------------------------------------------------------------- */

let emergencyTimers = [];

/**
 * Run the Tier 4 timing table.
 *
 * Every step completes WITHOUT user input. The window from "I can't breathe" to
 * unconsciousness is frequently one to three minutes, so anything that waits for
 * a tap assumes a user who will still be conscious three steps later — which is
 * the assumption that kills people (PRD §4.3).
 *
 * The naloxone prompt fires at t=0, in parallel with the 911 button, not after
 * it. Naloxone is the thing that actually reverses this.
 */
function runEmergencySequence() {
  clearEmergencyTimers();
  const status = document.getElementById('takeover-status');
  const say = (msg) => { if (status) status.textContent = msg; speak(msg); };

  // t=0 — calm, short, no questions yet. Naloxone offered simultaneously.
  say('Stay with me. Help is coming. If you have Narcan, use it now.');

  // t=5s — do not wait for a reply. Begin autonomous escalation.
  emergencyTimers.push(setTimeout(() => {
    say('Getting your location and contacting your people now.');
    acquireLocation();
    post('/api/sensor', { silent_seconds: 5, still: true }).catch(() => {});
  }, 5000));

  // t=10s — the phone shouts for a bystander itself, rather than asking the user
  // whether someone is nearby. Same intent, executed by the machine.
  emergencyTimers.push(setTimeout(() => {
    hailBystander();
  }, 10000));

  // t=15s — the 911 script, read one line at a time.
  emergencyTimers.push(setTimeout(() => { loadScript911(); }, 15000));
}

function clearEmergencyTimers() {
  emergencyTimers.forEach(clearTimeout);
  emergencyTimers = [];
}

/**
 * Broadcast a hail through the speaker for anyone in earshot.
 *
 * The single most useful thing a phone can do for an unconscious person is make
 * noise that recruits a human. Any tap on the screen opens Bystander Mode, which
 * requires no account (PRD §4.4).
 */
function hailBystander() {
  const msg =
    'This phone belongs to someone who may be overdosing. ' +
    'If you can hear this, tap the screen.';
  speak(msg, { loud: true });
  const btn = document.getElementById('arm-bystander');
  if (btn) btn.hidden = false;

  // Any tap anywhere opens the bystander guide — a stranger should not have to
  // find a button.
  document.addEventListener('click', openBystander, { once: true });
}

function openBystander() { window.location.href = '/bystander'; }

/** Best-effort location for responders. Only ever acquired during an emergency. */
function acquireLocation() {
  if (!navigator.geolocation) return;
  navigator.geolocation.getCurrentPosition(
    (pos) => {
      const { latitude, longitude } = pos.coords;
      setText('takeover-status',
        `Location acquired: ${latitude.toFixed(5)}, ${longitude.toFixed(5)}`);
    },
    () => { /* Denied or unavailable — the address from the profile still stands. */ },
    { enableHighAccuracy: true, timeout: 8000 }
  );
}

let wakeLock = null;
async function requestWakeLock() {
  try { wakeLock = await navigator.wakeLock?.request('screen'); } catch { /* unsupported */ }
}

/* -------------------------------------------------------------------------- */
/* Speech                                                                      */
/* -------------------------------------------------------------------------- */

/**
 * Speak text aloud.
 *
 * Deliberately slower and lower-pitched than the default: the listener may be
 * intoxicated, panicking, or both. We never claim to be a person (PRD P5) — the
 * copy passed in here always refers to the system as the system.
 */
function speak(text, { loud = false } = {}) {
  if (!('speechSynthesis' in window) || !text) return;
  const u = new SpeechSynthesisUtterance(text);
  u.rate = 0.9;
  u.pitch = 0.95;
  u.volume = loud ? 1 : 0.9;
  window.speechSynthesis.speak(u);
}

/* -------------------------------------------------------------------------- */
/* Push to talk                                                                */
/* -------------------------------------------------------------------------- */

let recognition = null;
let listening = false;

/**
 * Wire the push-to-talk control.
 *
 * Push-to-talk rather than always-on listening. Always-on would materially
 * improve Tier 5 detection, and it is also the single biggest trust cost this
 * product could pay: a microphone that is always open is a reason to leave the
 * phone in another room, and a phone in another room protects nobody
 * (PRD §15 Q1). Opt-in always-on is a roadmap item, not a default.
 */
function initVoice() {
  const btn = document.getElementById('ptt');
  const label = document.getElementById('ptt-label');
  if (!btn) return;

  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) {
    // No Web Speech support: fall back to typing rather than removing the
    // feature. Zero-typing is the goal, but a surface nobody can use is worse.
    if (label) label.textContent = 'Voice unavailable — type instead';
    enableTypedFallback();
    return;
  }

  recognition = new SR();
  recognition.continuous = false;
  recognition.interimResults = true;
  recognition.lang = 'en-US';

  recognition.addEventListener('result', (e) => {
    const text = Array.from(e.results).map((r) => r[0].transcript).join('');
    if (label) label.textContent = text;
    if (e.results[e.results.length - 1].isFinal) sendUtterance(text);
  });

  recognition.addEventListener('end', () => {
    listening = false;
    btn.dataset.state = 'idle';
    if (label && !label.textContent) label.textContent = 'Hold to talk';
  });

  recognition.addEventListener('error', (e) => {
    listening = false;
    btn.dataset.state = 'idle';
    if (label) {
      label.textContent = e.error === 'not-allowed'
        ? 'Microphone blocked — type instead'
        : 'Did not catch that. Try again.';
    }
    if (e.error === 'not-allowed') enableTypedFallback();
  });

  const start = () => {
    if (listening) return;
    listening = true;
    btn.dataset.state = 'listening';
    if (label) label.textContent = 'Listening…';
    try { recognition.start(); } catch { /* already started */ }
  };
  const stop = () => { if (listening) { try { recognition.stop(); } catch {} } };

  btn.addEventListener('pointerdown', start);
  btn.addEventListener('pointerup', stop);
  btn.addEventListener('pointerleave', stop);

  // Keyboard equivalent. A press-and-hold control that only works with a mouse
  // excludes keyboard and switch users from the product's primary interaction.
  btn.addEventListener('keydown', (e) => {
    if ((e.key === ' ' || e.key === 'Enter') && !e.repeat) { e.preventDefault(); start(); }
  });
  btn.addEventListener('keyup', (e) => {
    if (e.key === ' ' || e.key === 'Enter') { e.preventDefault(); stop(); }
  });
}

/** Typed input as an accessibility and no-support fallback. */
function enableTypedFallback() {
  if (document.getElementById('typed-fallback')) return;
  const host = document.getElementById('ptt')?.parentElement;
  if (!host) return;

  const form = document.createElement('form');
  form.id = 'typed-fallback';
  form.innerHTML = `
    <label class="label" for="typed-input">Type what you would have said</label>
    <input id="typed-input" name="text" type="text" autocomplete="off"
           placeholder="I'm having a hard night">`;
  form.addEventListener('submit', (e) => {
    e.preventDefault();
    const input = form.querySelector('input');
    if (input.value.trim()) { sendUtterance(input.value.trim()); input.value = ''; }
  });
  host.appendChild(form);
}

/* -------------------------------------------------------------------------- */
/* Conversation                                                                */
/* -------------------------------------------------------------------------- */

/**
 * Send an utterance for triage, then render the reply.
 *
 * The ordering here mirrors the server's: the tier is applied to the UI as soon
 * as triage returns, BEFORE the generated reply arrives. The safety response must
 * never be gated on a language model finishing a sentence.
 */
async function sendUtterance(text) {
  addTurn('you', text);
  try {
    const res = await post('/api/utterance', { text });

    // 1. Safety first — apply the deterministic decision immediately.
    if (res.triage) {
      renderTier(res.triage.tier, res.triage.reason);
      addSystemNote(res.triage.reason);
    }

    // 2. Then the language layer, which is allowed to fail without consequence.
    const reply = res.reply;
    if (reply?.text) {
      addTurn('threshold', reply.text, reply.live);
      speak(reply.text);
    } else if (reply?.error) {
      addSystemNote(reply.error);
    }

    // Tier 2 is where the Memory Vault earns its place.
    if (res.triage?.tier === 2) loadVaultClip(text);
  } catch (err) {
    addSystemNote('Could not reach the server. The emergency numbers still work.');
  }
}

/** Append a turn to the transcript. */
function addTurn(who, text, live) {
  const host = document.getElementById('transcript');
  if (!host) return;
  const el = document.createElement('div');
  el.className = 'turn';
  el.dataset.who = who;
  el.textContent = text;

  // A generation that came from cache rather than a live call is labelled as
  // such, every time. The user must always be able to tell what is real.
  if (who === 'threshold' && live === false) {
    const badge = document.createElement('span');
    badge.className = 'unverified';
    badge.textContent = 'offline fallback';
    el.appendChild(document.createTextNode(' '));
    el.appendChild(badge);
  }
  host.appendChild(el);
  host.scrollTop = host.scrollHeight;
}

/** A short machine-voice note explaining what the system just did and why. */
function addSystemNote(text) {
  if (!text) return;
  const el = document.getElementById('system-notice');
  if (el) el.textContent = text;
}

/* -------------------------------------------------------------------------- */
/* Generative surfaces                                                         */
/* -------------------------------------------------------------------------- */

/**
 * Render a Generation into a target element, honouring the live flag.
 *
 * This is the single place the UI decides how to present model output, so the
 * "a fallback never masquerades as live" rule holds everywhere by construction.
 */
function renderGeneration(targetId, gen) {
  const el = document.getElementById(targetId);
  if (!el) return;

  if (!gen || (!gen.text && gen.error)) {
    el.innerHTML = `<p class="prose">${escapeHtml(gen?.error || 'Unavailable.')}</p>`;
    return;
  }
  const badge = gen.live
    ? ''
    : ' <span class="unverified">offline fallback</span>';
  el.innerHTML =
    `<div class="prose">${escapeHtml(gen.text).replace(/\n/g, '<br>')}</div>${badge}`;
}

/** Escape before inserting model output into the DOM. Never trust a generation. */
function escapeHtml(s) {
  const d = document.createElement('div');
  d.textContent = String(s ?? '');
  return d.innerHTML;
}

async function loadScript911() {
  const gen = await api('/api/script/911');
  renderGeneration('script-911', gen);
  // Read it aloud one line at a time, pausing between: under acute stress a
  // paragraph is unusable, but a single line can be repeated to a dispatcher.
  if (gen?.text) gen.text.split('\n').filter(Boolean).forEach((line, i) => {
    setTimeout(() => speak(line), i * 3500);
  });
}

async function loadTolerance() {
  const gen = await api('/api/tolerance');
  renderGeneration('tolerance-msg', gen);
  return gen;
}

async function loadVaultClip(context) {
  const res = await api(`/api/vault/select?context=${encodeURIComponent(context || '')}`);
  const host = document.getElementById('vault-panel');
  if (!host || !res.clip) return;
  host.hidden = false;
  host.innerHTML = `
    <p class="label">From ${escapeHtml(res.clip.recorded_by)} · ${escapeHtml(res.clip.relation)}</p>
    <blockquote class="lede">${escapeHtml(res.clip.transcript)}</blockquote>
    ${res.why ? `<p class="prose">${escapeHtml(res.why)}</p>` : ''}
    ${res.live === false ? '<span class="unverified">offline fallback</span>' : ''}`;
  // A real recording in a real voice. We do not synthesise the caregiver
  // (PRD §7.2) — if there is no audio file, the transcript stands on its own.
  if (res.clip.audio_path) new Audio(res.clip.audio_path).play().catch(() => {});
}

/* -------------------------------------------------------------------------- */
/* Live ladder stream                                                          */
/* -------------------------------------------------------------------------- */

/**
 * Subscribe to server-sent ladder events.
 *
 * This is what lets a tier change on this screen appear on the caregiver's
 * screen without polling. EventSource reconnects on its own, so there is no
 * hand-written reconnection logic here to get subtly wrong.
 */
function initStream() {
  try {
    const es = new EventSource('/api/events');
    es.addEventListener('message', (e) => {
      const data = JSON.parse(e.data);
      if (data.type === 'tier') renderTier(data.tier, data.reason);
      if (data.type === 'reset') window.location.reload();
    });
  } catch { /* SSE unsupported — /api/state on load still renders correctly. */ }
}

/* -------------------------------------------------------------------------- */
/* Controls                                                                    */
/* -------------------------------------------------------------------------- */

function initControls() {
  // One-tap rescind. Must be instant and must never ask "are you sure?": if
  // undoing a false alarm is awkward, people disable the tier that protects
  // them (PRD §15 Q3).
  ['rescind', 'takeover-rescind'].forEach((id) => {
    document.getElementById(id)?.addEventListener('click', async () => {
      clearEmergencyTimers();
      window.speechSynthesis?.cancel();
      const res = await post('/api/rescind');
      renderTier(res.tier, res.reason);
      addSystemNote(res.reason);
    });
  });

  // Demo controls: set a tier directly so an evaluator can inspect any state
  // without performing a distressing script out loud in a crowded room.
  document.querySelectorAll('[data-set-tier]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const res = await post('/api/tier', { tier: Number(btn.dataset.setTier) });
      renderTier(res.tier, res.reason);
      addSystemNote(res.reason);
    });
  });

  // Silence simulation — the desktop stand-in for a phone's accelerometer.
  // Labelled as a simulation in the UI rather than dressed up as real sensing.
  document.getElementById('sim-silence')?.addEventListener('click', async () => {
    const res = await post('/api/sensor', { silent_seconds: 30, still: true });
    renderTier(res.tier, res.reason);
    addSystemNote(res.reason);
  });

  document.getElementById('show-samaritan')?.addEventListener('click', loadSamaritan);
  document.getElementById('show-refusal')?.addEventListener('click', async () => {
    renderGeneration('refusal-panel', await api('/api/script/refusal'));
  });
  document.getElementById('play-vault')?.addEventListener('click', () => loadVaultClip(''));
  document.getElementById('arm-bystander')?.addEventListener('click', openBystander);

  document.getElementById('reset-demo')?.addEventListener('click', async () => {
    await post('/api/reset');
    window.location.reload();
  });

  document.getElementById('logout')?.addEventListener('click', async () => {
    await post('/api/auth/logout').catch(() => {});
    window.location.href = '/login';
  });
}

/**
 * Good Samaritan brief — served from the static reviewed dataset, never generated.
 *
 * Rendered with its verification status visible. If a record has not been checked
 * against the current statute by a human, the user is told that, because the one
 * thing worse than no legal information here is confident wrong legal information
 * (PRD §6.5).
 */
async function loadSamaritan() {
  const state = document.body.dataset.state || 'KY';
  const rec = await api(`/api/legal/${state}`);
  const host = document.getElementById('samaritan-panel');
  if (!host) return;
  host.hidden = false;

  if (rec.unknown) {
    host.innerHTML = `<p class="prose">${escapeHtml(rec.summary)}</p>`;
    return;
  }
  host.innerHTML = `
    <p class="label">${escapeHtml(rec.state_name)}</p>
    <p class="lede">${escapeHtml(rec.plain_language_line)}</p>
    <p class="prose">${escapeHtml(rec.summary)}</p>
    <p class="prose"><strong>What it does not cover:</strong> ${escapeHtml(rec.does_not_cover)}</p>
    ${rec.verified ? '' : '<p><span class="unverified">unverified — confirm locally</span></p>'}`;
  speak(rec.plain_language_line);
}

/* -------------------------------------------------------------------------- */
/* Boot                                                                        */
/* -------------------------------------------------------------------------- */

async function boot() {
  initControls();
  initVoice();
  initStream();

  let state;
  try {
    state = await api('/api/state');
  } catch {
    addSystemNote('Offline. Emergency numbers and the overdose guide still work.');
    return;
  }

  renderTier(state.tier, '');
  if (state.profile) document.body.dataset.state = state.profile.state_code || 'KY';

  // Report AI availability honestly and visibly, rather than letting an offline
  // model look like a broken feature.
  const ai = document.getElementById('ai-status');
  if (ai) {
    ai.textContent = state.ai_online ? 'AI online' : 'AI offline — no API key';
    ai.dataset.tone = state.ai_online ? 'ok' : 'warn';
  }

  renderStats(state.profile);
  renderLog(state.events);

  // Tolerance Guard fires unprompted on a day when nothing is wrong. This is the
  // product's argument in a single interaction, so it runs on load rather than
  // waiting to be asked (PRD §5.1).
  const gen = await loadTolerance();
  if (gen?.window_active && gen.text) speak(gen.text);
}

function renderStats(profile) {
  if (!profile) return;
  setText('stat-naloxone', profile.naloxone_on_hand ? 'Within reach' : 'Not on hand');
  setText('stat-contacts', String(profile.contacts?.length ?? 0));

  const ev = profile.tolerance_events?.[0];
  if (ev) {
    const days = Math.floor((Date.now() - new Date(ev.date)) / 86400000);
    setText('stat-tolerance', `Day ${days}`);
  }
}

/** Render the event log. Every event is user-visible; nothing is filtered here. */
function renderLog(events) {
  const host = document.getElementById('recent-log');
  if (!host || !events?.length) return;
  host.innerHTML = events.slice(-8).reverse().map((e) => `
    <li class="log-row">
      <span class="data">T${e.tier}</span>
      <span>${escapeHtml(e.reason)}</span>
      <span class="data">${new Date(e.at).toLocaleTimeString()}</span>
    </li>`).join('');
}

document.addEventListener('DOMContentLoaded', boot);
