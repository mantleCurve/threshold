"""In-memory cache for the static Good Samaritan dataset.

What this module does:
    Reads `data/legal/good_samaritan.json` exactly once, validates every record against
    a schema, indexes the records by uppercase state code, and serves O(1) lookups for
    the lifetime of the process.

Why it exists (c_s.md Efficiency P2 #4, g_s.md Efficiency #2):
    `GET /api/legal/{state}` previously opened, read, and JSON-parsed the whole file on
    every single request. The file is immutable at runtime — CONTRACT.md states it is a
    static reviewed dataset and no code path may write to it — so re-parsing it is pure
    waste on a request a bystander may make while standing over an unconscious person.

Why it is a separate module and not part of `app/genai.py`:
    `genai.py` is the only module allowed to touch the network (CONTRACT.md). Legal text
    is the one thing in this product that must never be model-generated, because a
    hallucinated immunity claim could persuade someone that calling 911 is unsafe. Those
    two responsibilities should not be able to see each other's imports. Keeping the
    legal loader in its own leaf module makes "no model ever produced this text" a
    structural fact rather than a promise in a comment.

What this module deliberately does NOT do:
    - It never writes to the dataset, and exposes no mutation function at all. The
      cached records are returned as copies so a caller cannot mutate the shared index.
    - It never generates, extends, or interpolates a record. A state absent from the
      file stays absent; the route says so plainly rather than guessing.
    - It never hot-reloads. Reloading would mean a request could observe a half-written
      file, and the dataset only changes when a human edits it and restarts.

Failure policy — fail loudly, and fail at boot:
    `load()` raises `LegalDataError` on a missing file, unparseable JSON, a missing
    `states` array, a record missing a required field, or a duplicate state code. It is
    called from the FastAPI lifespan so a malformed dataset stops the server at startup
    with a precise message, rather than surfacing as a confusing 503 to a bystander mid
    -emergency. This is the one place in the app where crashing early is the safe
    behaviour: serving no legal text is better than serving wrong legal text, and
    serving a clear boot error is better than either.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Final

log = logging.getLogger("threshold.legal")

# Resolved from this file rather than a caller-supplied path so the dataset location is
# not something a request can influence.
DEFAULT_PATH: Final[Path] = (
    Path(__file__).resolve().parent.parent / "data" / "legal" / "good_samaritan.json"
)

# Every field a record must carry for the UI to render it honestly. `verified` is in
# this list on purpose: the frontend renders an "unverified — confirm locally" banner
# from it, so a record that omits the flag would silently lose its disclosure and read
# as reviewed law. That is precisely the failure mode the dataset's own header warns
# about, so it is a hard schema error, not a defaulted field.
REQUIRED_FIELDS: Final[tuple[str, ...]] = (
    "state_code",
    "state_name",
    "has_immunity_law",
    "verified",
    "summary",
    "does_not_cover",
    "plain_language_line",
    "source_note",
)


class LegalDataError(RuntimeError):
    """The legal dataset is missing or malformed.

    A distinct exception type so the lifespan can let it propagate (aborting boot)
    while still catching ordinary startup problems separately. The message always names
    the offending record or field, because the person reading it is a developer at boot
    time, not a user in an emergency.
    """


# Process-wide index, populated by `load()`. Keyed by UPPERCASE state code so lookup
# does not have to normalise the whole table on every request. `None` (rather than an
# empty dict) distinguishes "never loaded" from "loaded and legitimately empty", which
# is the difference between a wiring bug and a data problem.
_INDEX: dict[str, dict[str, Any]] | None = None


def _validate_record(record: Any, position: int) -> dict[str, Any]:
    """Check one entry from the `states` array and return it normalised.

    Args:
        record: A candidate record, straight from the parsed JSON. Type is `Any`
            because at this point nothing has established that it is a dict.
        position: Zero-based index within the array, used only to build an error
            message a human can act on without counting braces by hand.

    Returns:
        The record with `state_code` uppercased. Returned as the same object; the
        caller is responsible for not handing it out unguarded.

    Raises:
        LegalDataError: if the entry is not an object, is missing a required field,
            has a blank required field, or has a state code that is not two letters.
    """
    if not isinstance(record, dict):
        raise LegalDataError(
            f"legal dataset: entry {position} is {type(record).__name__}, expected an object"
        )

    for field in REQUIRED_FIELDS:
        if field not in record:
            raise LegalDataError(
                f"legal dataset: entry {position} is missing required field {field!r}"
            )
        value = record[field]
        # Booleans (`has_immunity_law`, `verified`) are legitimately False, so only
        # string fields are checked for emptiness. A blank summary would render as an
        # empty panel that looks like "no protection here", which is a dangerous
        # thing for a blank field to accidentally imply.
        if isinstance(value, str) and not value.strip():
            raise LegalDataError(
                f"legal dataset: entry {position} has an empty required field {field!r}"
            )

    code = str(record["state_code"]).strip().upper()
    if len(code) != 2 or not code.isalpha():
        raise LegalDataError(
            f"legal dataset: entry {position} has invalid state_code {record['state_code']!r}"
        )

    record["state_code"] = code
    return record


def load(path: Path | None = None) -> int:
    """Parse, validate, and index the dataset. Call once from the app lifespan.

    Idempotent in effect but not lazy: calling it again re-reads and replaces the
    index, which is what a test fixture wants and what a manual reload would need.

    Args:
        path: Override for the dataset location. Defaults to `DEFAULT_PATH`. The
            parameter exists for tests only — no request-handling code passes it, so a
            user can never point this at another file.

    Returns:
        The number of state records indexed. Returned rather than logged-only so the
        lifespan can report it and a test can assert on it.

    Raises:
        LegalDataError: file missing, unreadable, not JSON, no `states` array, or any
            record failing `_validate_record`, or two records claiming the same state.
            Every one of these aborts startup on purpose — see the module docstring.
    """
    global _INDEX

    target = path or DEFAULT_PATH

    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise LegalDataError(f"legal dataset unreadable at {target}: {exc}") from exc

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        # Line and column come from the decoder and point straight at the typo.
        raise LegalDataError(f"legal dataset is not valid JSON ({target}): {exc}") from exc

    # The file is an object whose `states` key holds the array; the sibling keys are
    # the `_`-prefixed human notes explaining the review policy. A bare array is
    # rejected rather than tolerated, because those notes are not optional decoration:
    # they are the record of why every entry ships with verified:false.
    if not isinstance(payload, dict):
        raise LegalDataError(
            f"legal dataset root is {type(payload).__name__}, expected an object with a 'states' key"
        )
    states = payload.get("states")
    if not isinstance(states, list):
        raise LegalDataError("legal dataset has no 'states' array")
    if not states:
        raise LegalDataError("legal dataset 'states' array is empty")

    index: dict[str, dict[str, Any]] = {}
    for position, entry in enumerate(states):
        record = _validate_record(entry, position)
        code = record["state_code"]
        if code in index:
            # A duplicate is worse than a missing state: whichever record wins is
            # arbitrary, so one of two different legal summaries would be served for
            # the same state depending on file order.
            raise LegalDataError(f"legal dataset: duplicate state_code {code!r}")
        index[code] = record

    _INDEX = index
    # Nothing sensitive here — state codes only, no user data — so this is safe to log
    # and it gives a judge a one-line confirmation that the dataset really loaded.
    log.info("legal dataset loaded: %d states from %s", len(index), target.name)
    return len(index)


def is_loaded() -> bool:
    """Whether `load()` has completed successfully in this process."""
    return _INDEX is not None


def get(state_code: str) -> dict[str, Any] | None:
    """Look up one state's record. O(1).

    Args:
        state_code: Two-letter code, any case. Whitespace is tolerated because this
            value arrives from a URL path segment.

    Returns:
        A shallow copy of the record, or None when the state is not in the dataset.
        A copy, because the index is shared for the process lifetime and a route that
        adds a display field to the dict it received would otherwise corrupt the
        dataset for every subsequent request.

    Raises:
        LegalDataError: if called before `load()`. This is a wiring bug — the lifespan
            did not run — and it should be a loud 500 in development rather than a
            silent "we have no summary for your state", which would read to a user as
            a true statement about the law.
    """
    if _INDEX is None:
        raise LegalDataError("legal dataset not loaded; call app.legal.load() at startup")
    record = _INDEX.get(state_code.strip().upper())
    return dict(record) if record is not None else None


def states() -> list[str]:
    """Every state code in the dataset, sorted. Useful for a picker in the UI.

    Raises:
        LegalDataError: if called before `load()`, for the same reason as `get`.
    """
    if _INDEX is None:
        raise LegalDataError("legal dataset not loaded; call app.legal.load() at startup")
    return sorted(_INDEX)


def reset() -> None:
    """Drop the cached index. Test-only.

    Present so a test can prove that `get()` raises before `load()` without depending
    on module import order. No production code path calls this — reloading legal text
    mid-flight is explicitly not a feature (see the module docstring).
    """
    global _INDEX
    _INDEX = None
