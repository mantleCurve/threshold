/* ============================================================================
   THRESHOLD — theme.js
   Light/dark theme selection.

   WHAT THIS DOES
     Reads a stored preference, applies it to <html data-theme>, and renders a
     toggle into any element with [data-theme-toggle].

   WHY IT IS LOADED SYNCHRONOUSLY IN <head>, NOT DEFERRED
     A deferred script runs after first paint, so the page would paint in the
     default theme and then snap to the chosen one — the "flash of wrong theme".
     On a product someone opens at 3am, a sudden flash of white is not a
     cosmetic problem: it is a face full of light when they are already
     struggling. The cost is one tiny blocking script.

   WHAT IT DELIBERATELY DOES NOT DO
     It stores nothing on the server and sends nothing over the network. A theme
     preference is not health data and has no business in a clinical record.
   ========================================================================= */

'use strict';

(function () {
  const KEY = 'threshold-theme';
  const root = document.documentElement;
  const defaultTheme =
    root.dataset.defaultTheme === 'light' || root.dataset.defaultTheme === 'dark'
      ? root.dataset.defaultTheme
      : null;

  /**
   * Apply a theme.
   * @param {'light'|'dark'|null} theme  null removes the attribute, which hands
   *   control back to the operating system's prefers-color-scheme.
   */
  function apply(theme) {
    if (theme === 'light' || theme === 'dark') {
      root.setAttribute('data-theme', theme);
    } else {
      root.removeAttribute('data-theme');
    }
  }

  /** Stored choice, or null when the user has never expressed one. */
  function stored() {
    try {
      const v = localStorage.getItem(KEY);
      return v === 'light' || v === 'dark' ? v : null;
    } catch {
      // Private browsing or blocked storage. Following the OS is a fine
      // fallback, so this is not worth surfacing to the user.
      return null;
    }
  }

  // Apply before first paint. This line is the whole reason the file is not
  // deferred.
  apply(stored() || defaultTheme);

  /** What the user would see right now, resolving "follow the OS" to a value. */
  function effective() {
    const s = stored();
    if (s) return s;
    if (defaultTheme) return defaultTheme;
    return window.matchMedia?.('(prefers-color-scheme: light)').matches
      ? 'light'
      : 'dark';
  }

  /**
   * Render toggles once the DOM exists.
   *
   * The control is a real <button> with aria-pressed, so it is reachable by
   * keyboard and announced correctly, rather than a styled div.
   */
  function mount() {
    document.querySelectorAll('[data-theme-toggle]').forEach((host) => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'theme-toggle';

      const sync = () => {
        const now = effective();
        // Label states the action, not the current state: "Light" means
        // "switch to light". Labelling the current state is the classic
        // ambiguity in theme toggles.
        btn.textContent = now === 'light' ? 'Dark' : 'Light';
        btn.setAttribute('aria-pressed', String(now === 'light'));
        btn.setAttribute(
          'aria-label',
          now === 'light' ? 'Switch to dark theme' : 'Switch to light theme'
        );
      };

      btn.addEventListener('click', () => {
        const next = effective() === 'light' ? 'dark' : 'light';
        apply(next);
        try { localStorage.setItem(KEY, next); } catch { /* storage blocked */ }
        sync();
      });

      sync();
      host.appendChild(btn);
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', mount);
  } else {
    mount();
  }

  // Follow the OS live, but only while the user has made no explicit choice.
  window.matchMedia?.('(prefers-color-scheme: light)')
    .addEventListener?.('change', () => {
      if (!stored()) {
        document.querySelectorAll('.theme-toggle').forEach((b) => {
          const now = effective();
          b.textContent = now === 'light' ? 'Dark' : 'Light';
          b.setAttribute('aria-pressed', String(now === 'light'));
        });
      }
    });
})();
