/* ============================================================================
   THRESHOLD — auth.js
   Behaviour for /login and /register.

   WHAT THIS FILE DOES
     Submits login.html#login-form and register.html#register-form to the auth
     API in CONTRACT.md, surfaces the SERVER'S OWN error text inline, and
     redirects to the surface that matches the account's role on success. It
     also wires the one-tap "Sign in as Sam" / "Sign in as Sarah" buttons that
     CONTRACT ground rule 4 requires.

     Both pages share this file because the two forms are the same interaction
     with a different endpoint. One file, two `if (form) …` guards — a second
     module would be two places to get session handling subtly different in.

   WHAT THIS FILE DELIBERATELY DOES NOT DO
     It does not validate credentials locally beyond the browser's own required
     attributes. A client-side "that password looks wrong" is a guess; the
     server is the only thing that actually knows, and inventing a rejection
     the server would not have made is how an evaluator gets locked out of a
     product whose entire point is that it never locks anyone out.

     It never invents an error message either. If the request reached the
     server, the user sees the server's `detail` string verbatim. The generic
     copy below fires ONLY when the network itself failed.

     It stores nothing. The session is an HttpOnly cookie set by the server, so
     there is no token in localStorage for a script on this page to leak.

   NO BUILD STEP
     Plain ES module, no bundler, no framework, no CDN.
   ========================================================================= */

'use strict';

/* -------------------------------------------------------------------------- */
/* API helpers                                                                 */
/* -------------------------------------------------------------------------- */

/**
 * POST JSON and report the outcome without throwing on an HTTP error status.
 *
 * A rejected sign-in is a 401 with a body worth reading, not an exception. This
 * returns the status alongside the parsed body so the caller can tell three
 * genuinely different situations apart: accepted, refused-with-a-reason, and
 * never-reached-the-server.
 *
 * @param {string} path      API path, e.g. '/api/auth/login'.
 * @param {object} body      Payload, serialised as JSON.
 * @param {number} timeoutMs Hard ceiling. A hung request is worse than a fast
 *                           failure, because the user sits on a dead button.
 * @returns {Promise<{ok: boolean, status: number, data: object}>}
 *          `status` is 0 when the request never completed (offline, DNS, abort).
 */
async function postJson(path, body, timeoutMs = 12000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      // Same-origin credentials so the Set-Cookie from the server is retained.
      credentials: 'same-origin',
      signal: controller.signal,
    });
    // A 500 can arrive as HTML. Parsing must not turn that into a thrown
    // exception that the caller would then report as "you are offline".
    let data = {};
    try { data = await res.json(); } catch { data = {}; }
    return { ok: res.ok, status: res.status, data };
  } catch {
    return { ok: false, status: 0, data: {} };
  } finally {
    clearTimeout(timer);
  }
}

/* -------------------------------------------------------------------------- */
/* Error display                                                               */
/* -------------------------------------------------------------------------- */

/**
 * Show a failure in the form's inline error element.
 *
 * The target is `role="alert"` in the markup, which is an assertive live
 * region: a failed sign-in must interrupt, because the user is blocked until
 * they hear it. Toggling `hidden` rather than emptying the node keeps the
 * region present in the accessibility tree between attempts.
 *
 * @param {string} id      Element id ('login-error' or 'register-error').
 * @param {string} message Text to display. Empty string clears and hides.
 */
function showError(id, message) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = message || '';
  el.hidden = !message;
}

/**
 * Turn a `postJson` result into the sentence the user should read.
 *
 * FastAPI's HTTPException serialises as `{detail: "…"}`, and those strings were
 * written for this screen ("Incorrect username or password.", "that username is
 * taken"), so they are shown verbatim. We only compose our own copy when the
 * server said nothing — status 0 means the request never landed.
 *
 * @param {{status: number, data: object}} res Result from `postJson`.
 * @returns {string} A sentence safe to place in the DOM as textContent.
 */
function errorMessage(res) {
  if (res.status === 0) {
    return 'Could not reach the server. Check your connection and try again — ' +
           'bystander mode below still works with no account.';
  }
  const detail = res.data?.detail;
  // FastAPI's 422 validation errors arrive as a list of objects rather than a
  // string. Rendering "[object Object]" at someone is worse than a plain line.
  if (typeof detail === 'string' && detail) return detail;
  if (Array.isArray(detail) && detail[0]?.msg) return String(detail[0].msg);
  return `Sign-in failed (${res.status}). Please try again.`;
}

/* -------------------------------------------------------------------------- */
/* Post-auth routing                                                           */
/* -------------------------------------------------------------------------- */

/**
 * Send a freshly authenticated user to the surface built for their role.
 *
 * A caregiver landing on the person-in-recovery surface would be looking at
 * someone else's ladder controls, and a person in recovery landing on the
 * caregiver alert page would be reading a briefing about themselves. The role
 * comes back in the login/register response, so no extra round trip is needed.
 *
 * @param {string} role 'caregiver' or anything else (treated as the user surface).
 */
function redirectForRole(role) {
  window.location.href = role === 'caregiver' ? '/caregiver' : '/';
}

/**
 * Put a submit button into its pending state.
 *
 * Disabling prevents a double submission creating two accounts from one impatient
 * double-click; the label change is the only progress indicator this screen needs.
 *
 * @param {HTMLButtonElement|null} btn      The submit button.
 * @param {boolean}                pending  True while the request is in flight.
 * @param {string}                 idleText Label to restore when done.
 */
function setPending(btn, pending, idleText) {
  if (!btn) return;
  btn.disabled = pending;
  btn.textContent = pending ? 'Working…' : idleText;
  // aria-busy so a screen reader user knows the delay is the system, not them.
  btn.setAttribute('aria-busy', String(pending));
}

