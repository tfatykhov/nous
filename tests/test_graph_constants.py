"""2b (2026-06-13 audit): canonical graph-edge exclusion sets + the meta-check
that every graph consumer routes through them (so the relation-exclusion logic
can't drift across consumers again)."""

from __future__ import annotations

import inspect

from nous.brain.graph_constants import (
    AUTOBEHAVIOR_EXCLUDED_METHODS,
    AUTOBEHAVIOR_EXCLUDED_RELATIONS,
    RETRIEVAL_EXCLUDED_RELATIONS,
    SPREADING_ALWAYS_EXCLUDED_RELATIONS,
    associative_edge_sql,
    autobehavior_exclusion_sql,
    spreading_exclusion_sql,
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
    assert "autobehavior_exclusion_sql" in dens_src  # density gate
    assert "spreading_exclusion_sql" in dens_src  # traversal (F105: its default is the same string)
    assert "autobehavior_exclusion_sql" in densifier_src  # find_orphans + discover_clusters
    # No stray hardcoded single-relation exclusions left behind.
    assert "relation <> 'supersedes'" not in dens_src
    assert "relation <> 'supersedes'" not in densifier_src


def test_neighbors_uses_retrieval_constant():
    from nous.brain import brain
    src = inspect.getsource(brain)
    assert "RETRIEVAL_EXCLUDED_RELATIONS" in src


# --- F105 Task 4: the spreading traversal's own exclusion ---


def test_spreading_exclusion_default_equals_autobehavior():
    for prefix in ("", "e.", "e2."):
        assert spreading_exclusion_sql(prefix) == autobehavior_exclusion_sql(prefix)
        assert spreading_exclusion_sql(prefix, allow_associative=False) == autobehavior_exclusion_sql(prefix)


def test_spreading_exclusion_allows_associative():
    sql = spreading_exclusion_sql("e.", allow_associative=True)
    for rel in ("supersedes", "contradicts", "happened_before"):
        assert f"'{rel}'" in sql
    assert "co_occurred" not in sql
    assert "co_mention" not in sql
    assert sql.startswith("e.relation NOT IN")


def test_autobehavior_set_is_always_excluded_plus_associative():
    """Every relation the auto-behavior set excludes is either never traversed or
    associative. A relation added to the auto-behavior set alone fails here, so
    whether spreading may cross it is decided rather than inherited."""
    assert SPREADING_ALWAYS_EXCLUDED_RELATIONS == {"supersedes", "contradicts", "happened_before"}
    assert AUTOBEHAVIOR_EXCLUDED_RELATIONS == SPREADING_ALWAYS_EXCLUDED_RELATIONS | {"co_occurred"}
    assert AUTOBEHAVIOR_EXCLUDED_METHODS == {"co_mention"}


def test_associative_edge_sql():
    assert associative_edge_sql("e.") == "(e.relation = 'co_occurred' OR e.extraction_method = 'co_mention')"
    assert associative_edge_sql() == "(relation = 'co_occurred' OR extraction_method = 'co_mention')"
