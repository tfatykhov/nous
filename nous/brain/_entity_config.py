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

from nous.brain.graph_constants import episode_live_sql

# Entity configuration: (table, type_name, content_column, extra_where)
# content_column uses `t.` alias for the main table.
_ENTITY_CONFIG: dict[str, tuple[str, str, str, str]] = {
    "fact": ("heart.facts", "fact", "t.content", "t.active = true"),
    "decision": ("brain.decisions", "decision", "t.description", "1=1"),
    "episode": (
        "heart.episodes",
        "episode",
        # F058 (2026-05-04): fall back to plain `summary` when
        # `structured_summary` is NULL. Stuck-open sessions never receive
        # `episode_ended` → episode_summarizer never fires → structured_summary
        # stays NULL forever. Plain `summary` (set at episode start, often the
        # first user message) is always populated. F040 was excluding 76/76
        # eval-scratch orphans because of the IS NOT NULL filter; same pattern
        # on prod (78 active orphans, all NULL structured_summary).
        "COALESCE(t.structured_summary->>'summary', t.summary)",
        # 2026-07-12: episodes.active=false is the normal CLOSED state
        # (008.3), not deletion — bare `t.active = true` excluded every
        # completed episode from backfill, so F053's over-prune could never
        # heal. Liveness predicate mirrors HT-1's search fix.
        episode_live_sql("t."),
    ),
    "procedure": ("heart.procedures", "procedure", "t.description", "t.active = true"),
    # F070 (2026-05-25): chunk node type. heart.episode_chunks has no `active`
    # column (deferred to F070.1), so the extra_where is the always-true
    # placeholder. content_col is `t.content` (raw transcript fragment).
    "chunk": ("heart.episode_chunks", "chunk", "t.content", "1=1"),
}
