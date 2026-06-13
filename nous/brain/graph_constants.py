"""Canonical graph-edge exclusion sets for graph consumers.

The 2026-06-13 audit (Theme 6) found the relation-exclusion logic had drifted
across the graph consumers — each excluded a different subset, so every new edge
relation leaked through whichever consumer forgot it (supersedes was fixed in
PR #518; contradicts / co_occurred / happened_before still leaked). These
constants centralise the rule so a future relation is a single-site edit.

Two sets, because the correct exclusion depends on consumer PURPOSE:

- **Auto-behavior consumers** — the auto-spreading density gate, spreading
  traversal, orphan detection, cluster discovery — must not be *driven* by edges
  that are not associative connectivity: lineage (``supersedes``), negative
  (``contradicts``), temporal (``happened_before``), or builder-generated
  co-occurrence (``co_occurred`` / ``co_mention``, whose builder flags can flip
  silently and change behaviour). Counting these can flip auto-spreading on or
  strand facts from densification.

- **Retrieval neighbour expansion** (``brain._neighbors``) must not surface
  *lineage* or *negative* edges as connectivity. ``co_occurred`` / ``co_mention``
  ARE legitimate associative connectivity for retrieval, so they are NOT excluded
  there — changing that is a live ranking decision (deferred, eval-gated).
"""

from __future__ import annotations

from collections.abc import Iterable

# --- auto-behavior consumers (density gate, spreading, orphan, cluster) ---
AUTOBEHAVIOR_EXCLUDED_RELATIONS: frozenset[str] = frozenset(
    {"supersedes", "contradicts", "happened_before", "co_occurred"}
)
# co_mention edges carry relation='related_to', so they're excluded by
# extraction_method, not relation name.
AUTOBEHAVIOR_EXCLUDED_METHODS: frozenset[str] = frozenset({"co_mention"})

# --- retrieval neighbour expansion (lineage + negative only) ---
RETRIEVAL_EXCLUDED_RELATIONS: frozenset[str] = frozenset({"supersedes", "contradicts"})


def _sql_list(values: Iterable[str]) -> str:
    return ", ".join(f"'{v}'" for v in sorted(values))


def autobehavior_exclusion_sql(col_prefix: str = "") -> str:
    """SQL boolean fragment excluding non-associative edges from auto-behavior
    consumers. ``col_prefix`` e.g. ``"e."`` for an aliased ``graph_edges`` table.
    Values are fixed literals (no user input), so interpolation is safe."""
    p = col_prefix
    rels = _sql_list(AUTOBEHAVIOR_EXCLUDED_RELATIONS)
    methods = _sql_list(AUTOBEHAVIOR_EXCLUDED_METHODS)
    return (
        f"{p}relation NOT IN ({rels}) "
        f"AND ({p}extraction_method IS NULL OR {p}extraction_method NOT IN ({methods}))"
    )