/* -------------------------------------------------------------------------- */
/* Login                                                                       */
/* -------------------------------------------------------------------------- */

/**
 * Sign in with an explicit username and password.
 *
 * Shared by the form submit handler and the one-tap demo buttons, so there is
 * exactly one sign-in code path. Three routes into the product (typed, pre-filled
 * Enter, one tap) that all exercise the same request is three routes that cannot
 * drift apart and leave one of them broken for an evaluator.
 *
 * @param {string} username
 * @param {string} password
 * @param {HTMLButtonElement|null} btn Button to hold in its pending state.
 */
async function signIn(username, password, btn) {
  showError('login-error', '');
  const idle = btn ? btn.textContent : '';
  setPending(btn, true, idle);

  const res = await postJson('/api/auth/login', { username, password });

  if (res.ok) {
    redirectForRole(res.data.role);
    return; // Deliberately leave the button disabled: the page is navigating away.
  }

  setPending(btn, false, idle);
  showError('login-error', errorMessage(res));
  // Move focus to the message. Without this a keyboard or screen-reader user
  // is left on a button that appears to have done nothing.
  document.getElementById('login-error')?.setAttribute('tabindex', '-1');
  document.getElementById('login-error')?.focus?.();
}

/** Wire login.html: the form plus the two one-tap demo buttons. */
function initLogin() {
  const form = document.getElementById('login-form');
  if (!form) return;

  const submit = document.getElementById('login-submit');

  form.addEventListener('submit', (e) => {
    // The form has a real method/action so it degrades without JS; we take over
    // here only to render errors inline instead of navigating to a JSON blob.
    e.preventDefault();
    const username = form.querySelector('#username')?.value.trim() || '';
    const password = form.querySelector('#password')?.value || '';
    signIn(username, password, submit);
  });

  /*
    ONE-TAP DEMO SIGN-IN (CONTRACT ground rule 4).
    The credentials live in data-demo-user / data-demo-pass on the buttons in
    login.html, so the printed credentials, the pre-filled inputs, and these
    buttons all come from the markup — there is no second copy in JavaScript to
    fall out of sync with the ones printed on the page.

    Each button also fills the visible inputs before submitting. An evaluator
    should be able to SEE which account they are entering as, not just arrive
    somewhere. These are real <button> elements, so Enter and Space work without
    any key handling here.
  */
  document.querySelectorAll('[data-demo-user]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const username = btn.dataset.demoUser || '';
      const password = btn.dataset.demoPass || '';
      const u = form.querySelector('#username');
      const p = form.querySelector('#password');
      if (u) u.value = username;
      if (p) p.value = password;
      signIn(username, password, btn);
    });
  });
}

/* -------------------------------------------------------------------------- */
/* Register                                                                    */
/* -------------------------------------------------------------------------- */

/**
 * Wire register.html.
 *
 * Registration is a full end-to-end path, not a decorative form: an evaluator
 * must be able to create their own account and watch every surface generate
 * from scratch rather than only ever seeing seeded state (CONTRACT ground
 * rule 4). On success the server sets the session cookie immediately, so a new
 * account lands inside the product without a second sign-in step.
 */
function initRegister() {
  const form = document.getElementById('register-form');
  if (!form) return;

  const submit = document.getElementById('register-submit');

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    showError('register-error', '');

    const username = form.querySelector('#r-username')?.value.trim() || '';
    const password = form.querySelector('#r-password')?.value || '';
    // Role decides which product this account gets, so it is read from the
    // checked radio rather than assumed.
    const role = form.querySelector('input[name="role"]:checked')?.value || 'user';

    const idle = submit ? submit.textContent : '';
    setPending(submit, true, idle);

    const res = await postJson('/api/auth/register', { username, password, role });

    if (res.ok) {
      redirectForRole(res.data.role || role);
      return;
    }

    setPending(submit, false, idle);
    showError('register-error', errorMessage(res));
    document.getElementById('register-error')?.setAttribute('tabindex', '-1');
    document.getElementById('register-error')?.focus?.();
  });
}

/* -------------------------------------------------------------------------- */
/* AI status badge                                                             */
/* -------------------------------------------------------------------------- */

/**
 * Report AI availability honestly on the sign-in page.
 *
 * The key is not guaranteed to be present (CONTRACT: "It is not set yet"), and
 * an evaluator deserves to know that before they judge a generated surface as
 * broken. `/api/state` answers for an anonymous caller, so this works before
 * anyone has signed in. Failure is silent: the badge is context, not a feature,
 * and an error strip on the sign-in page would read as "sign-in is down".
 */
async function initAiStatus() {
  const el = document.getElementById('ai-status');
  if (!el) return;
  try {
    const res = await fetch('/api/state', { credentials: 'same-origin' });
    const state = await res.json();
    el.textContent = state.ai_online ? 'AI online' : 'AI offline — no API key';
    el.classList.add(state.ai_online ? 'badge--live' : 'badge--offline');
    el.dataset.tone = state.ai_online ? 'ok' : 'warn';
  } catch {
    el.textContent = 'AI status unknown';
    el.classList.add('badge--offline');
  }
}

/* -------------------------------------------------------------------------- */
/* Boot                                                                        */
/* -------------------------------------------------------------------------- */

/**
 * Entry point. Both initialisers guard on their own form, so this one file can
 * be the script tag on both pages without either needing to know which is live.
 */
function boot() {
  initLogin();
  initRegister();
  initAiStatus();
}

document.addEventListener('DOMContentLoaded', boot);
