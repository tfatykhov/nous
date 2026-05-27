# F075 Architecture Review — Temporal Fact Extraction + Date-Aware Retrieval

**Reviewer:** Senior Architect (feature-dev:code-architect)
**Spec:** `docs/features/F075-temporal-fact-extraction.md`
**Date:** 2026-05-27
**Verdict:** Two P1 architectural gaps must be fixed before implementation. Schema and phase strategy are sound.

---

## Executive Summary

F075 correctly diagnoses the root cause and the synthetic validation proving the fix works is solid. The schema choice (single `event_date DATE` column on `heart.facts`, partial index, no new table) is correct. Two architectural gaps in Layer 1 will make it a no-op on production-shape ingestion if unaddressed.

**P1-1 (blocking) — Wrong augmentation target.** The spec proposes augmenting `FactExtractor._EXTRACT_PROMPT` at `nous/handlers/fact_extractor.py`. That is the LLM fallback path. In production, every conversation goes through the 008.4 fast-path at `fact_extractor.py:143-148`: if `candidate_facts` are present in the event data (which they always are in the standard episode-summarize pipeline), `_extract_facts` is never called and `_EXTRACT_PROMPT` is never used. The correct augmentation target is the **episode summarizer's `candidate_facts` schema** at `nous/handlers/episode_summarizer.py:50-56`, where adding optional `"event_date": "YYYY-MM-DD"` to the structured output schema fixes the primary path. The `_store_candidate_facts` dict-unpacking loop at `fact_extractor.py:251-256` also needs an explicit `event_date = item.get("event_date")` line, or the field is silently discarded regardless of what the LLM emits.

**P1-2 (blocking) — LLM context is lossy prose, not transcript.** Even at the correct target, the summarizer's LLM receives a 100-150 word prose summary, not the raw transcript. If "March 10" didn't survive summarization (as in the conv 2 Q0 smoke), neither path can recover it. The fix is a two-line prompt addition directing the summarizer to extract dates from the **transcript text** it already holds — the summarizer has the full transcript in scope at `episode_summarizer.py:258`.

**P1-3 (framing, not blocking) — `happened_before` consumer is real but cannot surface retrieval misses.** `graph_adjacency_boost` at `retrieval_pipeline.py:243-247` is a valid consumer and the F065 trap is genuinely avoided. However, the boost requires both endpoints to already be in the retrieved candidate set, and the default for `NOUS_GRAPH_ADJACENCY_BOOST_ENABLED` is `false`. Layer 2 provides modest reranking reinforcement; it cannot surface retrieval-miss failures. The spec should be explicit about this scope.

Fix P1-1 and P1-2 before implementation. Phase strategy (Layers 1+2+4 first) is sound.

---

## P1 Findings (Must Fix Before Implementation)

### P1-1: Layer 1 augments the wrong code path — production bypasses `_EXTRACT_PROMPT` entirely

**Spec claim:** "Prompt change in `FactExtractor._extract_prompt`" will cause date-anchored events to be extracted.

The production fact extraction path is controlled by the 008.4 fast-path at `nous/handlers/fact_extractor.py:143-148`:

```python
cands = candidate_facts if candidate_facts is not None else summary.get("candidate_facts", [])
if cands:
    return await self._store_candidate_facts(cands, episode_id, transcript=transcript)
# Fallback: LLM extraction  ← _EXTRACT_PROMPT lives here
candidates = await self._extract_facts(summary)
```

In every standard production conversation, the episode summarizer emits `candidate_facts` inline in its JSON output (`nous/handlers/episode_summarizer.py:50-56`). When `handle()` receives the `episode_summarized` event, it reads `event.data.get("candidate_facts", [])` and passes them to `_store_candidate_facts`, bypassing `_extract_facts` and its `_EXTRACT_PROMPT` entirely. Augmenting `_EXTRACT_PROMPT` has zero effect on the primary path.

**Required fix:** The correct augmentation target is the episode summarizer's `candidate_facts` schema at `nous/handlers/episode_summarizer.py:50-56`. Add an optional `"event_date": "YYYY-MM-DD"` field to the structured dict the summarizer's LLM is instructed to emit. The `_store_candidate_facts` dict-unpacking loop at `fact_extractor.py:251-256` must also add `event_date = item.get("event_date")` and thread it through `FactInput` construction.

