"""Tests for the deterministic escalation ladder (`app/triage.py`).

## What this suite protects

`triage.py` is the only module allowed to decide a tier, and a wrong decision
here is a missed overdose or a betrayed confidence. Each test below names the
specific failure it exists to prevent, because a test whose purpose is not
obvious gets deleted by the next person who sees it go red.

The suite is organised by the thing being protected, not by function:

1.  Signal table integrity — the table is inspectable and internally consistent
2.  Phrase matching — word boundaries, the "used to" false positive
3.  Negation — the "I don't want to use" suppression, and its deliberate limits
4.  Escalation by phrase — every T0->T2/T3/T4 transition
5.  Sensor escalation — silence to T4, silence + stillness to T5
6.  De-escalation — never implicit, never from T4/T5, one step at a time
7.  Rescind — the only door down from an emergency
8.  Caregiver notification — including "T4 notifies even with everything off"
9.  Actions — the PRD §4.3 timing table, including the parallel-at-zero rule
10. Tolerance window — the 90-day boundary, off-by-one both sides
11. Purity — no clock reads, no hidden state, repeatable results

## What this suite deliberately does NOT test

* Nothing is mocked or patched. `triage.py` has no I/O to mock; needing a mock
  here would itself be the bug.
* No test asserts the exact wording of a `reason` string beyond the substrings
  that carry meaning (the tier numbers and the matched label). Pinning full
  prose would make the suite fail on a copy edit and teach people to ignore it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models import (
    Contact,
    LadderConfig,
    Tier,
    ToleranceEvent,
    UserProfile,
)
from app.triage import (
    SIGNALS,
    TOLERANCE_WINDOW_DAYS,
    actions_for_tier,
    evaluate,
    match_signals,
    rescind,
    tolerance_window_active,
)

# A fixed instant. Every time-dependent test derives from this rather than the
# wall clock, so the suite gives the same result at 3am on a leap day.
NOW = datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------
def make_profile(
    *,
    tier_3_visible: bool = False,
    tier_2_visible: bool = False,
    missed_to_elevate: int = 2,
    silence_seconds: int = 20,
    naloxone: bool = False,
    tolerance_events: list[ToleranceEvent] | None = None,
) -> UserProfile:
    """Build a profile with the ladder knobs a test cares about.

    Defaults mirror `LadderConfig`'s defaults, so a test that overrides one
    field is visibly testing that field and nothing else.
    """
    return UserProfile(
        id="u1",
        name="Sam",
        address="118 Mercer St",
        unit="3B",
        entry_code="4412",
        state_code="KY",
        naloxone_on_hand=naloxone,
        ladder=LadderConfig(
            tier_3_visible_to_caregiver=tier_3_visible,
            tier_2_visible_to_caregiver=tier_2_visible,
            missed_checkins_to_elevate=missed_to_elevate,
            silence_seconds_to_escalate=silence_seconds,
        ),
        contacts=[
            Contact(
                name="Sarah",
                relation="sister",
                channel="phone",
                order=1,
                tiers=[Tier.EMERGENCY, Tier.UNRESPONSIVE],
            )
        ],
        tolerance_events=tolerance_events or [],
    )


@pytest.fixture
def profile() -> UserProfile:
    """The default user: maximum privacy, default thresholds, no naloxone."""
    return make_profile()


@pytest.fixture
def locked_down_profile() -> UserProfile:
    """A user who has turned OFF every disclosure setting they are allowed to.

    Used to prove that Tiers 4 and 5 override all of it. This is the profile
    that most needs to behave correctly, because it is the profile of the user
    most afraid of being reported — and therefore the one most at risk.
    """
    return make_profile(tier_3_visible=False, tier_2_visible=False)


# ==========================================================================
# 1. Signal table integrity
# ==========================================================================
class TestSignalTable:
    """The table is the product's public promise about what it listens for.

    If it is malformed, the `/ladder` screen shows a user something untrue
    about their own surveillance.
    """

    def test_every_signal_has_a_human_readable_label(self):
        """Protects: an unlabelled signal would render a blank UI row and an
        unexplainable `reason` string."""
        for signal in SIGNALS:
            assert signal.label.strip(), f"{signal.pattern} has no label"
            assert signal.example.strip(), f"{signal.label} has no example"

    def test_labels_are_unique(self):
        """Protects: two rows with one label make an escalation ambiguous when
        a user asks the caregiver 'which phrase did that?'"""
        labels = [s.label for s in SIGNALS]
        assert len(labels) == len(set(labels))

    def test_every_pattern_compiles_and_is_word_boundary_anchored(self):
        """Protects: substring matching, the root cause of the whole class of
        'used to' / 'helper' false positives."""
        for signal in SIGNALS:
            assert signal.regex is not None
            assert "\\b" in signal.pattern, f"{signal.label} lacks a \\b anchor"

    def test_every_signal_example_actually_fires_its_own_signal(self):
        """Protects: documentation drift. The example shown in the UI must be a
        sentence that genuinely triggers the row it sits beside — otherwise the
        transparency screen is lying."""
        for signal in SIGNALS:
            labels = [m.signal.label for m in match_signals(signal.example)]
            assert signal.label in labels, (
                f"{signal.label!r} example {signal.example!r} does not fire it"
            )

    def test_table_is_ordered_by_descending_tier(self):
        """Protects: `match_signals` returns table order and callers take the
        first escalating hit as the most severe. If the table were unsorted,
        'I want to use, I can't breathe' would triage as craving."""
        escalating = [s.tier for s in SIGNALS if s.tier is not None]
        assert escalating == sorted(escalating, reverse=True)

    def test_calm_signals_come_last_and_assert_no_tier(self):
        """Protects: a calm phrase acquiring an asserted tier, which would let
        'I'm okay' escalate someone."""
        calm = [s for s in SIGNALS if s.tier is None]
        assert calm, "there must be a de-escalation path"
        assert all(s.tier is None for s in SIGNALS[-len(calm):])


