# `data/legal/` — the Good Samaritan dataset

## The one-sentence version

`good_samaritan.json` is a **static, hand-written, human-reviewed** dataset. No
language model wrote any part of it, no code path in this application writes to
it, and every record is flagged `verified: false` until a human has read the
current statute — which is an **honest disclosure**, not an unfinished TODO.

## Why this file is special

Everything else in Threshold can be wrong in a recoverable way. If the vault
picks a slightly odd clip, or the caregiver brief is clumsily worded, nobody is
harmed. This file is different.

A person kneeling next to a friend who has stopped breathing is deciding, in
that moment, whether calling 911 will get them arrested. That hesitation is the
single biggest reason overdose deaths happen with someone else in the room.
Whatever this file says is what they will act on.

So there are two failure modes, and they are not symmetric:

- **Overstating protection** — telling someone they are covered when they are
  not. They call, they stay, and they are arrested for something we implied was
  immune. We caused that.
- **Understating protection** — being vague where the law is actually strong.
  Less harmful, but it still leaves the fear intact, which is the fear the whole
  product exists to defuse.

A language model asked to summarise a state's overdose immunity statute will
produce fluent, confident, correctly-formatted text with an invented section
number and a subtly wrong scope. It will look more authoritative than this file
does. That is precisely the danger. The build contract names this as the worst
possible hallucination in the product, and this directory is the mitigation.

## The rules

1. **Never model-generated.** Not at build time, not at runtime, not "just to
   fill in the missing states". If a state is missing, it is served the
   `_fallback` record, which says plainly that we do not know.
2. **Read-only at runtime.** The application only ever loads this file. There
   is no write path, by design.
3. **No unsourceable specifics.** No quantity thresholds, no penalties, no
   enactment dates, no case citations. Statute identifiers appear only inside
   `source_note`, and only as *navigation pointers* for the human reviewer —
   "start looking here" — never as an assertion that the section is current or
   says what the summary says.
4. **`does_not_cover` is the important field.** It is written before the
   summary and reviewed harder, because overstating immunity is the dangerous
   direction.
5. **Calibrated language.** `plain_language_line` for Texas is deliberately
   weaker than for Kentucky, because the Texas law is genuinely narrower. A
   uniform reassuring sentence across all states would itself be a lie.
6. **Conservative by default.** Where we are unsure, the text says to check.
   "We don't know" is always an acceptable answer here.

## What `verified: false` means

It means: *a human wrote this carefully and conservatively from general
public-health understanding of how these laws work, and no human has yet
confirmed it line-by-line against the current statute text.*

It does **not** mean the file is half-finished. It is a deliberate, visible
disclosure. The UI reads the flag and renders an "unverified — confirm locally"
banner beside the text, so the reader knows exactly how much weight to put on
it.

Setting `verified: true` without doing the reading would be the dishonest
option, and it would be invisible to everyone but us. Shipping with the flag
false and surfaced is the correct one.

## How to verify a record

1. Open the `source_url` for that state — always an official legislature or
   `.gov` domain, never a secondary summary site.
2. Find the current overdose / emergency-assistance immunity provision. Confirm
   it has not been amended, renumbered, or repealed.
3. Read the exclusions and conditions in full, not just the headline protection.
4. Correct `summary`, `does_not_cover`, and `plain_language_line` to match.
5. Only then set `verified: true` and fill in `verified_by` and `verified_on`.

Verify **KY first** — it is the demo profile's state and the record most likely
to be shown to a judge.

## Coverage

Eight states: KY, OH, WV, PA, NY, CA, FL, TX. Chosen for a mix of
high-overdose-burden states and the largest populations, with Texas included
specifically because its law is unusually narrow and the dataset needed at least
one record where the honest answer is "less than you'd hope".

Coverage is deliberately partial. **Accuracy here matters more than
completeness** — eight carefully hedged records plus an honest fallback is
worth more than fifty confident guesses.

## Adding a state

Hand-write it. Use the existing records as the shape, keep `verified: false`,
fill in a real official `source_url`, and write `does_not_cover` first. If you
cannot write the record without guessing at a detail, leave the state out and
let the fallback handle it.
