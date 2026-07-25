"""Refusal / exit lines — words for saying no *in the room*, in the user's own register.

What this module does:
    Builds the (system, user) prompt pair that produces a short set of ready-made
    sentences the user can say out loud to leave a situation or decline an offer.

What it deliberately does NOT do:
    - It does not counsel, motivate, or ask the person to commit to anything. The
      product's job is to hand over words, not to extract a promise.
    - It does not decide *when* refusal lines are appropriate; that is triage's call
      (PRD P4: the model never decides a tier, and never decides what fires).
    - It makes no network call and imports nothing from `genai` (CONTRACT.md:
      `app/genai.py` is the only networked module).
"""

from __future__ import annotations

from app.models import UserProfile
from app.prompts import SAFETY_RULES, STYLE_RULES

# WHY each block below is in the system prompt is commented inline — the prompt design
# is a large part of what this feature is, so the reasoning lives next to the text.
SYSTEM = """\
You write short lines a person can say out loud, right now, in a room with other people,
to get out of using or to turn down an offer without a scene.

{safety}

{style}

WHY THIS IS HARD — design for it
Refusal fails socially, not morally. The person is usually with people they like, who
are not villains, and who will keep offering if the no sounds like judgement. So every
line must let the other person keep their dignity. A line that implies "I'm better than
this now" will not get said out loud, and an unsaid line is a useless line.

WHAT MAKES A USABLE LINE
- Sayable in one breath. Under 10 words wherever possible.
- Boring on purpose. The best refusals are unremarkable and slightly dull, because
  drama invites follow-up questions.
- Needs no explanation, no backstory, and no apology.
- Never mentions recovery, sobriety, quitting, programmes, counsellors, or this app.
  Naming any of those turns a small moment into a confrontation.
- Works whether or not the person has already used tonight.

WHAT TO PRODUCE — exactly this shape
Output 6 to 8 plain lines. No numbering, no bullets, no labels, no headings, no
quotation marks, no commentary before or after. One usable sentence per line.
Cover this spread, roughly in this order:
- two flat declines that need no reason at all,
- two that borrow an ordinary excuse (early start, medication, stomach, driving, money),
- one that defers without refusing outright, to buy time,
- one that exits the room entirely,
- one that hands the moment back warmly, so nobody is embarrassed.

REGISTER
Match how this person actually talks, as shown in their own words below. Keep their
slang, their contractions, their bluntness or their softness. If they swear, you may.
A line in the wrong register is a line they will not use.
"""

USER = """\
How this person talks, in their own words:
{register}

The situation they expect to be in:
{situation}

Write the lines now. Lines only.
"""

# Fallback register sample used when we have no prior utterances from the user.
# Deliberate choice: a neutral, plain-spoken sample rather than an invented "street"
# voice, because guessing someone's slang wrongly is worse than being plain.
_DEFAULT_REGISTER = '"nah I\'m good"\n"I\'m alright, honestly"\n"not tonight"'


def build(
    profile: UserProfile,
    situation: str = "",
    register_samples: list[str] | None = None,
) -> tuple[str, str]:
    """Build the refusal-lines prompt.

    Args:
        profile: The user. Used only for situational context; no identifying detail
            (address, entry code) is ever sent for this task — those are needed by the
            911 script and nothing else, so they are deliberately omitted here.
        situation: Free-text description of the room/offer the user expects. Optional;
            a generic social-offer default is used when empty.
        register_samples: Recent things the user actually said, used to mirror their
            voice. Optional; falls back to a plain neutral sample.

    Returns:
        (system, user) prompt strings.
    """
    # Only the most recent handful of samples: register drifts, and a long history
    # dilutes the voice signal the model is meant to copy.
    samples = (register_samples or [])[-6:]
    register = "\n".join(f'"{s.strip()}"' for s in samples if s.strip()) or _DEFAULT_REGISTER

    return (
        SYSTEM.format(safety=SAFETY_RULES, style=STYLE_RULES),
        USER.format(
            register=register,
            situation=situation.strip()
            or "With people they know. Someone has offered, or is about to offer, and "
            "they want out without making it a thing.",
        ),
    )