# ==========================================================================
# 2. Phrase matching and false positives
# ==========================================================================
class TestPhraseMatching:
    """Word-boundary correctness. Each of these is a real sentence a user in
    recovery says, which must NOT summon their family."""

    def test_used_to_is_not_a_disclosure_of_use(self):
        """THE canonical false positive. 'I used to use every day' is what
        recovery sounds like; escalating on it punishes the user for telling
        their story and teaches them to stop talking."""
        result = evaluate(Tier.BASELINE, utterance="I used to use every day")
        assert result.tier is Tier.BASELINE
        assert result.matched_signal is None

    @pytest.mark.parametrize(
        "utterance",
        [
            "I used to shoot up",
            "back when I used to take pills",
            "I used to be high all the time",
        ],
    )
    def test_used_to_variants_never_escalate(self, utterance):
        """Protects: the `(?!\\s+to\\b)` guard regressing to a bare \\bused\\b."""
        assert evaluate(Tier.BASELINE, utterance=utterance).tier is Tier.BASELINE

    @pytest.mark.parametrize(
        "utterance",
        ["my helper is here", "that was helpful", "she helped me", "helping out"],
    )
    def test_help_substrings_do_not_trigger_emergency(self, utterance):
        """Protects: the second canonical false positive. 'That was helpful'
        must not dispatch an ambulance — the credibility cost of one wrongful
        911 call is the user uninstalling the app."""
        result = evaluate(Tier.BASELINE, utterance=utterance)
        assert result.tier is not Tier.EMERGENCY

    def test_bare_help_does_trigger_emergency(self):
        """The flip side: having excluded the suffixes, the bare word must
        still work. This is the sentence the product exists for."""
        result = evaluate(Tier.BASELINE, utterance="help")
        assert result.tier is Tier.EMERGENCY

    @pytest.mark.parametrize(
        "utterance",
        ["I took a walk", "I took my meds", "I did a break"],
    )
    def test_innocuous_completions_do_not_disclose_use(self, utterance):
        """Protects: prescribed-medication adherence being misread as relapse.
        A user on buprenorphine saying 'I took my meds' is doing the right
        thing and must not be triaged as active use."""
        assert evaluate(Tier.BASELINE, utterance=utterance).tier is Tier.BASELINE

    def test_matching_is_case_insensitive(self):
        """Protects: voice transcription and phone keyboards capitalise
        unpredictably."""
        assert evaluate(Tier.BASELINE, utterance="I CAN'T BREATHE").tier is Tier.EMERGENCY

    def test_curly_apostrophes_match(self):
        """Protects: iOS smart quotes silently defeating every contraction in
        the table. Invisible in a diff, fatal in production."""
        assert evaluate(Tier.BASELINE, utterance="I can’t breathe").tier is Tier.EMERGENCY

    def test_empty_and_whitespace_utterances_are_not_errors(self):
        """Protects: a crash on an empty message box taking down triage."""
        for utterance in [None, "", "   ", "\n\t"]:
            assert match_signals(utterance) == []

    def test_unmatched_text_holds_the_tier(self):
        """Protects: conversational text quietly moving the ladder. Most of
        what a user says is not a signal, and must be inert."""
        result = evaluate(Tier.CRAVING, utterance="the weather is bad today")
        assert result.tier is Tier.CRAVING
        assert result.previous_tier is Tier.CRAVING

    def test_most_severe_signal_wins_within_one_utterance(self):
        """Protects: a low-tier phrase shadowing a high-tier one in the same
        sentence. 'I want to use' plus 'I can't breathe' is an emergency."""
        result = evaluate(
            Tier.BASELINE, utterance="I want to use and I can't breathe"
        )
        assert result.tier is Tier.EMERGENCY


# ==========================================================================
# 3. Negation
# ==========================================================================
class TestNegation:
    """Suppression of negated signals, and the honest limits of that."""

    @pytest.mark.parametrize(
        "utterance",
        [
            "I don't want to use",
            "I do not want to use",
            "I never want to use again",
            "I'm not craving",
            "I resisted wanting to use",
        ],
    )
    def test_negated_craving_does_not_escalate(self, utterance):
        """Protects: the most common sentence in a recovery check-in becoming
        an escalation. A user who cannot say 'I don't want to use' without
        triggering their family will stop using the app."""
        result = evaluate(Tier.BASELINE, utterance=utterance)
        assert result.tier is Tier.BASELINE, utterance

    def test_negated_use_disclosure_does_not_escalate(self):
        """Protects: 'I didn't use' being read as 'I used'."""
        assert evaluate(Tier.BASELINE, utterance="I didn't use").tier is Tier.BASELINE

    def test_negation_does_not_leak_across_clauses(self):
        """Protects: an early negation swallowing a later real disclosure.
        'I didn't sleep, I used' is a disclosure and must escalate — the
        'didn't' belongs to the first clause only."""
        result = evaluate(Tier.BASELINE, utterance="I didn't sleep, I used")
        assert result.tier is Tier.ACTIVE_USE

    def test_self_negating_emergency_phrases_still_fire(self):
        """Protects: THE most dangerous possible regression. 'can't breathe'
        and 'not breathing' contain negation cues; if the negation handler
        treated them as suppressed, the product would go silent during the one
        event it was built for."""
        for utterance in ["I can't breathe", "he's not breathing", "I can't move"]:
            assert evaluate(Tier.BASELINE, utterance=utterance).tier is Tier.EMERGENCY

    def test_documented_negation_limit_is_a_known_miss(self):
        """Documents, rather than asserts correctness of, a known limitation.

        The negation window is 4 words inside one clause. In the sentence
        below, the negating "no" sits six words before the matched phrase, so
        the suppression does not reach it and the utterance over-escalates to
        Tier 2.

        This test asserts the CURRENT behaviour on purpose, so the limit is
        visible in the suite rather than discovered by a user. It fails in the
        safe direction — Tier 2 is a grounding exercise the user can dismiss,
        and the module docstring documents the limitation honestly. If the
        matcher is ever improved, this test should be updated, not deleted.
        """
        result = evaluate(
            Tier.BASELINE,
            utterance="there is no world in which I want to use tonight",
        )
        assert result.tier is Tier.CRAVING  # over-escalated, knowingly