The full wire path that must be enumerated in the impl plan:
1. Summarizer prompt schema (`episode_summarizer.py:50-56`) — add `event_date`
2. `_store_candidate_facts` item unpacking (`fact_extractor.py:251-256`) — read `event_date`
3. `FactInput` (`nous/heart/schemas.py:85-98`) — add `event_date: date | None = None`
4. `FactManager._learn` ORM construction (`nous/heart/facts.py:428-449`) — pass to `Fact()`
5. `Fact` ORM model (`nous/storage/models.py:469-511`) — add mapped column
6. Migration `053` — `ALTER TABLE heart.facts ADD COLUMN IF NOT EXISTS event_date DATE DEFAULT NULL`
7. `FactDetail` and `FactSummary` (`nous/heart/schemas.py:114-165`) — add field for downstream consumers

`FactExtractor._EXTRACT_PROMPT` should also be augmented as defense-in-depth for direct-ingest and eval paths that bypass the episode summarizer, but it is not the primary lever.

### P1-2: LLM feeding Layer 1 operates on lossy prose, not the transcript

Even targeting the episode summarizer correctly (fixing P1-1), the summarizer's LLM receives a 100-150 word prose summary and `key_points` list — not the raw transcript (`episode_summarizer.py:40-57` shows the output format; the input to the extraction call is the same prose). If the date-bearing sentence was dropped during transcript-to-summary compression (as occurred in conv 2 Q0, where "March 10" appeared in a user message about API rate limits), the LLM has no data to extract it from.

The transcript IS in scope: `episode_summarizer.py:258` passes `transcript=...` to `extract_and_store`, and the summarizer itself has the full transcript text available when generating the summary. The fix is a prompt addition at `episode_summarizer.py:76-77`:

> "Also extract date-anchored events: if the transcript mentions that something happened on a specific date, add it as a candidate_fact with `event_date: YYYY-MM-DD`. Resolve relative dates against the episode start timestamp."

This directs the summarizer's LLM — which **sees the transcript** — to extract dates from raw conversation content before compaction discards them.

### P1-3: `happened_before` consumer is real but materially weak for the retrieval-miss failure class

**Spec claim (Layer 2):** "`graph_adjacency_boost` is the consumer for `happened_before` edges."

**Verification:** `retrieval_pipeline.py:243-247` confirms the consumer exists:

```python
if getattr(settings, "graph_adjacency_boost_enabled", False):
    results = await _apply_graph_adjacency_boost(brain, results, alpha=alpha)
```

`_apply_graph_adjacency_boost` at `retrieval_pipeline.py:699-738` queries edges where BOTH `source_id` AND `target_id` are in the current candidate set (line 714-715: `AND source_id = ANY(...) AND target_id = ANY(...)`). The only excluded relation is `contradicts` (line 712). The spec's F065 claim is technically correct — `happened_before` edges have a real consumer and are not inert in the way F065's inferred-edge penalty was.

However, two constraints limit Layer 2's practical impact:

1. `NOUS_GRAPH_ADJACENCY_BOOST_ENABLED=false` is the default (`nous/config.py:1055-1056`). The spec says it is "gated by `NOUS_GRAPH_ADJACENCY_BOOST_ENABLED=true` in prod" — but the codebase default is `false`. The impl plan must include explicitly flipping this flag to realize any Layer 2 benefit.

2. For the 3/5 retrieval-miss failures (conv 2 Q0, conv 4 Q1, conv 5 Q1), the date-anchored fact is not in the retrieved candidate set at all (conv 2 Q0 smoke: rank >39 of 39). The boost acts on items already retrieved — it cannot surface items below the retrieval cut. Layer 2 reinforces when both temporally-linked facts independently reach retrieval; that condition does not hold for the dominant failure class.

**Verdict:** Not the F065 trap. Layer 2 is a valid reranking reinforcer. The spec should explicitly frame it as: "modest score reinforcement when both endpoints are already retrieved; ineffective for retrieval-miss failures." This is not a reason to omit Layer 2, but overstating its contribution risks misaligned acceptance criteria.

---

## P2 Findings (Should Fix)

### P2-1: Backfill context source inherits the same lossy data gap

The Layer 4 backfill script proposes feeding Haiku `episode.summary[:500]` as context. If the date was lost during summarization (the same root cause as P1-2), the backfill cannot recover it either. A stronger context source: query `heart.episode_chunks` for the source episode and pass the chunk whose content contains the candidate entity/action. The query shape exists at `retrieval_pipeline.py:818-851` (`_search_episode_chunks`). At ~5K facts, one extra DB query per row is feasible.

