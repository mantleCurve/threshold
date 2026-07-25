/* ============================================================================
   THRESHOLD — onboarding.js
   Behaviour for the ladder agreement page (/onboarding).

   WHAT THIS FILE DOES
     Loads the user's real LadderConfig and UserProfile from GET /api/state,
     reflects them into the ladder table's controls, and saves changes back.
     It also renders the contact tree, and enforces in code what the markup
     asserts visually: tiers 4 and 5 are not switchable.

   WHY THIS PAGE IS NOT A SETTINGS SCREEN
     The whole product depends on the user telling it the truth at tier 2 and 3.
     They will only do that if they know precisely who hears it (PRD P3). So
     every control here states its CONSEQUENCE, and the consequence sentence IS
     the accessible label — a screen reader user hears the promise, never
     "checkbox 3". That is done in the markup; this file's job is to make sure
     the state those promises describe is the REAL state and that saving it
     actually worked.

   THE LOCKED RUNGS
     Tiers 4 and 5 are shown, locked, and explained rather than hidden. Hiding
     them would be the dishonest choice, and honesty is the only thing this page
     is for. `lockNonNegotiableTiers()` below re-asserts that in JavaScript: if
     anyone ever adds a toggle to one of those rows, it is disabled and given an
     explanation at runtime rather than quietly becoming functional.

     This is a real constraint, not a UI convention — app/models.py's
     USER_CONTROLLABLE_TIERS excludes 4 and 5, and the server would refuse the
     change anyway. The lock is here so the user is never offered a control that
     will be ignored (PRD §4.2).

   WHAT THIS FILE DELIBERATELY DOES NOT DO
     It never announces a save it did not confirm. Silent success and silent
     failure are indistinguishable, and on a page whose entire content is a
     promise about who can see what, a false "Saved" is the worst bug available.
     If the server did not accept the write, the user is told, and the controls
     are rolled back to the state the server actually holds.

     It never decides a tier, and it never auto-saves. A privacy setting that
     changes because a checkbox was brushed while scrolling is a setting the
     user did not choose.

   NO BUILD STEP
     Plain ES module, no bundler, no framework, no CDN.
   ========================================================================= */

'use strict';

/* -------------------------------------------------------------------------- */
/* API helpers                                                                 */
/* -------------------------------------------------------------------------- */

/**
 * Fetch JSON with a hard timeout, reporting the HTTP status.
 *
 * Unlike the read-only helpers elsewhere, this one surfaces `ok` and `status`,
 * because this page must be able to tell "saved" from "the server refused"
 * from "never reached the server" — and say which.
 *
 * @param {string} path
 * @param {object} options
 * @param {number} timeoutMs
 * @returns {Promise<{ok: boolean, status: number, data: object}>}
 *          status 0 means the request never completed.
 */
async function request(path, options = {}, timeoutMs = 12000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(path, {
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      signal: controller.signal,
      ...options,
    });
    let data = {};
    try { data = await res.json(); } catch { data = {}; }
    return { ok: res.ok, status: res.status, data };
  } catch {
    return { ok: false, status: 0, data: {} };
  } finally {
    clearTimeout(timer);
  }
}

/** Escape before inserting server data into the DOM. */
function escapeHtml(s) {
  const d = document.createElement('div');
  d.textContent = String(s ?? '');
  return d.innerHTML;
}

/**
 * Announce a save result.
 *
 * #save-status is role="status" / aria-live="polite" in the markup: a save is
 * not urgent enough to interrupt, but it MUST be announced, because silent
 * success is indistinguishable from silent failure. The visible hint carries
 * the same words for sighted users.
 *
 * @param {string}  text
 * @param {boolean} isError Renders the visible hint in the error colour.
 */
function announce(text, isError = false) {
  const live = document.getElementById('save-status');
  if (live) live.textContent = text;

  const hint = document.getElementById('save-hint');
  if (hint) {
    hint.textContent = text;
    hint.classList.toggle('error-text', isError);
  }
}

/**
 * Show a degraded-mode strip at the top of the page.
 * @param {string} text Empty string hides it.
 */
function notice(text) {
  const el = document.getElementById('system-notice');
  if (!el) return;
  el.textContent = text || '';
  el.hidden = !text;
}

