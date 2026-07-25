/* ============================================================================
   THRESHOLD — bystander.js
   Behaviour for /bystander. NO ACCOUNT, EVER (PRD §3).

   WHAT THIS FILE DOES
     Four things, in the order the page needs them:

       1. Loads the Good Samaritan brief from GET /api/legal/{state} and renders
          its verification status honestly.
       2. Drives the naloxone walkthrough as a stepper, so a shaking hand reads
          one instruction at a time instead of a wall of five.
       3. Runs the rescue-breathing metronome at one breath every five seconds
          (~12/min), on three redundant channels: sound, spoken word, and text.
       4. Fills the address handoff when — and only when — this phone belongs to
          a Threshold user who armed bystander mode.

   WHY THE LEGAL BRIEF IS FIRST
     The dominant reason bystanders do not call 911 at an overdose is fear of
     arrest. Removing that fear IS the intervention; every medical instruction
     below it is useless if the person has already left the room. So the legal
     fetch is the first thing this file does, before the metronome is even wired.

   WHAT THIS FILE DELIBERATELY DOES NOT DO
     It never asks who you are. There is no auth call on this page and no
     redirect to /login on any failure path. A stranger at an overdose must
     never be asked to make an account (PRD §3).

     It never generates legal text, and never smooths over an unverified record.
     `/api/legal/{state}` serves a static, human-reviewed dataset; when a record
     carries verified:false, this renders an "unverified — confirm locally"
     badge. Confident wrong legal information is the worst possible failure in
     this product (CONTRACT: architecture invariants).

     It never blocks the medical content on a network call. Every instruction on
     this page — including the reassurance sentence above the statute and the
     "say exactly this" line — is in the markup, not injected. With the server
     unreachable, the page is still complete and the tel: link still dials.

     It never relies on animation. The metronome's ring is CSS-driven decoration
     that stops under prefers-reduced-motion; the word, the count, the tone and
     the spoken cue all still fire, because the timing IS the instruction.

   NO BUILD STEP
     Plain ES module, no bundler, no framework, no CDN.
   ========================================================================= */

'use strict';

/* -------------------------------------------------------------------------- */
/* Helpers                                                                     */
/* -------------------------------------------------------------------------- */

/** Escape before inserting server data into the DOM. Never trust a payload. */
function escapeHtml(s) {
  const d = document.createElement('div');
  d.textContent = String(s ?? '');
  return d.innerHTML;
}

/**
 * Fetch JSON with a hard timeout.
 *
 * A short ceiling here on purpose: this page may be open on a phone with one
 * bar in a stairwell. Eight seconds of waiting for a legal record is eight
 * seconds not spent on the chest of the person on the floor — after that we
 * fall back to the static sentence already in the markup.
 *
 * @param {string} path
 * @param {number} timeoutMs
 * @returns {Promise<object>} Parsed JSON. Rejects on failure or timeout.
 */
async function api(path, timeoutMs = 8000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    // credentials:'omit' — this page has no session and must not send one.
    // A bystander using a stranger's phone should not have that phone's cookie
    // attached to anything they do here.
    const res = await fetch(path, { credentials: 'omit', signal: controller.signal });
    return await res.json();
  } finally {
    clearTimeout(timer);
  }
}

/** True when the user has asked the OS to reduce motion. Checked at call time,
 *  not cached, so a mid-session system change is respected. */
function reducedMotion() {
  return window.matchMedia?.('(prefers-reduced-motion: reduce)').matches === true;
}

/* -------------------------------------------------------------------------- */
/* 1 — Good Samaritan brief                                                    */
/* -------------------------------------------------------------------------- */

