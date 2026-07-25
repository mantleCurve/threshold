"""Tests for the in-memory Good Samaritan dataset cache (`app/legal.py`).

WHAT THESE TESTS PROTECT AGAINST
    1. **Silently serving nothing.** The dataset is the one thing in this product that
       must never be model-generated, because a hallucinated immunity claim could
       persuade someone that calling 911 is unsafe. If the file is malformed the cache
       must fail *loudly at boot*, not degrade into "we have no summary for your
       state" — which a user would read as a true statement about the law.
    2. **Losing the honesty disclosure.** Every record ships `verified: false` and the
       UI renders an "unverified — confirm locally" banner from it. A record missing
       that flag would render as reviewed law, so the loader treats it as a hard schema
       error rather than a defaulted field.
    3. **Re-parsing on every request.** The whole point of the module is that the file
       is read once. There is an explicit test that a request-path lookup does not
       touch the filesystem.

WHAT THEY DELIBERATELY DO NOT DO
    They do not assert on the *content* of any legal summary. The text is human-written
    and human-reviewed; a test asserting on its wording would break every time a human
    correctly improves it, and would give false confidence that the law was checked.
    Only structure is machine-checkable here.
"""

from __future__ import annotations

import json

import pytest

from app import legal


# --------------------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_index():
    """Give every test a cold cache, and leave the real one loaded on the way out.

    The index is process-global by design (it is a startup-populated cache), so tests
    must not leak a temp-file index into each other or into the API tests, which share
    the same interpreter.
    """
    legal.reset()
    yield
    legal.reset()


def _record(code: str = "KY", **overrides) -> dict:
    """Build a minimally-valid record. Tests then break exactly one field."""
    record = {
        "state_code": code,
        "state_name": "Kentucky",
        "has_immunity_law": True,
        "verified": False,
        "summary": "A conservative summary.",
        "does_not_cover": "Trafficking and unrelated offences.",
        "plain_language_line": "Call, and stay with them.",
        "source_note": "UNVERIFIED. Start at KRS Chapter 218A.",
    }
    record.update(overrides)
    return record


def _write(tmp_path, payload):
    """Write a dataset file and return its path."""
    path = tmp_path / "good_samaritan.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# --------------------------------------------------------------------------------------
# The shipped dataset
# --------------------------------------------------------------------------------------


def test_the_real_shipped_dataset_loads_and_validates():
    """The file we actually ship must pass its own schema. This is the regression net.

    If a human edits `data/legal/good_samaritan.json` and drops a required field, this
    test fails in CI rather than the server failing to boot in front of an evaluator.
    """
    count = legal.load()
    assert count > 0
    assert legal.is_loaded()


def test_the_shipped_dataset_contains_the_seeded_demo_state():
    """The seeded profile's `state_code` defaults to KY (app/models.py).

    A demo whose own state is missing from the dataset would show the "no reviewed
    summary" path on the happy path, which reads as a broken feature.
    """
    legal.load()
    record = legal.get("KY")
    assert record is not None
    assert record["state_name"]


def test_every_shipped_record_carries_its_verification_disclosure():
    """`verified` is an honest disclosure, not a TODO — it must be present on all."""
    legal.load()
    for code in legal.states():
        assert isinstance(legal.get(code)["verified"], bool)


# --------------------------------------------------------------------------------------
# Lookup behaviour
# --------------------------------------------------------------------------------------


def test_lookup_is_case_and_whitespace_insensitive(tmp_path):
    """The code arrives from a URL path segment, so normalise rather than 404."""
    legal.load(_write(tmp_path, {"states": [_record("KY")]}))
    for spelling in ("KY", "ky", "Ky", " ky "):
        assert legal.get(spelling)["state_code"] == "KY"


def test_an_unknown_state_returns_none_rather_than_raising(tmp_path):
    """Absence is a normal, expected answer — the route has honest copy for it."""
    legal.load(_write(tmp_path, {"states": [_record("KY")]}))
    assert legal.get("ZZ") is None


def test_state_codes_are_normalised_to_uppercase_on_load(tmp_path):
    """Normalise once at load, not on every request."""
    legal.load(_write(tmp_path, {"states": [_record("ky")]}))
    assert legal.states() == ["KY"]


def test_returned_records_are_copies_so_a_route_cannot_corrupt_the_cache(tmp_path):
    """The index lives for the process lifetime; a mutated record would poison it.

    A route that adds a display field to the dict it received would otherwise change
    the legal text served to every subsequent request.
    """
    legal.load(_write(tmp_path, {"states": [_record("KY")]}))
    first = legal.get("KY")
    first["summary"] = "TAMPERED"
    assert legal.get("KY")["summary"] != "TAMPERED"


def test_lookup_does_not_touch_the_filesystem_after_load(tmp_path, monkeypatch):
    """The efficiency claim, asserted rather than assumed (c_s.md Efficiency #4).

    The dataset file is deleted after loading; lookups must keep working, which is only
    possible if nothing re-reads it per request.
    """
    path = _write(tmp_path, {"states": [_record("KY")]})
    legal.load(path)
    path.unlink()

    assert legal.get("KY")["state_code"] == "KY"
    assert legal.states() == ["KY"]