# ==========================================================================
# 4. Escalation by phrase
# ==========================================================================
class TestPhraseEscalation:
    """Every phrase-driven tier transition named in the PRD."""

    @pytest.mark.parametrize(
        "utterance,expected",
        [
            ("I'm craving", Tier.CRAVING),
            ("I want to use", Tier.CRAVING),
            ("thinking about using", Tier.CRAVING),
            ("I used", Tier.ACTIVE_USE),
            ("I just took it", Tier.ACTIVE_USE),
            ("I'm high", Tier.ACTIVE_USE),
            ("I can't breathe", Tier.EMERGENCY),
            ("I think I'm overdosing", Tier.EMERGENCY),
            ("help", Tier.EMERGENCY),
        ],
    )
    def test_prd_example_phrases_reach_their_stated_tier(self, utterance, expected):
        """Protects: the PRD §4.1 examples, verbatim. If any of these stop
        working the product no longer does what it claims."""
        assert evaluate(Tier.BASELINE, utterance=utterance).tier is expected

    def test_escalation_can_skip_tiers(self):
        """Protects: a tier being clamped to +1 per step. Escalation is NOT
        symmetric with de-escalation — someone at baseline who says 'I can't
        breathe' goes straight to 4, because walking 0->1->2->3->4 would spend
        the entire window in which naloxone works."""
        result = evaluate(Tier.BASELINE, utterance="I think I'm overdosing")
        assert result.tier is Tier.EMERGENCY
        assert result.previous_tier is Tier.BASELINE

    def test_lower_phrase_does_not_pull_a_higher_tier_down(self):
        """Protects: implicit de-escalation via an escalating phrase. Someone
        at Tier 4 who says 'I'm craving' stays at 4."""
        result = evaluate(Tier.EMERGENCY, utterance="I'm craving")
        assert result.tier is Tier.EMERGENCY

    def test_reason_names_the_matched_phrase_and_both_tiers(self):
        """Protects: an unexplainable escalation. The reason is shown to the
        user; 'tier changed' with no cause is how you lose their trust."""
        result = evaluate(Tier.BASELINE, utterance="I just took it")
        assert "Tier 0" in result.reason and "Tier 3" in result.reason
        assert result.matched_signal == "disclosed dosing"
        assert "took" in result.reason


# ==========================================================================
# 5. Sensor escalation: silence and stillness
# ==========================================================================
class TestSensorEscalation:
    """The path that works when the user has stopped talking — i.e. the path
    that matters, since an overdose removes speech first."""

    def test_silence_at_tier_3_escalates_to_emergency(self, profile):
        """Protects: the core PRD rule. Disclosed use plus no response past the
        threshold is an emergency without any further language."""
        result = evaluate(Tier.ACTIVE_USE, silent_seconds=25, profile=profile)
        assert result.tier is Tier.EMERGENCY
        assert "25s" in result.reason

    def test_silence_below_the_threshold_holds(self, profile):
        """Protects: a jittery timer firing an ambulance because the user put
        the phone down for ten seconds."""
        result = evaluate(Tier.ACTIVE_USE, silent_seconds=19, profile=profile)
        assert result.tier is Tier.ACTIVE_USE

    def test_silence_threshold_is_inclusive_at_the_boundary(self, profile):
        """Protects: an off-by-one at exactly the configured threshold. 20s
        with a 20s threshold must fire."""
        assert evaluate(Tier.ACTIVE_USE, silent_seconds=20, profile=profile).tier is Tier.EMERGENCY

    def test_silence_threshold_is_read_from_the_user_profile(self):
        """Protects: a hardcoded 20s ignoring the user's own configuration.
        PRD P3 — the user tunes their own ladder."""
        patient = make_profile(silence_seconds=60)
        assert evaluate(Tier.ACTIVE_USE, silent_seconds=30, profile=patient).tier is Tier.ACTIVE_USE
        assert evaluate(Tier.ACTIVE_USE, silent_seconds=60, profile=patient).tier is Tier.EMERGENCY

    def test_silence_at_low_tiers_does_not_escalate(self, profile):
        """Protects: the app treating an ordinary quiet evening as a crisis.
        Silence only arms at Tier 3+; at Tier 0-2 a silent user is a user
        living their life."""
        for tier in (Tier.BASELINE, Tier.ELEVATED, Tier.CRAVING):
            result = evaluate(tier, silent_seconds=600, profile=profile)
            assert result.tier is tier, tier

    def test_silence_plus_stillness_escalates_to_unresponsive(self, profile):
        """Protects: Tier 5. Not talking AND not moving is the signature of an
        unconscious person, and it is the trigger for the full contact tree."""
        result = evaluate(Tier.ACTIVE_USE, silent_seconds=25, still=True, profile=profile)
        assert result.tier is Tier.UNRESPONSIVE

    def test_stillness_alone_without_silence_does_not_escalate(self, profile):
        """Protects: a sleeping or simply sedentary user being escalated. A
        phone on a table is still; that is not evidence of anything."""
        result = evaluate(Tier.ACTIVE_USE, silent_seconds=0, still=True, profile=profile)
        assert result.tier is Tier.ACTIVE_USE

    def test_stillness_at_low_tiers_does_not_escalate(self, profile):
        """Protects: the same, from baseline. Most phones are still most of the
        time."""
        result = evaluate(Tier.BASELINE, silent_seconds=600, still=True, profile=profile)
        assert result.tier is Tier.BASELINE

    def test_emergency_plus_stillness_reaches_unresponsive(self, profile):
        """Protects: a user stuck at Tier 4 while going unresponsive. T4 + long
        silence + stillness must complete the climb to T5."""
        result = evaluate(Tier.EMERGENCY, silent_seconds=40, still=True, profile=profile)
        assert result.tier is Tier.UNRESPONSIVE

    def test_disclosure_and_silence_in_the_same_call_escalates_to_emergency(self, profile):
        """Protects: a state-machine seam. A user who says 'I used' in the same
        evaluation that reports silence must reach Tier 4 — the disclosure does
        not have to be committed on a prior turn first."""
        result = evaluate(
            Tier.BASELINE, utterance="I used", silent_seconds=30, profile=profile
        )
        assert result.tier is Tier.EMERGENCY

    def test_tier_5_does_not_walk_back_to_tier_4_on_movement(self, profile):
        """Protects: sensor data causing de-escalation. An unresponsive user
        whose phone is jostled must not be downgraded — movement is not
        consciousness."""
        result = evaluate(Tier.UNRESPONSIVE, silent_seconds=90, still=False, profile=profile)
        assert result.tier is Tier.UNRESPONSIVE