/**
 * Load and render the Good Samaritan record for a state.
 *
 * The record comes from data/legal/good_samaritan.json via the API. It is never
 * model-generated (CONTRACT: "the worst possible hallucination in this
 * product"), so this function's whole job is to render it faithfully and to be
 * loud about what it does not know:
 *
 *   - unknown state  -> the server's plain "we do not have a summary" sentence,
 *                       which still ends with "calling 911 is the right thing".
 *   - verified:false -> an "unverified — confirm locally" badge, always shown.
 *   - fetch failed   -> the static sentence already above it in the markup
 *                       stands on its own; we say the statute could not load
 *                       rather than leaving "Loading…" on screen forever.
 *
 * @param {string} stateCode Two-letter code from the picker, e.g. 'KY'.
 */
async function loadSamaritan(stateCode) {
  const host = document.getElementById('statute');
  if (!host) return;

  host.textContent = 'Checking reviewed regional guidance…';

  let rec;
  try {
    rec = await api(`/api/legal/${encodeURIComponent(stateCode)}`);
  } catch {
    // Degrade to the markup. The headline above, "Call 112 and stay with
    // them" — is authored, reviewed copy that is unconditionally true
    // everywhere, so the page still does its job with the network down. We
    // deliberately do NOT fall back to a generic claim about legal protection:
    // if we cannot load the state's record, we say we cannot, rather than
    // guessing at an immunity that varies by state.
    host.innerHTML =
      '<span class="unverified badge badge--fallback">regional guidance unavailable</span> ' +
      'The advice above still stands: call, stay, and say what you see.';
    return;
  }

  // A state we have no reviewed record for. We say so plainly rather than
  // guessing — a guess here is a legal claim we cannot stand behind.
  if (rec.unknown) {
    host.innerHTML =
      `<span class="unverified badge badge--fallback">no reviewed summary for ${escapeHtml(rec.state_code)}</span> ` +
      escapeHtml(rec.summary);
    return;
  }

  // verified:false means a human has not checked this record against the
  // current statute. It is still shown — an unverified summary beats none — but
  // it is never shown as settled fact.
  const badge = rec.verified
    ? ''
    : '<span class="unverified badge badge--fallback">unverified — confirm locally</span> ';

  host.innerHTML = `
    ${badge}
    <strong>${escapeHtml(rec.state_name || rec.state_code)}.</strong>
    ${escapeHtml(rec.plain_language_line || '')}
    ${rec.summary ? `<br>${escapeHtml(rec.summary)}` : ''}
    ${rec.does_not_cover
      ? `<br><em>What it does not cover:</em> ${escapeHtml(rec.does_not_cover)}`
      : ''}
    ${rec.naloxone_note ? `<br>${escapeHtml(rec.naloxone_note)}` : ''}
    ${rec.source_note ? `<br><small>${escapeHtml(rec.source_note)}</small>` : ''}`;

  // Read the one-line version aloud. A bystander is looking at a person, not a
  // phone, and this is the sentence most likely to keep them in the room.
  speak(rec.plain_language_line || '');
}

/** Wire the state picker. The legal protection is state-specific and getting it
 *  wrong is the worst failure this page can have, so the control is deliberately
 *  NOT marked [data-chrome] in the markup and survives the tier-4 chrome strip. */
function initStatePicker() {
  const select = document.getElementById('state');
  if (!select) return;

  select.addEventListener('change', () => loadSamaritan(select.value));

  // Best-effort personalisation: if this phone belongs to a Threshold user, use
  // THEIR state rather than the Kentucky default. Wrapped so an anonymous
  // bystander on a stranger's phone simply gets the default — a 401 or a
  // missing profile must never surface as an error on this page.
  fetch('/api/state', { credentials: 'same-origin' })
    .then((r) => r.json())
    .then((state) => {
      const code = state?.profile?.state_code;
      if (code && select.querySelector(`option[value="${code}"]`)) {
        select.value = code;
        loadSamaritan(code);
      }
      renderAddress(state?.profile);
    })
    .catch(() => { /* No session, no server, no problem. The default stands. */ });

  loadSamaritan(select.value);
}

/* -------------------------------------------------------------------------- */
/* Address handoff                                                             */
/* -------------------------------------------------------------------------- */

