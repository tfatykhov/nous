"""F065 Commit B: CI grep-guard test for GraphEdge writer-site count.

The F065 plan enumerates exactly 8 sites where rows enter
brain.graph_edges, and Commit B threads `classify(relation, source=...)`
through every one of them. Adding a 9th writer in a future feature
without registering it here means the new site silently falls back to
the DB default 'heuristic' — a misclassification that would only be
caught by a downstream MRR regression.

This test fails CI when the writer-site count drifts from the
registered expectation, forcing the new feature to either:
  - call edge_provenance.classify() explicitly at the new site, and
  - bump EXPECTED_WRITER_COUNT to match.

Mechanism: scan nous/brain/ and nous/heart/ source files for
GraphEdge instantiation patterns (`GraphEdge(`, `pg_insert(GraphEdge)`,
or `pg_insert(GraphEdge).values`). Counts must equal the registered
constants. Skip the model definition file itself.
"""

from __future__ import annotations

import re
from pathlib import Path

# Registered as of 2026-05-23 (F065 Commit B). Update if you add or
# remove a writer site. The new site MUST also call
# edge_provenance.classify() at insert time (or extraction_method=...
# explicitly), or the DB default 'heuristic' silently applies.
EXPECTED_WRITER_COUNT = 8

# (relative_path, expected_count_in_this_file).
_REGISTERED_SITES = {
    "nous/brain/brain.py": 2,        # _link constructor + _auto_link pg_insert
    "nous/heart/facts.py": 1,        # _create_graph_edge pg_insert
    "nous/brain/graph_linker.py": 5, # create_edge + 2 cross-type + 2 episode-deterministic
}


_PROJECT_ROOT = Path(__file__).parent.parent


def _count_writer_sites(path: Path) -> int:
    """Count GraphEdge insertion sites in a source file.

    Pattern matches BOTH ``pg_insert(GraphEdge)`` (used by 7 of 8 sites)
    AND ``GraphEdge(`` constructor calls (used by Brain._link at site #1).
    Excludes import lines and column references like ``GraphEdge.source_id``.
    """
    text = path.read_text(encoding="utf-8")
    insert_pattern = re.compile(r"pg_insert\(\s*GraphEdge\s*\)")
    ctor_pattern = re.compile(r"\bGraphEdge\s*\(")
    # Strip out ALL pg_insert(GraphEdge) matches first to avoid double-counting:
    # pg_insert(GraphEdge) also matches `GraphEdge(` in ctor_pattern.
    text_without_pg_inserts, n_pg = insert_pattern.subn("", text)
    n_ctor = len(ctor_pattern.findall(text_without_pg_inserts))
    return n_pg + n_ctor


def test_writer_site_count_matches_registered() -> None:
    """If this test fails: you added (or removed) a GraphEdge writer.
    Confirm the new site calls edge_provenance.classify() and update
    _REGISTERED_SITES + EXPECTED_WRITER_COUNT accordingly.
    """
    total = 0
    per_file_actual: dict[str, int] = {}
    for rel_path, expected in _REGISTERED_SITES.items():
        path = _PROJECT_ROOT / rel_path
        assert path.exists(), f"Registered site path missing: {rel_path}"
        actual = _count_writer_sites(path)
        per_file_actual[rel_path] = actual
        total += actual

    # Per-file assertion first (so the failure message points at the file).
    for rel_path, expected in _REGISTERED_SITES.items():
        assert per_file_actual[rel_path] == expected, (
            f"F065 writer-coverage drift in {rel_path}: "
            f"expected {expected} GraphEdge writers, found {per_file_actual[rel_path]}. "
            f"Did you add/remove a GraphEdge insertion site? If so, "
            f"call edge_provenance.classify() at the new site and update "
            f"_REGISTERED_SITES + EXPECTED_WRITER_COUNT in this file."
        )

    assert total == EXPECTED_WRITER_COUNT, (
        f"F065 writer-coverage drift: total writer count {total} != "
        f"EXPECTED_WRITER_COUNT {EXPECTED_WRITER_COUNT}."
    )


def test_all_registered_sites_call_classify() -> None:
    """Defense in depth: every file registered as a writer site must
    import or reference `classify` from edge_provenance, OR set
    extraction_method explicitly. Without this, a developer could
    accidentally `pg_insert(GraphEdge)` without setting the column and
    the test_writer_site_count_matches_registered test would still
    pass (count is right but values are wrong).
    """
    for rel_path in _REGISTERED_SITES:
        path = _PROJECT_ROOT / rel_path
        text = path.read_text(encoding="utf-8")
        # Must either import classify OR reference it.
        has_classify = "classify" in text and "edge_provenance" in text
        # OR set extraction_method explicitly at every insertion point.
        explicit_count = text.count("extraction_method=")
        writer_count = _count_writer_sites(path)
        assert has_classify or explicit_count >= writer_count, (
            f"{rel_path} has {writer_count} GraphEdge writer sites but "
            f"does not import edge_provenance.classify and only sets "
            f"extraction_method= in {explicit_count} places. Every "
            f"writer must set extraction_method explicitly (preferably "
            f"via classify())."
        )