# ==========================================================================
# 6. Standing conditions (Tier 1)
# ==========================================================================
class TestStandingConditions:
    """Tier 1 comes from the user's situation, not from anything they said."""

    def test_missed_checkins_at_threshold_elevate(self, profile):
        """Protects: the PRD's missed-check-in rule at its exact threshold."""
        result = evaluate(Tier.BASELINE, missed_checkins=2, profile=profile)
        assert result.tier is Tier.ELEVATED
        assert "missed check-ins" in result.reason

    def test_missed_checkins_below_threshold_do_not_elevate(self, profile):
        """Protects: elevating on a single missed ping, which would make Tier 1
        meaningless through overuse."""
        assert evaluate(Tier.BASELINE, missed_checkins=1, profile=profile).tier is Tier.BASELINE

    def test_missed_checkin_threshold_is_user_configurable(self):
        """Protects: PRD P3. A user who has set the threshold to 4 must not be
        elevated at 2."""
        relaxed = make_profile(missed_to_elevate=4)
        assert evaluate(Tier.BASELINE, missed_checkins=3, profile=relaxed).tier is Tier.BASELINE
        assert evaluate(Tier.BASELINE, missed_checkins=4, profile=relaxed).tier is Tier.ELEVATED

    def test_overshooting_the_threshold_still_elevates(self, profile):
        """Protects: an `==` comparison. A phone off overnight jumps from 0 to
        5 missed check-ins and must still elevate."""
        assert evaluate(Tier.BASELINE, missed_checkins=5, profile=profile).tier is Tier.ELEVATED

    def test_high_risk_window_elevates(self, profile):
        """Protects: the user-defined risk window (payday, an anniversary)
        being ignored."""
        result = evaluate(Tier.BASELINE, high_risk_window=True, profile=profile)
        assert result.tier is Tier.ELEVATED
        assert "high-risk window" in result.reason

    def test_all_standing_reasons_are_reported_not_just_the_first(self, profile):
        """Protects: PRD §11, no hidden state. If three things are elevating the
        user, the UI shows all three."""
        result = evaluate(
            Tier.BASELINE, missed_checkins=3, high_risk_window=True, profile=profile
        )
        assert "high-risk window" in result.reason
        assert "missed check-ins" in result.reason

    def test_standing_conditions_never_lower_a_higher_tier(self, profile):
        """Protects: Tier 1 acting as a ceiling instead of a floor. A user at
        Tier 3 with missed check-ins stays at Tier 3."""
        result = evaluate(Tier.ACTIVE_USE, missed_checkins=5, profile=profile)
        assert result.tier is Tier.ACTIVE_USE


# ==========================================================================
# 7. De-escalation
# ==========================================================================
class TestDeEscalation:
    """Tiers fall only through explicit action or a calm utterance — never
    silently, and never at all from an emergency."""

    def test_calm_utterance_steps_down_exactly_one_tier(self):
        """Protects: a jump from 3 to 0 on one 'I'm fine', which would cancel
        the elevated check-in cadence at the worst possible hour."""
        result = evaluate(Tier.ACTIVE_USE, utterance="I'm okay")
        assert result.tier is Tier.CRAVING
        assert result.previous_tier is Tier.ACTIVE_USE

    def test_repeated_calm_utterances_walk_all_the_way_down(self):
        """Protects: a user being stranded at an elevated tier with no path
        back. The ladder must be walkable in both directions."""
        tier = Tier.ACTIVE_USE
        for _ in range(5):
            tier = evaluate(tier, utterance="I'm okay").tier
        assert tier is Tier.BASELINE

    def test_de_escalation_never_falls_below_the_standing_floor(self, profile):
        """Protects: a calm phrase erasing a real situational risk. Three days
        out of detox, 'I'm fine' cannot take you below Tier 1."""
        result = evaluate(
            Tier.CRAVING, utterance="I'm okay", missed_checkins=3, profile=profile
        )
        assert result.tier is Tier.ELEVATED

    @pytest.mark.parametrize("tier", [Tier.EMERGENCY, Tier.UNRESPONSIVE])
    @pytest.mark.parametrize(
        "utterance",
        ["I'm okay", "I'm fine", "false alarm", "never mind", "I'm safe", "I'm good"],
    )
    def test_emergency_tiers_never_auto_de_escalate(self, tier, utterance):
        """THE most important de-escalation test.

        Protects: a dying user talking the system out of helping them. Opioid
        overdose impairs judgement before it impairs speech, so 'I'm fine' at
        Tier 4 is exactly what someone says on the way down. Only the deliberate
        physical act of `rescind` stands an emergency down.
        """
        result = evaluate(tier, utterance=utterance)
        assert result.tier is tier, f"{tier} de-escalated on {utterance!r}"
        assert "never de-escalate" in result.reason

    def test_escalating_signal_vetoes_a_calm_one_in_the_same_utterance(self):
        """Protects: mixed messages resolving the wrong way. 'I'm okay but I'm
        craving' must not step down."""
        result = evaluate(Tier.ACTIVE_USE, utterance="I'm okay but I'm craving")
        assert result.tier is Tier.ACTIVE_USE

    def test_silence_never_de_escalates(self, profile):
        """Protects: the inverse of the silence rule. Silence can only ever
        raise a tier; a quiet user is never assumed to be a recovered one."""
        result = evaluate(Tier.CRAVING, silent_seconds=3600, profile=profile)
        assert result.tier is Tier.CRAVING

    def test_no_input_at_all_holds_the_tier(self, profile):
        """Protects: the ladder drifting downward on empty evaluations. A tick
        with no evidence is not evidence of improvement."""
        for tier in Tier:
            assert evaluate(tier, profile=profile).tier is tier

    def test_calm_utterance_at_baseline_is_a_no_op(self):
        """Protects: an underflow below Tier 0."""
        result = evaluate(Tier.BASELINE, utterance="I'm okay")
        assert result.tier is Tier.BASELINE


