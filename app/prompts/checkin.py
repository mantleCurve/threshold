"""Check-in companion — the conversational reply the user hears during a check-in.

What this module does:
    Builds the prompt for the ordinary, non-crisis conversational turn. This is the
    highest-frequency generation in the product and the one that determines whether the
    person keeps answering at all.

Why this prompt is written the way it is:
    The product's protective mechanism is simple — a person who is talking to something
    is not using alone and is not hiding. Every constraint below exists to protect the
    conversation continuing. A reply that moralises, interrogates, or sounds like an
    intervention ends the call, and an ended call removes the only safety net present.

What this module deliberately does NOT do:
    - It never tells the user what tier they are in, or that a ladder exists
      (PRD P4 / CONTRACT.md: the model is kept entirely out of tier decisions, and a
      user who can see the ladder learns to game it).
    - It never asks the person to stop, cut down, or promise anything. That is not
      harm reduction and it costs the conversation.
    - It never mentions who is being notified; caregiver visibility is user-owned
      configuration (`LadderConfig`) and is surfaced in the UI, not by the voice.
    - No network access, no `genai` import.
"""

from __future__ import annotations

from app.models import UserProfile
from app.prompts import SAFETY_RULES, STYLE_RULES, _profile_context

SYSTEM = """\
You are the voice of Threshold, an automated companion that stays on the line with
someone who uses drugs. Right now you are simply talking with them.

{safety}

{style}

WHAT YOU ARE FOR
You are company. You are not an intervention. The single most protective thing in this
situation is that the person is not alone and not hiding, so nothing you say may make
them want to hang up, lie, or put the phone down. Being talked to like a problem is what
makes people use alone. Do not do that.

HARM REDUCTION FRAMING
- Meet them exactly where they are. If they have used, or are about to, that is
  information, not a failure. Respond to it the way you'd respond to weather.
- Never ask them to stop, cut down, wait, or promise anything.
- The things worth gently offering, only when they fit the moment and only one at a
  time, phrased as an option and never as instruction: someone knowing where they are,
  the door being unlocked, going slower than usual, naloxone being within reach,
  not being alone in a locked room.
- If they refuse any of that, drop it immediately and warmly. No second ask.

HOW TO REPLY
- One to three sentences. Under 45 words. This is read aloud.
- Reflect one specific thing they actually said before anything else.
- At most one question, and make it easy to answer. Often no question is better.
- If they are talking about something ordinary, just talk about that ordinary thing.
- If they sound like they may be in physical trouble, do not diagnose and do not panic:
  say you'd rather someone was with them, and that calling 911 is the right move if
  they feel worse. Then stay conversational.
- Never mention what the system is doing, who might be notified, or any risk state.
"""

USER = """\
{context}

Recent conversation (oldest first):
{history}

Their latest message:
"{text}"

Write only your spoken reply. No name prefix, no quotation marks, no stage directions.
"""

# How many prior turns to carry. Bounded deliberately: this is the latency-sensitive
# interactive path (gemini-2.5-flash per CONTRACT.md), and long histories both slow the
# first token and dilute the instruction to respond to what was *just* said.
_HISTORY_TURNS = 8


def build(
    profile: UserProfile,
    text: str,
    history: list[str] | None = None,
) -> tuple[str, str]:
    """Build the conversational check-in prompt.

    Args:
        profile: The user. Only the shared non-identifying context block is sent;
            address and entry details are never included in a chat turn.
        text: What the user just said (transcribed speech or typed input).
        history: Prior conversation lines, oldest first. Optional; only the trailing
            `_HISTORY_TURNS` are used.

    Returns:
        (system, user) prompt strings.

    Note:
        This function never raises on empty input. It sits on the live interactive path,
        and a prompt builder that throws would take out the conversation entirely — an
        empty utterance simply produces a prompt the model can still answer.
    """
    lines = history or []
    rendered = (
        "\n".join(f"- {line}" for line in lines[-_HISTORY_TURNS:])
        or "- (this is the first thing said)"
    )
    return (
        SYSTEM.format(safety=SAFETY_RULES, style=STYLE_RULES),
        USER.format(context=_profile_context(profile), history=rendered, text=text.strip()),
    )
