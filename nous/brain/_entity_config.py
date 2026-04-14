"""Shared entity configuration for F040 graph densification.

Extracted from ``nous.brain.graph_densifier`` as a leaf module so that
``nous.brain.backfill_rerank`` (F043) can import the same table/column
mapping without creating a circular dependency between
``backfill_rerank`` and ``graph_densifier``.

Shape: ``dict[entity_type -> (table, type_name, content_column, extra_where)]``.

``graph_densifier`` re-imports ``_ENTITY_CONFIG`` from this module at the
same location its local definition used to live, so all existing
references inside ``graph_densifier`` continue to resolve unchanged.
"""

from __future__ import annotations

# Entity configuration: (table, type_name, content_column, extra_where)
# content_column uses `t.` alias for the main table.
_ENTITY_CONFIG: dict[str, tuple[str, str, str, str]] = {
    "fact": ("heart.facts", "fact", "t.content", "t.active = true"),
    "decision": ("brain.decisions", "decision", "t.description", "1=1"),
    "episode": (
        "heart.episodes",
        "episode",
        "t.structured_summary->>'summary'",
        "t.active = true AND t.structured_summary IS NOT NULL",
    ),
    "procedure": ("heart.procedures", "procedure", "t.description", "t.active = true"),
}