# ==========================================================================
# 8. Rescind
# ==========================================================================
class TestRescind:
    """The one-tap false alarm — the only door down from an emergency."""

    @pytest.mark.parametrize(
        "tier", [Tier.CRAVING, Tier.ACTIVE_USE, Tier.EMERGENCY, Tier.UNRESPONSIVE]
    )
    def test_rescind_drops_to_tier_1_from_any_elevated_tier(self, tier):
        """Protects: a user trapped in an alarm they know is false. It must
        work from Tier 5 as readily as from Tier 2."""
        result = rescind(tier)
        assert result.tier is Tier.ELEVATED
        assert result.previous_tier is tier

    def test_rescind_lands_on_tier_1_not_tier_0(self):
        """Protects: a stand-down also cancelling the elevated check-in
        cadence. Someone who just cancelled an emergency is not baseline."""
        assert rescind(Tier.EMERGENCY).tier is not Tier.BASELINE

    def test_rescind_at_or_below_tier_1_is_a_no_op(self):
        """Protects: rescind pushing a baseline user UP to Tier 1."""
        assert rescind(Tier.BASELINE).tier is Tier.BASELINE
        assert rescind(Tier.ELEVATED).tier is Tier.ELEVATED

    def test_rescind_reason_records_it_as_a_user_action(self):
        """Protects: an audit log that cannot distinguish a user stand-down
        from a system de-escalation. The caregiver reading the log needs to
        know a human pressed the button."""
        result = rescind(Tier.EMERGENCY)
        assert "rescinded by the user" in result.reason
        assert "false alarm" in result.reason

    def test_rescind_clears_caregiver_notification(self):
        """Protects: a rescinded alarm still paging the contact tree."""
        assert rescind(Tier.UNRESPONSIVE).notify_caregiver is False

    def test_rescind_is_the_only_path_down_from_emergency(self):
        """Protects: the invariant as a whole. No utterance, silence value, or
        sensor combination may achieve what rescind achieves."""
        for utterance in ["I'm okay", "false alarm", "I'm fine", None]:
            for silence in [0, 10, 30, 600]:
                for still in [True, False]:
                    result = evaluate(
                        Tier.EMERGENCY,
                        utterance=utterance,
                        silent_seconds=silence,
                        still=still,
                    )
                    assert result.tier >= Tier.EMERGENCY
        assert rescind(Tier.EMERGENCY).tier is Tier.ELEVATED


# ==========================================================================
# 9. Caregiver notification
# ==========================================================================
class TestCaregiverNotification:
    """PRD §4.2 — the boundary between the user's privacy and their safety."""

    @pytest.mark.parametrize("tier", [Tier.BASELINE, Tier.ELEVATED, Tier.CRAVING])
    def test_low_tiers_never_notify(self, tier, profile):
        """Protects: a craving being reported to family. Craving is not news;
        reporting it trains the user to stop naming it."""
        assert evaluate(tier, profile=profile).notify_caregiver is False

    def test_tier_3_is_hidden_by_default(self, profile):
        """Protects: the privacy default. A user who has not opted in must not
        have a disclosure of use reported — fear of reporting is what drives
        people to use alone."""
        result = evaluate(Tier.BASELINE, utterance="I used", profile=profile)
        assert result.tier is Tier.ACTIVE_USE
        assert result.notify_caregiver is False

    def test_tier_3_notifies_when_the_user_opted_in(self):
        """Protects: the opt-in being ignored. A user who chose to share Tier 3
        must actually have it shared."""
        opted_in = make_profile(tier_3_visible=True)
        result = evaluate(Tier.BASELINE, utterance="I used", profile=opted_in)
        assert result.notify_caregiver is True

    @pytest.mark.parametrize("tier", [Tier.EMERGENCY, Tier.UNRESPONSIVE])
    def test_emergency_tiers_always_notify_even_with_everything_disabled(
        self, tier, locked_down_profile
    ):
        """THE non-negotiable rule.

        Protects: a user's privacy settings suppressing a call for help. Tiers 4
        and 5 sit outside `USER_CONTROLLABLE_TIERS` in models.py, and this test
        is the enforcement of that. The user is told this during onboarding —
        it is a disclosed boundary, not a hidden override.
        """
        result = evaluate(tier, profile=locked_down_profile)
        assert result.notify_caregiver is True

    def test_emergency_notifies_with_no_profile_at_all(self):
        """Protects: a missing profile (onboarding, bystander mode) being read
        as 'no consent, so do not call'. Absent configuration must fail toward
        help at Tier 4."""
        assert evaluate(Tier.EMERGENCY).notify_caregiver is True

    def test_notify_caregiver_for_is_public_api(self):
        """Protects: `app/main.py` needs this rule for the manual-override
        endpoint, where no `evaluate` call produces the result. Exposing it
        publicly stops that caller reaching into a private name and then
        breaking silently when the private name is renamed."""
        from app.triage import notify_caregiver_for

        assert notify_caregiver_for(Tier.EMERGENCY, make_profile()) is True
        assert notify_caregiver_for(Tier.UNRESPONSIVE, make_profile()) is True
        assert notify_caregiver_for(Tier.CRAVING, make_profile()) is False
        assert notify_caregiver_for(Tier.ACTIVE_USE, make_profile(tier_3_visible=True)) is True
        assert notify_caregiver_for(Tier.ACTIVE_USE, make_profile(tier_3_visible=False)) is False

    def test_emergency_reached_by_silence_also_notifies(self, locked_down_profile):
        """Protects: the override applying to phrase-driven escalation but not
        to sensor-driven escalation. The tier is what matters, not its cause."""
        result = evaluate(Tier.ACTIVE_USE, silent_seconds=60, profile=locked_down_profile)
        assert result.tier is Tier.EMERGENCY
        assert result.notify_caregiver is True