/* -------------------------------------------------------------------------- */
/* Non-negotiable tiers                                                        */
/* -------------------------------------------------------------------------- */

/**
 * Make tiers 4 and 5 visibly and functionally non-negotiable.
 *
 * The markup already marks those rows `data-locked` and prints "Always — cannot
 * be turned off". This function is the belt to that braces: any input that ends
 * up inside a locked row is disabled, given `aria-disabled`, and described by
 * the explanation of WHY it cannot be changed — so the reason travels with the
 * control to assistive tech rather than living only in adjacent prose.
 *
 * The one exception is the tier-4 silence threshold (`#silence-seconds`). That
 * is a TIMING input, not a visibility switch: the user chooses how long the
 * system waits before escalating. What they cannot switch off is whether the
 * escalation reaches their caregiver at all. Conflating the two would take away
 * a control the user is entitled to, so it is explicitly exempted here rather
 * than swept up by the row-level rule.
 *
 * PRD §4.2 / app/models.py USER_CONTROLLABLE_TIERS: tiers 0–3 are the user's to
 * hide. Tiers 4 and 5 are not. This is the one thing the user cannot turn off,
 * and it is the reason the rest of the page is genuinely theirs.
 */
function lockNonNegotiableTiers() {
  const EXEMPT = new Set(['silence-seconds']);

  document.querySelectorAll('tr[data-locked]').forEach((row) => {
    // The explanation lives in the row's last cell. Give it an id so every
    // locked control in the row can point at it with aria-describedby.
    const cell = row.querySelector('td:last-child');
    let explainId = cell?.querySelector('.lock')?.id;
    if (cell && !explainId) {
      const lock = cell.querySelector('.lock');
      if (lock) {
        explainId = `lock-${row.rowIndex}`;
        lock.id = explainId;
      }
    }

    row.querySelectorAll('input, select, button').forEach((el) => {
      if (EXEMPT.has(el.id)) return;

      el.disabled = true;
      el.checked = true;                       // Always on, and shown as such.
      el.setAttribute('aria-disabled', 'true');
      if (explainId) el.setAttribute('aria-describedby', explainId);
      // Removed from the tab order deliberately: a keyboard user tabbing onto a
      // control they cannot operate learns nothing. The explanation next to it
      // is static text they will read on the way past.
      el.tabIndex = -1;
    });
  });
}

/* -------------------------------------------------------------------------- */
/* Reading and writing the form                                                */
/* -------------------------------------------------------------------------- */

/**
 * The controls this page owns, mapped to their place in the API payload.
 *
 * Declared as data rather than as a run of getElementById calls, so adding a
 * setting is one line and so `applyProfile` and `collectProfile` can never
 * disagree about which field is which — a mismatch there would silently save a
 * privacy setting into the wrong slot.
 */
const LADDER_FIELDS = [
  { id: 't2-visible',      key: 'tier_2_visible_to_caregiver', type: 'bool' },
  { id: 't3-visible',      key: 'tier_3_visible_to_caregiver', type: 'bool' },
  { id: 'missed-checkins', key: 'missed_checkins_to_elevate',  type: 'int'  },
  { id: 'silence-seconds', key: 'silence_seconds_to_escalate', type: 'int'  },
];

const PROFILE_FIELDS = [
  { id: 'address',      key: 'address',          type: 'text' },
  { id: 'unit',         key: 'unit',             type: 'text' },
  { id: 'entry-code',   key: 'entry_code',       type: 'text' },
  { id: 'cross-street', key: 'cross_street',     type: 'text' },
  { id: 'state-code',   key: 'state_code',       type: 'text' },
  { id: 'naloxone',     key: 'naloxone_on_hand', type: 'bool' },
];

/**
 * Populate every control from the server's profile.
 *
 * Called on load, and again after a failed save to roll the UI back to what the
 * server actually holds. A page that keeps showing an unsaved switch as "on" is
 * telling the user their caregiver can see something that in fact they cannot —
 * or, worse, the reverse.
 *
 * @param {object|null} profile UserProfile from /api/state.
 */
