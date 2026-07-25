"""Caregiver brief — the 3am situation summary for a terrified family member.

What this module does:
    Turns deterministic ladder state plus the event log into a short, calm briefing:
    what happened, what the system already did automatically, what to do in the next
    sixty seconds, and what NOT to do.

Why the "what NOT to do" section exists:
    This is CRAFT-grounded (Community Reinforcement and Family Training). The reflexive
    caregiver response — confront, accuse, threaten, demand promises, drive over and
    force a conversation — reliably makes the person hide harder next time, and hiding
    is the mechanism that kills. The brief therefore spends real space telling a
    frightened person what to hold back, not just what to do.

What this module deliberately does NOT do:
    - It never lets the model infer, restate, or second-guess the tier. Tier and reason
      are passed in as already-decided facts from `app/triage.py`
      (PRD P4 / CONTRACT.md: the model never decides a tier).
    - It never generates legal or Good Samaritan content; that is a static reviewed
      dataset in `data/legal/` (CONTRACT.md — the worst possible hallucination here).
    - It never produces medical instruction beyond "call 911" and the naloxone/rescue
      -breathing steps the app is already showing on its own screens.
    - No network access, no `genai` import.

What this prompt sends to the provider (data minimisation, audited):
    This is the brief with the widest possible blast radius — it is *about* a person and
    it is read by someone else — so the field list is deliberately short and is stated
    here so a reviewer can check it against `build()` in one read.

    SENT:
      - First name only.
      - Naloxone-in-the-home: yes / no-or-unknown. Sent because the "next 60 seconds"
        section is materially different when there is naloxone in the house.
      - The already-decided tier name, and the deterministic reason string.
      - For each retained event: time of day (HH:MM), tier name, reason text.
      - A de-duplicated list of recorded action kinds.

    NOT SENT, and each for a reason:
      - `profile.substances` — the system prompt forbids naming a condition or
        dependency status, so the model has no legitimate use for it. It is the single
        most sensitive field on the record and it was previously transmitted on every
        brief for nothing.
      - `profile.address`, `unit`, `entry_code`, `cross_street` — location belongs only
        in the 911 script. A caregiver reading this brief already knows where the person
        lives.
      - `profile.contacts` — third parties who never consented to appear in a prompt.
      - `profile.tolerance_events` — clinical history the brief does not reference.
      - `profile.ladder` — configuration, not situation.
      - `Event.id`, `Event.user_id`, full dates — internal identifiers with no bearing on
        the prose. Only the clock time is sent, because "3:12" is what makes the brief
        read as tonight.
      - Surname — the brief is read by family; a legal name reads as institutional.

    Token cost is the secondary benefit, not the primary one. Fewer fields is less
    personal data leaving the machine, which is the point.
"""

from __future__ import annotations

from app.models import Event, Tier, TIER_NAMES, UserProfile

SYSTEM = """\
You write a briefing that a frightened family member reads on their phone in the middle
of the night, seconds after an alert woke them. Assume they are half asleep, breathing
fast, and about to do something impulsive.

ABSOLUTE RULES — these override everything else:
1. Never diagnose, and never name a condition or dependency status.
2. Never claim to be a human or imply anyone is live on the line.
3. Never state legal rights, statutes, immunity, or what police will do. The app shows
   reviewed legal information separately; you must not touch that subject.
4. Never state any number: no doses, no statistics, no probabilities, no risk figures.
5. Never state or guess a risk level, tier, score, or stage, and never use the words
   "tier" or "level". The status line you are given is already decided by the system;
   describe the situation in plain words instead of grading it.
6. Never tell the caregiver a medical procedure beyond calling 911. The app itself
   walks anyone present through naloxone and rescue breathing on its own screen.

VOICE: plain, level, kind. Short sentences. No markdown, no bullets, no emoji, no
headings other than the four required labels below. Do not perform calm at them — just
be ordered. Order is what reduces panic.

OUTPUT — exactly four labelled sections, in this order, each label on its own line:

WHAT HAPPENED
Two or three sentences, factual, drawn only from the status and events given to you.
State plainly what was detected and when. Do not speculate about cause, intent, or
what the person was thinking. If a detail is not in the events, it did not happen.

WHAT THE SYSTEM ALREADY DID
Two or three sentences listing only actions recorded in the events. This section is the
antidote to panic: it tells the caregiver that automatic steps already ran and that they
are not the only line of defence. Never claim an action that is not in the record.

NEXT 60 SECONDS
Three or four short imperative sentences, most urgent first. If there is any sign the
person is not responding or not breathing normally, the first sentence is to call 911
now. Otherwise favour: make contact in the lowest-pressure way available, keep the line
open, and get to them or get someone closer to them there. One action per sentence.

WHAT NOT TO DO RIGHT NOW
Three or four short sentences. This is the most important section and it must be
specific to tonight, not generic advice. Draw from these, choosing what fits:
do not accuse, interrogate, or ask what they took as a demand; do not threaten
consequences, ultimatums, or removing anything; do not extract a promise; do not
call repeatedly or send a wall of messages; do not arrive angry; do not bring up
money, past incidents, or other people's disappointment. Give the reason in half a
sentence each — the reason is always the same and always worth saying: pressure now
makes them hide next time, and hidden use is the dangerous kind.
Close this section with one sentence reminding them that being reachable tonight
matters more than being right tonight.

LENGTH: 150 to 220 words total across all four sections.
"""

