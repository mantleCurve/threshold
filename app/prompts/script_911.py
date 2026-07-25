"""Personalised 911 script — the words a terrified person reads to a dispatcher.

What this module does:
    Renders short deterministic dispatcher lines from validated profile fields.
    Address and entry facts never pass through a model or a network call.

Why the output shape is so constrained:
    Under acute stress, reading comprehension collapses to roughly a glance. Anything
    that requires holding two clauses at once will not be read out. Hence: one short
    sentence per line, no line depending on the line before it, dispatcher-priority
    ordering (what/where before who/why), and no formatting characters at all.

What this module deliberately does NOT do:
    - It never mentions arrest, charges, immunity, or police consequences. Good
      Samaritan information is a static reviewed dataset in `data/legal/` and is never
      model-generated (CONTRACT.md — the highest-harm hallucination in this product).
    - It never emits a dose, quantity, or time interval.
    - It never invents a missing detail or writes a placeholder — a wrong address read
      to a dispatcher is worse than a missing line.
    - No network access, no `genai` import.

    The older implementation built a model prompt and first called it during an
    emergency. A prompt asking a model to copy an address exactly is not a
    guarantee, so the executable path is now local.
"""

from __future__ import annotations

from app.models import UserProfile
from app.prompts import SAFETY_RULES, STYLE_RULES

SYSTEM = """\
You write the exact words a terrified person will read off a screen to a 911 dispatcher.
They may be shaking, crying, high, or holding someone who is not breathing. Assume they
can read about six words at a glance and cannot improvise a single one.

{safety}

{style}

FORMAT — follow exactly
- Output plain lines only. One line per screen. No numbering, no bullets, no labels,
  no headings, no quotation marks, no blank lines, no commentary before or after.
- 8 to 12 lines total.
- Each line is one short spoken sentence, ideally under 12 words. Never two sentences.
- Every line must be sayable out loud, alone, with no setup from the line before it.

ORDER — dispatchers need it in this order and nothing may be reordered
1. What is happening, in the first line, in the bluntest possible words.
2. The full street address, exactly as given, on its own line.
3. Unit or apartment on its own line, if there is one.
4. Cross street on its own line, if there is one.
5. Door or entry instruction on its own line, if there is one — how to get in.
6. Whether the person is breathing, then whether they are responding.
7. What was taken, in plain words, if it is known.
8. Whether naloxone has been given.
9. A line asking the dispatcher to stay on the line.

RULES
- Reproduce the address, unit, entry code and cross street character-for-character.
  Never abbreviate, reformat, correct, or invent any part of them.
- Never include a dose, amount, quantity, or time interval.
- Never mention arrest, charges, immunity, police, or any legal consequence.
- Do not tell the caller to do anything medical beyond what is listed; the app handles
  rescue breathing and naloxone prompts separately.
- If a detail was not provided, omit that line entirely. Never write a placeholder,
  never write "unknown", never write brackets.
"""
# Ordering rationale: dispatch is initiated on location, so address/unit/cross-street
# are front-loaded — if the caller drops the phone after four lines, help is already
# routed. Entry instruction sits immediately after because a locked door is the most
# common cause of delay between arrival and treatment.

USER = """\
Address: {address}
Unit: {unit}
Cross street: {cross_street}
Entry instruction: {entry}
Substances they may have taken: {substances}
Naloxone in the home: {naloxone}
Situation: {situation}

Write the script lines now. Lines only.
"""

# Sentinel used for absent fields. Chosen to be visibly non-substantive so the model's
# "omit that line entirely" rule fires cleanly, rather than an empty string, which
# models routinely fill in with a plausible invention.
_MISSING = "(none given)"


def build(profile: UserProfile, situation: str = "") -> tuple[str, str]:
    """Build the personalised 911 script prompt.

    Args:
        profile: The user. This is the ONE task that legitimately needs the address,
            unit, entry code, and cross street — every other prompt in this package
            deliberately withholds them.
        situation: What is happening, if known. Defaults to the overdose case, which is
            the scenario this script exists for.

    Returns:
        (system, user) prompt strings.
    """
    # Entry code is expanded into a spoken instruction here rather than in the prompt
    # template: the dispatcher needs "entry code 4471", not a bare number on a line.
    entry = f"entry code {profile.entry_code}" if profile.entry_code else _MISSING

    return (
        SYSTEM.format(safety=SAFETY_RULES, style=STYLE_RULES),
        USER.format(
            address=profile.address or _MISSING,
            unit=profile.unit or _MISSING,
            cross_street=profile.cross_street or _MISSING,
            entry=entry,
            substances=", ".join(profile.substances) or "(not specified)",
            naloxone="yes" if profile.naloxone_on_hand else "no or unknown",
            situation=situation.strip()
            or "Someone is unresponsive after using and may have overdosed.",
        ),
    )


def render(profile: UserProfile) -> str:
    """Render the emergency script locally, preserving profile facts exactly."""
    lines = ["Someone may have overdosed and is not responding normally."]
    if profile.address:
        lines.append(f"Address: {profile.address}")
    if profile.unit:
        lines.append(f"Unit: {profile.unit}")
    if profile.cross_street:
        lines.append(f"Cross street: {profile.cross_street}")
    if profile.entry_code:
        lines.append(f"Entry instruction: use code {profile.entry_code}")
    lines.append("They may not be breathing normally.")
    lines.append("They are not responding normally.")
    lines.append(
        "Naloxone is available."
        if profile.naloxone_on_hand
        else "Naloxone availability is unknown."
    )
    lines.append("Please stay on the line and tell me what to do.")
    return "\n".join(lines)


def preserves_dispatcher_facts(text: str, profile: UserProfile) -> bool:
    """Return whether generated output preserved every supplied routing fact.

    GenAI supplies the personalized phrasing, but it only reaches the emergency
    screen when the address, unit, cross street, and entry code survive exactly.
    """
    if not text.strip():
        return False
    required = (
        profile.address,
        profile.unit,
        profile.cross_street,
        profile.entry_code,
    )
    return all(not value or value in text for value in required)
