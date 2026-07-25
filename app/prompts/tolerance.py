"""Tolerance Guard — the proactive message sent after a break in use.

What this module does:
    Builds the prompt for the single most important message this product sends. After
    detox, hospital discharge, incarceration release, or any stretch of abstinence,
    tolerance falls. The dose the person remembers as normal is the dose that kills
    them. This message is the intervention: it arrives *before* use, not after.

What it deliberately does NOT do:
    - It never states a number: no milligrams, no percentages, no "X times more likely",
      no day counts framed as clinical thresholds. PRD hard constraint — a numeric
      clinical claim from a language model is an unacceptable hallucination risk, and
      the message does not need one to work.
    - It never tells the person not to use. A prevention message that reads as an
      abstinence demand gets dismissed, and a dismissed message prevents nothing.
    - It does not decide *whether* to fire; `app/triage.py` owns that from the
      tolerance_events on the profile (PRD P4: the model never decides a tier).
    - No network access, no `genai` import (CONTRACT.md architecture invariant).
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.models import ToleranceEvent, UserProfile
from app.prompts import SAFETY_RULES, STYLE_RULES

# Human-readable phrasing for each event kind. Deliberately plain and non-institutional:
# "since you got out" lands where "post-incarceration release" does not.
_EVENT_PHRASING: dict[str, str] = {
    "detox": "they finished detox",
    "hospital_discharge": "they came out of hospital",
    "incarceration_release": "they got out",
    "abstinence": "they last used",
}

SYSTEM = """\
You write one short message that arrives on someone's phone before they use again after
a break. This message exists for one reason: after time without a substance, the body's
tolerance drops, and the amount that used to feel normal can stop someone's breathing.
People die on the first use back at their old amount, believing it is a familiar dose.

{safety}

{style}

THE ONE THING THIS MESSAGE MUST DO
Land the idea that *the amount they remember is no longer the amount their body knows*,
and that going slower is the whole difference. Everything else is secondary. If the
message is only read halfway, the half that gets read must still carry that.

TONE — this is the entire design problem
This person has just done something hard. They are not being caught, warned, or graded.
Open by acknowledging the break plainly and without praise-that-sounds-like-pressure —
no "congratulations", no "you've come so far", no "don't throw it away". That framing
makes the message feel like a leash and it gets swiped away unread.
Assume they may use tonight. Write to the person who will, not to the person you wish
they were. If they sense you are trying to talk them out of it, you have lost them and
the safety information goes unread.

WHAT TO SAY, IN THIS ORDER
1. One line naming the break, factually and warmly. No praise, no pressure.
2. The core fact in ordinary words: after a break, tolerance drops, and the amount they
   remember can be too much now. Say it in body language, not clinical language —
   their body has changed, their memory has not.
3. The two things that actually change the outcome, stated as practical moves and not
   as rules: go much slower than feels necessary, and do not be alone behind a locked
   door. Someone who can hear you is the difference.
4. If naloxone is in the home, one line that it should be in the room, not in a drawer.
   If it is not, one line that it is worth having nearby. Keep it brief either way.
5. Close by making it easy to come back — the line is open, no explaining required.

HARD LIMITS
- 55 to 90 words. Plain sentences, no lists, no headings, no markdown, no emoji.
- No numbers of any kind. Not doses, not percentages, not multiples, not day counts.
- No statistics, no "studies show", no "most people".
- Never say "relapse", "clean", "sober", "slip", or "using again" as a moral event.
- Never ask them to promise, check in, reply, or confirm they have read this.
- Never mention risk levels, tiers, monitoring, or who else can see anything.
"""

USER = """\
Person's first name: {name}
What happened: {event_phrase}
Roughly how long ago: {elapsed}
Note recorded with that event: {note}
Substances they have told us about: {substances}
Naloxone in the home: {naloxone}

Write the message now. Message text only, no preamble.
"""


def _describe_elapsed(event_at: datetime) -> str:
    """Render a gap in vague human units, never as a precise clinical count.

    Deliberate imprecision: "a few weeks" is enough for the message to make sense,
    while an exact day count invites the model to reason numerically about risk —
    which the safety rules forbid, and which would be a fabricated clinical claim.

    Args:
        event_at: Timestamp of the tolerance event. Naive datetimes are treated as UTC
            so a profile loaded from seeded JSON never raises here.

    Returns:
        A vague human-readable duration such as "a few weeks ago".
    """
    now = datetime.now(timezone.utc)
    at = event_at if event_at.tzinfo else event_at.replace(tzinfo=timezone.utc)
    days = max((now - at).days, 0)

    if days <= 1:
        return "in the last day or so"
    if days < 7:
        return "a few days ago"
    if days < 21:
        return "a couple of weeks ago"
    if days < 60:
        return "about a month ago"
    if days < 180:
        return "a few months ago"
    return "a while ago"


def latest_event(profile: UserProfile) -> ToleranceEvent | None:
    """Return the most recent tolerance event on a profile, or None.

    Tolerance is governed by the *most recent* break, not the first one, so callers
    that need "which event is this message about" should use this rather than
    indexing the list.
    """
    if not profile.tolerance_events:
        return None
    return max(
        profile.tolerance_events,
        # Normalise tz so naive and aware timestamps can be compared without raising.
        key=lambda e: e.date if e.date.tzinfo else e.date.replace(tzinfo=timezone.utc),
    )


def build(profile: UserProfile, event: ToleranceEvent | None = None) -> tuple[str, str]:
    """Build the Tolerance Guard prompt.

    Args:
        profile: The user. Substances and naloxone status shape the closing lines.
        event: The tolerance event this message is about. When omitted, the most
            recent event on the profile is used. If the profile has no events at all,
            the prompt degrades to a generic "after a break" framing rather than
            failing — the caller (triage) is what decides this should fire, and a
            prompt builder must never be the thing that raises on a live crisis path.

    Returns:
        (system, user) prompt strings.
    """
    ev = event or latest_event(profile)

    if ev is None:
        # Deliberate soft-degrade rather than an exception; see docstring above.
        event_phrase, elapsed, note = "they have had a break from using", "recently", ""
    else:
        event_phrase = _EVENT_PHRASING.get(ev.kind, "they have had a break from using")
        elapsed = _describe_elapsed(ev.date)
        note = ev.note

    return (
        SYSTEM.format(safety=SAFETY_RULES, style=STYLE_RULES),
        USER.format(
            name=profile.name.split(" ")[0] if profile.name else "unknown",
            event_phrase=event_phrase,
            elapsed=elapsed,
            note=note or "(none)",
            substances=", ".join(profile.substances) or "(not specified)",
            naloxone="yes, in the home" if profile.naloxone_on_hand else "no or unknown",
        ),
    )
