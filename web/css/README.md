# Threshold — design system

Vanilla CSS. No framework, no build step, no CDN, no webfont request. Three
stylesheets, loaded in this order on every page:

```html
<link rel="stylesheet" href="/css/base.css">   <!-- tokens, reset, primitives -->
<link rel="stylesheet" href="/css/tiers.css">  <!-- the escalation ladder -->
<link rel="stylesheet" href="/css/pages.css">  <!-- per-surface components -->
```

---

## 1. The transformation rule — read this first

**JS sets exactly one attribute. Nothing else.**

```js
document.body.dataset.tier = String(tier); // "0".."5"
```

The selectors are plain `[data-tier="N"]`, so the attribute works on **either**
`<html>` or `<body>`. Both are used, deliberately, and they layer:

- `<html data-tier="N">` is the **static floor**, written into the markup. It is
  what renders before any script runs, and what renders if scripting is off
  entirely. `bystander.html` is pinned to `4` this way and needs no JS at all.
- `<body data-tier="N">` is the **runtime override** set by `app.js`. Because
  `<body>` is the inner element, custom properties inherited from it win over
  `<html>` for everything inside the page — so JS always takes precedence.

If you set the tier on `<html>` at runtime while `<body>` still carries a stale
value, `<body>` wins and nothing appears to change. Set it on `<body>`.

That attribute rebinds four variables and the entire interface follows:

| var | 0 | 1 | 2 | 3 | 4 | 5 |
|---|---|---|---|---|---|---|
| `--accent` | teal | sage | gold | amber | red | red |
| `--density` (spacing multiplier) | 1 | 1.05 | 1.14 | 1.35 | 1.7 | 1.7 |
| `--scale` (type multiplier) | 1 | 1.04 | 1.1 | 1.25 | 1.5 | 1.5 |
| `--chrome` | 1 | 1 | 1 | 0 | 0 | 0 |

As the tier climbs the interface **inverts**: tier 0 is dense, quiet and
information-rich; tier 4 is one enormous button and nothing else. Information is
not rearranged, it is **removed**. At tier 4 the user may be unable to read, so
every pixel that is not the single life-saving action is a hazard.

Do not add per-tier branching in JS. If something needs to change with the tier,
express it in `tiers.css` against `[data-tier="N"]`.

### Visibility hooks

| attribute | behaviour |
|---|---|
| `data-chrome` | Furniture. `display: none !important` at tiers 3, 4, 5. Also leaves the a11y tree and tab order. Use for nav, settings, and secondary cards. |
| `data-when-tier="2 3"` | Hidden by default; displayed only at the listed tiers. Use for escalation-only actions. |

Both are pure CSS. JS never toggles them.

### Tier change animation

On a tier change, add `tier-shift` to `<html>` for one cycle:

```js
root.classList.add('tier-shift');
setTimeout(() => root.classList.remove('tier-shift'), 750);
```

One 700ms wash of the new accent across the viewport. Disabled entirely under
`prefers-reduced-motion` — nothing depends on it, the aria-live announcement
carries the information.

---

## 2. Tokens

### Colour

Near-black, blue-shifted canvas. Never pure `#000` — pure black kills depth and
makes the tier flood less legible.

```
--canvas #0b0e12   --surface #121821   --surface-2 #18202a   --surface-3 #1f2833
--line   #222c37   --line-strong #35424f
--ink    #e9eef4   --ink-2 #adbac8   --ink-3 #8698aa
```

`--ink-3` is the floor — nothing dimmer is ever used for text. Ratios verified
against every surface these appear on, not just the canvas:

| | on `--canvas` | on `--surface` | on `--surface-3` |
|---|---|---|---|
| `--ink` | 16.58 | 15.28 | 12.77 |
| `--ink-2` | 9.79 | 9.02 | 7.54 |
| `--ink-3` | 6.53 | 6.02 | **5.03** |

