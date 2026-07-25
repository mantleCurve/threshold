# Threshold Design System

## Overview

Threshold is a crisis-aware product interface. Its visual hierarchy becomes
sparser and more forceful as the escalation tier rises. The system uses familiar
controls, restrained color, and explicit state labels so it remains usable under
stress and with assistive technology.

## Theme

The default dark theme supports late-night, low-light use. A complete light theme
is available for user preference. Neither theme uses pure black or white. All
surfaces and text are slightly blue-tinted, while the active escalation tier is
the only strong chromatic signal.

## Color

- Canvas: `#0b0e12`
- Surface: `#121821`
- Raised surface: `#18202a`
- Strong surface: `#1f2833`
- Primary text: `#e9eef4`
- Secondary text: `#adbac8`
- Muted text: `#8698aa`
- Tier 0 teal: `#3fbf9b`
- Tier 1 sage: `#7cc386`
- Tier 2 gold: `#e0b457`
- Tier 3 amber: `#ee8b47`
- Tier 4 red: `#ff5a4e`
- Tier 5 red: `#ff3b30`

Tier meaning is always repeated through text, number, spacing, and scale. Color
is never the only indicator.

## Typography

The interface uses local system fonts only:

- UI and controls: system sans-serif.
- Human or Threshold speech: system serif.
- Timestamps, tier numbers, and audit data: system monospace.

Body prose is limited to approximately 70 characters per line. Crisis headings
may scale up, while labels and metadata remain stable to preserve hierarchy.

## Layout

Authenticated pages use a persistent escalation rail and task-focused content
pane. On narrow screens, the rail becomes a compact horizontal strip rather than
a hidden menu. Spacing increases with the tier; secondary chrome disappears at
tiers 3 through 5.

At tiers 4 and 5, the emergency takeover becomes the only primary surface. It
moves focus to a plain `tel:911` link, makes background content inert, presents
captions for spoken guidance, and keeps rescind as the only secondary action at
tier 4.

## Components

- Buttons use one shared vocabulary with default, hover, focus, active,
  disabled, loading, and error states.
- Emergency call controls are always red and remain real links.
- Generation badges distinguish live, cached fallback, and offline output.
- Notices use text plus semantic roles, never color alone.
- Event logs distinguish scheduled, attempted, delivered, failed, and completed
  work.
- Loading and empty states explain the next available action.

## Motion

Motion communicates state changes only. Transitions last 150–250ms except the
single tier-change wash. All nonessential motion is disabled under
`prefers-reduced-motion`.

## Accessibility

- WCAG 2.2 AA contrast and interaction targets.
- One page-level `h1`, sequential headings, skip links, and landmark regions.
- Visible `:focus-visible` treatment.
- Keyboard and screen-reader parity for all actions.
- Persistent visual equivalents for speech and timer-driven guidance.
- No document-wide click handlers that can intercept emergency controls.
- Dark/light theme, 200% zoom, high contrast, and reduced motion are supported.
