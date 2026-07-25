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

/** Last tier announced to assistive tech, so a repaint never repeats it. */
let lastAnnouncedTier = -1;

/** True while the Tier 4/5 takeover owns the screen. Guards entry/exit work so
 *  a 4->5 transition does not re-run it and clobber the saved focus. */
let emergencyActive = false;

/** Where focus was before the takeover appeared, restored on rescind. */
let focusBeforeTakeover = null;

/**
 * Keep Tab inside the emergency dialog.
 *
 * `inert` on .shell already removes everything behind the overlay from the tab
 * order, which handles the interior of the page. This closes the remaining gap:
 * Tab from the last control in the dialog otherwise escapes to the browser
 * chrome (address bar, tab strip) and the next Tab returns to the top of the
 * document — so a keyboard user in an emergency can walk off the one screen
 * that matters. Registered once at boot; it no-ops unless the takeover is up.
 */
function initFocusTrap() {
  const takeover = document.getElementById('takeover');
  if (!takeover) return;

  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Tab' || !emergencyActive || takeover.hidden) return;

    // Queried on every press rather than cached: the rescind button is removed
    // by CSS at Tier 5, so the set of focusable controls genuinely changes
    // while the dialog is open.
    const focusable = Array.from(
      takeover.querySelectorAll('a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])')
    ).filter((el) => el.offsetParent !== null || el === document.activeElement);

    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];

    // Focus sitting outside the dialog entirely (page just took over, or an
    // extension moved it) — pull it back rather than letting Tab walk away.
    if (!takeover.contains(document.activeElement)) {
      e.preventDefault();
      first.focus();
      return;
    }
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  });
}

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

  // Write to BOTH <html> and <body>.
  //
  // The stylesheets select on [data-tier] unqualified, and the markup declares
  // the attribute on <html> as its pre-script floor. Writing only to <body>
  // therefore left the real tier on the inner element while <html> kept its
  // stale initial value — the ladder never repainted, and at Tier 0 an
  // ancestor still matching a higher tier could paint the emergency takeover
  // over an untouched page. Setting both keeps the floor and the runtime value
  // in agreement, which is the invariant the CSS was written against.
  document.documentElement.dataset.tier = String(tier);
  document.body.dataset.tier = String(tier);

  // The ladder rail: mark the active rung for sighted users and for AT.
  document.querySelectorAll('[data-tier-step]').forEach((el) => {
    const step = Number(el.dataset.tierStep);
    el.setAttribute('data-state', step === tier ? 'active' : step < tier ? 'passed' : 'ahead');
    el.setAttribute('aria-current', step === tier ? 'step' : 'false');
  });

  // The demo tier buttons ship aria-pressed in the markup and nothing ever
  // updated it, so button "0" announced itself as pressed at every tier while
  // the accent highlight (keyed off the same attribute in pages.css) stayed
  // stuck on zero. The control looked broken to a sighted evaluator and lied
  // to a screen reader.
  document.querySelectorAll('[data-set-tier]').forEach((el) => {
    el.setAttribute('aria-pressed', String(Number(el.dataset.setTier) === tier));
  });

  // Tier 4/5 replace the interface with a single action. The takeover is a real
  // element that is shown, not a new page: navigation during an emergency risks
  // losing the session, and the phone may be about to leave the user's hand.
  // Announce the transition once, and only when it actually changed. The
  // markup ships #tier-announcer for exactly this and nothing ever wrote to
  // it, so a screen reader user got no notification that the tier had moved —
  // including into an emergency.
  if (tier !== lastAnnouncedTier) {
    lastAnnouncedTier = tier;
    setText('tier-announcer', `${TIER_NAMES[tier]}. ${reason || ''}`);
  }
  setText('tier-name', TIER_NAMES[tier]);
  if (reason) setText('tier-reason', reason);

  const takeover = document.getElementById('takeover');
  if (takeover) {
    const emergency = tier >= 4;
    takeover.hidden = !emergency;

    // A REAL focus trap, not aria-hidden alone.
    //
    // aria-hidden on #main left two holes the markup already promised were
    // closed: the focused element could remain inside the hidden subtree
    // (which is an accessibility error, not just untidy), and every control in
    // the rail stayed Tab-reachable behind the overlay. `inert` removes the
    // shell from focus order AND the accessibility tree in one attribute, so a
    // keyboard or screen reader user at Tier 4 has literally one reachable
    // control: the 911 link.
    const shell = document.querySelector('.shell');
    if (shell) {
      if (emergency) {
        shell.setAttribute('inert', '');
        shell.setAttribute('aria-hidden', 'true');
      } else {
        shell.removeAttribute('inert');
        // Removed rather than set to "false": an aria-hidden attribute present
        // at all is a thing AT has to evaluate, and the absence of it is the
        // unambiguous "this is normal content" signal.
        shell.removeAttribute('aria-hidden');
      }
    }

    if (emergency) {
      setText('takeover-tier', TIER_NAMES[tier]);
      setText('takeover-reason', reason || '');

      // Remember where focus was so rescind can put it back. Only captured on
      // ENTRY to the emergency — re-reading it on a 4->5 transition would
      // record the 911 link itself and lose the real origin.
      if (!emergencyActive) {
        focusBeforeTakeover =
          document.activeElement instanceof HTMLElement ? document.activeElement : null;
        emergencyActive = true;
      }

      // Move focus to the call action. Without this, focus stays wherever it
      // was — now inside an inert subtree, which strands the user entirely.
      document.getElementById('takeover-action')?.focus();

      if (tier === 4) runEmergencySequence();
      // Keep the screen awake: a locked screen mid-overdose is a dead phone to a
      // bystander who picks it up. Best-effort — not supported everywhere.
      requestWakeLock();
    } else {
      // Leaving the emergency: stop anything still queued, or a rescinded
      // alarm keeps speaking and hailing bystanders after being stood down.
      clearEmergencyTimers();
      window.speechSynthesis?.cancel();
      const caption = document.getElementById('speech-caption');
      if (caption) caption.hidden = true;

      // Restore focus to whatever the user was on before the takeover seized
      // the screen. A keyboard user who rescinds and lands back at the top of
      // the document has effectively been thrown out of their place.
      if (emergencyActive) {
        emergencyActive = false;
        // isConnected: the element may have been removed while the takeover was
        // up (the chrome strip at tier 3+ display:nones a lot of the rail).
        if (focusBeforeTakeover?.isConnected) {
          focusBeforeTakeover.focus();
        } else {
          document.getElementById('rescind')?.focus();
        }
        focusBeforeTakeover = null;
      }
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

  // Speak, and caption it on screen. The caption is a separate region from the
  // receipt list: spoken guidance is what to DO right now, the receipt list is
  // what has already happened. Writing speech into the receipt list (which this
  // previously did via textContent) both destroyed the receipts and made
  // instructions look like completed actions.
  const say = (msg) => { captionSpeech(msg); speak(msg); };

  // t=0 — calm, short, no questions yet. Naloxone offered simultaneously.
  // "Help is coming" was a promise this software cannot keep — it dispatches
  // nothing. Someone who believes an ambulance is already on its way may wait
  // instead of pressing the button. The line must stay calm, but it has to put
  // the action back in the hands of whoever is holding the phone.
  say('Stay with me. Press the big button to call 911. If you have Narcan, use it now.');
  recordReceipt('naloxone_prompt_displayed', 'Naloxone guidance was shown and spoken.');

  // t=5s — do not wait for a reply. Continue without user input.
  //
  // The spoken line said "contacting your people now", which was false: this
  // build sends no SMS, places no call, and pushes to no device. What actually
  // happens is that the alert appears on any caregiver screen already open to
  // /caregiver. Saying more than that would let someone stand down and wait.
  emergencyTimers.push(setTimeout(() => {
    say('Finding your location to read out. Anyone watching on the caregiver screen can see this now.');
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
 * Show on screen whatever the app just said out loud.
 *
 * Deaf and hard-of-hearing users must receive identical guidance. In the
 * emergency flow the spoken lines ARE the instructions, so speech-only delivery
 * would mean a whole class of users gets a red screen with no direction at all.
 * aria-live=assertive because this interrupts by design at Tier 4.
 */
function captionSpeech(text) {
  const el = document.getElementById('speech-caption');
  if (!el) return;
  el.textContent = text;
  el.hidden = false;
}

/**
 * Append a line to the emergency status list — AFTER the thing actually happened.
 *
 * This is the execution receipt, and it is deliberately separate from the list
 * of *planned* actions the triage engine returns. Planned and completed are not
 * the same thing, and conflating them is what produced "Contacts called" on a
 * screen belonging to software that has never called anyone.
 *
 * Rule for anything added here: if you cannot point at the line of code that
 * performed the action and succeeded, it does not get written.
 */
function addStatusLine(text, action = '', detail = '') {
  const host = document.getElementById('takeover-status');
  if (!host) return;
  const li = document.createElement('li');
  li.textContent = text;
  host.appendChild(li);
  if (action) recordReceipt(action, detail || text);
}

/** Persist a completed action separately from the triage plan. */
function recordReceipt(action, detail = '') {
  post('/api/action-receipt', { action, detail }).catch(() => {});
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
  // Captioned as well as shouted. A deaf bystander who picks up the phone gets
  // the same sentence — and this is the one line on the screen addressed to
  // them rather than to the phone's owner.
  captionSpeech(msg);
  speak(msg, { loud: true });
  const btn = document.getElementById('arm-bystander');
  if (btn) btn.hidden = false;
  addStatusLine(
    'Bystander guide armed and the phone called out for nearby help',
    'bystander_hail_started',
  );
}

function openBystander() { window.location.href = '/bystander'; }

/** Best-effort location for responders. Only ever acquired during an emergency. */
function acquireLocation() {
  if (!navigator.geolocation) return;
  navigator.geolocation.getCurrentPosition(
    (pos) => {
      const { latitude, longitude } = pos.coords;
      // Appended, not written over the list, and phrased as what it is:
      // coordinates displayed on THIS device for someone to read aloud to a
      // dispatcher. Nothing transmits them anywhere.
      addStatusLine(
        `Location shown here to read aloud: ${latitude.toFixed(5)}, ${longitude.toFixed(5)}`,
        'location_displayed',
        'Coordinates were displayed on the member device for a caller to read aloud.',
      );
    },
    () => { /* Denied or unavailable — the address from the profile still stands. */ },
    { enableHighAccuracy: true, timeout: 8000 }
  );
}

let wakeLock = null;
async function requestWakeLock() {
  try {
    wakeLock = await navigator.wakeLock?.request('screen');
    if (wakeLock) {
      addStatusLine(
        'Screen wake lock acquired',
        'wake_lock_acquired',
        'The device confirmed that the emergency screen will stay awake.',
      );
    }
  } catch { /* unsupported */ }
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
 *
 * SPEECH IS NEVER THE ONLY CHANNEL. Every call site pairs this with visible
 * text: the emergency and 911-script lines go through captionSpeech(), replies
 * and grounding steps and the legal brief are already rendered on screen before
 * they are spoken. A deaf or hard-of-hearing user must receive identical
 * guidance, and in the emergency flow the spoken lines ARE the instructions —
 * speech-only delivery there would leave a whole class of users with a red
 * screen and no direction. If you add a speak() call, add its visible half.
 *
 * Deliberately NOT captioned automatically inside this function: the caption
 * region lives in the takeover, so writing to it from an ordinary Tier 0 reply
 * would push text into a hidden emergency surface rather than the transcript
 * the user is actually reading.
 *
 * THE VOICE IS ALWAYS SYNTHETIC, AND IT IS ALWAYS THE MEMBER'S CHOICE.
 * Three possibilities, and the member picks between them in onboarding:
 *   - the browser's own speech (THE DEFAULT, and what this file falls back to);
 *   - a stock cloud narrator, which is nobody;
 *   - a supporter voice that a caregiver recorded of themselves, consented to
 *     explicitly, and separately chose to share — and that this member
 *     separately chose to turn on. Default off at every one of those steps.
 * Routing lives in voice.js; this function delegates to it.
 *
 * EVERY CLONED UTTERANCE IS VISIBLY LABELLED as an AI recreation for as long as
 * it plays. That is applied inside voice.js at the single point where cloned
 * audio reaches the speaker, so no call site here can forget it, and a cloned
 * voice is never permitted to claim the real person is present (PRD P5).
 *
 * MEMORY VAULT CLIPS NEVER COME THROUGH HERE. Those are real recordings of real
 * people, played as recorded, and no code path routes one into speech synthesis
 * of any kind. The supporter-voice feature does not touch them.
 *
 * PRD §7.2 declined caregiver voice cloning, and the reasoning has not been
 * refuted, only mitigated — consent obtained in calm is spent in crisis, and a
 * "this is synthesised" label does limited cognitive work on someone
 * intoxicated or panicking. The product owner chose to enable it behind the
 * consent chain in app/voice.py. Read that module's docstring before changing
 * anything here.
 */

/** localStorage key for the app-voice preference. Must match onboarding.js. */
const VOICE_KEY = 'threshold.voice';

/**
 * The member's chosen synthetic voice, or null for the browser default.
 *
 * Resolved at CALL time rather than cached at load: getVoices() is empty until
 * the engine warms up, so a value captured on DOMContentLoaded would pin the
 * default for the whole session. Falls back silently when the stored voice is no
 * longer installed — an OS update removing a voice must not make the app mute.
 *
 * @returns {SpeechSynthesisVoice|null}
 */
function preferredVoice() {
  let uri = '';
  try { uri = localStorage.getItem(VOICE_KEY) || ''; } catch { return null; }
  if (!uri) return null;
  const voices = window.speechSynthesis?.getVoices?.() || [];
  return voices.find((v) => v.voiceURI === uri) || null;
}

/**
 * Speak through whichever voice the member chose.
 *
 * Delegates to voice.js, which handles the cloud path, the AI-recreation label,
 * and the fallback. The dynamic import and its `.catch` are load-bearing rather
 * than tidy: a voice module that fails to load, on any browser, must not be
 * able to mute the emergency path. If it does not resolve, this page still
 * speaks — in the browser's own voice, immediately, from the function below.
 */
function speak(text, { loud = false } = {}) {
  if (!text) return;
  import('/static/js/voice.js')
    .then((voice) => voice.speakWithChosenVoice(text, { loud }))
    .catch(() => speakInBrowser(text, { loud }));
}

/**
 * The floor beneath every other speech path: the browser's own synthesis.
 *
 * Kept here, in the page's own bundle, rather than only in voice.js. It is the
 * fallback, and a fallback that lives behind the thing it is a fallback for is
 * not one. Always available, needs no network, costs nothing, imitates nobody.
 */
function speakInBrowser(text, { loud = false } = {}) {
  if (!('speechSynthesis' in window) || !text) return;
  const u = new SpeechSynthesisUtterance(text);
  const voice = preferredVoice();
  if (voice) u.voice = voice;
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
  // The markup calls this button `talk` (index.html). This looked for `ptt`
  // and returned early on null, so speech recognition was NEVER wired and
  // the product's primary interaction did nothing at all.
  const btn = document.getElementById('talk');
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

  /**
   * Write the recording state to every channel at once.
   *
   * There are three, and they must never disagree:
   *   aria-pressed  what a screen reader announces
   *   data-state    what CSS hooks (and what pages.css actually styles is
   *                 [aria-pressed="true"], so this alone changed nothing visible)
   *   label text    what a sighted user reads
   *
   * Before this, only data-state was written: the button was announced as
   * "not pressed" for the entire time it was recording, and the pulsing ring —
   * keyed off aria-pressed in pages.css — never appeared.
   */
  const meter = document.getElementById('ptt-meter');

  const setRecording = (on) => {
    listening = on;
    btn.setAttribute('aria-pressed', String(on));
    btn.dataset.state = on ? 'listening' : 'idle';

    // The meter bar sat at --level: 0 permanently because nothing wrote to it.
    // It is filled while recording and emptied when not, so it is a truthful
    // "the microphone is open" indicator.
    //
    // DELIBERATELY NOT A REAL AMPLITUDE READING. Doing that means opening a
    // second getUserMedia stream alongside SpeechRecognition purely to animate
    // a decorative bar — a second microphone permission, and a second live
    // audio capture, bought for no safety value. A bar that invented a
    // fluctuating level would be fabricated data on a screen whose entire
    // credibility rests on not doing that.
    if (meter) meter.style.setProperty('--level', on ? '1' : '0');
  };

  recognition.addEventListener('end', () => {
    setRecording(false);
    if (label && !label.textContent) label.textContent = 'Hold to talk';
  });

  recognition.addEventListener('error', (e) => {
    setRecording(false);
    if (label) {
      label.textContent = e.error === 'not-allowed'
        ? 'Microphone blocked — type instead'
        : 'Did not catch that. Try again.';
    }
    if (e.error === 'not-allowed') enableTypedFallback();
  });

  const start = () => {
    if (listening) return;
    setRecording(true);
    if (label) label.textContent = 'Listening…';
    try { recognition.start(); } catch { /* already started */ }
  };

  /**
   * Stop recording.
   *
   * The ARIA and visual state are cleared HERE rather than waiting for the
   * recognition 'end' event, because 'end' is not guaranteed to fire — a
   * browser that never started the engine, or one that swallows the stop, would
   * leave the button announced as pressed and visibly pulsing forever. The
   * 'end' handler clearing it a second time is harmless and idempotent.
   */
  const stop = () => {
    if (!listening) return;
    setRecording(false);
    if (label) label.textContent = 'Hold to talk';
    try { recognition.stop(); } catch { /* never started */ }
  };

  btn.addEventListener('pointerdown', (e) => {
    // Capture the pointer so a finger that slides off the circle still delivers
    // its pointerup here. Without capture the browser retargets the release to
    // whatever is under the finger and we never hear about it — which is one of
    // the two ways this control could stick in the recording state.
    try { btn.setPointerCapture(e.pointerId); } catch { /* unsupported */ }
    start();
  });
  btn.addEventListener('pointerup', stop);

  // The other stuck-on path. pointercancel fires when the OS takes the gesture
  // away — a scroll takes over, a call arrives, the browser decides this is a
  // pan. No pointerup ever follows, so without this the microphone stays open
  // with the button claiming to be pressed. lostpointercapture is the belt to
  // that braces: whatever the reason capture ended, recording ends with it.
  btn.addEventListener('pointercancel', stop);
  btn.addEventListener('lostpointercapture', stop);

  // Releasing outside the window (drag off the tab, alt-tab mid-press) also
  // never produces a pointerup on the button.
  window.addEventListener('blur', stop);

  // Keyboard equivalent. A press-and-hold control that only works with a mouse
  // excludes keyboard and switch users from the product's primary interaction.
  btn.addEventListener('keydown', (e) => {
    if ((e.key === ' ' || e.key === 'Enter') && !e.repeat) { e.preventDefault(); start(); }
  });
  btn.addEventListener('keyup', (e) => {
    if (e.key === ' ' || e.key === 'Enter') { e.preventDefault(); stop(); }
  });
  // A keyboard user who tabs away mid-hold never sends the keyup.
  btn.addEventListener('blur', stop);
}

/**
 * Wire the always-visible typed form.
 *
 * index.html ships a real #utterance-form. It previously had NO submit listener
 * anywhere in the codebase — the only typed handler was built inside the speech
 * fallback, which itself never ran because of the ID bug above. So typing a
 * message and pressing Send did nothing.
 *
 * This is wired UNCONDITIONALLY at boot, not only when speech is unavailable.
 * Zero-typing is the goal, not a prohibition: someone who cannot speak out loud
 * because another person is in the room must always be able to reach the system.
 */
function initTypedInput() {
  const form = document.getElementById('utterance-form');
  if (!form) return;

  form.addEventListener('submit', (e) => {
    e.preventDefault();
    const input = document.getElementById('utterance');
    const text = (input?.value || '').trim();
    if (!text) return;
    input.value = '';
    sendUtterance(text);
  });
}

/** Tell the user speech is unavailable. The typed form is already present. */
function enableTypedFallback() {
  const label = document.getElementById('ptt-label');
  if (label) label.textContent = 'Voice unavailable — type below';
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
    const fallback =
      'Threshold could not reach the server. If you may be in immediate danger, ' +
      'call 911 now. You can also call or text 988 for crisis support.';
    addSystemNote(fallback);
    // Use the device directly on a network failure. Routing this through the
    // cloud voice path would retry the service that just failed before falling
    // back, adding silence at the worst possible moment.
    speakInBrowser(fallback, { loud: true });
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

  // The header badge tracks the most recent reply's provenance. It ships
  // hidden and nothing ever revealed it, so the "live" indicator the markup
  // provides was permanently invisible.
  if (who === 'threshold' && live !== undefined) {
    const badge = document.getElementById('generation-badge');
    if (badge) {
      badge.hidden = false;
      badge.textContent = live ? 'live' : 'offline fallback';
      badge.classList.toggle('unverified', live === false);
    }
  }
}

/** A short machine-voice note explaining what the system just did and why. */
function addSystemNote(text) {
  if (!text) return;
  const el = document.getElementById('system-notice');
  if (el) {
    el.textContent = text;
    el.hidden = false;
  }
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
  const badge = gen.deterministic
    ? ' <span class="badge">verified local</span>'
    : (gen.live ? '' : ' <span class="unverified">offline fallback</span>');
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
  // #harm-panel, not #script-911 — the latter is not in the markup, so this
  // rendered into nothing and the script never appeared on screen.
  renderGeneration('harm-panel', gen);
  const host = document.getElementById('harm-panel');
  if (host) host.hidden = false;

  // Read it aloud one line at a time, pausing between: under acute stress a
  // paragraph is unusable, but a single line can be repeated to a dispatcher.
  // Captioned as well as spoken — a deaf user reading a dispatcher script needs
  // to know which line is being read now, not just that a wall of text exists.
  if (gen?.text) gen.text.split('\n').filter(Boolean).forEach((line, i) => {
    setTimeout(() => { captionSpeech(line); speak(line); }, i * 3500);
  });
  if (gen?.text) {
    addStatusLine(
      'Personalised 911 script displayed',
      '911_script_displayed',
    );
  }
}

async function loadTolerance() {
  const gen = await api('/api/tolerance');

  // The markup's tolerance surface is #tolerance-card / #tolerance-text /
  // #tolerance-badge. This wrote to #tolerance-msg, which does not exist, so
  // the Tolerance Guard — the product's lead prevention feature — never
  // appeared on the page at all.
  const card = document.getElementById('tolerance-card');
  const text = document.getElementById('tolerance-text');
  const badge = document.getElementById('tolerance-badge');
  if (!card || !text) return gen;

  if (!gen?.text) {
    // No message and no card, rather than an empty accented box implying
    // something was said. If there is an error worth showing, the system
    // notice carries it.
    if (gen?.error) addSystemNote(gen.error);
    return gen;
  }

  text.textContent = gen.text;
  if (badge) {
    // Contract rule 2: a fallback is labelled a fallback, always.
    badge.textContent = gen.live ? 'live' : 'offline fallback';
    badge.classList.toggle('unverified', gen.live === false);
  }
  card.hidden = false;
  return gen;
}

/* -------------------------------------------------------------------------- */
/* Grounding (Tier 2)                                                          */
/* -------------------------------------------------------------------------- */

/*
  5-4-3-2-1 sensory grounding. This is a standard, authored clinical exercise,
  NOT model output — same reasoning as the Good Samaritan dataset: the value is
  that the wording is fixed and correct, and there is nothing here a language
  model could improve. It is therefore not labelled as a generation, because it
  is not one.

  It runs entirely client-side so it keeps working with the network down, which
  is exactly the moment a craving does not pause to wait for a server.
*/
const GROUNDING_STEPS = [
  'Look around and name five things you can see.',
  'Name four things you can feel. The floor, your sleeve, the air.',
  'Name three things you can hear.',
  'Name two things you can smell.',
  'Name one thing you can taste.',
  'That is the whole exercise. The urge has already started coming down.',
];

/** Timers for the grounding sequence, so a second press cannot double-run it. */
let groundingTimers = [];

/**
 * Run the grounding sequence in the support panel.
 *
 * One step at a time with a visible position count, spoken and captioned
 * together. Re-pressing the button restarts it cleanly rather than layering a
 * second sequence over the first.
 */
function startGrounding() {
  groundingTimers.forEach(clearTimeout);
  groundingTimers = [];
  window.speechSynthesis?.cancel();

  const host = document.getElementById('support-panel');
  if (!host) return;
  host.hidden = false;

  const render = (i) => {
    host.innerHTML = `
      <p class="label">Grounding · step ${i + 1} of ${GROUNDING_STEPS.length}</p>
      <p class="lede">${escapeHtml(GROUNDING_STEPS[i])}</p>`;
    speak(GROUNDING_STEPS[i]);
  };

  render(0);
  recordReceipt('grounding_started', 'The grounding exercise started on the member device.');
  // 12 seconds a step: long enough to actually look around and name five
  // things, which is the entire mechanism. Rushing it makes it decorative.
  GROUNDING_STEPS.slice(1).forEach((_, n) => {
    groundingTimers.push(setTimeout(() => render(n + 1), (n + 1) * 12000));
  });
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
  if (res.clip.audio_path) {
    new Audio(res.clip.audio_path).play()
      .then(() => recordReceipt(
        'vault_clip_played',
        'A consented Memory Vault recording started playing.',
      ))
      .catch(() => {});
  }
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

  // ---- Tier 3 "If you are using" row ------------------------------------
  // All three render into #harm-panel, the one card that actually exists in
  // that section. The previous code targeted #samaritan-panel and #script-911,
  // neither of which is in the markup, so renderGeneration() found no element
  // and returned silently: the buttons appeared to do nothing at all.
  document.getElementById('show-samaritan')?.addEventListener('click', loadSamaritan);

  // "The words to say to 911" had no handler whatsoever. It is the single most
  // important control at Tier 3 — the script is the whole point of PRD §6.1 —
  // and pressing it did nothing.
  document.getElementById('show-911')?.addEventListener('click', async () => {
    const host = document.getElementById('harm-panel');
    if (host) {
      host.hidden = false;
      host.innerHTML = '<p class="prose">Writing your script…</p>';
    }
    await loadScript911();
  });

  document.getElementById('arm-bystander')?.addEventListener('click', openBystander);

  // ---- Tier 2/3 "Right now" row -----------------------------------------
  document.getElementById('play-vault')?.addEventListener('click', () => loadVaultClip(''));

  document.getElementById('show-refusal')?.addEventListener('click', async () => {
    const host = document.getElementById('support-panel');
    if (host) {
      host.hidden = false;
      host.innerHTML = '<p class="prose">Finding your words…</p>';
    }
    // #refusal-panel does not exist in index.html either. Same silent failure.
    renderGeneration('support-panel', await api('/api/script/refusal'));
  });

  // "Ground me" had no handler. The deterministic engine already emits a
  // start_grounding action at Tier 2 (app/triage.py), so the button was
  // promising something the backend had genuinely decided to offer.
  document.getElementById('start-grounding')?.addEventListener('click', startGrounding);

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
  // #harm-panel: the section this button lives in. #samaritan-panel was never
  // in the markup, so this returned at the null check and the button was dead.
  const host = document.getElementById('harm-panel');
  if (!host) return;
  host.hidden = false;
  host.innerHTML = '<p class="prose">Checking your state\'s law…</p>';

  let rec;
  try {
    rec = await api(`/api/legal/${state}`);
  } catch {
    // Never leave a legal question showing a spinner. The reassurance below is
    // reviewed copy that holds in every state with a Good Samaritan law.
    host.innerHTML =
      '<p class="prose"><span class="unverified">could not load your state\'s statute</span> ' +
      'Calling 911 for an overdose is still the right thing to do.</p>';
    return;
  }

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
  recordReceipt(
    'good_samaritan_displayed',
    'The reviewed state-specific Good Samaritan summary was displayed.',
  );
}

/* -------------------------------------------------------------------------- */
/* Boot                                                                        */
/* -------------------------------------------------------------------------- */

async function boot() {
  initControls();
  initVoice();
  initTypedInput();
  initFocusTrap();
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

  renderStats(state.profile, state.events);
  renderLog(state.events);

  // Tolerance Guard fires unprompted on a day when nothing is wrong. This is the
  // product's argument in a single interaction, so it runs on load rather than
  // waiting to be asked (PRD §5.1).
  const gen = await loadTolerance();
  if (gen?.window_active && gen.text) speak(gen.text);
}

function renderStats(profile, events) {
  if (!profile) return;
  setText('stat-naloxone', profile.naloxone_on_hand ? 'Within reach' : 'Not on hand');
  setText('stat-contacts', String(profile.contacts?.length ?? 0));

  const ev = profile.tolerance_events?.[0];
  if (ev) {
    const days = Math.floor((Date.now() - new Date(ev.date)) / 86400000);
    setText('stat-tolerance', `Day ${days}`);
  }

  // #stat-checkin was in the markup showing a permanent em-dash because nothing
  // ever wrote to it. The last event IS the last check-in — there is no
  // separate record — so it is derived rather than invented.
  const last = events?.length ? events[events.length - 1] : null;
  setText('stat-checkin', last ? new Date(last.at).toLocaleTimeString() : 'No check-ins yet');
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
