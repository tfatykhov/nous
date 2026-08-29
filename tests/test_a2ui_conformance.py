"""F092: the vendored upstream conformance samples must validate.

These 43 fixtures are the A2UI project's own gallery examples, vendored at
the pinned protocol commit (see nous/a2ui/catalogs/VENDORED.md). They are the
only independent check that our schema loading — the ``\\p{...}`` pattern
rewrite and the merged catalog, both of which EDIT the schemas at load time —
did not quietly loosen or break validation. Our own builders cannot show
that: they were written against this validator.

Envelope validation only. The fixtures legitimately reference agent-side
functions and use placeholder child ids across incremental messages, so the
structural checks in ``validate_structure`` (exactly one root, no dangling
refs) do not apply to them — those are a contract on OUR builders, not on
the protocol.

XFAIL/skip list: EMPTY, and it should stay that way. All 43 fixtures pass,
including the ones using components we vendor but do not render (Video,
AudioPlayer) — schema conformance is independent of renderer coverage.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nous.a2ui.validator import validate_envelope

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "a2ui" / "examples"

# Number of examples vendored at the pinned upstream commit. Pinned so that
# re-vendoring is a deliberate, visible change rather than a silent shrink of
# the conformance corpus — a suite that iterates a directory reports success
# just as happily when the directory is empty.
EXPECTED_FIXTURE_COUNT = 43

FIXTURE_PATHS = sorted(FIXTURES_DIR.glob("*.json"))


def test_fixture_corpus_is_present_and_complete() -> None:
    assert FIXTURES_DIR.is_dir(), f"missing vendored fixtures at {FIXTURES_DIR}"
    assert len(FIXTURE_PATHS) == EXPECTED_FIXTURE_COUNT


@pytest.mark.parametrize("fixture_path", FIXTURE_PATHS, ids=lambda p: p.stem)
def test_vendored_example_validates(fixture_path: Path) -> None:
    """Every message in the fixture validates as an agent->renderer envelope."""
    document = json.loads(fixture_path.read_text(encoding="utf-8"))
    messages = document.get("messages", [])

    assert messages, f"{fixture_path.name} has no messages"

    failures: list[str] = []
    for index, message in enumerate(messages):
        for error in validate_envelope(message):
            failures.append(f"  message[{index}] {error['path']}: {error['message'][:200]}")

    assert not failures, (
        f"{fixture_path.name} ({document.get('name', '?')}) failed envelope validation:\n"
        + "\n".join(failures)
    )


def test_fixtures_exercise_every_envelope_type() -> None:
    """The corpus covers all four envelope types, so the suite is a real check.

    If a re-vendor dropped every updateDataModel example, the tests above
    would still pass while covering strictly less of the schema.
    """
    seen: set[str] = set()
    for fixture_path in FIXTURE_PATHS:
        document = json.loads(fixture_path.read_text(encoding="utf-8"))
        for message in document.get("messages", []):
            seen.update(
                key
                for key in ("createSurface", "updateComponents", "updateDataModel", "deleteSurface")
                if key in message
            )

    assert {"createSurface", "updateComponents", "updateDataModel"} <= seen