# ==========================================================================
# 10. Actions and the PRD §4.3 timing table
# ==========================================================================
class TestActions:
    """The timing table is a clinical protocol, not a UI detail."""

    def test_baseline_emits_no_actions(self):
        """Protects: an app that nags at baseline. Nagging gets it uninstalled,
        and an uninstalled app saves nobody."""
        assert actions_for_tier(Tier.BASELINE) == []

    def test_naloxone_prompt_and_911_are_both_at_second_zero(self):
        """THE parallelism rule, called out explicitly in the PRD.

        Protects: a sequential implementation where naloxone gates the 911 call
        or vice versa. Naloxone wears off before an ambulance arrives; an
        ambulance arrives too late without naloxone. Both, immediately.
        """
        actions = actions_for_tier(Tier.EMERGENCY)
        naloxone = [a for a in actions if a.kind == "naloxone_prompt"]
        call = [a for a in actions if a.kind == "offer_contact"]
        assert naloxone and call
        assert naloxone[0].at_second == 0
        assert call[0].at_second == 0

    @pytest.mark.parametrize(
        "kind,at_second",
        [
            ("naloxone_prompt", 0),
            ("acquire_location", 5),
            ("bystander_hail", 10),
            ("show_emergency_script", 15),
            ("fire_contact_tree", 30),
        ],
    )
    def test_emergency_timing_table_matches_the_prd(self, kind, at_second):
        """Protects: drift in the §4.3 schedule. Each beat is here because of
        what is happening to the body at that second."""
        actions = actions_for_tier(Tier.EMERGENCY)
        found = [a for a in actions if a.kind == kind]
        assert found, f"{kind} missing from Tier 4"
        assert found[0].at_second == at_second

    def test_unresponsive_fires_the_contact_tree_immediately(self):
        """Protects: a Tier 5 user waiting 30 seconds for help. The 30s delay at
        Tier 4 exists to allow a rescind; an unresponsive user cannot rescind,
        so the delay has no purpose and only costs time."""
        actions = actions_for_tier(Tier.UNRESPONSIVE)
        tree = [a for a in actions if a.kind == "fire_contact_tree"]
        assert tree[0].at_second == 0

    def test_unresponsive_adds_rescue_breathing(self):
        """Protects: the bystander being given no physical instruction at the
        one tier where the user cannot act for themselves."""
        kinds = {a.kind for a in actions_for_tier(Tier.UNRESPONSIVE)}
        assert "rescue_breathing" in kinds

    def test_good_samaritan_brief_accompanies_the_bystander_hail(self):
        """Protects: the single biggest reason bystanders do not call 911 —
        fear of arrest. The legal brief must arrive with the hail, not after."""
        actions = actions_for_tier(Tier.EMERGENCY)
        hail = [a for a in actions if a.kind == "bystander_hail"][0]
        legal = [a for a in actions if a.kind == "show_good_samaritan"][0]
        assert legal.at_second == hail.at_second

    def test_naloxone_wording_reflects_whether_any_is_on_hand(self):
        """Protects: telling a user to use a kit they do not own, or sending
        them to beg for one they have in the drawer."""
        has = actions_for_tier(Tier.EMERGENCY, make_profile(naloxone=True))
        hasnt = actions_for_tier(Tier.EMERGENCY, make_profile(naloxone=False))
        assert "on file at this address" in [a for a in has if a.kind == "naloxone_prompt"][0].detail
        assert "No naloxone on file" in [a for a in hasnt if a.kind == "naloxone_prompt"][0].detail

    def test_tier_3_pre_arms_the_bystander_path_before_it_is_needed(self):
        """Protects: Tier 4 having to cold-start the bystander flow. Setup
        happens at Tier 3, while the user can still participate in it."""
        kinds = {a.kind for a in actions_for_tier(Tier.ACTIVE_USE)}
        assert "arm_bystander_mode" in kinds
        assert "naloxone_prompt" in kinds

    def test_actions_on_the_result_match_the_reported_tier(self, profile):
        """Protects: the class of bug where a Tier 4 result carries Tier 3
        actions. Every exit builds its actions from the decided tier."""
        result = evaluate(Tier.ACTIVE_USE, silent_seconds=60, profile=profile)
        assert result.tier is Tier.EMERGENCY
        assert result.actions == actions_for_tier(Tier.EMERGENCY, profile)

    def test_every_tier_produces_at_least_one_action_above_baseline(self):
        """Protects: a silent tier — an escalation the user can see on the
        ladder but which does nothing."""
        for tier in Tier:
            if tier is Tier.BASELINE:
                continue
            assert actions_for_tier(tier), f"{tier} has no actions"