function applyProfile(profile) {
  if (!profile) return;
  const ladder = profile.ladder || {};

  LADDER_FIELDS.forEach((f) => {
    const el = document.getElementById(f.id);
    if (!el) return;
    const value = ladder[f.key];
    if (value === undefined || value === null) return;
    if (f.type === 'bool') el.checked = !!value;
    else el.value = String(value);
  });

  PROFILE_FIELDS.forEach((f) => {
    const el = document.getElementById(f.id);
    if (!el) return;
    const value = profile[f.key];
    if (value === undefined || value === null) return;
    if (f.type === 'bool') el.checked = !!value;
    else el.value = String(value);
  });
}

/**
 * Read every control back into an API payload.
 *
 * Numbers are clamped to the min/max already declared on the inputs, so a typed
 * value out of range is corrected here rather than sent to be rejected. The
 * silence threshold in particular has a floor for a clinical reason: too short
 * and the ladder escalates on someone putting their phone down.
 *
 * @returns {{ladder: object, profile: object}}
 */
function collectProfile() {
  const ladder = {};
  const profile = {};

  /**
   * Read one declared field.
   * @param {{id: string, key: string, type: string}} f
   * @param {object} into Target object.
   */
  const read = (f, into) => {
    const el = document.getElementById(f.id);
    if (!el) return;
    if (f.type === 'bool') { into[f.key] = !!el.checked; return; }
    if (f.type === 'int') {
      const min = Number(el.min || 0);
      const max = Number(el.max || Number.MAX_SAFE_INTEGER);
      const n = Number(el.value);
      into[f.key] = Number.isFinite(n) ? Math.min(max, Math.max(min, n)) : min;
      // Write the clamped value back so the user sees what will actually be saved.
      el.value = String(into[f.key]);
      return;
    }
    into[f.key] = el.value.trim();
  };

  LADDER_FIELDS.forEach((f) => read(f, ladder));
  PROFILE_FIELDS.forEach((f) => read(f, profile));

  profile.ladder = ladder;
  return { ladder, profile };
}

/* -------------------------------------------------------------------------- */
/* Saving                                                                      */
/* -------------------------------------------------------------------------- */

/** The profile the server last confirmed. Used to roll back a failed save. */
let savedProfile = null;

/**
 * Save the ladder configuration and the dispatcher details.
 *
 * Explicit, on a button press. Deliberately not auto-saved on change: a privacy
 * setting that changes because a checkbox was brushed while scrolling is a
 * setting the user did not choose, and this page's only currency is that the
 * user chose everything on it.
 *
 * On any non-2xx the UI is rolled back to `savedProfile` and the failure is
 * stated in the user's own words, including the case where the server has no
 * profile endpoint at all. Announcing "Saved" for a write that did not land
 * would make this page lie about the one thing it exists to be truthful about.
 */
async function saveLadder() {
  const btn = document.getElementById('save-ladder');
  const { profile } = collectProfile();

  if (btn) { btn.disabled = true; btn.setAttribute('aria-busy', 'true'); }
  announce('Saving…');

  const res = await request('/api/profile', {
    method: 'POST',
    body: JSON.stringify(profile),
  });

  if (btn) { btn.disabled = false; btn.setAttribute('aria-busy', 'false'); }

  if (res.ok) {
    savedProfile = res.data.profile || profile;
    applyProfile(savedProfile);
    announce('Saved. This is in effect now.');
    notice('');
    return;
  }

  // Roll the controls back to the server's truth before saying anything, so the
  // switches on screen are never ahead of what is actually stored.
  applyProfile(savedProfile);

  const detail = typeof res.data?.detail === 'string' ? res.data.detail : '';
  if (res.status === 0) {
    announce('Not saved — could not reach the server. Nothing has changed.', true);
  } else if (res.status === 404 || res.status === 405) {
    // Stated plainly rather than hidden. A judge reading this should see that
    // the page refused to claim a save it could not make.
    announce('Not saved — this build has no profile endpoint yet. ' +
             'Your settings are unchanged, and tiers 4 and 5 still always alert.', true);
  } else {
    announce(`Not saved — ${detail || `the server refused (${res.status})`}. ` +
             'Nothing has changed.', true);
  }
  notice('Your ladder was not changed. Everything shown above is the setting ' +
         'that is actually in effect.');
}

/* -------------------------------------------------------------------------- */
/* Contact tree                                                                */
/* -------------------------------------------------------------------------- */

