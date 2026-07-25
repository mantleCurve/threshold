"""Deterministic escalation-ladder state machine — the safety-critical core.

## What this module does

Takes the user's current tier plus fresh evidence (an utterance, a silence
timer, a stillness flag, missed check-ins, the profile) and returns exactly one
`TriageResult`: the new tier, the actions due with their timings, an auditable
reason string, and whether the caregiver is notified. It is the only thing in
Threshold allowed to decide a tier.

## What this module deliberately does NOT do

* **No network, no `genai` import, no I/O of any kind.** CONTRACT.md
  "Architecture invariants": `app/genai.py` is the only module that touches the
  network. Triage must return an answer when the API key is missing, the
  network is down, or the model is refusing — an overdose does not wait for a
  200 OK.
* **No wall-clock reads.** `now` is injected. Every time-dependent branch (the
  tolerance-reset window) is therefore reproducible in a test, and no result
  can vary between two calls with identical inputs.
* **No randomness, no ML, no scoring, no thresholds learned from data.** PRD P4:
  the model never decides a tier. The model does language work only — composing
  and selecting words *after* this module has already decided what tier the user
  is on.
* **No model-written prose.** `reason` is rendered in the UI and read by a
  caregiver deciding whether to drive over. Every reason string is assembled
  from literals in this file, so the ladder is auditable line by line.
* **No persistence and no side effects.** Writing the `Event` log, pushing SSE,
  and firing the contact tree belong to the caller. Triage only says what
  should happen; keeping it side-effect-free is what makes it safe to call
  speculatively (e.g. to preview a tier on the `/ladder` screen).

## What the phrase matcher is, and what it is not

`SIGNALS` is keyword triage, not natural-language understanding. It is a flat,
inspectable table of regular expressions, each with a tier and a human-readable
label, rendered verbatim on the `/ladder` screen so a user can see exactly what
the app listens for. That transparency is the point, and it is a deliberate P4
choice: a user in withdrawal deserves to know precisely what will summon their
mother, and a classifier whose behaviour cannot be enumerated cannot give them
that.

Honest limits of this approach:

* **Recall is bad.** Slang, regional usage, typos, misspellings, emoji, and
  languages other than English are missed. "gonna get well", "fixin to",
  "I'm sick" are all real relapse language this table does not catch.
* **Sarcasm, quotation, and hypotheticals fire it.** "My sponsor asked if I
  wanted to use" escalates. So does reading a message aloud.
* **Negation handling is shallow.** `_is_negated` scans a short window of words
  inside the same clause. It catches "I don't want to use" and "I'm not
  craving". It will not catch "there's no world in which I use tonight".
* **Word boundaries, not substrings.** "used to" never matches "I used";
  "helper" never matches "help". Those two false positives are specifically
  tested.

The mitigations that make this acceptable are structural, not linguistic:
escalation is graduated rather than binary, every escalation is user-visible
(PRD §11: no hidden log), and one tap rescinds a false alarm (`rescind`). A
missed signal is recovered by the silence and stillness paths, which need no
language at all — which is the point, because the failure mode that kills
people is a user who has stopped talking.

## Precedence

Exactly one tier comes out, resolved in this fixed order. The order encodes a
clinical bias: physiological evidence (not talking, not moving) outranks
anything the user says, because an opioid overdose silences the user before it
kills them.

1. Stillness + silence at Tier 3+   -> Tier 5
2. Silence at Tier 3+               -> Tier 4
3. Emergency / use / craving phrase -> Tier 4 / 3 / 2
4. Standing conditions              -> Tier 1
5. Calm-state utterance             -> one tier down, never below (4)
6. Otherwise hold the current tier.

Tiers never fall silently. They fall only through `rescind` (an explicit user
tap) or a calm-state utterance, and Tiers 4 and 5 never auto-de-escalate at all
— an unresponsive person cannot say "I'm fine", so a de-escalation from Tier 4
would be evidence about someone who by definition cannot supply it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from app.models import (
    Action,
    LadderConfig,
    Tier,
    TriageResult,
    UserProfile,
)

__all__ = [
    "SIGNALS",
    "Signal",
    "SignalMatch",
    "TOLERANCE_WINDOW_DAYS",
    "actions_for_tier",
    "evaluate",
    "match_signals",
    "notify_caregiver_for",
    "rescind",
    "tolerance_window_active",
]

# PRD: opioid tolerance collapses within days of stopping, but the elevated
# fatal-overdose risk persists for roughly three months after the interruption
# (detox, hospital discharge, release from custody, a stretch of abstinence) —
# the danger is that the user's *memory* of their dose outlives their body's
# tolerance for it. 90 days is the window the PRD specifies; it is a single
# named constant so a clinician can change it in one place.
TOLERANCE_WINDOW_DAYS = 90

# Signal labels that indicate intent to die or self-harm rather than a purely
# physical medical emergency. They share Tier 4 — and therefore share its
# non-overridable caregiver notification — but the response leads with the
# crisis line rather than with naloxone.
_CRISIS_SIGNAL_LABELS = frozenset(
    {"suicidal intent", "self-harm intent", "intentional overdose"}
)

# Used when no profile is supplied (onboarding, the bystander surface, tests).
# Deliberately the same defaults as models.LadderConfig rather than a stricter
# local copy: two sources of truth for a safety threshold is how they drift.
DEFAULT_LADDER = LadderConfig()

# Words that flip the meaning of a following signal phrase. Apostrophes are
# stripped before lookup, so "don't" and "dont" both land on "dont".
NEGATION_CUES = frozenset(
    {
        "not",
        "no",
        "never",
        "dont",
        "doesnt",
        "didnt",
        "wont",
        "wouldnt",
        "aint",
        "cant",
        "cannot",
        "couldnt",
        "havent",
        "hasnt",
        "isnt",
        "wasnt",
        "stopped",
        "quit",
        "avoid",
        "avoided",
        "resisted",
        "without",
    }
)

# How many words before a match are scanned for a negation cue. Kept short on
# purpose: a long window turns "I'm not going to lie, I used" into a miss, and
# in a safety system a missed disclosure costs more than a false alarm the user
# can rescind with one tap.
NEGATION_WINDOW_WORDS = 4

# Clause separators. Negation does not carry across them, so "I didn't sleep,
# I used" still escalates — the "didn't" belongs to the first clause only.
_CLAUSE_SPLIT = re.compile(r"[,.;:!?\n]| but | and then | though | however ")

# Phone keyboards and voice transcription emit curly apostrophes; the patterns
# are written with straight ones. Normalising here keeps every pattern in the
# table readable instead of littering each with a [''] character class.
_APOSTROPHES = str.maketrans({"’": "'", "ʼ": "'", "‘": "'", "`": "'"})


# ---------------------------------------------------------------------------
# Context suppressors
# ---------------------------------------------------------------------------
# Enumerating innocuous completions inside each pattern does not converge: the
# "took" row already excluded trash, laundry, dishes and bins, and "recycling"
# still slipped through. These are the three CONTEXTS in which a matched phrase
# is systematically not a disclosure, checked once against the whole utterance
# rather than bolted onto every row.
#
# Each is scoped narrowly on purpose. A suppressor that is too broad silently
# disables a safety signal, which is the more dangerous failure — so these match
# explicit framing only, never mere sentiment.

# Reported as a past event, not a present one. "Ten years ago I overdosed" is
# autobiography; escalating on it teaches people not to tell you their history.
_PAST_TENSE = re.compile(
    r"\b(years?|months?|weeks?|days?)\s+ago\b"
    r"|\b(last|previous)\s+(year|month|week|night|time|summer|winter)\b"
    r"|\bback\s+(then|in\s+\d{4}|in\s+the\s+day)\b"
    r"|\bwhen\s+i\s+was\s+(a\s+)?(kid|young|\d+)\b"
    r"|\bused\s+to\b|\bin\s+\d{4}\b|\bhistory\s+of\b",
    re.IGNORECASE,
)

# Hypothetical or quoted rather than asserted.
_HYPOTHETICAL = re.compile(
    r"\bwhat\s+if\b|\bif\s+i\s+(ever|were|was)\b|\bhypothetically\b"
    r"|\bthe\s+(song|lyric|film|movie|book|line)\s+(goes|says)\b"
    r"|\b(he|she|they)\s+said\s+[\"']",
    re.IGNORECASE,
)

# Figurative intensifiers. "Dying of laughter", "gasping at the price".
_FIGURATIVE = re.compile(
    r"\bdying\s+(of|for|to)\s+\w"
    r"|\bgasping\s+at\b"
    r"|\bnodding\s+(along|in\s+agreement|off\s+to\s+sleep)\b"
    r"|\bblue\s+(from|with)\s+\w"
    r"|\b(paint|painted|dye|dyed|ink)\w*\b.*\b(blue|grey|gray|purple)\b"
    r"|\b(blue|grey|gray|purple)\b.*\b(paint|shirt|nails?|dress|wall|car|sky)\b"
    r"|\bwent\s+grey\s+years\b"
    r"|\bchoking\s+up\b|\brattling\s+(around|on)\b",
    re.IGNORECASE,
)


def _is_suppressed(text: str) -> str | None:
    """Return the suppressing context, or None if the utterance stands.

    Deliberately checked AFTER a signal matches rather than before: the cost of
    running three regexes on a matched utterance is nothing, and keeping the
    signal table readable matters more.
    """
    if _PAST_TENSE.search(text):
        return "reported as a past event"
    if _HYPOTHETICAL.search(text):
        return "hypothetical or quoted"
    if _FIGURATIVE.search(text):
        return "figurative usage"
    return None


@dataclass
class Signal:
    """One row of the phrase table. Rendered verbatim on the `/ladder` screen.

    PRD P4 transparency: a user must be able to read the exact list of phrases
    that will summon their mother, so this is a plain data row, not a learned
    weight.

    Attributes:
        label: human-readable name, shown in the UI and embedded in `reason`.
        tier: the tier this phrase asserts, or None for a calm-state phrase
            that *permits* de-escalation (calm phrases never assert a tier —
            they only license a one-step drop).
        pattern: word-boundary-anchored regex, matched case-insensitively.
        negatable: whether a preceding negation cue suppresses the match. False
            for phrases that contain their own negation ("can't breathe", "not
            breathing"); suppressing those would silence the single most
            important sentence in the product.
        example: a canonical utterance, shown in the UI beside the label so the
            user sees a sentence rather than a regex.
        regex: compiled `pattern`. Compiled once at import; excluded from
            comparison so two Signals with the same pattern compare equal.
    """

    label: str
    tier: Tier | None
    pattern: str
    negatable: bool = True
    example: str = ""
    regex: re.Pattern[str] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        # Compile at construction (import time) so a malformed pattern fails
        # loudly on startup rather than mid-overdose on first use.
        self.regex = re.compile(self.pattern, re.IGNORECASE)


@dataclass(frozen=True)
class SignalMatch:
    """A signal that fired, with the exact substring that fired it.

    The substring is carried so `reason` can quote the user's own words back to
    them ('matched "I just took"') instead of asserting an opaque verdict. A
    user who disagrees with an escalation can see precisely what caused it.

    Attributes:
        signal: the table row that matched.
        text: the exact matched substring, lowercased and stripped.
        negated: reserved for callers that want to *display* suppressed matches
            (e.g. an explain-my-ladder view). `match_signals` never returns a
            negated match; it filters them out entirely.
    """

    signal: Signal
    text: str
    negated: bool = False


# --------------------------------------------------------------------------
# THE SIGNAL TABLE
#
# Ordered by tier, descending. `match_signals` preserves this order, so the
# first escalating match is always the most severe one — no sorting step, and
# no possibility of a Tier 2 phrase shadowing a Tier 4 phrase in the same
# sentence ("I want to use, I can't breathe" resolves to Tier 4).
#
# Every pattern is word-boundary anchored (\b). Nothing here is generated,
# learned, or loaded at runtime: the table a judge reads is the table that runs.
# --------------------------------------------------------------------------
SIGNALS: list[Signal] = [
    # ---- Tier 4: explicit medical distress -------------------------------
    # These are the sentences a person says while dying. They are matched
    # without negation handling wherever the phrase is itself a negation, and
    # they outrank every other row in the table.
    # ---- Tier 4: suicidal statements -------------------------------------
    #
    # THIS BLOCK EXISTS BECAUSE ITS ABSENCE WAS A LETHAL GAP.
    # Before it, "I am ending my life right now" triaged to Tier 0 and the
    # system did nothing whatsoever.
    #
    # Suicide and overdose are not separate populations. Overdose deaths are
    # frequently intentional or ambiguous in intent, and a person in withdrawal
    # or early recovery is at elevated risk of both. A product built around
    # overdose that stays silent when someone says they are going to die is not
    # a neutral omission — it is the single worst failure this table could have.
    #
    # These rows sit at Tier 4 alongside the medical emergencies, which means
    # they carry the same non-overridable caregiver notification (PRD §4.2).
    # That is deliberate and is disclosed at onboarding: the one thing the user
    # cannot switch off is us alerting someone when we believe they are in
    # danger. Intent to die is that.
    #
    # NEGATION IS DISABLED on the explicit-intent rows. "I don't want to kill
    # myself" and "I'm not going to hurt myself" are things people say while
    # in serious distress, and are frequently disclosures rather than denials.
    # In every other row in this table a false positive costs a rescind tap; in
    # this one a false negative costs a life, so the asymmetry is inverted on
    # purpose. The member can always stand it down with one press.
    #
    # The 988 Suicide & Crisis Lifeline is surfaced by the actions for this
    # tier — not 911 alone, because a police response to a mental-health crisis
    # carries its own well-documented risk of harm.
    Signal(
        label="suicidal intent",
        tier=Tier.EMERGENCY,
        pattern=(
            # THIRD PERSON IS NOT OPTIONAL HERE. The bystander surface — the
            # one outside the auth wall — is used by someone standing over
            # another person, so "he's going to kill himself" must escalate
            # exactly as "I am" does. Every other Tier 4 row was built this way;
            # these rows were added later and missed it.
            r"\b(kill|killing)\s+(myself|my\s?self|himself|herself|themselves|"
            r"themself|yourself)\b"
            r"|\bend(ing|ed)?\s+(my|his|her|their|your|it)\s+(life|all)\b"
            r"|\bend(ing)?\s+it\s+(tonight|now|today)\b"
            r"|\btak(e|ing|en)\s+(my|his|her|their)\s+own\s+life\b"
            # Indirect ideation — the phrasings that precede attempts, and the
            # ones people actually use. "I don't want to be here anymore"
            # matched while "I don't want to be alive" did not: a one-word
            # difference across a lethal boundary.
            r"|\bbetter\s+off\s+(dead|without\s+me)\b"
            r"|\bno\s+(point|reason)\s+(anymore|any\s?more|in\s+living|to\s+go\s+on)\b"
            r"|\btop\s+(myself|himself|herself)\b"
            r"|\bsaid\s+my\s+goodbyes\b|\bwritten\s+(letters|a\s+note)\b"
            r"|\bwant\s+it\s+to\s+(stop|end)\s+forever\b"
            r"|\bkms\b"
            r"|\bsuicid(e|al)\b"
            r"|\bnot\s+be\s+(here|alive)\s+(anymore|any\s?more|tomorrow)\b"
            r"|\bwant\s+to\s+die\b"
            r"|\bdon'?t\s+want\s+to\s+(live|be\s+here|be\s+alive|wake\s+up|"
            r"go\s+on|exist)\b"
        ),
        negatable=False,  # see the note above — deliberately not negatable
        example="I am ending my life right now",
    ),
    Signal(
        label="self-harm intent",
        tier=Tier.EMERGENCY,
        pattern=r"\b(hurt|harm|cut)\s+(myself|my\s?self|himself|herself|"
                r"themselves|themself)\b",
        negatable=False,
        example="I am going to hurt myself",
    ),
    Signal(
        label="intentional overdose",
        tier=Tier.EMERGENCY,
        # "I took all of them" is a description of an intentional overdose and
        # was previously landing at Tier 3 (disclosure of use), which is far
        # too low: it is an emergency in progress, not a relapse.
        pattern=(
            # Anchored to SUBSTANCES. An unanchored "took all/the whole"
            # matched "I took all the trash out", which is housework.
            r"\b(took|taken)\s+(all|every|the\s+whole|a\s+whole)\s+"
            r"(of\s+)?(the\s+|my\s+|his\s+|her\s+|their\s+|them|it)?"
            r"(pills?|bottle|script|bag|tabs?|dose|doses|meds|medication|"
            r"benzos?|xanax|oxys?|percs?|klonopin|methadone|subs?)\b"
            r"|\ball\s+(of\s+)?(the\s+|my\s+|his\s+|her\s+|their\s+)?"
            r"(pills|bottle|script|bag)\b"
        ),
        negatable=False,
        example="I took all the pills",
    ),
    Signal(
        label="cannot breathe",
        tier=Tier.EMERGENCY,
        pattern=r"\b(can'?t|cannot|can not|struggling to|hard to|trouble)\s+breath(e|ing)?\b",
        negatable=False,  # the phrase is itself a negation
        example="I can't breathe",
    ),
    Signal(
        label="not breathing",
        tier=Tier.EMERGENCY,
        # Contractions and progressive forms. "he isn't breathing" and "he's
        # barely breathing" both returned Tier 0 — the second is arguably the
        # more urgent, since it describes respiratory depression in progress.
        pattern=r"\b(not|no|stopped|stopping|barely|hardly|isn'?t|ain'?t|"
                r"wasn'?t|aren'?t)\s+breath(ing|e)\b"
                r"|\bbreathing\s+(problem|trouble|difficulty|issues?)\b",
        negatable=False,
        example="he's not breathing",
    ),
    # ---- Tier 4: how an overdose is actually described out loud ----------
    # Added after review: the original table caught the clinical phrasings but
    # missed the words people actually use, so "she won't wake up" and "he's
    # nodding out" — both descriptions of an overdose in progress — triaged to
    # Baseline. That is the exact sentence a frightened bystander says into a
    # phone, and it was returning nothing.
    #
    # These are all THIRD-PERSON capable on purpose: at Tier 4 the speaker is
    # frequently not the person in danger.
    Signal(
        label="unrousable",
        tier=Tier.EMERGENCY,
        pattern=(
            # "wake up" only counts when it is about rousing a person NOW.
            # "she won't wake up early for work" is a complaint about a
            # schedule; the trailing qualifier excludes it.
            r"\b(wo'?n'?t|will not|can'?t|cannot|not|isn'?t|ain'?t|aren'?t|"
            r"is\s+not)\s+wak(e|ing)\s+(up|him|her|them)\b"
            # The scheduling exclusion, restored — it was dropped when the
            # contraction forms were added. "She won't wake up early for work"
            # is a complaint about a schedule, not an unrousable person.
            r"(?!\s+(early|late|before|until|on|for|in|at|tomorrow|today|"
            r"till|any\s+earlier))"
            r"|\bout\s+cold\b|\bnot\s+getting\s+up\b|\bgone\s+under\b"
            r"(?!\s+(early|late|before|until|on|for|in|at|tomorrow|today))"
            r"|\bunresponsive\b|\bnot\s+respond(ing|s)?\b"
            # Present sense only: "he passed out" / "passing out" is happening.
            # "I passed out at the party last year" is a story being told, and
            # escalating on it is the over-alerting that teaches people to stop
            # talking to the app (PRD §12).
            r"|\bpass(ed|ing)\s+out\b(?!.*\b(last|ago|years?|months?|weeks?|"
            r"when i was|back then|once)\b)"
            r"|\bunconscious\b"
        ),
        negatable=False,   # the phrase is itself a negation
        example="she won't wake up",
    ),
    Signal(
        label="nodding out",
        tier=Tier.EMERGENCY,
        # "Nodding" is the street description of opioid-induced sedation, and
        # the step before respiratory arrest. Bounded to the phrasal forms so
        # it cannot fire on nodding one's head in agreement.
        pattern=r"\bnodd?(ing|ed)\s+(out|off)\b|\bnodding\b(?=\s|$)",
        example="he's nodding out",
    ),
    Signal(
        label="cyanosis",
        tier=Tier.EMERGENCY,
        # Blue or grey lips and skin mean oxygen has already been low for some
        # time. This is a late sign and must never be filed below emergency.
        pattern=(
            # Anchored to a BODY PART in both halves. The original matched any
            # subject turning blue, so "the sky turned blue" dispatched an
            # ambulance. Cyanosis is only meaningful on a person.
            r"\b(lips?|face|skin|fingers?|nails?|hands?|they|he|she)"
            r"(?:'?s|'?re)?\s+"
            r"(is|are|was|were|look(s|ed|ing)?|went|gone|turn(ed|ing)?|going)?\s*"
            r"(blue|grey|gray|purple|ashen)\b"
        ),
        example="his lips are blue",
    ),
    Signal(
        label="agonal breathing",
        tier=Tier.EMERGENCY,
        # Gurgling, snoring, or gasping in someone who cannot be roused is
        # agonal respiration — commonly mistaken for sleep, which is precisely
        # why it belongs in this table.
        # "choking" excludes the ordinary sense — choking ON something is a
        # different (and usually self-resolving) event from agonal respiration.
        pattern=r"\b(gurgl(e|ing)|snor(e|ing)\s+(weird|strange|funny|loudly)|"
                r"gasping|rattling)\b"
                # The original guard excluded every "choking on X" — but
                # "choking on vomit" is aspiration during an overdose, which is
                # exactly the case that matters. Only the innocuous objects are
                # excluded now.
                r"|\bchoking\b(?!\s+on\s+(my|a|the|his|her)?\s*"
                r"(drink|water|food|coffee|tea|laughter|it)\b)",
        example="he's gurgling",
    ),
    Signal(
        label="overdose disclosure",
        tier=Tier.EMERGENCY,
        pattern=r"\b(overdosing|overdosed|od'?(ing|d|\'d)|took too much|took too many|"
                r"taken too (much|many)|"
                r"too much of it)\b",
        example="I think I'm overdosing",
    ),
    Signal(
        label="call for help",
        tier=Tier.EMERGENCY,
        # WHITELIST, not a blacklist.
        #
        # Two rounds of exclusion lists failed here: enumerating the innocuous
        # completions of "help me ..." never terminated ("help me pack" slipped
        # a twelve-verb list), and the nested lookahead that replaced it
        # suppressed "help me please", which is a genuine cry.
        #
        # The shape of the distinction is simple and finite in the other
        # direction: an urgent cry for help takes NO object. So the cry forms
        # are listed exhaustively and anchored to the whole clause. Anything
        # with a task attached — "help me move house", "can you help me?" —
        # simply is not on the list and cannot match.
        #
        # Listing what DOES fire also means a reader of /ladder can see the
        # complete set, which is the transparency this table exists for.
        pattern=(
            r"^\s*(please\s+)?(somebody|someone|anybody|anyone)?\s*"
            r"help"
            r"(\s+(me|us))?"
            r"(\s+(please|now|quick|quickly))?"
            r"\s*[!.?]*\s*$"
            r"|\bi\s+need\s+help\b"
            r"|\bneed\s+help\s+(now|please|here)\b"
            r"|\bhelp\s+(me|us)\s+(please|now)\b"
        ),
        example="help",
    ),
    Signal(
        label="dying",
        tier=Tier.EMERGENCY,
        # "dying to <verb>" is an idiom for eagerness, not a disclosure —
        # "I'm dying to see her" was reaching Tier 4. The lookahead excludes
        # the infinitive form while leaving every genuine use intact.
        pattern=r"\b(i'?m dying|i think i'?m dying)\b(?!\s+to\s+\w)"
                r"|\b(going to die|gonna die)\b",
        example="I think I'm dying",
    ),
    Signal(
        label="physical collapse signs",
        tier=Tier.EMERGENCY,
        pattern=r"\b(turning blue|lips are blue|chest pain|can'?t move|"
        r"can'?t stay awake|passing out|blacking out|seizing|convulsing)\b",
        negatable=False,
        example="my lips are blue",
    ),
    # ---- Tier 3: disclosure of use ---------------------------------------
    # A user who discloses use is not in trouble yet — they are in the window
    # where the app's job is to stay on the line and pre-arm the bystander
    # path. PRD §4.2: Tier 3 is the highest tier the user is allowed to hide
    # from a caregiver, because a user who fears surveillance uses alone, and
    # using alone is the actual cause of death.
    Signal(
        label="disclosed use",
        # (?!\s+to\b) is the "used to" guard: "I used to use every day" is a
        # recovery story told in a support context, not a disclosure. This is
        # the single most likely false positive in the whole table and has a
        # dedicated test.
        tier=Tier.ACTIVE_USE,
        pattern=r"\bi\s+(just\s+|already\s+)?used\b(?!\s+to\b)",
        example="I used",
    ),
    Signal(
        label="disclosed dosing",
        tier=Tier.ACTIVE_USE,
        # The exclusion list covers the innocuous completions of "I took" /
        # "I did" that showed up in testing: "I took a walk", "I took my meds",
        # "I did a break". Prescribed medication is excluded on purpose — this
        # app must not treat adherence to a prescription as a relapse.
        # Extended after testing: "I took all the trash out" and "I took the
        # laundry upstairs" were both being logged as disclosed drug use. A
        # relapse falsely written into someone's permanent event log — which
        # their caregiver may read — is not a harmless false positive.
        pattern=r"\bi\s+(just\s+|already\s+)?(took|did|shot|slammed|snorted|smoked)\b"
        r"(?!\s+(to|a\s+(walk|break|nap|shower|bath|look|photo|picture|day|"
        r"class|cab|taxi|bus|train)|my\s+(meds|medication|pills|dog|kid|kids|"
        r"son|daughter|car|time)|the\s+(trash|bins?|rubbish|laundry|dishes|"
        r"dog|kids?|bus|train|stairs|day\s+off)|out\s+the|all\s+the\s+"
        r"(trash|rubbish|laundry|dishes|bins?))\b)",
        example="I just took it",
    ),
    Signal(
        label="currently high",
        tier=Tier.ACTIVE_USE,
        pattern=r"\bi'?m\s+(really\s+|so\s+|pretty\s+)?(high|loaded|nodding|on one)\b",
        example="I'm high",
    ),
    Signal(
        label="use in progress",
        tier=Tier.ACTIVE_USE,
        pattern=r"\b(i'?m using|using right now|about to use|"
        r"i relapsed|i picked up|got some and used)\b",
        example="I'm using right now",
    ),
    # ---- Tier 2: craving --------------------------------------------------
    # Craving is the tier where intervention is cheapest and most effective, so
    # the patterns here are the most permissive in the table. A false Tier 2 is
    # a grounding exercise the user can dismiss; a missed Tier 2 is the last
    # moment anyone could have interrupted the sequence.
    Signal(
        label="craving",
        tier=Tier.CRAVING,
        pattern=r"\bcrav(e|ing|ings)\b",
        example="I'm craving",
    ),
    Signal(
        label="wants to use",
        # Deliberately not anchored on "I" so that "I don't want to use" is
        # caught by the matcher and then suppressed by negation handling —
        # the suppression is visible and testable, not an accident of anchoring.
        tier=Tier.CRAVING,
        pattern=r"\bwan(t|na)\s+(to\s+)?(use|get high|pick up|score)\b",
        example="I want to use",
    ),
    Signal(
        label="thinking about using",
        tier=Tier.CRAVING,
        pattern=r"\b(thinking about|thought about|keep thinking about)\s+"
        r"(using|getting high|picking up|scoring)\b",
        example="thinking about using",
    ),
    Signal(
        label="needs to use",
        tier=Tier.CRAVING,
        pattern=r"\bi\s+need\s+(to\s+use|a fix|to get well|something)\b",
        example="I need to use",
    ),
    Signal(
        label="seeking supply",
        tier=Tier.CRAVING,
        pattern=r"\b(texted my (guy|dealer)|calling my (guy|dealer)|"
        r"gonna go (see|meet) my (guy|dealer))\b",
        example="I texted my guy",
    ),
    # ---- Calm state: permits de-escalation --------------------------------
    # tier=None. These never assert a tier; they license a single step down
    # (see `evaluate` step 5). They are the ONLY language path downward, and
    # they are inert at Tiers 4 and 5 by design.
    Signal(
        label="reports safe",
        tier=None,
        pattern=r"\bi'?m\s+(ok|okay|alright|fine|safe|good|better|sober|clean)\b",
        example="I'm okay",
    ),
    Signal(
        label="false alarm",
        tier=None,
        pattern=r"\b(false alarm|nothing happened|i'?m not in trouble|"
        r"disregard that|never ?mind)\b",
        negatable=False,
        example="false alarm",
    ),
    Signal(
        label="craving passed",
        tier=None,
        pattern=r"\b(craving (passed|is gone|went away)|"
        r"feeling better|the urge passed|i got through it)\b",
        negatable=False,
        example="the craving passed",
    ),
    Signal(
        label="denies use",
        tier=None,
        pattern=r"\bi\s+(didn'?t|did not|haven'?t|have not)\s+use[d]?\b",
        negatable=False,
        example="I didn't use",
    ),
]


# --------------------------------------------------------------------------
# Phrase matching
# --------------------------------------------------------------------------
def _normalise(text: str) -> str:
    """Lowercase and straighten apostrophes so one pattern spelling suffices.

    Args:
        text: raw utterance as typed or transcribed.

    Returns:
        The normalised string. Nothing else is stripped — punctuation is needed
        intact because `_CLAUSE_SPLIT` uses it to bound negation scope.
    """
    return text.translate(_APOSTROPHES).lower()


def _is_negated(clause: str, match_start: int) -> bool:
    """True if a negation cue sits in the short window preceding the match.

    Args:
        clause: a single normalised clause (already split on punctuation, so a
            negation in a previous clause cannot reach into this one).
        match_start: character offset of the signal match within `clause`.

    Returns:
        True if any of the last NEGATION_WINDOW_WORDS words before the match is
        a negation cue.

    Failure mode, stated honestly: this is proximity-based, not syntactic. It
    catches "I don't want to use" and misses "there's no world in which I use
    tonight". It errs toward firing — an unsuppressed match escalates, and an
    over-escalation is one tap to rescind.
    """
    before = clause[:match_start]
    # Word-character extraction rather than str.split() so that trailing
    # punctuation ("no, ") does not fuse into the token and miss the cue set.
    words = re.findall(r"[a-z']+", before)
    window = words[-NEGATION_WINDOW_WORDS:]
    return any(w.replace("'", "") in NEGATION_CUES for w in window)


def match_signals(utterance: str | None) -> list[SignalMatch]:
    """Every signal that fires on `utterance`, with negated matches removed.

    Args:
        utterance: what the user said or typed. None/empty is normal (a sensor
            tick carries no speech) and yields an empty list, not an error.

    Returns:
        Matches in `SIGNALS` order — tier-descending — so the first escalating
        entry is always the most severe. Callers rely on this ordering instead
        of sorting.

    Pure: no I/O, no clock, no state. Same input, same output, forever.
    """
    if not utterance or not utterance.strip():
        # A silent tick is the common case, not an error case. Sensor-driven
        # escalation (silence, stillness) needs no language at all.
        return []

    text = _normalise(utterance)

    # Context suppression runs against the WHOLE utterance, before clause
    # splitting. That is the point: the old per-row lookaheads sat inside a
    # single clause, so "last year, I passed out at a party" escalated — the
    # comma split the qualifier away from the phrase it qualified, and the
    # lookahead could never see it. Whole-utterance context is the only scope
    # at which "this is a memory, not an event" is decidable.
    #
    # Suppression is total rather than tier-reducing. A figurative or
    # historical statement is not weak evidence of an emergency; it is not
    # evidence of one at all.
    if _is_suppressed(text):
        return []

    # Split into clauses first so negation scope is bounded: "I didn't sleep,
    # I used" must still escalate. Empty fragments from adjacent punctuation
    # are dropped.
    clauses = [c for c in _CLAUSE_SPLIT.split(text) if c and c.strip()]

    matches: list[SignalMatch] = []
    for signal in SIGNALS:
        for clause in clauses:
            hit = signal.regex.search(clause)
            if hit is None:
                continue
            if signal.negatable and _is_negated(clause, hit.start()):
                # Suppressed, and deliberately NOT recorded as a negated match:
                # returning it would risk a caller treating truthiness of the
                # list as "something fired". An explain-view can re-run the
                # table if it wants suppressed rows.
                continue
            matches.append(SignalMatch(signal=signal, text=hit.group(0).strip()))
            # One hit per signal is enough — the label, not the count, drives
            # the decision, and repeats would only inflate the reason string.
            break
    return matches


# --------------------------------------------------------------------------
# Standing conditions (Tier 1)
# --------------------------------------------------------------------------
def _as_utc(value: datetime) -> datetime:
    """Coerce a datetime to aware UTC.

    Args:
        value: aware or naive datetime, from the profile or from the caller.

    Returns:
        The same instant, tz-aware in UTC. Naive inputs are *assumed* UTC.

    Why: seeded demo data, JSON round-trips, and test fixtures mix aware and
    naive datetimes, and subtracting one from the other raises TypeError. A
    crash inside the tolerance-window check would take down the whole triage
    call, so awareness is normalised rather than trusted.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def tolerance_window_active(
    profile: UserProfile | None, now: datetime | None
) -> tuple[bool, str | None]:
    """Is `now` inside TOLERANCE_WINDOW_DAYS of a tolerance-loss event?

    PRD: post-detox / post-discharge / post-release is the highest-mortality
    period in the entire condition, because tolerance falls faster than habit.
    An open window is a standing reason to sit at Tier 1 even with no other
    signal at all.

    Args:
        profile: the user's profile; None or an empty event list means no
            window (a user who has never recorded an event is not elevated).
        now: injected clock. Required only when there are events to compare.

    Returns:
        (active, label) where label describes the most recent qualifying event,
        e.g. "hospital discharge 12 days ago", or (False, None).

    Raises:
        ValueError: if the profile carries tolerance events but `now` is None.
            Defaulting to `datetime.now()` here would break purity; silently
            returning False would suppress a fatal-risk window. Both are worse
            than making the caller pass a clock, so this fails loudly.

    Boundary: the window is inclusive of exactly TOLERANCE_WINDOW_DAYS
    (day 90 is still open, day 91 is closed) and ignores events dated in the
    future, so a scheduled discharge does not elevate anyone in advance.
    """
    if profile is None or not profile.tolerance_events:
        return False, None
    if now is None:
        raise ValueError(
            "now= is required to evaluate the tolerance-reset window; "
            "triage never reads the wall clock itself"
        )

    reference = _as_utc(now)
    cutoff = reference - timedelta(days=TOLERANCE_WINDOW_DAYS)
    # Track the most recent qualifying event, not the first found: a user with
    # both a detox six weeks ago and a discharge yesterday should see the
    # discharge named in the UI, because that is the one driving current risk.
    best: tuple[datetime, str] | None = None
    for event in profile.tolerance_events:
        at = _as_utc(event.date)
        if at > reference:
            # A discharge scheduled for next week is not a live window. Without
            # this guard a future-dated record would pin the user at Tier 1
            # indefinitely.
            continue
        if at < cutoff:
            continue  # older than the window; risk has normalised
        if best is None or at > best[0]:
            best = (at, event.kind)
    if best is None:
        return False, None
    days = (reference - best[0]).days
    return True, f"{best[1].replace('_', ' ')} {days} days ago"


def _standing_tier(
    profile: UserProfile | None,
    missed_checkins: int,
    high_risk_window: bool,
    now: datetime | None,
) -> tuple[Tier, list[str]]:
    """The floor the ladder cannot drop below right now, plus its reasons.

    "Standing" means: true because of the user's situation, not because of
    anything they just said. These conditions hold the user at Tier 1 and also
    act as the floor for de-escalation — a calm utterance cannot drop someone
    below Tier 1 while they are three days out of detox.

    Args:
        profile: supplies `ladder.missed_checkins_to_elevate` and the tolerance
            events. None falls back to DEFAULT_LADDER.
        missed_checkins: consecutive scheduled check-ins missed.
        high_risk_window: caller-determined; see `evaluate`.
        now: injected clock, forwarded to the tolerance check.

    Returns:
        (Tier.ELEVATED, [reasons]) if any condition holds, else
        (Tier.BASELINE, []). Reasons are phrases, joined by the caller.

    Every condition here is deliberately additive rather than exclusive: all
    reasons are collected so the UI can show the user every factor, not just
    the first one that tripped. PRD §11 — no hidden state.
    """
    ladder = profile.ladder if profile is not None else DEFAULT_LADDER
    reasons: list[str] = []

    if high_risk_window:
        reasons.append("inside a user-defined high-risk window")
    # >= not ==: a caller that jumps from 1 to 3 missed check-ins (app killed,
    # phone off overnight) must still elevate rather than skip past the
    # threshold. The threshold is user-tunable — PRD P3, the user owns the
    # ladder — so it is read from the profile, never hardcoded here.
    if missed_checkins >= ladder.missed_checkins_to_elevate:
        reasons.append(
            f"{missed_checkins} missed check-ins "
            f"(threshold {ladder.missed_checkins_to_elevate})"
        )
    active, label = tolerance_window_active(profile, now)
    if active:
        reasons.append(f"tolerance-reset window open — {label}")

    return (Tier.ELEVATED if reasons else Tier.BASELINE), reasons


# --------------------------------------------------------------------------
# Actions (PRD §4.3 timing table)
# --------------------------------------------------------------------------
def _naloxone_detail(profile: UserProfile | None) -> str:
    """Wording for the naloxone prompt, branched on whether any is on hand.

    Args:
        profile: read for `naloxone_on_hand`. None is treated as "not on file" —
            the safe assumption, since it produces the instruction to ask a
            bystander rather than to search for a kit that may not exist.

    Returns:
        A literal instruction string. Note it is a literal: PRD P4, the model
        never writes crisis instructions, only conversational language.
    """
    if profile is not None and profile.naloxone_on_hand:
        return "Naloxone is on file at this address — give it now, then call 911."
    return "No naloxone on file — ask anyone nearby, then call 911."


def _emergency_actions(profile: UserProfile | None, contact_tree_at: int) -> list[Action]:
    """The Tier 4/5 action sequence, per the PRD §4.3 timing table.

    Args:
        profile: forwarded to the naloxone prompt for its wording.
        contact_tree_at: seconds until the contact tree fires. 30 at Tier 4
            (there is still time for the user to rescind a false alarm);
            0 at Tier 5 (there is not).

    Returns:
        Actions with `at_second` set. Ordering in the list is presentation
        order; `at_second` is the contract for timing, and several actions
        share a timestamp on purpose.

    The critical invariant, tested explicitly: the naloxone prompt and the 911
    button are BOTH at_second=0. Naloxone does not gate the 911 call and the
    911 call does not gate naloxone — they are parallel because naloxone wears
    off before an ambulance arrives, and an ambulance arrives too late without
    naloxone.
    """
    return [
        Action(kind="naloxone_prompt", detail=_naloxone_detail(profile), at_second=0),
        Action(
            kind="offer_contact",
            detail="911 — one tap to call, shown in parallel with the naloxone prompt.",
            at_second=0,
        ),
        Action(
            kind="keep_awake",
            detail="Loud audio and screen kept lit to hold consciousness.",
            at_second=0,
        ),
        # 5s: responders need a unit number and a door code more than they need
        # a street. Resolved early because the user may be unable to speak by
        # the time the dispatcher asks.
        Action(
            kind="acquire_location",
            detail="Resolve address, unit and entry code for responders.",
            at_second=5,
        ),
        # 10s: no self-rescue by now, so recruit a body in the room. PRD §3 —
        # the bystander has no account and is never asked to make one.
        Action(
            kind="bystander_hail",
            detail="Hail anyone within earshot; hand off to the bystander screen.",
            at_second=10,
        ),
        # Same 10s beat as the hail: the single biggest reason a bystander does
        # not call 911 is fear of arrest. The brief is a static reviewed
        # dataset (CONTRACT.md) — a hallucinated immunity statute is the worst
        # possible failure this product could produce.
        Action(
            kind="show_good_samaritan",
            detail="Static reviewed Good Samaritan brief for the bystander who "
            "is afraid to call.",
            at_second=10,
        ),
        # 15s: words for whoever is holding the phone, because panicked callers
        # cannot summarise. This is a model-composed script — language work
        # only, after triage has already decided the tier.
        Action(
            kind="show_emergency_script",
            detail="Read-aloud script for the dispatcher.",
            at_second=15,
        ),
        # Last, and timing supplied by the caller: contacting other people is
        # the most privacy-invasive action in the system, so at Tier 4 it waits
        # 30s for a rescind. At Tier 5 it does not wait at all.
        Action(
            kind="fire_contact_tree",
            detail="Escalate through the contact tree in order.",
            at_second=contact_tree_at,
        ),
    ]


def actions_for_tier(tier: Tier, profile: UserProfile | None = None) -> list[Action]:
    """Actions due at a tier, with their PRD §4.3 timings.

    Args:
        tier: the tier being entered (or held).
        profile: only consulted for naloxone wording at Tiers 4/5.

    Returns:
        A fresh list of Actions. Pure and repeatable — calling this twice with
        the same arguments yields equal lists, which is what lets the caller
        diff old and new action sets to decide what has already fired.

    Deliberately NOT here: executing anything. This function describes; the
    caller performs. That separation is what keeps triage callable for preview
    on the `/ladder` screen without dialling anyone's mother.
    """
    if tier is Tier.BASELINE:
        # Baseline is not "do nothing quietly" — it is the absence of
        # intervention, and that emptiness is the point. An app that nags at
        # baseline gets uninstalled, and an uninstalled app saves nobody.
        return []

    if tier is Tier.ELEVATED:
        return [
            Action(
                kind="speak",
                detail="Low-key check-in: name the risk window, ask one question.",
            ),
            Action(kind="offer_contact", detail="Offer a one-tap call to a chosen person."),
        ]

    if tier is Tier.CRAVING:
        return [
            Action(kind="start_grounding", detail="Begin the grounding sequence."),
            Action(kind="play_vault_clip", detail="Play a vault clip chosen for craving."),
            Action(kind="speak", detail="Ride out the urge; offer the refusal script."),
            Action(kind="offer_contact", detail="Offer a one-tap call to a chosen person."),
        ]

    if tier is Tier.ACTIVE_USE:
        # Tier 3 is explicitly non-judgemental. The user has already used;
        # lecturing them now only teaches them not to disclose next time. Every
        # action here is harm reduction: stay on the line, stay awake, get the
        # bystander path and the naloxone within reach BEFORE they are needed.
        return [
            Action(
                kind="speak",
                detail="Stay-with-me dialogue: never use alone, keep talking to me.",
            ),
            Action(kind="keep_awake", detail="Keep the session live and the screen lit."),
            Action(
                kind="arm_bystander_mode",
                detail="Pre-arm the bystander screen so Tier 4 is one step away.",
            ),
            Action(
                kind="naloxone_prompt",
                detail="Put naloxone within reach before it is needed.",
            ),
        ]

    if tier is Tier.EMERGENCY:
        return _emergency_actions(profile, contact_tree_at=30)

    # Tier 5 — unresponsive. Same sequence, no waiting: the contact tree fires
    # immediately and rescue breathing is coached for whoever is present.
    return _emergency_actions(profile, contact_tree_at=0) + [
        Action(
            kind="rescue_breathing",
            detail="Coach rescue breathing for any bystander until responders arrive.",
            at_second=0,
        )
    ]


# --------------------------------------------------------------------------
# Caregiver notification
# --------------------------------------------------------------------------
def notify_caregiver_for(tier: Tier, profile: UserProfile | None = None) -> bool:
    """Whether this tier reaches a caregiver, per PRD §4.2.

    Args:
        tier: the tier just decided.
        profile: supplies `ladder.tier_3_visible_to_caregiver`. None uses the
            default, which is False — privacy-preserving when unknown.

    Returns:
        True if the caregiver is notified.

    The rule, and why it is shaped this way:

    * Tiers 4 and 5 -> always True, regardless of every setting the user has
      turned off. `USER_CONTROLLABLE_TIERS` in models.py encodes the same
      boundary. This is the one place the user does not get to decide, and it
      is disclosed during onboarding rather than buried — a surveillance tool
      the user cannot predict is one they will stop carrying.
    * Tier 3 -> only if the user opted in. Defaults to False (models.py) on
      purpose: a user who fears their disclosure will be reported will not
      disclose, and will use alone instead.
    * Tiers 0-2 -> never. Craving is not news; it is Tuesday. Reporting it
      would train the user to stop naming it.
    """
    if tier >= Tier.EMERGENCY:
        return True
    if tier is Tier.ACTIVE_USE:
        ladder = profile.ladder if profile is not None else DEFAULT_LADDER
        return ladder.tier_3_visible_to_caregiver
    return False


# Back-compat alias. `app/main.py` reached for the private name when building
# the manual-override result; kept so that import cannot break, but the public
# name above is the one to use. Deliberately not deleted without coordinating.
_notify_caregiver = notify_caregiver_for


# --------------------------------------------------------------------------
# The state machine
# --------------------------------------------------------------------------
def _actions_with_crisis(
    tier: Tier, profile: UserProfile | None, matched_signal: str | None
) -> list[Action]:
    """Tier actions, with the crisis line surfaced first for suicidal statements.

    The physical emergency actions still run underneath. Intent to die and
    overdose co-occur often enough that dropping either set would be a guess
    about which one is happening, and this module does not guess — it offers
    both and lets the person in front of the screen choose.

    Tele-MANAS leads for mental-health support while 112 remains one tap away
    for immediate physical danger.
    """
    actions = actions_for_tier(tier, profile)
    if matched_signal in _CRISIS_SIGNAL_LABELS:
        actions.insert(
            0,
            Action(
                kind="show_crisis_line",
                detail="Tele-MANAS 14416 — Government of India mental-health support, 24/7.",
                at_second=0,
            ),
        )
    return actions


def _result(
    tier: Tier,
    previous: Tier,
    reason: str,
    profile: UserProfile | None,
    matched_signal: str | None,
) -> TriageResult:
    """Assemble a TriageResult. The single construction point for every exit.

    Args:
        tier: the decided tier.
        previous: the tier held before this call.
        reason: literal, human-readable explanation. Never model-written.
        profile: forwarded for actions and caregiver visibility.
        matched_signal: label of the signal that drove the decision, or None.

    Returns:
        A fully populated TriageResult.

    Every `return` in `evaluate` goes through here so that actions and
    `notify_caregiver` can never be derived from a different tier than the one
    reported — the class of bug where a Tier 4 result carries Tier 3 actions is
    structurally impossible rather than merely tested against.
    """
    return TriageResult(
        tier=tier,
        previous_tier=previous,
        reason=reason,
        matched_signal=matched_signal,
        actions=_actions_with_crisis(tier, profile, matched_signal),
        notify_caregiver=_notify_caregiver(tier, profile),
    )


def evaluate(
    current_tier: Tier | int,
    *,
    utterance: str | None = None,
    silent_seconds: int = 0,
    still: bool = False,
    missed_checkins: int = 0,
    profile: UserProfile | None = None,
    now: datetime | None = None,
    high_risk_window: bool = False,
) -> TriageResult:
    """Decide the tier. Deterministic: same inputs, same result, always.

    Args:
        current_tier: the tier the user is on right now.
        utterance: what the user just said or typed. None if this is a sensor
            tick rather than speech.
        silent_seconds: seconds since the user last responded.
        still: True if the device has reported no motion.
        missed_checkins: consecutive scheduled check-ins missed.
        profile: the user's profile; thresholds and caregiver visibility come
            from `profile.ladder`. Defaults are used when None.
        now: the clock, injected. Required only when the profile carries
            tolerance events.
        high_risk_window: True if the caller has determined that `now` falls in
            a window the user marked as high-risk (payday, an anniversary, a
            time of day). Kept as a caller-supplied boolean because the schedule
            itself is not in `UserProfile` — see the note in the module report.

    Returns:
        TriageResult with the new tier, the actions due, a human-readable
        reason, and whether the caregiver is notified.

    Raises:
        ValueError: only via `tolerance_window_active`, when the profile has
            tolerance events but no `now` was given. See that function.

    Never raises on unparseable or hostile utterances: any string is legal
    input, and the worst case is an empty match list.
    """
    previous = Tier(current_tier)
    ladder = profile.ladder if profile is not None else DEFAULT_LADDER
    silence_threshold = ladder.silence_seconds_to_escalate

    # Language evidence. Split into the two kinds immediately: escalating
    # signals assert a tier; calm signals only license a step down.
    matches = match_signals(utterance)
    escalating = next((m for m in matches if m.signal.tier is not None), None)
    calm = next((m for m in matches if m.signal.tier is None), None)

    # Situational evidence — true regardless of what was just said.
    standing, standing_reasons = _standing_tier(
        profile, missed_checkins, high_risk_window, now
    )

    signal_tier = escalating.signal.tier if escalating is not None else Tier.BASELINE
    # `base` is the tier the user is effectively on for the purposes of the
    # sensor rules below. Taking the max of all three sources means a user who
    # says "I just used" and then goes silent in the SAME call still qualifies
    # for the Tier 3+ silence rule — the disclosure does not have to have been
    # committed on a previous turn for the silence path to apply.
    base = Tier(max(previous, standing, signal_tier))

    silent = silent_seconds >= silence_threshold
    matched_label = escalating.signal.label if escalating is not None else None

    # ---- 1 / 2: silence and stillness at Tier 3+ -------------------------
    # These come first and outrank every phrase in the table. PRD §4.1: an
    # opioid overdose takes the user's voice before it takes their life, so the
    # absence of evidence IS the evidence. Note these rules only arm at Tier 3+
    # — silence at Tier 0 is a person living their life, not a casualty.
    if base >= Tier.ACTIVE_USE and silent:
        if still:
            reason = (
                f"Tier {previous.value} -> Tier 5: no response for "
                f"{silent_seconds}s (threshold {silence_threshold}s) and the "
                f"device has reported no movement. Treating as unresponsive."
            )
            # matched_signal is None: this tier came from SENSORS, not speech.
            # Passing the utterance's label through made the reason read
            # "disclosed use" when nothing of the sort had been said, and put a
            # signal name on the caregiver screen that contradicted the reason
            # beside it.
            return _result(Tier.UNRESPONSIVE, previous, reason, profile, None)

        # Silent but still moving: Tier 4, not 5. Guarded so an already-Tier-5
        # user who is silent but has started moving again is not walked
        # backwards to Tier 4 — de-escalation never happens on sensor data.
        if base < Tier.UNRESPONSIVE:
            reason = (
                f"Tier {previous.value} -> Tier 4: disclosed use with no "
                f"response for {silent_seconds}s (threshold {silence_threshold}s)."
            )
            # As above: sensor-derived tier, so no speech signal is attached.
            return _result(Tier.EMERGENCY, previous, reason, profile, None)

    # ---- 3: escalating phrase -------------------------------------------
    # Strictly `>`: a phrase can only ever raise the tier, never lower it. A
    # Tier 2 craving phrase said by someone already at Tier 4 must not pull
    # them down to 2 — that path is handled by step 5 and by `rescind` alone.
    if escalating is not None and signal_tier > previous:
        reason = (
            f"Tier {previous.value} -> Tier {signal_tier.value}: matched "
            f"\"{escalating.text}\" ({escalating.signal.label})."
        )
        return _result(signal_tier, previous, reason, profile, matched_label)

    # ---- 4: standing conditions -----------------------------------------
    # Reached only when no phrase outranked the current tier. Also strictly
    # `>`, for the same reason: a standing condition is a floor, not a ceiling.
    if standing > previous:
        reason = (
            f"Tier {previous.value} -> Tier 1: "
            + "; ".join(standing_reasons)
            + "."
        )
        return _result(standing, previous, reason, profile, matched_label)

    # ---- 5: calm-state de-escalation ------------------------------------
    # `escalating is None` is required: "I'm okay but I'm craving" must not
    # de-escalate. Any escalating signal in the same utterance vetoes the calm
    # one outright, rather than the two being weighed against each other.
    if calm is not None and escalating is None:
        # Tiers 4 and 5 NEVER auto-de-escalate. PRD §4.2 and the module
        # docstring: an unresponsive person cannot testify that they are fine,
        # and a person mid-overdose may sincerely believe they are. Standing
        # down an emergency requires the deliberate physical act of `rescind`.
        if previous >= Tier.EMERGENCY:
            reason = (
                f"Holding at Tier {previous.value}: matched "
                f"\"{calm.text}\" ({calm.signal.label}), but Tiers 4 and 5 "
                f"never de-escalate automatically. Use rescind to stand down."
            )
            return _result(previous, previous, reason, profile, calm.signal.label)
        if previous > standing:
            # One step at a time, and never below the standing floor. Stepping
            # 3 -> 0 on a single "I'm fine" would discard the elevated check-in
            # cadence at precisely the hour it matters most; the user has to
            # walk back down the same ladder they walked up.
            target = Tier(max(standing, previous - 1))
            reason = (
                f"Tier {previous.value} -> Tier {target.value}: matched "
                f"\"{calm.text}\" ({calm.signal.label}). Tiers step down one at "
                f"a time, never in a jump."
            )
            return _result(target, previous, reason, profile, calm.signal.label)
        reason = (
            f"Holding at Tier {previous.value}: matched \"{calm.text}\" "
            f"({calm.signal.label}), already at the lowest tier the current "
            f"conditions allow."
        )
        return _result(previous, previous, reason, profile, calm.signal.label)

    # ---- 6: hold ---------------------------------------------------------
    # Three distinct hold reasons rather than one generic string. The reason is
    # rendered in the UI, and "no new signal" would be actively misleading to a
    # user looking at their ladder wondering why they are still at Tier 3.
    if escalating is not None:
        reason = (
            f"Holding at Tier {previous.value}: matched \"{escalating.text}\" "
            f"({escalating.signal.label}), which does not exceed the current tier."
        )
        return _result(previous, previous, reason, profile, matched_label)

    if standing_reasons and previous == standing:
        reason = f"Holding at Tier {previous.value}: " + "; ".join(standing_reasons) + "."
        return _result(previous, previous, reason, profile, None)

    reason = f"Holding at Tier {previous.value}: no new signal."
    return _result(previous, previous, reason, profile, None)


def rescind(
    current_tier: Tier | int, *, profile: UserProfile | None = None
) -> TriageResult:
    """One-tap false alarm. The only way down from Tier 4 or 5.

    Args:
        current_tier: the tier being stood down from.
        profile: forwarded for the resulting actions and caregiver visibility.

    Returns:
        A TriageResult at Tier 1 (or the current tier if already at or below
        it), with a reason that records the stand-down as a user action.

    Why this exists as a separate entry point rather than an `evaluate` flag:
    `evaluate` processes evidence, and a rescind is not evidence — it is an
    authority. Keeping it separate means no combination of utterance, silence,
    or sensor input can ever synthesise one.

    Why Tier 1 and not Tier 0: someone who just stood down an emergency is not
    baseline. Dropping to 0 would cancel the elevated check-in cadence at
    exactly the wrong moment. PRD P3 — the user may rescind the alarm, but the
    ladder still remembers the hour they are having.

    Deliberately NOT rate-limited or challenged ("are you sure?"). A user who
    must fight their phone to cancel a false alarm learns to leave the phone in
    another room, and the phone in another room is the whole failure mode this
    product exists to prevent.
    """
    previous = Tier(current_tier)
    if previous <= Tier.ELEVATED:
        reason = (
            f"Holding at Tier {previous.value}: rescinded by the user, already "
            f"at or below Tier 1."
        )
        return _result(previous, previous, reason, profile, None)
    reason = (
        f"Tier {previous.value} -> Tier 1: rescinded by the user. Explicit "
        f"stand-down, logged as a false alarm."
    )
    return _result(Tier.ELEVATED, previous, reason, profile, None)