# ==========================================================================
# 11. Tolerance-reset window
# ==========================================================================
class TestToleranceWindow:
    """The 90-day post-detox window — the highest-mortality period in the
    condition, and therefore the boundary most worth testing to the day."""

    def _profile_with_event_days_ago(self, days: int, kind: str = "detox") -> UserProfile:
        return make_profile(
            tolerance_events=[
                ToleranceEvent(kind=kind, date=NOW - timedelta(days=days))
            ]
        )

    def test_recent_event_opens_the_window(self):
        """Protects: the window failing to arm at all."""
        active, label = tolerance_window_active(self._profile_with_event_days_ago(3), NOW)
        assert active is True
        assert "detox" in label and "3 days ago" in label

    def test_day_89_is_inside_the_window(self):
        """Boundary, safe side."""
        assert tolerance_window_active(self._profile_with_event_days_ago(89), NOW)[0] is True

    def test_day_90_is_still_inside_the_window(self):
        """Boundary, exact. The window is inclusive of day 90 — an off-by-one
        here closes the window a day early on a real user."""
        assert tolerance_window_active(self._profile_with_event_days_ago(90), NOW)[0] is True

    def test_day_91_is_outside_the_window(self):
        """Boundary, other side. Protects a window that never closes, which
        would make Tier 1 permanent and therefore meaningless."""
        assert tolerance_window_active(self._profile_with_event_days_ago(91), NOW)[0] is False

    def test_window_constant_is_the_documented_90_days(self):
        """Protects: silent drift of a clinically chosen constant."""
        assert TOLERANCE_WINDOW_DAYS == 90

    def test_future_dated_events_do_not_open_the_window(self):
        """Protects: a scheduled discharge pinning the user at Tier 1 for
        weeks before it happens."""
        future = self._profile_with_event_days_ago(-10, kind="hospital_discharge")
        assert tolerance_window_active(future, NOW)[0] is False

    def test_most_recent_qualifying_event_is_the_one_named(self):
        """Protects: the UI naming a stale event. A user with a detox six weeks
        ago and a discharge yesterday should see the discharge, because that is
        what is driving current risk."""
        p = make_profile(
            tolerance_events=[
                ToleranceEvent(kind="detox", date=NOW - timedelta(days=45)),
                ToleranceEvent(kind="hospital_discharge", date=NOW - timedelta(days=1)),
            ]
        )
        _, label = tolerance_window_active(p, NOW)
        assert "hospital discharge" in label

    def test_expired_event_alongside_a_live_one_still_opens_the_window(self):
        """Protects: an old event masking a recent one during iteration."""
        p = make_profile(
            tolerance_events=[
                ToleranceEvent(kind="detox", date=NOW - timedelta(days=400)),
                ToleranceEvent(kind="abstinence", date=NOW - timedelta(days=2)),
            ]
        )
        assert tolerance_window_active(p, NOW)[0] is True

    def test_open_window_elevates_to_tier_1(self):
        """Protects: the window being computed but not acted on."""
        result = evaluate(Tier.BASELINE, profile=self._profile_with_event_days_ago(5), now=NOW)
        assert result.tier is Tier.ELEVATED
        assert "tolerance-reset window" in result.reason

    def test_closed_window_leaves_the_user_at_baseline(self):
        """Protects: permanent elevation, which erodes the meaning of Tier 1."""
        result = evaluate(Tier.BASELINE, profile=self._profile_with_event_days_ago(120), now=NOW)
        assert result.tier is Tier.BASELINE

    def test_no_events_needs_no_clock(self):
        """Protects: a mandatory `now` on the common path. Most users have no
        tolerance events and must not require a clock to be triaged."""
        assert tolerance_window_active(make_profile(), None) == (False, None)
        assert evaluate(Tier.BASELINE, utterance="I'm craving").tier is Tier.CRAVING

    def test_missing_clock_with_events_raises_rather_than_silently_skipping(self):
        """Protects: the worst available failure mode. Silently returning False
        would suppress a fatal-risk window; reading the wall clock would break
        purity. Failing loudly is the only acceptable third option."""
        with pytest.raises(ValueError, match="now="):
            tolerance_window_active(self._profile_with_event_days_ago(5), None)

    def test_naive_and_aware_datetimes_interoperate(self):
        """Protects: a TypeError from mixed tz-awareness taking down the whole
        triage call. Seeded data and JSON round-trips produce naive datetimes."""
        naive = make_profile(
            tolerance_events=[
                ToleranceEvent(kind="detox", date=datetime(2026, 7, 20, 12, 0, 0))
            ]
        )
        assert tolerance_window_active(naive, NOW)[0] is True
        assert tolerance_window_active(naive, NOW.replace(tzinfo=None))[0] is True

    def test_de_escalation_floor_respects_an_open_window(self):
        """Protects: 'I'm fine' erasing a post-detox window. The floor holds."""
        p = self._profile_with_event_days_ago(2)
        result = evaluate(Tier.CRAVING, utterance="I'm okay", profile=p, now=NOW)
        assert result.tier is Tier.ELEVATED


# ==========================================================================
# 12. Purity and determinism
# ==========================================================================
class TestPurity:
    """The architectural invariants from CONTRACT.md, asserted as tests rather
    than trusted as conventions."""

    def test_module_never_imports_genai(self):
        """Protects: CONTRACT.md's hardest invariant. If triage ever imports
        genai, a network failure or a missing API key becomes a failure to
        triage an overdose. Asserted on the source text so it fails even if the
        import is lazy or conditional."""
        from pathlib import Path

        import app.triage as triage_module

        source = Path(triage_module.__file__).read_text()
        code_lines = [
            line
            for line in source.splitlines()
            if line.startswith(("import ", "from "))
        ]
        assert not any("genai" in line for line in code_lines)

    def test_no_network_or_clock_modules_are_imported(self):
        """Protects: an incidental `requests`, `httpx`, or `time` import
        creeping in and re-introducing nondeterminism or I/O."""
        from pathlib import Path

        import app.triage as triage_module

        source = Path(triage_module.__file__).read_text()
        for banned in ("import requests", "import httpx", "import socket", "import random"):
            assert banned not in source

    def test_repeated_evaluation_is_identical(self, profile):
        """Protects: hidden state or randomness. The same inputs must give
        byte-identical results forever — a safety decision that varies between
        calls cannot be audited after the fact."""
        kwargs = dict(
            utterance="I just took it", silent_seconds=5, profile=profile, now=NOW
        )
        first = evaluate(Tier.ELEVATED, **kwargs)
        second = evaluate(Tier.ELEVATED, **kwargs)
        assert first.model_dump() == second.model_dump()

    def test_evaluate_does_not_mutate_the_profile(self, profile):
        """Protects: triage quietly rewriting user configuration. It is a read
        of the world, not a write to it."""
        before = profile.model_dump()
        evaluate(Tier.ACTIVE_USE, utterance="help", silent_seconds=90, profile=profile)
        assert profile.model_dump() == before

    def test_result_reports_the_previous_tier_on_every_path(self, profile):
        """Protects: an audit trail that cannot show what changed. The event log
        and the SSE stream both diff previous against current."""
        for tier in Tier:
            assert evaluate(tier, profile=profile).previous_tier is tier

    def test_reason_is_always_populated_and_names_the_tier(self, profile):
        """Protects: a blank explanation reaching the UI. Every path, including
        the boring hold paths, owes the user a sentence."""
        cases = [
            dict(utterance="I'm craving"),
            dict(utterance="I used"),
            dict(utterance="help"),
            dict(utterance="I'm okay"),
            dict(silent_seconds=60),
            dict(still=True, silent_seconds=60),
            dict(missed_checkins=3),
            dict(),
        ]
        for tier in Tier:
            for case in cases:
                result = evaluate(tier, profile=profile, **case)
                assert result.reason.strip(), (tier, case)
                assert f"Tier {result.tier.value}" in result.reason, (tier, case)

    def test_integer_tiers_are_accepted_as_well_as_enum_members(self):
        """Protects: the HTTP layer. `POST /api/tier {tier}` arrives as a JSON
        integer, and must not need the caller to remember to coerce it."""
        assert evaluate(3, utterance="help").tier is Tier.EMERGENCY
        assert rescind(4).tier is Tier.ELEVATED

    def test_every_reachable_tier_is_reachable(self):
        """Protects: a dead tier — one defined in models.py that no input can
        ever produce, which would mean the ladder in the UI is a fiction."""
        reached = {
            evaluate(Tier.BASELINE).tier,
            evaluate(Tier.BASELINE, missed_checkins=9).tier,
            evaluate(Tier.BASELINE, utterance="I'm craving").tier,
            evaluate(Tier.BASELINE, utterance="I used").tier,
            evaluate(Tier.BASELINE, utterance="help").tier,
            evaluate(Tier.ACTIVE_USE, silent_seconds=99, still=True).tier,
        }
        assert reached == set(Tier)


