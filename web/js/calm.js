/* ============================================================================
   THRESHOLD — calm.js
   Voice-guided grounding for craving, panic, and anxiety.

   WHAT THIS FILE DOES
     Runs three authored exercises — paced breathing, 5-4-3-2-1 sensory
     grounding, and an urge-surfing timer — spoken aloud in the member's chosen
     voice, with a single very large control and text that stays on screen.

   WHAT THIS FILE DELIBERATELY DOES NOT DO
     * The scripts are NOT model-generated. They are standard clinical
       exercises, authored and fixed. A person mid-panic attack is not who you
       want a language model improvising breathing instructions at, and a
       generation that stalls or drifts mid-exercise is worse than no exercise.
       The model's job in this product is language work around the edges; this
       is not one of those places (PRD P4).
     * It never claims a human is present. The voice says "I'll count with you",
       never "I'm here with you" — the first is true of software, the second
       implies presence we do not have (PRD P5).
     * It does not decide or change a tier. Grounding is something the member
       chooses to do; finishing it is not evidence they are safe, and the ladder
       is not moved from here.

   WHY BIG BUTTONS AND WHY VOICE
     Panic narrows attention and degrades reading. Someone mid-attack often
     cannot parse a paragraph but can follow a voice and press one large thing.
     Every control here is at least 88px tall — double the 44px minimum — and
     the running exercise needs no further input at all. It runs to completion
     on its own, and one press stops it.

   ACCESSIBILITY
     Every spoken line is also written to a live region, so a deaf or
     hard-of-hearing member gets identical guidance. The breathing pacer is
     animated, counted in text, AND spoken — three channels, because relying on
     motion alone fails anyone with reduced-motion set.
   ========================================================================= */

'use strict';

/* -------------------------------------------------------------------------- */
/* The exercises                                                               */
/* -------------------------------------------------------------------------- */

/**
 * Box breathing, 4-4-4-4.
 *
 * Chosen over the more common 4-7-8 deliberately: the long 7-count hold is
 * difficult for someone already short of breath or hyperventilating, and
 * failing at a breathing exercise during a panic attack makes the panic worse.
 * Equal counts are forgiving.
 *
 * Not offered at Tier 4+. If someone may be overdosing, respiratory depression
 * is the thing that kills them and coaching them to hold their breath is
 * actively dangerous — see guardTier() below.
 */
const BREATHING = {
  id: 'breathing',
  title: 'Breathe with me',
  intro: "I'll count with you. Follow the circle. Nothing else to do.",
  cycles: 4,
  phases: [
    { say: 'Breathe in',   seconds: 4, scale: 1.0 },
    { say: 'Hold',         seconds: 4, scale: 1.0 },
    { say: 'Breathe out',  seconds: 4, scale: 0.55 },
    { say: 'Hold',         seconds: 4, scale: 0.55 },
  ],
  outro: 'That was four rounds. You can do that again any time.',
};

/**
 * 5-4-3-2-1 sensory grounding — the standard exercise for acute anxiety and
 * dissociation. It works by forcing attention outward onto the room.
 *
 * Generous pauses: rushing someone through this defeats the mechanism. The
 * whole point is the slow, deliberate looking.
 */
const SENSES = {
  id: 'senses',
  title: 'Come back to the room',
  intro: "We'll find things around you. Take your time. There's no wrong answer.",
  steps: [
    { say: 'Name five things you can see.',                seconds: 20 },
    { say: 'Now four things you can feel. The chair, the floor, your own hands.', seconds: 18 },
    { say: 'Three things you can hear.',                   seconds: 16 },
    { say: 'Two things you can smell.',                    seconds: 14 },
    { say: 'One thing you can taste.',                     seconds: 12 },
  ],
  outro: "You're here. That's all that exercise was for.",
};