/**
 * Show the address for the dispatcher, if this phone knows one.
 *
 * The section stays hidden when there is no profile, rather than showing an
 * empty box that implies missing information. Paramedics standing outside a
 * locked door is the failure this exists to prevent, so the entry code is
 * included — it is only ever released at tier 4 and above, and this page is
 * pinned to tier 4.
 *
 * @param {object|null} profile UserProfile from /api/state, or null/undefined.
 */
function renderAddress(profile) {
  const card = document.getElementById('address-card');
  const text = document.getElementById('address-text');
  if (!card || !text || !profile?.address) return;

  const parts = [
    profile.address,
    profile.unit ? `Unit ${profile.unit}` : '',
    profile.entry_code ? `Entry code ${profile.entry_code}` : '',
    profile.cross_street ? `Near ${profile.cross_street}` : '',
  ].filter(Boolean);

  text.textContent = parts.join(' · ');
  card.hidden = false;
}

/* -------------------------------------------------------------------------- */
/* Speech                                                                      */
/* -------------------------------------------------------------------------- */

/**
 * Speak text aloud.
 *
 * Slower and lower-pitched than the default, as in app.js: the listener is
 * frightened, possibly in a loud room, and reading nothing. We never claim to
 * be a person (PRD P5) — the copy passed in here always refers to the system as
 * the system.
 *
 * @param {string} text
 * @param {{loud?: boolean}} opts
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
/* 3 — Rescue-breathing metronome                                              */
/* -------------------------------------------------------------------------- */

/*
  ONE BREATH EVERY FIVE SECONDS — twelve a minute, the rescue-breathing rate for
  an adult in respiratory arrest. The interval is the clinical content here, so
  it is a named constant rather than a literal buried in a setInterval call.

  FOUR redundant channels, because a bystander is looking at a person and not a
  phone, may be in a loud room, and may have motion or audio disabled:
    tone    a short beep on the breath beat (Web Audio)
    speech  the word spoken aloud
    text    #metronome-word flips BREATHE / WAIT (aria-live="assertive")
    count   #metronome-count shows the seconds remaining
  The CSS ring animation is a fifth, decorative channel that is disabled under
  prefers-reduced-motion — which is safe precisely because it carries nothing
  the other four do not.
*/
const BREATH_INTERVAL_MS = 5000;
const BREATH_TICK_MS = 1000;

/** Interval handle for the one-second tick. Null when stopped. */
let breathTimer = null;
/** Seconds remaining until the next breath. Counts 5 -> 1, then fires. */
let breathCountdown = 5;
/** Lazily created AudioContext — created on the first user gesture, because
 *  browsers refuse to start one without an interaction. */
let audioCtx = null;

/**
 * Emit a short tone.
 *
 * Web Audio rather than an <audio> file: no network request, so it works with
 * the connection down, and no decoding delay before the first beat. Wrapped in
 * try/catch throughout — audio is a redundant channel, and a browser that
 * refuses to make noise must not stop the countdown.
 *
 * @param {number} freq     Frequency in Hz. Higher for the breath, lower for ticks.
 * @param {number} duration Seconds.
 */
function tone(freq, duration) {
  try {
    if (!audioCtx) {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) return;
      audioCtx = new Ctx();
    }
    if (audioCtx.state === 'suspended') audioCtx.resume();

    const osc = audioCtx.createOscillator();
    const gain = audioCtx.createGain();
    osc.type = 'sine';
    osc.frequency.value = freq;
    // Ramp rather than a hard stop: a square-edged gate clicks, and a clicking
    // phone next to an unconscious person is one more thing to be frightened of.
    gain.gain.setValueAtTime(0.0001, audioCtx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.25, audioCtx.currentTime + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, audioCtx.currentTime + duration);
    osc.connect(gain).connect(audioCtx.destination);
    osc.start();
    osc.stop(audioCtx.currentTime + duration + 0.02);
  } catch { /* Audio unavailable. Three other channels still carry the beat. */ }
}

