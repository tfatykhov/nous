"""2b (2026-06-13 audit): canonical graph-edge exclusion sets + the meta-check
that every graph consumer routes through them (so the relation-exclusion logic
can't drift across consumers again)."""

from __future__ import annotations

import inspect

from nous.brain.graph_constants import (
    AUTOBEHAVIOR_EXCLUDED_METHODS,
    AUTOBEHAVIOR_EXCLUDED_RELATIONS,
    RETRIEVAL_EXCLUDED_RELATIONS,
    autobehavior_exclusion_sql,
)


def test_autobehavior_set_covers_the_audit_relations():
    assert {"supersedes", "contradicts", "happened_before", "co_occurred"} <= AUTOBEHAVIOR_EXCLUDED_RELATIONS
    assert "co_mention" in AUTOBEHAVIOR_EXCLUDED_METHODS


def test_retrieval_set_is_lineage_and_negative_only():
    # co_occurred / co_mention are legitimate associative connectivity for
    # retrieval and MUST NOT be excluded there.
    assert RETRIEVAL_EXCLUDED_RELATIONS == {"supersedes", "contradicts"}
    assert "co_occurred" not in RETRIEVAL_EXCLUDED_RELATIONS


def test_exclusion_sql_renders_all_relations_and_method():
    sql = autobehavior_exclusion_sql("e.")
    for rel in AUTOBEHAVIOR_EXCLUDED_RELATIONS:
        assert f"'{rel}'" in sql
    assert "e.relation NOT IN" in sql
    assert "e.extraction_method" in sql
    assert "IS NULL" in sql  # keeps NULL/legacy rows counted


def test_all_autobehavior_consumers_use_the_constant():
    """Every auto-behavior consumer must route through graph_constants so a new
    relation is a single-site edit."""
    from nous.brain import graph_densifier, spreading_activation

    dens_src = inspect.getsource(spreading_activation)
    densifier_src = inspect.getsource(graph_densifier)
    assert "autobehavior_exclusion_sql" in dens_src  # density + traversal
    assert "autobehavior_exclusion_sql" in densifier_src  # find_orphans + discover_clusters
    # No stray hardcoded single-relation exclusions left behind.
    assert "relation <> 'supersedes'" not in dens_src
    assert "relation <> 'supersedes'" not in densifier_src


def test_neighbors_uses_retrieval_constant():
    from nous.brain import brain
    src = inspect.getsource(brain)
    assert "RETRIEVAL_EXCLUDED_RELATIONS" in src
