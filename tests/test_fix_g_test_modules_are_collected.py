"""CI runs ``pytest tests/``: a test module anywhere else is never collected.

The eight F058 era-split tests lived in ``nous_eval/probes/`` and passed when
run by hand, but no CI run ever executed them.
"""

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
# Every tracked directory that holds Python, except ``tests/`` itself.
SOURCE_DIRS = ("benchmarks", "docs", "nous", "nous_eval", "scripts")


def _test_modules(root: Path, patterns: list[str]) -> list[str]:
    """Files under the source directories that pytest would treat as test modules."""
    return sorted(
        {
            path.relative_to(root).as_posix()
            for top in SOURCE_DIRS
            for pattern in patterns
            for path in (root / top).rglob(pattern)
        }
    )


def test_no_test_module_lives_outside_tests(pytestconfig):
    # pytest's own definition of a test module (``test_*.py`` and ``*_test.py``
    # unless ``python_files`` is configured), read from the running config so
    # the guard cannot drift from what pytest would collect.
    stray = _test_modules(REPO, pytestconfig.getini("python_files"))

    assert stray == [], f"never collected by `pytest tests/`: {stray}"


def test_the_guard_sees_every_name_pytest_treats_as_a_test_module(tmp_path, pytestconfig):
    patterns = pytestconfig.getini("python_files")
    names = [pattern.replace("*", "era_split") for pattern in patterns]
    for top in SOURCE_DIRS:
        (tmp_path / top).mkdir()
    package = tmp_path / "nous_eval" / "probes"
    package.mkdir()
    for name in names:
        (package / name).write_text("")

    assert _test_modules(tmp_path, patterns) == sorted(f"nous_eval/probes/{name}" for name in names)
