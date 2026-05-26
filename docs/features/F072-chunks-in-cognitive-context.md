# F072 — Chunks in cognitive context (+ adjacent stitching)

**Status:** ⏸️ Deferred (2026-05-26) — see F071 v1 spec review for scope-cut rationale
**Proposed by:** Tim
**Depends on:** F067, F069, F071 (cross-context dedup — provides forward-compatible exclusion-set wiring)
**Related:** F036 (Prompt Cache), F022 (Graph-Augmented Recall)

---

## What this is

F072 is the original F071 v1 design (chunks in cognitive context + adjacent stitching) **after** the cross-context dedup piece was extracted into a standalone F071 v2. The dedup wire-up is forward-compatible: when F072 ships, it plugs the `chunk` key into the existing exclusion-set dict without touching the wire.

See `docs/features/F071-cross-context-dedup.md` "What gets deferred to F072" for the full scope.

---

## Prereqs before un-deferring

Three blockers — none currently met as of 2026-05-26:

1. **Prod-shape eval harness.** The 2026-05-24 LongMemEval methodology-dependency finding (`project_lme_methodology_dependency.md` in memory) showed chunks lose 17pp on shared-corpus / prod-shape vs +18pp on per-question isolation. F067 chunks are already in `recall_deep`; F072 would pre-load them every turn. Pre-loading is exactly the high-exposure operation. We must NOT flip F072 defaults on benchmarks that all suffer the same isolation overfit (LME per-question, F051 synthetic). Need a benchmark mirroring prod shape: one user, one `agent_id`, multi-topic corpus, cross-topic queries.

2. **`recall_deep` call-rate telemetry.** The load-bearing claim of F072 ("frames with aggressive pre-fill don't call recall_deep, so chunks payoff misses") is currently unverified. Before building, instrument the dispatcher to log recall_deep call counts per frame. If aggressive-context frames already call recall_deep enough, the entire feature premise dissolves.

3. **F069 semantic chunking outcome.** F072's adjacent-stitching piece is a band-aid for the fact that 600-char sliding-window chunks are wrong for documents. If F069 ships its deferred Phase 2 (header anchors, semantic boundaries, always-include-abstract), the stitching value collapses. Decide F069 Phase 2 before committing to F072 stitching.

---

## What lives here (placeholder)

When un-deferring, port the relevant sections from the F071 v1 spec at `git log --grep="F071 spec" docs/features/F071-cross-context-dedup.md` (and the original review-team findings in `docs/reviews/` once those land). Carry forward:

- `## Memory Chunks` section in `ContextEngine.build` after Relevant Facts
- `Heart.search_chunks` → `ChunkSummary` pydantic (mirroring `FactSummary`)
- Adjacent-chunk stitching with `source_kind`-aware window (`dialogue`: ±1, `document`: ±2), single batched `unnest(uuid[], int[])` query, Python-side interval-merge
- `TurnContext.recalled_chunk_ids` field
- F071 exclusion-set gets 5th type key (`chunk`) plugged in
- F036 cache-tier classification: dynamic (no breakpoint impact)

Carry forward the three-agent spec review feedback:

- Architect P0-1 / P0-2 (TurnContext plumbing, dispatcher injection mechanism — partially resolved by F071's contextvar pattern)
- Python-pro P0 (paired-array `unnest` SQL, `ChunkSummary` pydantic), P1 (stitching merge algorithm), P2 (`_format_chunks` token density, staleness penalty, priority slot)
- Devil's advocate (full critique — keep it visible)

---

## Status until un-deferred

This file exists so the work isn't lost. No code, no schema, no env vars are added by this spec in its current state.