/**
 * Advance the metronome by one second.
 *
 * Fires the breath cue at zero, then resets. Everything visible is written on
 * every tick rather than only on the beat, so a bystander who looks up
 * mid-cycle immediately knows how long they have.
 */
function breathTick() {
  const word = document.getElementById('metronome-word');
  const count = document.getElementById('metronome-count');

  breathCountdown -= 1;

  if (breathCountdown <= 0) {
    breathCountdown = BREATH_INTERVAL_MS / BREATH_TICK_MS;
    if (word) word.textContent = 'BREATHE';
    if (count) count.textContent = String(breathCountdown);
    tone(660, 0.35);
    // Spoken cue, so the instruction lands with the phone face-down on the floor.
    speak('Breathe', { loud: true });
    return;
  }

  if (word) word.textContent = 'WAIT';
  if (count) count.textContent = String(breathCountdown);
  // A quieter, lower tick on the in-between seconds. Enough to keep the rhythm
  // audible without competing with the breath cue.
  tone(330, 0.06);
}

/**
 * Start or stop the metronome.
 *
 * Toggle rather than start-only: a bystander whose person has started breathing
 * needs the noise to stop immediately, and hunting for a second control while
 * the phone beeps is exactly the wrong experience.
 *
 * @param {boolean} run True to start, false to stop.
 */
function setMetronome(run) {
  const host = document.getElementById('metronome');
  const btn = document.getElementById('metronome-toggle');
  const word = document.getElementById('metronome-word');
  const count = document.getElementById('metronome-count');

  if (breathTimer) { clearInterval(breathTimer); breathTimer = null; }

  // data-running drives the CSS ring animation, which is itself disabled under
  // prefers-reduced-motion by a media query in pages.css. We still set the
  // attribute in that case: it is also the state hook for the border colour,
  // which is not motion and is useful to everyone.
  if (host) host.dataset.running = String(run);
  if (btn) {
    btn.textContent = run ? 'Stop the rhythm' : 'Start the rhythm';
    // A11Y: the button is a real toggle, so its pressed state is exposed rather
    // than left for a sighted user to infer from the label.
    btn.setAttribute('aria-pressed', String(run));
  }

  if (!run) {
    if (word) word.textContent = 'Stopped';
    if (count) count.textContent = String(BREATH_INTERVAL_MS / BREATH_TICK_MS);
    window.speechSynthesis?.cancel();
    return;
  }

  // Start on a breath, not on a wait. The first thing that happens when you
  // press the button is the thing you are supposed to do.
  breathCountdown = BREATH_INTERVAL_MS / BREATH_TICK_MS;
  if (word) word.textContent = 'BREATHE';
  if (count) count.textContent = String(breathCountdown);
  tone(660, 0.35);
  speak('Breathe. One breath every five seconds.', { loud: true });

  breathTimer = setInterval(breathTick, BREATH_TICK_MS);

  // Under reduced motion the ring does not animate, so the count is the only
  // continuous visual signal. Say so once, rather than leaving someone watching
  // a still circle and wondering whether it is working.
  if (reducedMotion() && word) {
    word.setAttribute('aria-description', 'Follow the number and the spoken cue.');
  }
}

/**
 * Wire the metronome control.
 *
 * The toggle is a real <button>, so Enter and Space already work and no key
 * handling is needed here. The AudioContext is created inside the click
 * handler because browsers will not allow one to start without a user gesture.
 */
function initMetronome() {
  const btn = document.getElementById('metronome-toggle');
  if (!btn) return;
  btn.addEventListener('click', () => {
    setMetronome(btn.getAttribute('aria-pressed') !== 'true');
  });

  // Stop the noise if the page is hidden — a beeping phone in a pocket during a
  // 911 call is actively harmful. Deliberately does not auto-resume: restarting
  // rescue breathing is a decision a person makes, not a page.
  document.addEventListener('visibilitychange', () => {
    if (document.hidden && breathTimer) setMetronome(false);
  });
}