### P2-2: Migration number is correct; confirm before branching

`053_temporal_fact_extraction.sql` is the next available slot after `052_f069_document_source_kind.sql`. No conflict exists on current `main`. Verify again immediately before cutting the feature branch.

### P2-3: Same-episode `happened_before` constraint inherits F070's inertness risk for cross-session queries

F070's same-episode-only chunk edges were observed to be inert on session-level recall (per `project_f070_shipped` memory note: "all 3 retrieval consumers inert on session-level recall (same-episode-only constraint)"), requiring F070.1 for cross-episode edges. The `happened_before` same-episode constraint avoids O(N²) pairs but means "how long between X and Y" queries where X and Y come from different sessions never get edges built. For BEAM's single long-conversation haystacks, within-episode ordering is the common case — this is acceptable for v1. The spec should document the ceiling explicitly.

### P2-4: Layer 3 requires `event_date` in `PipelineResult.metadata` — wire path missing

Layer 3's boost at `r.metadata.get("event_date")` assumes `event_date` is already present in each `PipelineResult`. Currently `_heart_results_to_pipeline` at `retrieval_pipeline.py:659-671` constructs `PipelineResult` with an empty metadata dict. Surfacing `event_date` requires either modifying the Heart recall SQL to return the column, or a batch post-join. Layer 3 is deferred pending measurement, but the impl plan should budget for this additional wiring.

---

## P3 Findings (Nice to Have)

### P3-1: `content_date_extractor.py` is untracked; move before reuse

`nous/heart/content_date_extractor.py` is git-untracked (confirmed in `git status`). The module is well-structured but should be committed and given test coverage before being imported from `retrieval_pipeline.py` as a production dependency.

### P3-2: Default `temporal_extraction_enabled=True` is inconsistent with dark-launch convention

All prior behavioral augmentations (F042, F047, F067, F071) defaulted off and required an explicit flag flip after measurement. Recommend `default=False` with flip gated on the LME pre-check.

---

## Schema Choice Assessment

`event_date DATE DEFAULT NULL` on `heart.facts` with a partial index is the correct design. The ORM model at `nous/storage/models.py:469-511` needs one `Mapped[date | None]` column using Python's `datetime.date` type (not `datetime`).

## Phase Strategy Assessment

"Layers 1+2+4 first" is sound after incorporating P1 fixes. Reframe as:

- **Layer 1a (primary):** Augment episode summarizer prompt — extract dates from transcript text.
- **Layer 1b (defense-in-depth):** Augment `FactExtractor._EXTRACT_PROMPT` for direct-ingest/eval paths.
- **Layer 2:** Ship with Layer 1. Document as reranking reinforcer; flip `NOUS_GRAPH_ADJACENCY_BOOST_ENABLED`.
- **Layer 4:** Backfill with chunk context source (P2-1).
- **Layer 3:** Defer; budget metadata surfacing work for when it lands.

## Finding Summary

| Finding | Files | Priority |
|---------|-------|----------|
| `_EXTRACT_PROMPT` is the fallback; prod uses `_store_candidate_facts` via 008.4 fast-path | `fact_extractor.py:143-148`, `episode_summarizer.py:50-56` | P1 |
| Summarizer LLM sees 150-word prose, not transcript; date lost in compression is unrecoverable | `episode_summarizer.py:76-77`, `fact_extractor.py:297-308` | P1 |
| `happened_before` consumer is real but default-off and cannot surface retrieval-miss failures | `retrieval_pipeline.py:243-247, 699-738`, `config.py:1055-1056` | P1 |
| `_store_candidate_facts` dict-unpacking silently discards unknown keys including `event_date` | `fact_extractor.py:251-256` | P2 |
| Backfill Haiku context (`episode.summary[:500]`) suffers same lossy gap as Layer 1 | spec Layer 4, `retrieval_pipeline.py:818-851` | P2 |
| Same-episode `happened_before` cannot link cross-session date pairs (F070 ceiling pattern) | `brain/graph_densifier.py:1045-1098` | P2 |
| Layer 3 `metadata.get("event_date")` requires new surfacing logic in `_heart_results_to_pipeline` | `retrieval_pipeline.py:659-671` | P2 |
| `content_date_extractor.py` is untracked; needs commit + tests before production reuse | `nous/heart/content_date_extractor.py` | P3 |
| `temporal_extraction_enabled=True` default inconsistent with dark-launch convention | spec §Settings | P3 |