`--surface-3` is the binding constraint. `--ink-3` was raised from `#7d8fa1`
(4.48:1 there — passing on the canvas, failing inside a nested panel).

Accent ratios on `--canvas`: teal 8.42, sage 9.22, gold 9.98, amber 7.75,
red 6.29 / 5.45. Each `--accent-ink` pairing clears 6.4:1 as text on its filled
accent surface.

**One accent, and it is the only chromatic element in the product.** Because
nothing else is coloured, the accent's hue *is* the status indicator — readable
from across a dark room before a word is read.

```
--accent-0 #3fbf9b teal    steady, medical-calm
--accent-1 #7cc386 sage    barely different from teal on purpose; tier 1 must not accuse
--accent-2 #e0b457 gold    attention, not alarm
--accent-3 #ee8b47 amber   urgency without finality
--accent-4 #ff5a4e red     first red anywhere in the product, so it has never been diluted
--accent-5 #ff3b30 red     deeper; reads as continuation of 4, not a new state
```

Not a traffic light: green/red alone fails for ~8% of men with deuteranopia.
Every tier is *also* distinguished by number, name, spacing and type size, so
colour is never the sole carrier of meaning (WCAG 1.4.1).

**Always author against `--accent`**, never `--accent-N`. The only exception is
the event log, where each row carries its own historical tier colour.

Paired tokens, all derived and all tier-aware:
`--accent-ink` (near-black text on a filled accent), `--accent-soft` (16% wash),
`--accent-line` (42% border), `--glow`.

### Type

Three voices, deliberately different, all system faces:

```
--font-voice  serif  a person or Threshold speaking   (Charter → Iowan → Palatino → Georgia)
--font-ui     sans   the machine: state, labels, controls
--font-data   mono   timestamps, tiers, the audit log
```

No Google Fonts, no CDN, no `@font-face`. This must work with the network down,
which is exactly when it matters most.

Scale `--step-000` (0.6875rem) through `--step-8` (up to 10rem). Steps 5–8 are
`clamp()`ed and only used in crisis states. `main`, `.lede`, `h1`, `h2` are
multiplied by `--scale`; labels and metadata deliberately are **not**, so the
hierarchy sharpens rather than everything inflating uniformly.

### Space

`--sp-1`…`--sp-8`, all `calc(base * var(--density))`. Never hardcode a rem gap;
use the token, and it escalates for free.

---

## 3. Components

**`.btn--emergency` vs `.btn--primary`.** `--primary` fills with the *live*
accent, so at tier 0 it is calm teal. Never use it for a control that calls
emergency services — a 911 button must not inherit a reassuring colour from a
low tier. `.btn--emergency` is always red at every tier. Used on the caregiver
alert; `.call-911` and `.takeover__action` are the same idea at larger sizes.

`base.css` — `.card`, `.btn` (`--primary` `--emergency` `--quiet` `--danger` `--block` `--lg`),
`.field` / `.input` / `.switch`, `.badge` (`--live` `--fallback` `--offline`),
`.notice` (`--warn` `--error` `--ok`), `.rail` + `.ladder`, `.shell`, `.pane`,
`.stack` / `.row`, `.label` / `.lede` / `.prose` / `.data` / `.voice`.

`tiers.css` — `.takeover` and its parts.

`pages.css` — `.auth` + `.creds`, `.ptt` + `.ptt-meter`, `.transcript` + `.turn`,
`.stat-row`, `.tier-control`, `.alert-head` / `.timeline` / `.say-not` /
`.visibility`, `.samaritan` / `.call-911` / `.steps` / `.metronome` / `.diagram`,
`.promise` / `.ladder-table`, `.log`.

### The ladder rail

Present on every authenticated surface, at every tier. It never collapses into a
hamburger — at 3am you should not have to press anything to know where you are.
On narrow viewports it becomes a horizontal strip, names hidden, numbers kept.

