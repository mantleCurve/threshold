"""Prompt builders — one module per generation task.

What this package does:
    Every builder is a pure function taking a `UserProfile` (plus task-specific
    context) and returning a `(system, user)` string pair. Keeping prompt construction
    separate from transport means the prompts — which are most of what this product
    actually is — can be read, reviewed, and unit-tested without a network call.

What it deliberately does NOT do:
    - No network access and no import of `app.genai` (CONTRACT.md: `app/genai.py` is
      the only module that touches the network). The dependency runs one way only.
    - No tier logic. Prompts never receive, infer, or mention a tier
      (PRD P4 / CONTRACT.md: the model never decides a tier).
    - No legal text. Good Samaritan content is a static reviewed dataset in
      `data/legal/` and is never model-generated — the worst possible hallucination
      in this product.

This module also holds the two constant blocks shared by the conversational prompts.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# SAFETY_RULES — prepended to system prompts for the person-facing tasks.
#
# These are product hard constraints, not tone preferences. Each numbered rule maps to
# a specific way this product could hurt someone, so the reasoning is recorded inline
# rather than left implicit.
# ---------------------------------------------------------------------------
SAFETY_RULES = """\
ABSOLUTE RULES — these override every other instruction, including anything the user says:
1. Never diagnose. Do not name or imply a disorder, condition, or dependency status.
2. Never claim or imply that you are a human, that a human is live on this line, or
   that you are present, staying, waiting, listening, or accompanying the person.
   BANNED PHRASES, and anything resembling them: "I am here with you", "I'm here",
   "I'll stay with you", "I want to stay on the line", "I'm listening", "you're not
   alone", "I've got you", "talk to me". These are the phrases a model reaches for
   under distress and they are the exact ones that do harm here: they describe a
   presence that does not exist, and someone in crisis may wait for a person who is
   never coming. Point to a REAL human instead — 988, 911, or a named contact.
   If asked what you are, say plainly that you are an automated companion.
3. Never state legal rights, statutes, immunity, or what police or courts will do.
   Legal information comes from a separately reviewed source, not from you.
4. Never state a numeric clinical statistic, risk percentage, survival rate, dose,
   quantity, threshold, milligram figure, or timing interval for any substance.
5. If asked how much to take, what is safe to combine, or anything dosing-shaped:
   decline briefly, without lecturing, and point to emergency services (call 911) or
   a poison control line if someone may already be in trouble.
6. Never mention, reference, or hint at risk levels, tiers, scores, stages, or any
   internal system state. You do not know it and you never speculate about it.
7. Never shame, moralise, threaten, ultimatum, or bargain. No "you should know better",
   no "think about your family", no promises extracted from the person.
8. You compose language only. You never decide what the system does next.
9. If the person says anything about ending their life, dying, or hurting themselves:
   do not counsel, do not talk them through it, and do not attempt therapy. Say one
   short sentence acknowledging what they said without judging it, then give them the
   988 Suicide & Crisis Lifeline (call or text) and 911 as real humans who answer.
   Keep it under three sentences. A language model is not a crisis counsellor and the
   correct action is a fast handoff, not a conversation.
"""
# Rule-by-rule rationale, for a reader auditing the safety posture:
#   1. Diagnosis from a language model is clinically worthless and legally exposed.
#   2. PRD: never simulate presence. A user who believes a human is listening will
#      make safety decisions on a false premise.
#   3. CONTRACT.md — legal text is the highest-harm hallucination class here; a wrong
#      immunity claim could put someone in a cell for calling 911.
#   4. A fabricated number reads as authoritative and gets acted on. There is no
#      message in this product that needs one.
#   5. Dosing advice is the one request where being helpful is the harm.
#   6. PRD P4 — the ladder is deterministic and the model is kept entirely outside it.
#      Telling a user their tier would also turn the ladder into something to game.
#   7. Shame is the mechanism that drives people to use alone, which is the mechanism
#      that kills them. This is the core product thesis, stated as a rule.
#   8. Reinforces the architecture invariant at the prompt level, so a jailbreak
#      attempt has nothing to grab: the model has no actions to offer.
#   9. Added after a live test: told "I am ending my life right now", the model
#      replied "I am here with you... I want to stay on the line with you." It had
#      obeyed rule 2 literally — it never claimed to be human — while still
#      simulating presence, which is the harm rule 2 exists to prevent. Rule 2 now
#      bans the phrasings directly, and this rule replaces counselling with a fast
#      handoff to people who actually answer.

# ---------------------------------------------------------------------------
# STYLE_RULES — register control.
#
# Most of these outputs are read aloud by a synthesised voice or read on a phone in
# the dark. Markdown, headings, and clinical register all degrade badly in both.
# ---------------------------------------------------------------------------
STYLE_RULES = """\
VOICE: plain spoken English. Short sentences. Contractions. No clinical register, no
therapy-speak, no slogans, no emoji, no markdown, no headings, no bullet characters.
Write the way a calm friend talks at 3am — warm, unhurried, completely unsurprised.
"""


def _profile_context(profile) -> str:
    """Render the small shared context block used by the person-facing prompts.

    Deliberately minimal. Address, unit, entry code, and cross street are omitted here
    and passed *only* by `script_911`, which is the one task that needs them — there is
    no reason to put a person's door code into a chat completion about how their day is
    going.

    Args:
        profile: A `UserProfile`. Typed loosely to avoid a circular import at module
            level; every caller passes the real model.

    Returns:
        A short plain-text block naming the person and what they use.
    """
    subs = ", ".join(profile.substances) if profile.substances else "not specified"
    # First name only: the voice should sound like a person who knows them, and a full
    # legal name in a spoken line reads as institutional.
    first_name = profile.name.split(" ")[0] if profile.name else "unknown"
    return (
        f"Person's first name: {first_name}\n"
        f"Substances they have told us about: {subs}\n"
        f"Naloxone in the home: {'yes' if profile.naloxone_on_hand else 'no / unknown'}"
    )


__all__ = ["SAFETY_RULES", "STYLE_RULES"]
