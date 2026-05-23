"""F065: edge-provenance classification — single source of truth.

Used by every GraphEdge writer (eight sites enumerated in the F065 spec)
to set the `extraction_method` column at insert time. The mapping mirrors
migration 047's backfill exactly — keeping write-time and backfill rules
in one place ensures no drift.

Tiers:
  - deterministic: explicit structural provenance (supersession edges,
                   episode-token extraction). Highest trust.
  - inferred:      LLM reasoning over similar items (F027 contradiction
                   path). Lowest trust; recall_deep applies the
                   `NOUS_GRAPH_INFERRED_EDGE_PENALTY` multiplier.
  - heuristic:     cosine-threshold matching by graph_linker /
                   graph_densifier. Default safe tier.

NULL handling at consumer sites: NeighborResult.extraction_method
defaults to 'heuristic' (fail-open) with a one-time WARN log per agent
if a row somehow arrives without a value.
"""

from __future__ import annotations

from typing import Final, Literal

# Mirror of the migration 047 CHECK constraint. Keep in sync.
VALID_METHODS: Final[frozenset[str]] = frozenset(
    {"deterministic", "heuristic", "inferred"}
)

ExtractionMethod = Literal["deterministic", "heuristic", "inferred"]


def classify(
    relation: str,
    *,
    source: str | None = None,
) -> ExtractionMethod:
    """Map (relation, optional source) → extraction_method tier.

    Args:
        relation: the edge relation string (e.g. 'supersedes', 'contradicts',
                  'related_to', 'extracted_from', 'discussed_in', etc.).
        source: writer-identity tag explicitly passed by the call site.
                Recognized values:
                  - ``'structural'``: explicit episode-token / supersession
                    references, not cosine-derived. → deterministic.
                  - ``'auto_linker'``: event-bus cosine auto-linker writes
                    (``nous/brain/graph_linker.py`` related_to / evidence_for
                    paths). → inferred. Added 2026-05-23 as the F065 phase 4
                    follow-up: prod has 0 ``contradicts`` rows (F027
                    classifier is biased toward UPDATE — see
                    ``nous/heart/facts.py:35``), so the original
                    relation-only rule left ``inferred`` empty and the
                    penalty multiplier dormant. Tagging the auto-linker as
                    its own writer aligns the tier with operational reality.
                  - ``'ce_backfill'``: F040 sleep-cycle cross-encoder
                    backfill. Also cosine-derived. → inferred.

    Returns:
        One of 'deterministic', 'inferred', 'heuristic'.
    """
    # Precedence: structural-provenance relations win over any source
    # tag, because supersedes/contradicts encode the writer's intent in
    # the relation itself. The `source` tag only disambiguates relations
    # that COULD be either heuristic or inferred (related_to,
    # evidence_for, informed_by, etc.).
    if relation == "supersedes":
        return "deterministic"
    if relation == "contradicts":
        return "inferred"
    if source == "structural":
        return "deterministic"
    if source in ("auto_linker", "ce_backfill"):
        return "inferred"
    return "heuristic"
