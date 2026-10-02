"""CI runs ``pytest tests/``: a test module anywhere else is never collected.

The eight F058 era-split tests lived in ``nous_eval/probes/`` and passed when
run by hand, but no CI run ever executed them.
"""

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_no_test_module_lives_outside_tests():
    stray = sorted(
        path.relative_to(REPO).as_posix()
        for top in ("nous", "nous_eval", "scripts")
        for path in (REPO / top).rglob("test_*.py")
    )

    assert stray == [], f"never collected by `pytest tests/`: {stray}"
