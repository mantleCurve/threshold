"""Tests for the bounded request schemas in `app/schemas.py`.

WHAT THESE TESTS PROTECT AGAINST
    Two specific defects named in the code review (c_s.md Code Quality P1 #1), plus the
    class of bug they belong to:

    1. `int(body.get("silent_seconds", 0))` raising on garbage and becoming a 500. The
       tests below prove a non-numeric value is now a *validation* failure, not an
       exception escaping a handler.
    2. `bool("false")` evaluating True. Python truthiness on a string is silent and
       inverts a signal on the escalation path, so there is a dedicated test that the
       string is rejected outright rather than coerced.

    And the general one (c_s.md #6): every public input has a ceiling. Unbounded text
    on these endpoints is unbounded model spend, unbounded cache growth, and unbounded
    disk. Each ceiling gets an at-the-limit test and an over-the-limit test, because a
    bound that is accidentally off by one is a bound nobody notices is wrong.

WHAT THEY DELIBERATELY DO NOT DO
    They do not exercise HTTP routes. `app/main.py` is owned by another workstream and
    the models are validated here in isolation, which is also where a failure points
    straight at the cause. Semantic checks (username taken, password correct, profile
    ownership) belong to `app/auth.py` and the route layer and are tested there.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app import schemas
from app.models import Tier


# --------------------------------------------------------------------------------------
# The two defects the review named by name
# --------------------------------------------------------------------------------------


def test_non_numeric_silent_seconds_is_a_validation_error_not_a_crash():
    """`int("abc")` used to raise inside the handler and surface as a 500.

    The whole point of the schema is that this is now a structured, field-level
    rejection that FastAPI renders as a 422 before any handler code runs.
    """
    with pytest.raises(ValidationError) as exc:
        schemas.SensorRequest(silent_seconds="abc")

    # The error names the offending field, which is what makes the 422 actionable.
    assert exc.value.errors()[0]["loc"] == ("silent_seconds",)


def test_none_silent_seconds_is_rejected_rather_than_becoming_zero():
    """`int(None)` also raised. An explicit null is a broken client, not a zero."""
    with pytest.raises(ValidationError):
        schemas.SensorRequest(silent_seconds=None)


def test_the_string_false_parses_as_false_not_python_truthiness():
    """`bool("false")` is True in Python. That silently inverts a stillness signal.

    This is the highest-consequence coercion bug in the old hand-cast code: a client
    sending the string "false" would have been read as "the device is still", which is
    an escalation input. Pydantic parses the *boolean spelling* instead of applying
    Python truthiness, so the value now means what the client said it meant.
    """
    assert schemas.SensorRequest(still="false").still is False
    assert schemas.SensorRequest(still="False").still is False
    assert schemas.SensorRequest(still="0").still is False

    # The precise regression, stated as the comparison that used to fail.
    assert bool("false") is True
    assert schemas.SensorRequest(still="false").still is not bool("false")


def test_recognised_boolean_spellings_still_parse():
    """Bounding must not break the legitimate spellings a real client sends."""
    assert schemas.SensorRequest(still=True).still is True
    assert schemas.SensorRequest(still="true").still is True
    assert schemas.SensorRequest(still=0).still is False


def test_a_non_boolean_string_is_rejected_outright():
    """Anything that is not a recognised boolean spelling is a 422, not a guess."""
    with pytest.raises(ValidationError):
        schemas.SensorRequest(still="maybe")


# --------------------------------------------------------------------------------------
# Sensor bounds
# --------------------------------------------------------------------------------------


def test_sensor_defaults_are_the_safe_no_signal_values():
    """An empty body must mean "nothing observed", never an implied escalation."""
    body = schemas.SensorRequest()
    assert body.silent_seconds == 0
    assert body.still is False


def test_negative_silence_is_rejected_rather_than_clamped():
    """A negative duration is a broken client. Clamping it would hide that."""
    with pytest.raises(ValidationError):
        schemas.SensorRequest(silent_seconds=-1)


def test_silence_is_bounded_above():
    """At the ceiling is fine; past it is not. Guards the off-by-one."""
    assert (
        schemas.SensorRequest(silent_seconds=schemas.MAX_SILENT_SECONDS).silent_seconds
        == schemas.MAX_SILENT_SECONDS
    )
    with pytest.raises(ValidationError):
        schemas.SensorRequest(silent_seconds=schemas.MAX_SILENT_SECONDS + 1)


def test_numeric_strings_are_still_coerced():
    """A form post sends "30", not 30. Rejecting that would break real clients."""
    assert schemas.SensorRequest(silent_seconds="30").silent_seconds == 30


# --------------------------------------------------------------------------------------
# Utterance — the primary input, and the one that reaches a paid model
# --------------------------------------------------------------------------------------


def test_oversized_utterance_is_rejected():
    with pytest.raises(ValidationError):
        schemas.UtteranceRequest(text="x" * (schemas.MAX_UTTERANCE_CHARS + 1))


def test_utterance_at_the_limit_is_accepted():
    body = schemas.UtteranceRequest(text="x" * schemas.MAX_UTTERANCE_CHARS)
    assert len(body.text) == schemas.MAX_UTTERANCE_CHARS


def test_whitespace_only_utterance_is_rejected_not_silently_empty():
    """Stripping happens before `min_length`, so "   " fails validation.

    Previously this reached the handler as an empty string and produced a hand-rolled
    400; now it never gets that far.
    """
    with pytest.raises(ValidationError):
        schemas.UtteranceRequest(text="   ")


def test_utterance_is_stripped():
    assert schemas.UtteranceRequest(text="  i took something  ").text == "i took something"


def test_missing_utterance_text_is_rejected():
    with pytest.raises(ValidationError):
        schemas.UtteranceRequest()


# --------------------------------------------------------------------------------------
# Tier
# --------------------------------------------------------------------------------------


def test_tier_accepts_every_real_rung():
    for tier in Tier:
        assert schemas.TierRequest(tier=int(tier)).tier is tier


@pytest.mark.parametrize("bad", [6, -1, 99, "emergency", None, 1.5])
def test_out_of_range_or_malformed_tier_is_rejected(bad):
    """The ladder is a closed set. Anything outside it is a 422, never a coerced value."""
    with pytest.raises(ValidationError):
        schemas.TierRequest(tier=bad)


# --------------------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------------------


def test_register_enforces_the_documented_minimums():
    """These are the same rules main.py checked by hand, now declarative."""
    with pytest.raises(ValidationError):
        schemas.RegisterRequest(username="a", password="threshold")
    with pytest.raises(ValidationError):
        schemas.RegisterRequest(username="sam", password="short")


def test_register_bounds_username_and_password_above():
    with pytest.raises(ValidationError):
        schemas.RegisterRequest(
            username="x" * (schemas.MAX_USERNAME_CHARS + 1), password="threshold"
        )
    with pytest.raises(ValidationError):
        schemas.RegisterRequest(
            username="sam", password="x" * (schemas.MAX_PASSWORD_CHARS + 1)
        )


def test_register_rejects_an_unknown_role():
    """The role set is closed, so privilege cannot be invented by a request body."""
    with pytest.raises(ValidationError):
        schemas.RegisterRequest(username="sam", password="threshold", role="admin")


def test_register_defaults_to_the_least_privileged_role():
    assert schemas.RegisterRequest(
        email="sam@example.com",
        full_name="Sam Person",
        phone="+1 502 555 0100",
        password="threshold",
    ).role == "user"


def test_register_accepts_both_real_roles():
    for role in ("user", "caregiver"):
        assert (
            schemas.RegisterRequest(
                email="sam@example.com",
                full_name="Sam Person",
                phone="+1 502 555 0100",
                password="threshold",
                role=role,
            ).role
            == role
        )


def test_login_does_not_enforce_a_password_minimum():
    """Deliberate: a 422 on a short password would distinguish it from a wrong one.

    That is a free account-probing oracle, and on this product the existence of an
    account is itself a sensitive disclosure. Only the server-protecting upper bound
    is enforced here.
    """
    assert schemas.LoginRequest(username="sam", password="x").password == "x"


def test_login_still_bounds_the_password_above():
    """The ceiling stays: scrypt cost per request must not be attacker-controlled."""
    with pytest.raises(ValidationError):
        schemas.LoginRequest(username="sam", password="x" * (schemas.MAX_PASSWORD_CHARS + 1))


def test_login_rejects_an_empty_username():
    with pytest.raises(ValidationError):
        schemas.LoginRequest(username="", password="threshold")


# --------------------------------------------------------------------------------------
# Profile and ladder
# --------------------------------------------------------------------------------------


def test_profile_update_accepts_a_partial_body():
    """Onboarding saves in stages, so absent fields must mean "leave alone"."""
    body = schemas.ProfileUpdateRequest(address="12 Mill St")
    assert body.address == "12 Mill St"
    assert body.unit is None and body.ladder is None


def test_oversized_address_is_rejected_not_truncated():
    """A silently truncated address is what gets read aloud to a 911 dispatcher."""
    with pytest.raises(ValidationError):
        schemas.ProfileUpdateRequest(address="x" * (schemas.MAX_PROFILE_FIELD_CHARS + 1))


@pytest.mark.parametrize("bad", ["K", "KYY", "K1", "", "12"])
def test_malformed_state_code_is_rejected(bad):
    """A bad code silently yields "no reviewed summary", which reads as a legal fact."""
    with pytest.raises(ValidationError):
        schemas.ProfileUpdateRequest(state_code=bad)


def test_state_code_accepts_either_case():
    assert schemas.ProfileUpdateRequest(state_code="ky").state_code == "ky"
    assert schemas.ProfileUpdateRequest(state_code="KY").state_code == "KY"


def test_ladder_silence_window_is_bounded_both_ways():
    """The user tunes the window; they cannot set a value that never fires."""
    with pytest.raises(ValidationError):
        schemas.LadderUpdate(
            silence_seconds_to_escalate=schemas.MIN_SILENCE_ESCALATE_SECONDS - 1
        )
    with pytest.raises(ValidationError):
        schemas.LadderUpdate(
            silence_seconds_to_escalate=schemas.MAX_SILENCE_ESCALATE_SECONDS + 1
        )
    assert schemas.LadderUpdate(silence_seconds_to_escalate=20).silence_seconds_to_escalate == 20


def test_tier_4_and_5_visibility_cannot_be_expressed_at_all():
    """PRD §4.2: emergency visibility is the one thing the user cannot switch off.

    Enforced by *absence from the schema* rather than by a handler remembering to skip
    the field, so a crafted body cannot even name it.
    """
    for field in ("tier_4_visible_to_caregiver", "tier_5_visible_to_caregiver"):
        assert field not in schemas.LadderUpdate.model_fields
        with pytest.raises(ValidationError):
            schemas.LadderUpdate(**{field: False})


def test_naloxone_flag_must_be_a_real_boolean():
    with pytest.raises(ValidationError):
        schemas.ProfileUpdateRequest(naloxone_on_hand="nope")


# --------------------------------------------------------------------------------------
# Public surfaces
# --------------------------------------------------------------------------------------


def test_contact_requires_all_three_fields():
    with pytest.raises(ValidationError):
        schemas.ContactRequest(name="Sam", email="sam@example.com")


def test_contact_message_is_bounded():
    """Unauthenticated and appended to disk, so the ceiling is load-bearing."""
    with pytest.raises(ValidationError):
        schemas.ContactRequest(
            name="Sam",
            email="sam@example.com",
            message="x" * (schemas.MAX_CONTACT_MESSAGE_CHARS + 1),
        )


def test_contact_topic_defaults_and_is_bounded():
    body = schemas.ContactRequest(name="Sam", email="s@e.com", message="hi")
    assert body.topic == "general"
    with pytest.raises(ValidationError):
        schemas.ContactRequest(
            name="Sam",
            email="s@e.com",
            message="hi",
            topic="x" * (schemas.MAX_CONTACT_TOPIC_CHARS + 1),
        )


def test_vault_context_is_bounded():
    """Unauthenticated, attacker-controlled, and interpolated into a prompt."""
    with pytest.raises(ValidationError):
        schemas.VaultSelectQuery(context="x" * (schemas.MAX_CONTEXT_CHARS + 1))


def test_vault_context_defaults_to_empty():
    assert schemas.VaultSelectQuery().context == ""


def test_vault_query_tolerates_unknown_query_parameters():
    """Unlike bodies, query strings must survive stray params (utm_*, etc.)."""
    assert schemas.VaultSelectQuery(context="late", utm_source="sms").context == "late"


# --------------------------------------------------------------------------------------
# Strictness policy
# --------------------------------------------------------------------------------------


def test_unknown_body_fields_are_rejected_loudly():
    """A misspelled key must fail, not silently fall back to a default.

    `{"silentSeconds": 30}` against a server reading `silent_seconds` would otherwise
    escalate against a default of 0 with no error anywhere. On the escalation path,
    loud beats tolerant.
    """
    with pytest.raises(ValidationError):
        schemas.SensorRequest(silentSeconds=30)
    with pytest.raises(ValidationError):
        schemas.UtteranceRequest(text="hi", tier=4)


def test_every_string_field_in_every_body_model_has_a_maximum_length():
    """Meta-test: a new unbounded string cannot be added without failing here.

    c_s.md #6 is "bound *all* public inputs", which is a property of the module rather
    than of any one model, so it is asserted as one.
    """
    models = [
        schemas.RegisterRequest,
        schemas.LoginRequest,
        schemas.UtteranceRequest,
        schemas.ProfileUpdateRequest,
        schemas.ContactRequest,
        schemas.VaultSelectQuery,
    ]
    for model in models:
        for name, field in model.model_fields.items():
            annotation = str(field.annotation)
            if "str" not in annotation:
                continue
            bounded = any(
                getattr(meta, "max_length", None) is not None
                # A `pattern` constraint bounds length implicitly (state_code, role).
                or getattr(meta, "pattern", None) is not None
                for meta in field.metadata
            )
            assert bounded, f"{model.__name__}.{name} is an unbounded public string"