The dashed **Threshold** divider between rungs 3 and 4 is the line the product is
named after: below it we ask you, above it we act. It is drawn, never implied.

JS updates the rail by setting, on each `.ladder__step`:
- `aria-current="step"` on the live rung (exactly one)
- `data-state="passed"` on rungs below it

### The takeover (tier 4/5)

`#takeover` is `role="alertdialog" aria-modal="true"`, `display:none` until
`[data-tier="4"]` or `[data-tier="5"]`.

JS must, on entering tier 4/5:
1. set `inert` on `.shell` so focus cannot reach the stripped UI behind it;
2. move focus to `#takeover-action`;
3. announce the tier in `#tier-announcer`.

And reverse 1–2 on the way down.

The one button is an `<a href="tel:911">`, **not** a `<button>` — if every script
on the page has failed to load, the call still works. Same for `.call-911` on the
bystander page. Do not convert either to a button.

The rescind control is the only secondary action at tier 4, and CSS removes it
entirely at tier 5: an unresponsive person cannot press it, so leaving it would
only let a bystander cancel a real emergency.

### Generation badges

Contract rule 2 — a fallback must never pass as a live generation. Every surface
that renders a `Generation` has a `.badge` next to it. Set
`badge--live` / `badge--fallback` / `badge--offline` and the text from
`Generation.live` and `Generation.error`. Never leave it reading "live" by default.

---

## 4. Accessibility (scored category — the decisions, and why)

- **Semantic HTML throughout.** The ladder is an `<ol>` because higher genuinely
  is more dangerous. The onboarding ladder is a real `<table>` with
  `<th scope="row">`. Rescue steps are an `<ol>` so a screen reader says
  "step 2 of 5".
- **`#tier-announcer` is `aria-live="assertive"`** on every authenticated page. A
  tier change is by definition the one thing that must interrupt. It is the
  single source of spoken state; the visual ladder is not separately announced,
  so the same change is never read twice.
- **Everything else is `aria-live="polite"`** — transcript, briefs, log,
  notices. Conditions, not emergencies.
- **`role="alert"` on auth errors**, because a failed sign-in blocks the user
  entirely.
- **One focus treatment everywhere:** a solid `--accent` ring plus a
  `--canvas-deep` spacer ring, so it survives on dark fills, light fills and the
  saturated red takeover button alike. `:focus-visible` only; mouse users never
  see it. The takeover button uses a white ring instead — accent-on-accent would
  be invisible.
- **4.5:1 minimum** on every text token against every surface it is used on.
  `prefers-contrast: more` lifts `--ink-2`, `--ink-3` and the lines further.
- **`prefers-reduced-motion`** is honoured globally (all durations → 0.001ms) and
  again per-effect: the tier wash is removed, the takeover breath is frozen at
  full opacity, the metronome ring stops scaling. No effect ever carries
  information alone.
- **Colour is never the only signal.** Tier is also number, name, spacing and
  type size. The metronome has three redundant channels (ring, word, count).
- **No motion faster than one cycle per 2.6s** anywhere in a crisis state —
  strobing is a seizure risk and raises panic.
- **Touch targets ≥ 44px** (`min-height: 2.75rem` on every control).
- **Safe-area padding** on the takeover, so the primary action is never under a
  home indicator on a one-handed grip.
- **`data-chrome` uses `display:none`, not `visibility`/`opacity`**, so at tier 4
  a keyboard or screen-reader user lands on the emergency action immediately
  instead of tabbing through dead navigation.

---

## 5. Conventions

- New colour? There isn't one. Use `--accent` or a neutral.
- New spacing? Use `--sp-N`. Hardcoded rem gaps break the density escalation.
- New tier behaviour? `tiers.css`, keyed on `[data-tier="N"]`. Never in JS.
- Ids in the HTML shells are stable hooks for the JS agent — rename nothing.
- No emoji anywhere in this product.