/**
 * Urge surfing — for craving specifically rather than panic.
 *
 * The clinical premise, and the reason the copy says what it says: a craving
 * is a wave, not a straight line. It peaks and it comes down whether or not
 * you act on it. The exercise is simply to outlast the peak, which is usually
 * minutes rather than hours.
 *
 * Note the framing: it never says "don't use". It says the feeling will pass.
 * Instruction produces resistance; description does not (PRD P6, harm
 * reduction over abstinence enforcement).
 */
const URGE = {
  id: 'urge',
  title: 'Ride it out',
  intro:
    'A craving is a wave. It rises, it peaks, and it comes down on its own. ' +
    "We're going to sit with it for a few minutes and let it do that.",
  steps: [
    { say: 'Where do you feel it in your body? Just notice it. Don’t argue with it.', seconds: 30 },
    { say: 'Is it getting stronger, or is it holding steady?', seconds: 30 },
    { say: 'It has a shape and it has an edge. You are watching it, not obeying it.', seconds: 30 },
    { say: 'Still here. Still breathing. Notice if it has shifted at all.', seconds: 30 },
  ],
  outro:
    'That was two minutes. If it is still strong, we can go again, or you can ' +
    'call someone. Both are good choices.',
};

const EXERCISES = { breathing: BREATHING, senses: SENSES, urge: URGE };

/* -------------------------------------------------------------------------- */
/* Runtime                                                                     */
/* -------------------------------------------------------------------------- */

let timers = [];
let running = null;

/** Cancel everything. Called on stop, on tier change, and before a new start. */
function stopAll() {
  timers.forEach(clearTimeout);
  timers = [];
  running = null;
  window.speechSynthesis?.cancel();
  document.querySelectorAll('audio[data-calm]').forEach((a) => {
    a.pause();
    a.remove();
  });
  const stage = document.getElementById('calm-stage');
  if (stage) stage.dataset.state = 'idle';
  setPacer(1, '');
  paint('');
  const btn = document.getElementById('calm-stop');
  if (btn) btn.hidden = true;
}

/**
 * Refuse to run breathing exercises during a suspected overdose.
 *
 * THIS IS A SAFETY GUARD, NOT A UX RULE. Opioid overdose kills through
 * respiratory depression. Telling someone in that state to hold their breath
 * for four seconds is coaching them toward the thing that is already killing
 * them, and a well-meaning bystander could easily start this exercise on a
 * person who is going quiet. At Tier 4+ the only correct instruction is to
 * call 112 and give naloxone.
 */
function guardTier(exerciseId) {
  const tier = Number(document.body.dataset.tier || 0);
  if (tier >= 4 && exerciseId === 'breathing') {
    paint(
      'Not now. If someone may be overdosing, call 112 and give naloxone. ' +
      'Do not coach breath-holding.'
    );
    return false;
  }
  return true;
}

/* -------------------------------------------------------------------------- */
/* Output                                                                      */
/* -------------------------------------------------------------------------- */

/** Write a line to the on-screen caption. Always paired with speech. */
function paint(text) {
  const el = document.getElementById('calm-text');
  if (el) el.textContent = text;
}

/** Drive the breathing circle. Scale is the only animated property. */
function setPacer(scale, label) {
  const pacer = document.getElementById('calm-pacer');
  if (pacer) pacer.style.setProperty('--calm-scale', String(scale));
  const cue = document.getElementById('calm-cue');
  if (cue) cue.textContent = label;
}

/**
 * Speak a line in the member's chosen voice, and caption it.
 *
 * Routes through the shared speakInChosenVoice() from app.js when available so
 * a selected cloud or supporter voice is used, and falls back to the browser's
 * own synthesis otherwise. Falling back to a robotic voice is always correct;
 * going silent partway through a panic exercise is not.
 */
function guide(text, { seconds } = {}) {
  paint(text);
  if (typeof window.speakInChosenVoice === 'function') {
    window.speakInChosenVoice(text);
  } else if ('speechSynthesis' in window) {
    const u = new SpeechSynthesisUtterance(text);
    // Slower and lower than default. This voice is talking to someone whose
    // heart rate we are trying to bring down; briskness is counterproductive.
    u.rate = 0.82;
    u.pitch = 0.95;
    window.speechSynthesis.speak(u);
  }
  return seconds;
}