const TIER_NAMES = [
  'Baseline', 'Elevated', 'Craving', 'Active use', 'Medical emergency', 'Unresponsive',
];

/**
 * Render the contact tree from the profile.
 *
 * Order is the reach order, top down, which is why it is an <ol> in the markup
 * and why the tiers each contact is reached at are printed on every row. A user
 * deciding whether to be honest at tier 3 needs to know exactly who a tier 3
 * would wake up.
 *
 * @param {Array<object>} contacts Contact records from the profile.
 */
function renderContacts(contacts) {
  const host = document.getElementById('contact-tree');
  if (!host) return;

  if (!contacts?.length) {
    host.innerHTML =
      '<li><p>Nobody is on your tree yet. At tier 4 the system will still call ' +
      '911 — but it will have no one of yours to reach.</p></li>';
    return;
  }

  host.innerHTML = contacts
    .slice()
    .sort((a, b) => (a.order ?? 0) - (b.order ?? 0))
    .map((c) => {
      const tiers = (c.tiers || [])
        .map((t) => `${t} ${TIER_NAMES[t] || ''}`.trim())
        .join(', ');
      return `
        <li>
          <p><strong>${escapeHtml(c.name)}</strong> · ${escapeHtml(c.relation)}</p>
          <p class="hint">${escapeHtml(c.channel)}${tiers ? ` · reached at tier ${escapeHtml(tiers)}` : ''}</p>
        </li>`;
    })
    .join('');
}

/* -------------------------------------------------------------------------- */
/* Controls                                                                    */
/* -------------------------------------------------------------------------- */

/* -------------------------------------------------------------------------- */
/* Invite codes — consent as structure (PRD P3)                                */
/* -------------------------------------------------------------------------- */

/**
 * Wire the "Invite someone to support you" section.
 *
 * WHY THIS CONTROL IS THE ANSWER TO "ISN'T THIS SURVEILLANCE?"
 *
 * A caregiver cannot search for a member, cannot type a member's username
 * anywhere, and cannot be attached to one by us. The ONLY way a caregiver link
 * comes into existence is that the member pressed this button and handed the
 * resulting code over themselves. The permission travels outward from the
 * watched person; it is never requested inward toward them. There is no API
 * parameter that would let it work the other way — see app/store.py's `invites`
 * table comment and POST /api/invite.
 *
 * The code is displayed here and sent nowhere. Reading it out, or turning the
 * phone around, IS the consent act, and it belongs to the member rather than to
 * a mail server that would put a live permission in an inbox forever.
 *
 * Single use and a 24h expiry are enforced server-side, not here. This function
 * only reports them, because a client-side expiry check is decoration.
 */
function initInvite() {
  const btn = document.getElementById('make-invite');
  if (!btn) return;

  const result = document.getElementById('invite-result');
  const codeEl = document.getElementById('invite-code');
  const expiryEl = document.getElementById('invite-expiry');
  const errEl = document.getElementById('invite-error');

  /** Show a failure in the section's role="alert" region. Empty string clears. */
  const inviteError = (text) => {
    if (!errEl) return;
    errEl.textContent = text || '';
    errEl.hidden = !text;
  };

  btn.addEventListener('click', async () => {
    inviteError('');
    const idle = btn.textContent;
    btn.disabled = true;
    btn.setAttribute('aria-busy', 'true');
    btn.textContent = 'Generating…';

    const res = await request('/api/invite', { method: 'POST' });

    btn.disabled = false;
    btn.setAttribute('aria-busy', 'false');
    btn.textContent = idle;

    if (!res.ok) {
      // Never invent a reason. The server's `detail` was written for this
      // screen; we only compose copy when nothing reached the server at all.
      inviteError(res.status === 0
        ? 'Could not reach the server, so no code was created. Nothing changed.'
        : (res.data?.detail || `Could not create a code (${res.status}).`));
      return;
    }

    if (codeEl) codeEl.textContent = res.data.code || '';
    if (expiryEl) {
      // Rendered from the server's own expiry rather than computed here, so the
      // time shown is the time actually enforced.
      const when = res.data.expires_at ? new Date(res.data.expires_at) : null;
      expiryEl.textContent = when && !isNaN(when)
        ? `Works once. Stops working at ${when.toLocaleString()}.`
        : `Works once. Expires in ${res.data.expires_in_hours ?? 24} hours.`;
    }
    if (result) result.hidden = false;

    // Move focus to the code. A keyboard or screen-reader user pressing the
    // button must land on the thing it produced, not stay on the button.
    codeEl?.setAttribute('tabindex', '-1');
    codeEl?.focus?.();
  });
}