def test_load_returns_the_number_of_states_indexed(tmp_path):
    payload = {"states": [_record("KY"), _record("OH", state_name="Ohio")]}
    assert legal.load(_write(tmp_path, payload)) == 2


def test_load_replaces_rather_than_merges_the_previous_index(tmp_path):
    """A reload must not leave stale states behind from an older file."""
    legal.load(_write(tmp_path, {"states": [_record("KY"), _record("OH")]}))
    second = tmp_path / "second.json"
    second.write_text(json.dumps({"states": [_record("OH")]}), encoding="utf-8")
    legal.load(second)
    assert legal.states() == ["OH"]


# --------------------------------------------------------------------------------------
# Failing loudly — the whole reason this module validates instead of trusting
# --------------------------------------------------------------------------------------


def test_lookup_before_load_raises_instead_of_reporting_no_data(tmp_path):
    """A wiring bug must not be indistinguishable from a genuine data gap.

    Returning None here would render as "we have no reviewed summary for your state",
    which a user would reasonably read as a fact about the law rather than a bug.
    """
    assert not legal.is_loaded()
    with pytest.raises(legal.LegalDataError):
        legal.get("KY")
    with pytest.raises(legal.LegalDataError):
        legal.states()


def test_a_missing_file_raises_at_load(tmp_path):
    with pytest.raises(legal.LegalDataError) as exc:
        legal.load(tmp_path / "nope.json")
    assert "unreadable" in str(exc.value)


def test_unparseable_json_raises_with_a_locating_message(tmp_path):
    path = tmp_path / "good_samaritan.json"
    path.write_text('{"states": [', encoding="utf-8")
    with pytest.raises(legal.LegalDataError) as exc:
        legal.load(path)
    assert "not valid JSON" in str(exc.value)


def test_a_bare_array_root_is_rejected(tmp_path):
    """The `_`-prefixed sibling notes are the record of the review policy, not decoration."""
    with pytest.raises(legal.LegalDataError):
        legal.load(_write(tmp_path, [_record("KY")]))


def test_a_missing_states_array_raises(tmp_path):
    with pytest.raises(legal.LegalDataError) as exc:
        legal.load(_write(tmp_path, {"_policy": "notes only"}))
    assert "no 'states' array" in str(exc.value)


def test_an_empty_states_array_raises(tmp_path):
    """Zero states is never a legitimate shipping state for this dataset."""
    with pytest.raises(legal.LegalDataError):
        legal.load(_write(tmp_path, {"states": []}))


@pytest.mark.parametrize("field", legal.REQUIRED_FIELDS)
def test_every_required_field_is_actually_required(field, tmp_path):
    """Parameterised over the constant, so adding a field automatically gets covered."""
    record = _record()
    del record[field]
    with pytest.raises(legal.LegalDataError) as exc:
        legal.load(_write(tmp_path, {"states": [record]}))
    assert field in str(exc.value)


def test_a_blank_summary_is_rejected(tmp_path):
    """An empty panel would read as "no protection here" — a dangerous accident."""
    with pytest.raises(legal.LegalDataError) as exc:
        legal.load(_write(tmp_path, {"states": [_record(summary="   ")]}))
    assert "empty" in str(exc.value)


def test_a_false_boolean_is_not_mistaken_for_an_empty_field(tmp_path):
    """`has_immunity_law: false` is meaningful data, not a missing value.

    Guards against a naive falsiness check rejecting the states that genuinely have no
    immunity law — precisely the states where the summary matters most.
    """
    legal.load(
        _write(tmp_path, {"states": [_record(has_immunity_law=False, verified=False)]})
    )
    assert legal.get("KY")["has_immunity_law"] is False


@pytest.mark.parametrize("bad", ["K", "KYY", "K1", "", "12"])
def test_an_invalid_state_code_raises(bad, tmp_path):
    with pytest.raises(legal.LegalDataError) as exc:
        legal.load(_write(tmp_path, {"states": [_record(bad)]}))
    assert "state_code" in str(exc.value)


def test_a_non_object_entry_raises_naming_its_position(tmp_path):
    with pytest.raises(legal.LegalDataError) as exc:
        legal.load(_write(tmp_path, {"states": [_record("KY"), "OH"]}))
    assert "entry 1" in str(exc.value)


def test_duplicate_state_codes_raise(tmp_path):
    """Whichever record won would be arbitrary — two different laws for one state."""
    payload = {"states": [_record("KY"), _record("ky", summary="A different summary.")]}
    with pytest.raises(legal.LegalDataError) as exc:
        legal.load(_write(tmp_path, payload))
    assert "duplicate" in str(exc.value)


def test_a_failed_load_leaves_the_cache_unloaded(tmp_path):
    """A partially-built index must never become visible.

    If a bad record could leave half the states loaded, the app would serve an
    incomplete legal dataset while appearing healthy.
    """
    with pytest.raises(legal.LegalDataError):
        legal.load(_write(tmp_path, {"states": [_record("KY"), _record("XX", summary="")]}))
    assert not legal.is_loaded()