# ---------------------------------------------------------------------------
# Overdose descriptions in plain speech
# ---------------------------------------------------------------------------
# Added after review found that the table caught clinical phrasings but missed
# the words people actually use. "she won't wake up" — the single most likely
# sentence a frightened bystander says out loud — triaged to Baseline.
import pytest

from app import triage as _triage
from app.models import Tier as _Tier


@pytest.mark.parametrize(
    "utterance",
    [
        "she wont wake up",
        "I can't wake him up",
        "he is unresponsive",
        "she's not responding",
        "he passed out",
        "she is unconscious",
        "he is nodding out",
        "he nodded off and won't wake up",
        "his lips are blue",
        "she's turning grey",
        "he is gurgling",
        "she's gasping",
    ],
)
def test_plain_speech_overdose_descriptions_reach_emergency(utterance):
    """These are the words a bystander uses. Missing one costs a life.

    Every phrase here describes an opioid overdose in progress — sedation,
    unrousability, cyanosis, or agonal breathing. None of them contain the word
    "overdose", which is exactly why they need their own rows.
    """
    result = _triage.evaluate(_Tier.BASELINE, utterance=utterance)
    assert result.tier == _Tier.EMERGENCY, f"{utterance!r} did not reach Tier 4"


@pytest.mark.parametrize(
    "utterance",
    [
        "I passed out at the party last year",   # autobiography, not an event
        "she wont wake up early for work",       # a complaint about a schedule
        "the sky turned blue",                   # weather
        "my shirt is blue",                      # clothing
        "I was choking on my drink",             # ordinary, self-resolving
        "I nodded in agreement",                 # agreement, not sedation
        "I snore at night",                      # sleep, not agonal breathing
        "I woke up fine",                        # the opposite of the signal
    ],
)
def test_ordinary_speech_does_not_trigger_an_emergency(utterance):
    """The precision half. Every one of these was a real false positive.

    A false Tier 4 wakes a family member at 3am over nothing. Do that twice and
    the user stops talking to the app — and a user who has gone quiet is the
    failure mode this product exists to prevent (PRD §12, concealment risk).
    """
    result = _triage.evaluate(_Tier.BASELINE, utterance=utterance)
    assert result.tier < _Tier.EMERGENCY, f"{utterance!r} falsely escalated"


# ---------------------------------------------------------------------------
# Suicidal statements
# ---------------------------------------------------------------------------
# THE MOST IMPORTANT TESTS IN THIS FILE.
#
# Before these signals existed, "I am ending my life right now" triaged to
# Tier 0 and the product did nothing at all. Overdose and suicide are not
# separate populations — overdose deaths are frequently intentional or
# ambiguous in intent — so a recovery product that stays silent here is not
# making a neutral omission.


@pytest.mark.parametrize(
    "utterance",
    [
        "I am ending my life right now",
        "I want to kill myself",
        "I am going to end it tonight",
        "I don't want to be here anymore",
        "I want to die",
        "I feel suicidal",
        "I don't want to live",
        "I am going to hurt myself",
        "I took all the pills",
        "I took the whole bottle",
    ],
)
def test_suicidal_statements_reach_emergency(utterance):
    """Every one of these must reach Tier 4 and notify someone."""
    result = _triage.evaluate(_Tier.BASELINE, utterance=utterance)
    assert result.tier == _Tier.EMERGENCY, f"{utterance!r} did not escalate"
    assert result.notify_caregiver is True, f"{utterance!r} told nobody"


@pytest.mark.parametrize(
    "utterance",
    ["I don't want to kill myself", "I'm not going to hurt myself"],
)
def test_negated_suicidal_statements_still_escalate(utterance):
    """Negation is DISABLED on these rows, deliberately.

    "I don't want to kill myself" is something people say while in serious
    distress, and is frequently a disclosure rather than a denial. Everywhere
    else in this table a false positive costs one rescind tap; here a false
    negative costs a life, so the usual asymmetry is inverted on purpose.
    """
    result = _triage.evaluate(_Tier.BASELINE, utterance=utterance)
    assert result.tier == _Tier.EMERGENCY


def test_suicidal_statement_surfaces_the_crisis_line_first():
    """Tele-MANAS leads while emergency services stay one tap away."""
    result = _triage.evaluate(
        _Tier.BASELINE, utterance="I am ending my life right now"
    )
    assert result.actions[0].kind == "show_crisis_line"
    assert "14416" in result.actions[0].detail
    # The physical emergency actions still run: intent and overdose co-occur,
    # and dropping either set would be a guess about which is happening.
    assert any(a.kind == "naloxone_prompt" for a in result.actions)


def test_physical_emergency_does_not_surface_the_crisis_line():
    """Someone who cannot breathe needs naloxone and an ambulance, not a
    counselling line at the top of the screen."""
    result = _triage.evaluate(_Tier.BASELINE, utterance="I can't breathe")
    assert result.tier == _Tier.EMERGENCY
    assert all(a.kind != "show_crisis_line" for a in result.actions)


def test_ordinary_speech_does_not_trigger_a_crisis_response():
    """Precision guard. These are figures of speech, not disclosures."""
    for utterance in (
        "that meeting killed me",
        "I'm dying to see her",
        "this hill is going to end me",
        "I took all the trash out",
    ):
        result = _triage.evaluate(_Tier.BASELINE, utterance=utterance)
        assert result.tier < _Tier.EMERGENCY, f"{utterance!r} falsely escalated"