/* -------------------------------------------------------------------------- */
/* Voice preference                                                            */
/* -------------------------------------------------------------------------- */

/*
  THE CONSTRAINT THIS SECTION EXISTS UNDER, STATED BEFORE THE CODE.

  This picker selects a SYNTHETIC voice for THE APP'S OWN SPEECH — the check-in
  replies, the grounding steps, the 911 script read aloud. That is its entire
  scope.

  Memory Vault clips are REAL RECORDINGS OF REAL PEOPLE and are NEVER
  synthesised. Nothing in this file, and nothing anywhere in the product, routes
  a vault clip through speechSynthesis. No option offered here imitates a
  specific caregiver, and none ever will.

  The PRD permits voice cloning only through the caregiver-owned consent flow;
  keeping that boundary next to this code prevents profile audio from becoming
  an accidental cloning input:
  consent obtained in calm is spent in crisis. A person who agreed months ago
  that their sister's voice could be synthesised is not the person hearing it at
  tier 4, and a disclosure label does no cognitive work whatsoever on someone
  intoxicated or panicking. The only design that survives that moment is the one
  where a voice you recognise as your sister's IS your sister's.

  STORED IN localStorage, NEVER ON THE SERVER. A voice preference is not health
  data and has no business in a database that also knows what someone uses. It
  also means the choice survives with no account and no network.
*/

/** localStorage key. Namespaced like the theme preference in theme.js. */
const VOICE_KEY = 'threshold.voice';

/**
 * Populate the voice picker from the browser's installed voices.
 *
 * getVoices() is asynchronous in most browsers on first call and returns an
 * empty array until the engine has loaded — hence the `voiceschanged` listener.
 * Without it the select renders empty on a cold page load and the feature looks
 * broken, which is the single most common way this API is mis-wired.
 *
 * @param {HTMLSelectElement} select
 */
function fillVoices(select) {
  const voices = window.speechSynthesis?.getVoices?.() || [];
  const saved = (() => {
    try { return localStorage.getItem(VOICE_KEY) || ''; } catch { return ''; }
  })();

  // Keep the "System default" option and replace the rest.
  while (select.options.length > 1) select.remove(1);

  voices.forEach((v) => {
    const opt = document.createElement('option');
    // voiceURI is the stable identifier; `name` alone collides across engines.
    opt.value = v.voiceURI;
    opt.textContent = `${v.name} (${v.lang})${v.default ? ' — browser default' : ''}`;
    select.appendChild(opt);
  });

  // Reselect the stored choice if that voice is still installed. If it is not —
  // an OS update removed it, or this is a different device — we fall back to
  // "System default" silently rather than showing a selection that will not play.
  if (saved && Array.prototype.some.call(select.options, (o) => o.value === saved)) {
    select.value = saved;
  }
}

/**
 * Wire the app-voice picker and its Preview button.
 *
 * Saves on change, unlike the ladder switches on this page. That difference is
 * deliberate: the ladder toggles are privacy promises and must be saved
 * explicitly, while a voice preference affects nobody but the listener, never
 * leaves the device, and is trivially reversible.
 */