USER = """\
Person: {name}
Current status (already decided by the system — restate in plain words, never grade it):
{status}
Why the system says that: {reason}
Naloxone in the home: {naloxone}

Event log, oldest first (time — what the system saw):
{events}

Actions the system recorded taking:
{actions}

Write the four sections now.
"""


# Trailing events sent to the model. Lowered from 12 as part of the prompt-token work
# (g_s.md Efficiency #3). A caregiver at 3am needs tonight's shape, not a session
# history: past eight transitions covers the escalation that woke them with room to
# spare, and every additional line is latency on a brief someone is refreshing.
_DEFAULT_MAX_EVENTS = 8

# Ceiling on a single event's reason string. Reasons are written by `app/triage.py` and
# are normally one short clause, so this only ever bites on a pathological value; it is
# here so one bad row cannot dominate the prompt.
_MAX_REASON_CHARS = 120


def _compress(events: list[Event]) -> str:
    """Render the event tail as the shortest lines that still carry the situation.

    Three compressions, each with a reason:

    1. **Consecutive repeats are collapsed.** Silence and stillness sensors fire
       repeatedly with an identical tier and reason. Sending the same line six times
       costs six times the tokens and actively misleads the model, which reads
       repetition as emphasis and writes "this happened again and again". One line with
       a time span is both cheaper and more accurate.
    2. **Only the clock time is sent.** The date, the event id, and the user id are
       dropped — none of them can appear in the output, and the id fields are internal
       identifiers with no business leaving the machine.
    3. **The trigger source is a bare tag** rather than "(source: sensor)". The model
       needs to distinguish a sensor reading from something the person said; it does not
       need the prose scaffolding around it.

    Args:
        events: Already-trimmed event tail, oldest first.

    Returns:
        A newline-joined block, or a placeholder line when there are no events. Never
        an empty string — an empty slot in the prompt template reads as a formatting
        bug to the model and invites it to fill the gap.
    """
    if not events:
        return "- (no events recorded)"

    # Each run is [start_time, end_time, status, reason, source]. `end_time` is rewritten
    # in place as identical events keep arriving, so a run of ten sensor readings
    # collapses to a single "03:12-03:19" line.
    runs: list[list[str]] = []
    for event in events:
        status = TIER_NAMES.get(event.tier, str(event.tier))
        reason = (event.reason or "").strip()[:_MAX_REASON_CHARS]
        at = f"{event.at:%H:%M}"

        if runs and runs[-1][2:] == [status, reason, event.trigger_source]:
            runs[-1][1] = at
            continue
        runs.append([at, at, status, reason, event.trigger_source])

    return "\n".join(
        f"- {start if start == end else f'{start}-{end}'} {status} — {reason} [{source}]"
        for start, end, status, reason, source in runs
    )


def build(
    profile: UserProfile,
    tier: Tier,
    events: list[Event],
    reason: str = "",
    max_events: int = _DEFAULT_MAX_EVENTS,
) -> tuple[str, str]:
    """Build the caregiver situation-brief prompt.

    Args:
        profile: The person the brief is about.
        tier: Current tier, already decided by `app/triage.py`. Passed in as a *fact*;
            the prompt forbids the model from reasoning about or naming it.
        events: Append-only event log. Only the tail is sent — see `max_events`.
        reason: The auditable, human-written reason string from `TriageResult`. Never
            model-written (CONTRACT.md), so it is safe to quote into the prompt. When
            omitted it is recovered from the most recent event, which carries the same
            reason string — so the caller is not forced to thread it through.
        max_events: How many trailing events to include. Bounded on purpose: a
            caregiver at 3am needs tonight, and an unbounded log would both blow the
            latency budget and let old, resolved incidents contaminate the summary.

    Returns:
        (system, user) prompt strings. See the module docstring for the audited list of
        exactly which profile and event fields are transmitted.
    """
    tail = events[-max_events:]

    # Recover the reason from the log rather than inventing one. Everything the model
    # is told about *why* must trace to a deterministic, auditable source.
    if not reason:
        reason = tail[-1].reason if tail else "(no reason recorded)"

    # Flat lines rather than JSON: the model copies structure it is shown, and
    # JSON-shaped input reliably produces JSON-shaped prose output here. `_compress`
    # additionally collapses repeated sensor readings — see its docstring.
    rendered = _compress(tail)

    # Actions are de-duplicated and flattened so the model cannot pad the
    # "what the system already did" section by repeating one action several times.
    seen: list[str] = []
    for e in tail:
        for a in e.actions_taken:
            if a not in seen:
                seen.append(a)
    actions = "\n".join(f"- {a}" for a in seen) or "- (none recorded)"

    return (
        SYSTEM,
        USER.format(
            # First name only. The surname adds nothing the brief can use and reads as
            # institutional in prose meant to sound like a person.
            name=(profile.name.split(" ")[0] if profile.name else "unknown"),
            status=TIER_NAMES.get(tier, str(tier)),
            reason=reason,
            naloxone="yes" if profile.naloxone_on_hand else "no or unknown",
            # `profile.substances` is deliberately NOT passed. The system prompt forbids
            # naming a condition or dependency status, so the model cannot legitimately
            # use it — and it is the most sensitive field on the record.
            events=rendered,
            actions=actions,
        ),
    )