/* -------------------------------------------------------------------------- */
/* 2 — Naloxone walkthrough stepper                                            */
/* -------------------------------------------------------------------------- */

/**
 * Turn the static <ol class="steps"> into a one-at-a-time walkthrough.
 *
 * PROGRESSIVE ENHANCEMENT, NOT A REWRITE. Every step is already complete in the
 * markup, so with this script absent or broken the page is a full, correct,
 * scrollable set of instructions. This only adds a "next" affordance and a
 * position indicator on top of it — and it never HIDES a step, because a
 * bystander who needs to look back at the naloxone instruction while the
 * walkthrough has moved on must not have to hunt for it.
 *
 * The current step is marked with `aria-current="step"` and given focus on
 * advance, so a screen-reader user hears "step 3 of 5" and lands on the right
 * heading instead of being silently re-ordered.
 */
function initStepper() {
  const list = document.querySelector('.steps');
  if (!list) return;

  const steps = Array.from(list.children);
  if (steps.length < 2) return;

  let index = 0;

  /**
   * Mark a step as the current one.
   * @param {number} i Zero-based index; clamped to the list.
   */
  const focusStep = (i) => {
    index = Math.max(0, Math.min(i, steps.length - 1));
    steps.forEach((li, n) => {
      li.setAttribute('aria-current', n === index ? 'step' : 'false');
      li.dataset.state = n < index ? 'done' : n === index ? 'active' : 'ahead';
    });

    const heading = steps[index].querySelector('.step__title');
    if (heading) {
      // tabindex -1 so focus can be moved programmatically without adding the
      // heading to the tab order permanently.
      heading.setAttribute('tabindex', '-1');
      heading.focus({ preventScroll: true });
      // scrollIntoView is 'auto' rather than 'smooth' under reduced motion:
      // a smooth scroll IS motion, and this one is triggered by the system.
      heading.scrollIntoView({
        block: 'center',
        behavior: reducedMotion() ? 'auto' : 'smooth',
      });
    }
    if (progress) progress.textContent = `Step ${index + 1} of ${steps.length}`;
  };

  // The controls are built here rather than in the markup, because without JS
  // there is no stepper to control and a dead "Next step" button on a page like
  // this is a small betrayal.
  const controls = document.createElement('div');
  controls.className = 'row';
  controls.style.marginTop = 'var(--sp-5)';

  const progress = document.createElement('p');
  progress.className = 'hint';
  // A11Y: polite. The step count is useful context, not an interruption — the
  // metronome's assertive region is the only thing on this page allowed to cut in.
  progress.setAttribute('role', 'status');
  progress.setAttribute('aria-live', 'polite');
  progress.textContent = `Step 1 of ${steps.length}`;

  const prev = document.createElement('button');
  prev.type = 'button';
  prev.className = 'btn btn--quiet';
  prev.textContent = 'Back';
  prev.addEventListener('click', () => focusStep(index - 1));

  const next = document.createElement('button');
  next.type = 'button';
  next.className = 'btn';
  next.textContent = 'Next step';
  next.addEventListener('click', () => focusStep(index + 1));

  controls.append(prev, next, progress);
  list.after(controls);

  focusStep(0);
}

/* -------------------------------------------------------------------------- */
/* Boot                                                                        */
/* -------------------------------------------------------------------------- */

/**
 * Entry point.
 *
 * The legal brief goes first, deliberately: see the ordering note at the top of
 * this file. Nothing here awaits anything else — a slow legal fetch must not
 * delay the metronome being tappable.
 */
function boot() {
  initStatePicker();   // 1 — reassurance, fetched first.
  initStepper();       // 2 — the walkthrough.
  initMetronome();     // 3 — the rhythm.
  // 4 — the address handoff is filled by initStatePicker's /api/state call,
  // because it is the same round trip and this page should make as few as
  // possible on a bad connection.
}

document.addEventListener('DOMContentLoaded', boot);