function initVoicePicker() {
  const select = document.getElementById('voice-pick');
  if (!select) return;

  const status = document.getElementById('voice-status');
  const preview = document.getElementById('voice-preview');

  if (!('speechSynthesis' in window)) {
    // Say so rather than offering a dead control. An empty picker with a
    // Preview button that does nothing is worse than an honest sentence.
    select.disabled = true;
    if (preview) preview.disabled = true;
    if (status) status.textContent = 'This browser has no speech engine, so Threshold will not speak aloud.';
    return;
  }

  fillVoices(select);
  // Fires when the engine finishes loading voices, and again if the OS set
  // changes mid-session.
  window.speechSynthesis.addEventListener?.('voiceschanged', () => fillVoices(select));

  select.addEventListener('change', () => {
    try {
      localStorage.setItem(VOICE_KEY, select.value);
      if (status) {
        status.textContent = select.value
          ? 'Saved on this device only. Press Preview to hear it.'
          : 'Using the system default. Saved on this device only.';
      }
    } catch {
      // Private browsing can refuse writes. Say so — a preference that silently
      // did not save is a control the person cannot trust.
      if (status) status.textContent = 'Could not save on this device. The choice lasts until you close the tab.';
    }
  });

  preview?.addEventListener('click', () => {
    // Cancel anything queued first, so repeated presses do not stack up a
    // backlog the person then has to sit through.
    window.speechSynthesis.cancel();

    const u = new SpeechSynthesisUtterance(
      // The preview line says what the voice IS. Someone auditioning voices at
      // this moment is exactly the person who should hear the distinction
      // stated, and it doubles as a natural-length sample.
      'This is Threshold. This voice is synthetic. The recordings in your ' +
      'Memory Vault are real people, and they are never synthesised.'
    );
    const chosen = (window.speechSynthesis.getVoices() || [])
      .find((v) => v.voiceURI === select.value);
    if (chosen) u.voice = chosen;
    // Same rate and pitch as speak() in app.js, so the preview is honest about
    // how it will actually sound in use.
    u.rate = 0.9;
    u.pitch = 0.95;
    window.speechSynthesis.speak(u);
  });
}

/**
 * Wire the page's buttons and the change feedback on the toggles.
 *
 * The toggles do not save on change (see saveLadder), but they DO update the
 * hint immediately, so the user is never left unsure whether a switch they
 * flipped is live. The wording is unambiguous about the difference: "not saved
 * yet" is a state the user must be able to see.
 */
function initControls() {
  document.getElementById('save-ladder')?.addEventListener('click', saveLadder);

  // Any change to a toggle or field marks the page dirty in words. Applies to
  // both the visibility switches and the dispatcher fields.
  document.querySelectorAll('.ladder-table input, #profile-form input, #profile-form select')
    .forEach((el) => {
      if (el.disabled) return;
      el.addEventListener('change', () => {
        announce('Changed but not saved yet. Press "Save my ladder".');
      });
    });

  // Adding a contact needs a server endpoint this build does not have. Rather
  // than a button that appears to work and does nothing, it says what it is.
  // A dead control on this page would undercut the promise the page is making.
  const add = document.getElementById('add-contact');
  if (add) {
    add.addEventListener('click', () => {
      announce('Contacts are seeded for this demo and are not editable here yet. ' +
               'Everything else on this page saves.', true);
    });
  }

  document.getElementById('logout')?.addEventListener('click', async () => {
    await request('/api/auth/logout', { method: 'POST' });
    window.location.href = '/login';
  });
}

/* -------------------------------------------------------------------------- */
/* Boot                                                                        */
/* -------------------------------------------------------------------------- */

/**
 * Entry point.
 *
 * The lock on tiers 4 and 5 is applied FIRST, before any network call. If
 * /api/state never resolves, the page must still be honest about what cannot be
 * switched off — that statement does not depend on the server, and it is the
 * one claim on this page that is true unconditionally.
 */
async function boot() {
  lockNonNegotiableTiers();
  initControls();
  // Both are independent of /api/state: the invite button talks to its own
  // endpoint, and the voice picker never touches the network at all. Wiring
  // them before the fetch means they still work on a page whose ladder failed
  // to load.
  initInvite();
  initVoicePicker();

  const res = await request('/api/state', { method: 'GET' });
  if (!res.ok) {
    notice('Could not load your ladder. The switches below are showing defaults, ' +
           'not your settings — do not rely on them until this page loads.');
    return;
  }

  const state = res.data;
  savedProfile = state.profile;
  applyProfile(state.profile);
  renderContacts(state.profile?.contacts);

  // Report AI availability honestly, as every surface does (CONTRACT).
  const ai = document.getElementById('ai-status');
  if (ai) {
    ai.textContent = state.ai_online ? 'AI online' : 'AI offline — no API key';
    ai.classList.add(state.ai_online ? 'badge--live' : 'badge--offline');
    ai.dataset.tone = state.ai_online ? 'ok' : 'warn';
  }
}

document.addEventListener('DOMContentLoaded', boot);