/* -------------------------------------------------------------------------- */
/* Sequencing                                                                  */
/* -------------------------------------------------------------------------- */

/** Run the breathing pacer for N cycles, then finish. */
function runBreathing(ex) {
  let t = 0;
  guide(ex.intro);
  t += 4;

  for (let cycle = 0; cycle < ex.cycles; cycle++) {
    ex.phases.forEach((phase) => {
      timers.push(setTimeout(() => {
        setPacer(phase.scale, phase.say);
        guide(phase.say);
        // Count the seconds down in text as well as motion, so the pacing is
        // legible with prefers-reduced-motion set or animations unsupported.
        for (let s = phase.seconds; s > 0; s--) {
          timers.push(setTimeout(() => {
            const cue = document.getElementById('calm-cue');
            if (cue) cue.textContent = `${phase.say} · ${s}`;
          }, (phase.seconds - s) * 1000));
        }
      }, t * 1000));
      t += phase.seconds;
    });
  }

  timers.push(setTimeout(() => {
    setPacer(1, '');
    guide(ex.outro);
    finish();
  }, t * 1000));
}

/** Run a step-list exercise (senses, urge surfing). */
function runSteps(ex) {
  let t = 0;
  guide(ex.intro);
  t += 5;

  ex.steps.forEach((step) => {
    timers.push(setTimeout(() => guide(step.say), t * 1000));
    t += step.seconds;
  });

  timers.push(setTimeout(() => {
    guide(ex.outro);
    finish();
  }, t * 1000));
}

/** Common tail: offer the next step without pushing it. */
function finish() {
  timers.push(setTimeout(() => {
    const stage = document.getElementById('calm-stage');
    if (stage) stage.dataset.state = 'done';
    const btn = document.getElementById('calm-stop');
    if (btn) btn.hidden = true;
    running = null;
  }, 6000));
}

/**
 * Start an exercise.
 *
 * Exposed on window so the ladder can offer it contextually at Tier 2 without
 * this module having to know about triage.
 */
function startCalm(id) {
  const ex = EXERCISES[id];
  if (!ex) return;
  if (!guardTier(id)) return;

  stopAll();
  running = id;

  const stage = document.getElementById('calm-stage');
  if (stage) {
    stage.hidden = false;
    stage.dataset.state = 'running';
    stage.dataset.exercise = id;
  }
  const title = document.getElementById('calm-title');
  if (title) title.textContent = ex.title;

  const stop = document.getElementById('calm-stop');
  if (stop) stop.hidden = false;

  // Move focus to the stop control. It is the only thing anyone needs to reach
  // once an exercise is running, and a keyboard user should not have to hunt.
  stop?.focus?.();

  if (id === 'breathing') runBreathing(ex);
  else runSteps(ex);
}

window.startCalm = startCalm;
window.stopCalm = stopAll;

/* -------------------------------------------------------------------------- */
/* Wiring                                                                      */
/* -------------------------------------------------------------------------- */

function initCalm() {
  document.querySelectorAll('[data-calm-start]').forEach((btn) => {
    btn.addEventListener('click', () => startCalm(btn.dataset.calmStart));
  });
  document.getElementById('calm-stop')?.addEventListener('click', stopAll);

  // Stop if the ladder escalates underneath a running exercise. Continuing to
  // coach breathing while the screen turns into an emergency takeover would be
  // both confusing and, at Tier 4, unsafe.
  const body = document.body;
  new MutationObserver(() => {
    if (running && Number(body.dataset.tier || 0) >= 4) stopAll();
  }).observe(body, { attributes: true, attributeFilter: ['data-tier'] });
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initCalm);
} else {
  initCalm();
}
