"""Vault clip selection — picks which recorded caregiver voice message to play.

What this module does:
    Given the transcripts of the clips a caregiver has recorded, and a short description
    of what is happening right now, asks the model to choose the single best-fitting
    clip and justify it in one sentence. Returns structured JSON so the API layer can
    resolve the choice back to a real `VaultClip`.

    It also owns `parse_selection`, the defensive parser for the model's reply, because
    the shape of the expected output and the code that trusts it belong together.

What this module deliberately does NOT do:
    - It never writes or paraphrases the clip content. The whole point of the vault is
      that a real person's real recorded voice is played (PRD: never simulate presence).
      The model's role is selection and one line of caption — nothing is synthesised.
    - It never invents a clip id. `parse_selection` validates the returned id against
      the clips that were actually offered and rejects anything else.
    - It never decides whether a clip should play at all; triage does.
    - No network access, no `genai` import.
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.models import UserProfile, VaultClip

SYSTEM = """\
You choose which recorded voice message to play for someone right now. Each message was
recorded by a real person who cares about them — a mother, a sponsor, a friend. You are
only choosing between real recordings. You never write, rewrite, or imagine what any
recording says.

ABSOLUTE RULES:
1. Never diagnose and never name a condition.
2. Never claim to be a human, and never imply the recorded person is live on the line.
   Your reason must not suggest the speaker is present, listening, or responding now.
3. Never state legal information or any number, statistic, or dose.
4. Never mention risk levels, tiers, scores, or system state.
5. Choose only from the clip ids given to you. Never invent an id, never return more
   than one, never return an id you were not shown.

HOW TO CHOOSE
- Pick the clip whose actual words fit what is happening right now, not the clip that
  sounds nicest in isolation.
- In a frightening or physical moment, favour a voice that is steady and simply present
  over one that is emotional, hopeful, or asks anything of the listener.
- In a low, lonely, or craving moment, favour a voice that expresses plain attachment
  over one that gives instruction.
- Avoid any clip whose words could read as disappointment, pressure, or a reminder of
  what the person owes someone. Guilt is the wrong medicine tonight.
- If nothing fits well, still choose the least wrong clip and say so honestly in the
  reason. There is always a clip playing; refusing to choose is not an option.

OUTPUT — JSON only, nothing else
Return exactly one JSON object, no code fence, no prose before or after:
{{"clip_id": "<one id from the list>", "reason": "<one sentence>"}}
The reason is one plain sentence, under 25 words, written to be shown to the person
hearing the clip. Say who recorded it and why it fits right now. No markdown, no emoji.
"""

USER = """\
Person's first name: {name}
What is happening right now: {context}

Available recordings:
{clips}

Return the JSON object now.
"""


def build(
    clips: list[VaultClip],
    context: str = "",
    profile: UserProfile | None = None,
) -> tuple[str, str]:
    """Build the vault-selection prompt.

    Args:
        clips: The clips actually available to play. Must be non-empty — the caller is
            responsible for the "no clips recorded" case, which is a UI state, not a
            generation.
        context: Short description of the current moment (see below).
        profile: The person who will hear the clip. Optional, because clip selection is
            driven by the transcripts and the moment, not by who is listening — the
            vault route serves whoever is at the device. When present, only the first
            name is used; address and entry details are deliberately withheld here.
    Note:
        `context` is supplied by the caller from deterministic state and must never be
        a tier name (see rule 4 in the system prompt).

    Returns:
        (system, user) prompt strings.

    Raises:
        ValueError: if `clips` is empty. Raising here is deliberate: silently prompting
            with an empty list would invite the model to invent an id.
    """
    if not clips:
        raise ValueError("vault_select.build requires at least one clip")

    # Transcripts are sent verbatim and clearly delimited: the selection quality is
    # entirely a function of the model seeing what each recording actually says.
    rendered = "\n".join(
        f'- id: {c.id} | recorded by: {c.recorded_by} ({c.relation})'
        f'{" | tags: " + ", ".join(c.tags) if c.tags else ""}\n'
        f'  says: "{c.transcript.strip()}"'
        for c in clips
    )

    return (
        SYSTEM,
        USER.format(
            name=(profile.name.split(" ")[0] if profile and profile.name else "unknown"),
            context=context.strip() or "A hard moment. No further detail available.",
            clips=rendered,
        ),
    )


# Matches the first {...} block in a reply. Models routinely wrap JSON in prose or a
# ```json fence despite instructions, so we extract rather than trusting the envelope.
_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def parse_selection(raw: str, clips: list[VaultClip]) -> tuple[VaultClip, str, bool]:
    """Parse the model's JSON reply defensively and resolve it to a real clip.

    This is the trust boundary for this feature. Everything the model returns is
    treated as untrusted text: it may be fenced, prefixed with prose, truncated, valid
    JSON of the wrong shape, or name a clip id that does not exist.

    Args:
        raw: The raw model output.
        clips: The clips that were offered. The first is used as the fallback choice,
            so callers should pass them in a sensible default order.

    Returns:
        (clip, reason, parsed_cleanly) where `parsed_cleanly` is False whenever we had
        to fall back for any reason. Callers surface that on the `Generation` so a
        salvaged parse is never presented as a clean model selection.

    Raises:
        ValueError: if `clips` is empty — there is nothing to fall back to.
    """
    if not clips:
        raise ValueError("parse_selection requires at least one clip")

    by_id = {c.id: c for c in clips}
    # Fallback reason is intentionally honest and playable: the user still hears a real
    # recording, and the caption does not pretend a selection rationale exists.
    fallback = (clips[0], f"A message from {clips[0].recorded_by}.", False)

    match = _JSON_BLOCK.search(raw or "")
    if not match:
        return fallback

    try:
        data: Any = json.loads(match.group(0))
    except json.JSONDecodeError:
        return fallback

    if not isinstance(data, dict):
        return fallback

    clip_id = data.get("clip_id")
    # A hallucinated id is the failure mode that matters most here: it would 404 or,
    # worse, play nothing. Validate against what we actually offered.
    if not isinstance(clip_id, str) or clip_id not in by_id:
        return fallback

    reason = data.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        # Valid clip, unusable reason: keep the good half of the answer, flag the parse.
        return (by_id[clip_id], f"A message from {by_id[clip_id].recorded_by}.", False)

    # Bound the caption length; an overlong "one sentence" is a prompt failure and the
    # UI has a fixed space for it.
    return (by_id[clip_id], reason.strip()[:240], True)
