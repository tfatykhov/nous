# F075 — Temporal Fact Extraction + Date-Aware Retrieval

**Status:** 📝 Draft **v2.3** (2026-05-27) — incorporates arch / python-pro / devil's-advocate spec review + 3 rounds of codex PR review fixes
**Proposed by:** Tim + investigation thread
**Date:** 2026-05-27
**Depends on:** F002 (Heart), F022 (Cross-type linking), F040 (Graph Densifier), F047 (Actionability backfill pattern), F051 (Eval harness for measurement)
**Blocks:** F074.x re-measurement of temporal_reasoning on BEAM
**Related:** `nous/heart/content_date_extractor.py` (untracked, retrieval-time approach — see §Alternatives)
**Forge decision:** TBD (will record when impl plan greenlit)
**Reviews:** `docs/reviews/F075-spec-arch-review.md`, `F075-spec-python-pro-review.md`, `F075-spec-devil-review.md`

---

## v2.3 changelog (codex re-review round 3)

After v2.2 push, codex flagged 2 more P2s. All incorporated:

- **P2 — Wrong `GraphDensifier.run_backfill_cycle` call shape.** v2.2's edge-build trigger wrote `densifier.run_backfill_cycle(agent_id=...)` but `run_backfill_cycle()` at `graph_densifier.py:1045` takes no args — `agent_id` is captured in the constructor (`graph_densifier.py:108-119`). Calling with `agent_id=` would `TypeError`. Fixed: §Edge-build trigger now shows the correct per-agent instantiation pattern, mirroring `sleep_handler.py:1354`.
- **P2 — ORM `CheckConstraint` not updated alongside SQL migration.** The SQLAlchemy `GraphEdge` class at `models.py:239-244` declares its own `ck_edges_relation` that fresh-schema paths (`Base.metadata.create_all` in tests, Alembic autogenerate) honor. v2.2's spec only updated the migration SQL. Pre-existing drift: the ORM constraint was never updated for F070's `part_of`/`summarized_by` either. Fixed: §Schema migration now requires updating the ORM `CheckConstraint` to include all three missing relations (`part_of`, `summarized_by`, `happened_before`).

## v2.2 changelog (codex re-review round 2)

After v2.1 push, codex re-reviewed and flagged 5 more findings. All incorporated:

- **P1 — stale eligibility predicate in §Idempotence.** The v2.1 fix updated the per-batch SELECT to `event_date_classified_at IS NULL` but left the §Idempotence one-liner saying `WHERE event_date IS NULL`. Same starvation bug, separate doc location. Fixed: §Idempotence now matches the SELECT predicate.
- **P2 — quadratic happened_before edges.** The `NOT EXISTS (intermediate-date)` formulation links every fact on date D to every fact on date D+1 because it excludes intermediate *dates*, not intermediate *facts*. 20 facts on Jan 1 + 20 on Jan 2 = 400 edges, not 20. Fixed: §Layer 2 INSERT now uses `LATERAL ... LIMIT 1` to pick exactly one representative successor per source fact.
- **P2 — `EPISODE_START_TIMESTAMP` not wired through summarizer.** The Layer 1 prompt directs the LLM to resolve relative dates against `EPISODE_START_TIMESTAMP`, but `_summarize_single(transcript, decision_context)` at `episode_summarizer.py:394` doesn't receive it. Fixed: §Layer 1a wire path adds 3 hops to thread `Episode.started_at` from `summarize_episode` → `_summarize_single` → prompt template.
- **P2 — backfill SELECT missing `subject` column.** The chunk-context lookup in §Layer 4 uses `$3 = subject keywords` but the batch SELECT only returns `(id, content, source_episode_id)`. Fixed: SELECT now includes `subject`.

## v2.1 changelog (codex PR review)

Codex flagged 2 P1s on PR #460 against the v2 spec; both incorporated here:

- **Codex P1 — `brain.graph_edges` schema constraints.** Layer 2's INSERT was missing `agent_id` (`NOT NULL` per `sql/init.sql:197`) and the existing relation CHECK constraint (last extended by migration `051_f070_chunk_graph_edges.sql:34-45`) does not allow `'happened_before'`. v2.1 §Schema migration now extends the CHECK to add the new relation (mirroring F070's pattern verbatim), and the INSERT includes `agent_id` + `auto_linked=TRUE`.
- **Codex P1 — backfill starvation.** v2's eligibility predicate `WHERE event_date IS NULL` meant non-event rows (stable facts the classifier correctly returned `None` for) stayed eligible forever — the script would loop on the newest-N stable facts and never advance to older dated rows. F047 doesn't have this problem because every actionability classification produces a non-NULL TRUE/FALSE. v2.1 adds an `event_date_classified_at TIMESTAMPTZ` column written on every classification attempt regardless of outcome; eligibility becomes `event_date_classified_at IS NULL`. The live-path extractor also writes the marker so freshly-ingested facts skip backfill.

## v2 changelog

v1 → v2 incorporates 3 parallel spec reviews. Key changes:

**Critical (architectural target was wrong):**
- **Arch P1-1, Python P1-1 fixed.** v1 proposed augmenting `FactExtractor._EXTRACT_PROMPT`. That is the **fallback** path. Prod uses the 008.4 fast-path at `fact_extractor.py:143-148` where `candidate_facts` come from the **episode summarizer**. v2 targets the summarizer's `candidate_facts` schema (`episode_summarizer.py:50-56`) as **primary**, FactExtractor's prompt as **defense-in-depth**. Also: `ExtractedFact` class doesn't exist — v2 targets `FactInput` (`nous/heart/schemas.py:85-98`) and the `_store_candidate_facts` dict-unpacking loop (`fact_extractor.py:251-256`) which silently discards unknown keys today.
- **Arch P1-2 fixed.** v1 assumed prompt change would let the LLM see dates. But the summarizer's extraction call operates on 100-150 word prose (the just-generated summary), not the raw transcript. Date-in-rate-limit-code (conv 2 Q0 case) gets compressed out. v2 directs the summarizer to extract dates from the **transcript text it already holds in scope** (`episode_summarizer.py:258`).
- **Python P1-4 fixed.** v1 added the SQL column but missed: (a) `Fact` ORM at `storage/models.py:469-511` needs `Mapped[date | None]`, AND (b) `Heart.recall` must populate `RecallResult.metadata["event_date"]` or Layer 3 is a silent no-op forever.

**Layer scope reframing:**
- **Arch P1-3 / Devil's #3 fixed.** v1 framed Layer 2 (`happened_before` edges) as a primary retrieval lever. v2 reframes as **modest reranking reinforcer when both endpoints already retrieved**. The consumer (`graph_adjacency_boost`) is real and the F065 trap is avoided — but the consumer is default-off (`config.py:1055-1056`) AND cannot surface candidates below the retrieval cut. Impl plan must include flipping `NOUS_GRAPH_ADJACENCY_BOOST_ENABLED=true`.

**Diagnostic update from Devil's #2:**
- **Failure-mode reclassification** (verified empirically 2026-05-27 via `diag_temporal_failure_classes.py`):
  - 2 of 5 = PATTERN_MATCH_CONV2_Q0 (conv 2 Q0, conv 4 Q1): date in chunks, missing from facts, chunk not in top-20 retrieval
  - 2 of 5 = PARTIAL_MATCH (conv 3 Q1, conv 5 Q1): date in chunks, missing from facts, chunk IS retrieved (rank 3), but LLM doesn't compose a correct date-arithmetic answer from the prose
  - 1 of 5 = source ambiguity (conv 2 Q1): unrelated to extractor work
- v1's "3 of 5 retrieval-miss" was an under-classification. Extractor still helps the PARTIAL_MATCH class by creating discrete dated facts that are easier for the LLM to combine, but it's a different mechanism than for the PATTERN_MATCH class.

**Python-level corrections:**
- **Backfill SQL was malformed.** v1's `WHERE id = (SELECT id FROM batch)` crashes when LIMIT > 1. v2 copies F047's per-row pattern at `actionability_backfill.py:198-216`.
- **Advisory lock keying.** v1 invented a keying scheme. v2 copies F047's `hashlib.sha256().digest()[:8]` → signed bigint pattern at `actionability_backfill.py:108-115`.
- **Migration shape.** Added `BEGIN;...COMMIT;` wrapper per `052_f069_document_source_kind.sql:22,34` style.
- **LLM client.** v2 specifies `call_background_llm_structured` from `nous/handlers/__init__.py:86` with tool_use schema for guaranteed JSON — no parse-repair logic needed.

**Default flags:**
- **Python/Arch P3 fixed.** All new flags default to `False` for consistency with prior dark-launch convention (F042, F047, F067, F071).

---

## Problem

BEAM-100K prod-v3 measures temporal_reasoning at **0.417** (n=5 conv, decision `631cbc75`). LongMemEval N=100 paper-faithful measured temporal_reasoning at **0.583** in the same family of weakest categories. Both benchmarks expose the same gap: Nous can recall topical conversation content but **fails on date arithmetic** ("how many days between X and Y").

### Diagnostic chain (decisions `0b52b37d`, `2c6c2c37`; memory `project_f074_temporal_diagnosis_2026_05_27`)

Three "easy lever" hypotheses were investigated and all empirically or analytically falsified:

| Lever | How falsified | Cost |
|---|---|---|
| `NOUS_STALENESS_HALF_LIFE_DAYS` flip 20→30 | Code-read: staleness lives in `nous/cognitive/context.py` only. BEAM harness bypasses pre_turn per F074 §5 | $0 |
| `recall_top_k` 10→20 | n=3 empirical smoke: per-Q delta = 0.000 on all 6 temporal Qs. Aggregate −0.016, abstention −0.167 | $7.30 |
| Inline date markers (`content_date_extractor.py`) | Annotates dates **inside retrieved snippets**; for PATTERN_MATCH_CONV2_Q0 class, the right snippet isn't retrieved | $0 (eliminated by diagnostic) |

### Failure-mode classification (verified `diag_temporal_failure_classes.py`)

5 prod-v3 temporal_reasoning failures (out of 10 Qs across conv 1-5) classified empirically:

| Q | Class | Source-chat | Heart facts | Heart chunks | Retrieved top-20 |
|---|---|---|---|---|---|
| Conv 2 Q0 ("API key March 10") | **PATTERN_MATCH_CONV2_Q0** | 1× "march 10" | 0 | 1 | NO |
| Conv 4 Q1 ("8/10 triangle score") | **PATTERN_MATCH_CONV2_Q0** | 1× "8/10" | 0 | 1 | NO |
| Conv 3 Q1 ("planning peer review April 2") | **PARTIAL_MATCH** | 2× "April 2" | 0 | 2 | YES (rank 3) |
| Conv 5 Q1 ("15 problems quiz") | **PARTIAL_MATCH** | 1× "15 problems" | 0 | 3 | YES (rank 3) |
| Conv 2 Q1 ("testing period April 5") | Source ambiguity | competing dates | varied | varied | both retrieved |

**Common across PATTERN_MATCH + PARTIAL_MATCH (4 of 5 failures):** the date-anchored event exists in `heart.episode_chunks` but has **0 corresponding facts in `heart.facts`**. The fact extractor produced facts about endpoints, error codes, components — but never the date-anchored event tuple.

### Synthetic validation (2026-05-27, ~$0.05)

Hand-injecting one fact — `"Christina obtained the OpenWeather API key on March 10, 2024."` — into `heart.facts` for `beam-100K-conv-002` with text-embedding-3-large embedding caused:
- Synthetic fact ranked **#3 of 39** in `run_recall_pipeline` at K=20 (score 0.827)
- LLM answer became: *"you obtained the OpenWeather API key on March 10, 2024, and completed the UI wireframe on March 12, 2024. That's **2 days** between the two events."*
- Conv 2 Q0 score moves from 0.000 → 1.000

### Root cause

**The episode summarizer's `candidate_facts` schema does not capture date-anchored event tuples.** The summarizer's output prompt (`episode_summarizer.py:50-56`) instructs the LLM to emit `{"subject", "content", "category"}` per candidate fact — no slot for dates. As a result, output is biased toward stable facts (`<entity> <attribute>` form) rather than episodic events (`<entity> <action> on <date>` form). Dates survive in chunks but chunks rank low on date-arithmetic queries when chunk content is dominated by surrounding context (code, prose).

This is also a **real product gap**, not benchmark-specific. Any user asking Nous "when did I do X" or "how long ago was Y" hits the same wall.

---

## Goals

1. **Extract date-anchored events as discrete facts** during episode summarization, structured as `<entity> <action> on <ISO-date>`.
2. **Persist `event_date: date | null`** on `heart.facts` — indexable column for date-range queries and graph edges. Surfaced into `RecallResult.metadata` for downstream consumers.
3. **Build `happened_before` edges** during sleep cycle: chronologically-adjacent same-episode facts only. Same-episode constraint inherits F070's ceiling (see §Risks).
4. **Date-aware retrieval boost** in `run_recall_pipeline` — detect date-language queries, gentle multiplicative boost on `event_date != NULL` facts within inferred window. *Deferred pending Layer 1+2+4 measurement.*
5. **Retrofit existing data** via `scripts/backfill_temporal_facts.py` — re-process NULL rows under PG advisory lock with token budget (F047 pattern), using chunk context not summary prose.
6. **Measurable**: re-run BEAM Phase 1 n=5 with this feature ON; expect temporal_reasoning ≥0.55. Validated on LongMemEval N=20 temporal-category retrieval first to avoid wasting BEAM budget.

## Non-goals

- **No new memory type.** Date-anchored events are still `heart.facts` rows.
- **No multi-date facts.** One fact = one event = one `event_date`. Range events → two facts + optional `happened_before` edge. F075.2 deferred.
- **No timezone handling.** ISO date at day granularity (`YYYY-MM-DD`).
- **No timeline UI.** Dashboard surface is F075.1.
- **No content_date_extractor.py wiring.** That module annotates dates at retrieval-time inside snippets; this feature operates at ingest-time on facts. The `_extract_regex` helper is reused (with commit + tests) by Layer 3's date-language detector.
- **No removal of existing FactExtractor prompts.** Augments behavior; existing extraction continues unchanged.
- **No reading per-message timestamps from chat metadata.** Some BEAM chats carry timestamps; prod conversations don't reliably. Dates must come from chat content.

---

## Design

### Layer 1 — Date-anchored extraction at summarization time

**Approach:** primary target is `EpisodeSummarizer.candidate_facts` schema (`nous/handlers/episode_summarizer.py:50-56`). Defense-in-depth augmentation of `FactExtractor._EXTRACT_PROMPT` for eval / direct-ingest paths that bypass the summarizer.

#### Layer 1a (primary): EpisodeSummarizer

`nous/handlers/episode_summarizer.py:50-56` defines the structured JSON the summarizer's LLM emits. Today's schema for each candidate fact: `{"subject", "content", "category"}`. v2 augments to optional 4th field:

```python
# Before (episode_summarizer.py:50-56, paraphrased):
"candidate_facts": [
  {"subject": "...", "content": "...", "category": "..."}
]

# After (F075):
"candidate_facts": [
  {"subject": "...", "content": "...", "category": "...",
   "event_date": "YYYY-MM-DD"  # OPTIONAL — only when fact describes a dated event
  }
]
```

**Prompt addition at `episode_summarizer.py:76-77`** (the candidate-facts instruction block):

```
DATE-ANCHORED EVENTS (F075):
When the transcript describes an event happening on a specific date — particularly
something the user did or that was completed — capture it as a SEPARATE candidate
fact with the date attached:

  subject:    <short descriptor of the event>
  content:    "<entity> <action verb> <object> on <full date>."
  category:   "event" (or relevant existing category)
  event_date: "<ISO YYYY-MM-DD>"

Examples:
  - "I got my OpenWeather API key on March 10" →
    {"subject": "OpenWeather API key acquisition",
     "content": "Christina obtained the OpenWeather API key on March 10, 2024.",
     "category": "event",
     "event_date": "2024-03-10"}
  - "We deployed v2.1 to staging last Tuesday" (episode_start_timestamp = 2024-04-11) →
    {"subject": "v2.1 staging deployment",
     "content": "Team deployed v2.1 to staging on 2024-04-09.",
     "category": "event",
     "event_date": "2024-04-09"}

CRITICAL: extract from the TRANSCRIPT text below — not from any summary you have
generated. Dates mentioned in passing (e.g. inside code blocks or user asides) are
just as important as headline dates. Resolve relative phrases ("yesterday", "last
week", "3 days ago") against EPISODE_START_TIMESTAMP. If the date is ambiguous
or unresolvable, OMIT event_date (set null) but still extract the fact without
the date.
```

**Critical wiring detail (Arch P1-2):** the summarizer already passes the transcript content into the LLM call when constructing the summary. The prompt addition above directs the same LLM to also extract dates from that transcript — no new data is needed; the LLM just needs the instruction. This is why we target the summarizer rather than the downstream extractor.

#### Layer 1b (defense-in-depth): FactExtractor

`nous/handlers/fact_extractor.py:_EXTRACT_PROMPT` is the fallback path used when `candidate_facts` are absent (eval shortcuts, direct ingest tools). Same instruction block as 1a, adapted for the fact-extractor's output schema. Lower priority because production traffic always has summarizer-emitted candidates.

#### Wire path (required additions across 7 hops)

Per Arch P1-1's enumeration:

| # | File:Line | Change |
|---|---|---|
| 1 | `episode_summarizer.py:50-77` | Add `event_date` to candidate_facts schema + prompt |
| 2 | `fact_extractor.py:251-256` | `_store_candidate_facts` reads `event_date = item.get("event_date")` from dict |
| 3 | `nous/heart/schemas.py:85-98` | `FactInput` adds `event_date: date \| None = None` |
| 4 | `nous/heart/facts.py:428-449` | `FactManager._learn` passes `event_date` to `Fact()` ORM constructor |
| 5 | `nous/storage/models.py:469-511` | `Fact` ORM adds `event_date: Mapped[date \| None] = mapped_column(Date, nullable=True)` AND `event_date_classified_at: Mapped[datetime \| None] = mapped_column(DateTime(timezone=True), nullable=True)` |
| 6 | `sql/migrations/053_temporal_fact_extraction.sql` | `ALTER TABLE heart.facts ADD COLUMN event_date DATE + event_date_classified_at TIMESTAMPTZ` + partial indexes + extend `brain.graph_edges` relation CHECK to add `'happened_before'` |
| 7 | `nous/heart/schemas.py:114-165` | `FactDetail` and `FactSummary` add `event_date: date \| None = None` |

**Plus**: `FactManager._learn` sets `event_date_classified_at = datetime.now(UTC)` on every fact write whose extraction path can produce dates (i.e., the new summarizer/extractor codepath always marks the row classified, even when `event_date` ends up `None`). This keeps newly-ingested facts out of the Layer 4 backfill eligibility set.

**Plus (Codex v2.1 review):** the summarizer's LLM call must receive `Episode.started_at` so it can resolve relative dates ("yesterday", "last Tuesday") deterministically. Current signature `_summarize_single(transcript, decision_context)` at `episode_summarizer.py:394` doesn't take it. Wire path additions for relative-date resolution:

| File:Line | Change |
|---|---|
| `episode_summarizer.py:377, 383` | Pass `started_at=episode.started_at` to `_summarize_single` calls |
| `episode_summarizer.py:394` | `_summarize_single(transcript, decision_context, started_at: datetime)` signature |
| `episode_summarizer.py:50-77` | Prompt template includes `EPISODE_START_TIMESTAMP: {started_at.isoformat()}` block above the transcript so the LLM has a deterministic anchor for relative-date resolution |

Without this thread-through, the prompt's "resolve relative phrases against EPISODE_START_TIMESTAMP" instruction has no value to anchor against and dates from transcripts like "deployed yesterday" become guesses.

**Plus** Heart.recall surface (Python P1-4): when serializing recall results, `RecallResult.metadata["event_date"]` must be populated with the iso string (or absent if None). Without this, Layer 3 is silent no-op.

#### Pydantic v2 validator

In `FactInput`:

```python
from datetime import date
from pydantic import field_validator

event_date: date | None = None

@field_validator("event_date", mode="before")
@classmethod
def _parse_event_date(cls, v):
    if v is None or isinstance(v, date):
        return v
    if isinstance(v, str):
        try:
            return date.fromisoformat(v)  # strict — rejects "2024-02-30" etc
        except ValueError:
            return None  # fail-soft: drop bad date, keep fact
    return None
```

`date.fromisoformat` is strict enough that we don't need a separate regex check; Python 3.12's stdlib rejects out-of-range days, malformed strings, etc.

### Layer 2 — `happened_before` edges (reranking reinforcer)

**Scope reframing per Arch P1-3 / Devil's #3:** Layer 2 is a **modest reranking reinforcer**, not a retrieval-surfacing lever. It only affects candidates already in the retrieved set. For the dominant PATTERN_MATCH failure class, the right fact is below the retrieval cut — Layer 2 cannot help that class directly. The value is on the PARTIAL_MATCH class, where both temporally-linked facts often ARE in the candidate set.

**Edge build (in `GraphDensifier.run_backfill_cycle`):**

`brain.graph_edges` requires `agent_id NOT NULL` (`sql/init.sql:197`) and constrains `relation` via a CHECK clause that currently allows only the existing relation set (`sql/init.sql:198-201` + migration `051_f070_chunk_graph_edges.sql:34-45`). The schema migration (§Schema migration) must extend the CHECK to add `'happened_before'`, mirroring the pattern F070 used to add `'part_of'` and `'summarized_by'`.

```sql
-- Each dated fact gets at most ONE outgoing happened_before edge,
-- pointing to a single representative fact on the next-distinct date
-- in the same episode. LATERAL with LIMIT 1 prevents the quadratic
-- explosion that would occur when multiple facts share the same
-- event_date (a NOT EXISTS-on-dates approach would link every fact
-- on date D to every fact on date D+1, producing O(N²) edges per
-- adjacent-date pair).
INSERT INTO brain.graph_edges
    (source_id, source_type, target_id, target_type, agent_id, relation, weight, auto_linked)
SELECT a.id, 'fact', b.id, 'fact',
       a.agent_id, 'happened_before', 1.0, TRUE
FROM heart.facts a
JOIN LATERAL (
    SELECT b.id
    FROM heart.facts b
    WHERE b.agent_id = a.agent_id
      AND b.source_episode_id = a.source_episode_id
      AND b.event_date IS NOT NULL
      AND b.event_date > a.event_date
    ORDER BY b.event_date ASC, b.id ASC   -- deterministic tiebreaker
    LIMIT 1
) b ON TRUE
WHERE a.agent_id = $1
  AND a.event_date IS NOT NULL
ON CONFLICT (source_id, target_id, relation) DO NOTHING;
```

Edge count per episode: at most N (where N = facts with non-NULL `event_date` in the episode). When multiple facts share the same `event_date`, all of them get a single outgoing edge pointing to the *same* representative successor — a 20-on-Jan-1 / 20-on-Jan-2 cluster produces 20 edges (Jan-1 facts → one Jan-2 fact), not 400. `auto_linked=TRUE` mirrors F040's sleep-cycle convention. Same-date facts deliberately get no edges between each other (we don't claim ordering on concurrent events).

**Consumer (already-shipped):**

`_apply_graph_adjacency_boost` in `nous/api/retrieval_pipeline.py:243-247, 699-738`. Excludes `contradicts` only; sums all other edge relations including `happened_before`. **No new consumer code required.**

**Required impl-plan step:** flip `NOUS_GRAPH_ADJACENCY_BOOST_ENABLED` from `false` to `true` (current default in `config.py:1055-1056`). Without this flip Layer 2 ships dark.

**Same-episode constraint ceiling (Arch P2-3):** following F070's pattern, edges only cross facts within the same `source_episode_id`. This is acceptable for v1 because BEAM convs are single long haystacks where within-episode date arithmetic is the common case. Cross-episode `happened_before` (e.g., "how long between event in March vs event in May") deferred to F075.1.

### Layer 3 — Date-aware retrieval boost (DEFERRED)

**Phase decision per Arch §Phase Strategy:** defer until Layer 1+2+4 are measured. Synthetic validation showed the date-anchored fact ranks #3 of 39 on its own embedding strength — Layer 3 may be unnecessary. Gate decision on LME pre-check + BEAM measurement.

**If implemented later**, design:

```python
async def _apply_date_boost(
    results: list[PipelineResult],
    query: str,
    settings: Settings,
) -> list[PipelineResult]:
    """F075: gentle multiplicative boost for facts whose event_date is
    within the date window implied by the query.
    """
    if not settings.date_aware_boost_enabled:
        return results
    window = _infer_query_date_window(query)  # (start, end) | None
    if window is None:
        return results

    factor = settings.date_aware_boost_factor
    boosted = []
    for r in results:
        if r.type != "fact":
            boosted.append(r); continue
        ed_iso = r.metadata.get("event_date")  # surfaced via Layer 1 Heart.recall change
        if ed_iso is None:
            boosted.append(r); continue
        try:
            event_date = date.fromisoformat(ed_iso)
        except ValueError:
            boosted.append(r); continue
        if window[0] <= event_date <= window[1]:
            # Match _apply_adjacency_boost coalesce pattern at retrieval_pipeline.py:736-737
            new_score = (r.score or 0.0) * factor
            boosted.append(replace(r, score=new_score))
        else:
            boosted.append(r)
    boosted.sort(key=lambda r: (r.score or 0.0), reverse=True)
    return boosted
```

`_infer_query_date_window` reuses the `_extract_regex` helper from `nous/heart/content_date_extractor.py`. Per Arch P3-1 / Python P3, that file must be committed and given tests before being imported in `retrieval_pipeline.py`.

### Layer 4 — Retrofit script

`scripts/backfill_temporal_facts.py`. F047 pattern — copies `nous/handlers/actionability_backfill.py` patterns verbatim.

#### Advisory lock (copy F047 verbatim — Python P1-3)

```python
import hashlib

def _advisory_lock_key(agent_id: str) -> int:
    """Stable signed bigint key derived from agent_id SHA-256.
    Mirrors actionability_backfill.py:108-115.
    """
    digest = hashlib.sha256(agent_id.encode("utf-8")).digest()[:8]
    return int.from_bytes(digest, byteorder="big", signed=True)
```

`async with db.transaction()` + `SELECT pg_try_advisory_xact_lock($1)` with this key. Mirrors F047 + F049 patterns exactly.

#### Per-row UPDATE (Python P1-2 fix) + classification-state marker (Codex P1 fix)

v1's `WHERE id = (SELECT id FROM batch)` was broken for `LIMIT > 1`. v2 copies F047's `actionability_backfill.py:198-216` shape AND fixes a starvation bug Codex flagged on the spec-v2 review: F047 works because every row gets a non-NULL `actionable` verdict. F075 has a third state ("classified, no date found") that looks identical to "never classified" if we use `event_date IS NULL` as the eligibility predicate — so the script would re-process the same newest-N stable facts forever and never advance to older dated rows.

Fix: the schema adds `event_date_classified_at TIMESTAMPTZ` (see §Schema migration). The backfill writes it on every classification attempt regardless of outcome. Eligibility uses `event_date_classified_at IS NULL` (not `event_date IS NULL`).

```python
# Pseudo-code; concrete sites verified against F047
async def _process_batch(conn, agent_id: str, batch_size: int) -> int:
    rows = await conn.fetch("""
        SELECT id, subject, content, source_episode_id
        FROM heart.facts
        WHERE agent_id = $1
          AND event_date_classified_at IS NULL  -- eligibility = "never tried"
          AND active = TRUE
        ORDER BY learned_at DESC
        LIMIT $2
    """, agent_id, batch_size)

    updated = 0
    for row in rows:
        result = await _classify_event_date(row)  # returns date | None
        # ALWAYS mark classified — even when no date found. This is what
        # prevents re-processing the same stable facts on subsequent batches.
        await conn.execute("""
            UPDATE heart.facts
            SET event_date = $1,
                event_date_classified_at = NOW(),
                updated_at = NOW()
            WHERE id = $2
        """, result, row["id"])  # $1 may be NULL — that's correct
        updated += 1
    return updated
```

This makes the script terminate cleanly (every batch advances the eligibility cursor) and remains idempotent (re-runs only pick up rows still at `event_date_classified_at IS NULL`, which by definition haven't been tried).

The live-path Layer 1 extractor also writes `event_date_classified_at = NOW()` whenever it stores a fact (whether or not it found a date), so newly-ingested facts never enter the backfill eligibility set.

#### Classification LLM call (Python P2 — guaranteed JSON)

Use `call_background_llm_structured` from `nous/handlers/__init__.py:86` (or wherever the helper lives) with a tool_use input_schema for guaranteed JSON output. No parse-repair logic in the script.

#### Context source (Arch P2-1 / Python P2 — chunk context, not summary)

v1 fed `episode.summary[:500]` to the classifier. That suffers the same lossy-prose problem as Layer 1. v2 fetches the most-relevant `heart.episode_chunks` row for the fact (best chunk by content similarity OR by simply selecting the chunk whose content contains the candidate entity/action):

```sql
-- Per-row: find chunks containing the fact's subject keywords
SELECT content
FROM heart.episode_chunks
WHERE agent_id = $1 AND episode_id = $2
  AND content ILIKE '%' || $3 || '%'  -- subject as keyword
ORDER BY chunk_index
LIMIT 1;
```

Feeds raw chunk text (up to ~600 chars) to the classifier instead of the lossy 500-char summary slice.

#### CLI shape

```bash
uv run python scripts/backfill_temporal_facts.py \
    --agent-id nous-default \
    --batch-size 100 \
    --token-budget 50000 \
    --dry-run     # estimate cost without LLM calls
```

#### Idempotence

`WHERE event_date_classified_at IS NULL` predicate makes re-runs safe — only rows that have NEVER been classified are picked up. Stable facts that were classified and intentionally left with `event_date = NULL` (because the classifier correctly found no date) are NOT eligible for re-processing. This is the same column the per-batch SELECT above uses; the predicate stays consistent everywhere.

#### Edge-build trigger

At end-of-script (post Layer 1 column populated), build `happened_before` edges synchronously. `GraphDensifier` captures `agent_id` in its constructor (`graph_densifier.py:108-119`) and `run_backfill_cycle()` itself takes no args (`graph_densifier.py:1045`) — the call shape mirrors `sleep_handler.py:1354`:

```python
# In the backfill script, after all rows have been classified for the agent:
from nous.brain.graph_densifier import GraphDensifier
from nous.brain.graph_linker import GraphLinker

graph_linker = GraphLinker(db, embedder, settings, agent_id)
densifier = GraphDensifier(db, graph_linker, embedder, settings, agent_id)
result = await densifier.run_backfill_cycle()
```

The densifier instance is agent-scoped; running it post-classification ensures any `happened_before` edges between newly-dated facts are written before the script exits.

---

## Schema migration

`sql/migrations/053_temporal_fact_extraction.sql`:

```sql
BEGIN;

-- F075: add event_date + classification-state columns to heart.facts
ALTER TABLE heart.facts
    ADD COLUMN IF NOT EXISTS event_date DATE DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS event_date_classified_at TIMESTAMPTZ DEFAULT NULL;

-- Index for date-range queries (Layer 3 + Layer 2 edge build)
CREATE INDEX IF NOT EXISTS idx_facts_event_date_agent
    ON heart.facts(agent_id, event_date)
    WHERE event_date IS NOT NULL;

-- Index for backfill eligibility scan (event_date_classified_at IS NULL)
CREATE INDEX IF NOT EXISTS idx_facts_event_date_unclassified_agent
    ON heart.facts(agent_id, learned_at)
    WHERE event_date_classified_at IS NULL;

COMMENT ON COLUMN heart.facts.event_date IS
    'F075: ISO date of the event this fact describes. NULL = stable fact (not event-anchored) OR pre-F075 row pending backfill.';
COMMENT ON COLUMN heart.facts.event_date_classified_at IS
    'F075: timestamp the backfill (or live extractor) classified this row for event_date. NULL = never classified, eligible for backfill. NOT NULL with event_date IS NULL = classified but no date found (terminal state, do NOT re-classify).';

-- F075 Layer 2: extend brain.graph_edges relation CHECK to allow 'happened_before'.
-- Mirrors migration 051_f070_chunk_graph_edges.sql:34-45 pattern.
ALTER TABLE brain.graph_edges DROP CONSTRAINT IF EXISTS ck_edges_relation;
ALTER TABLE brain.graph_edges DROP CONSTRAINT IF EXISTS graph_edges_relation_check;
ALTER TABLE brain.graph_edges
    ADD CONSTRAINT ck_edges_relation CHECK (
        relation IN (
            'supports', 'contradicts', 'supersedes', 'related_to', 'caused_by',
            'informed_by', 'evidence_for', 'discussed_in', 'extracted_from',
            'part_of', 'summarized_by',
            -- F075 additions:
            'happened_before'
        )
    );

COMMIT;
```

Partial indexes:
- `idx_facts_event_date_agent` excludes NULL rows so date-arithmetic queries scan only the event-fact subset.
- `idx_facts_event_date_unclassified_agent` accelerates backfill's eligibility query (`WHERE event_date_classified_at IS NULL ORDER BY learned_at DESC LIMIT N`).

The relation CHECK extension is required by Layer 2's INSERT — without it the edge build raises a constraint violation. The pattern matches F070's migration 051 verbatim.

**ORM CheckConstraint must also be updated** (`nous/storage/models.py:239-244`). The SQLAlchemy `GraphEdge.__table_args__` declares its own `ck_edges_relation` constraint that is currently **stale** — it still lists only the init.sql-era relations and was not updated when F070's migration 051 added `part_of` and `summarized_by`. Any fresh-schema path (tests using `Base.metadata.create_all`, future Alembic autogenerate) would reject all three relations.

F075 brings the ORM fully current:

```python
# nous/storage/models.py — GraphEdge.__table_args__
CheckConstraint(
    "relation IN ('supports', 'contradicts', 'supersedes', 'related_to', 'caused_by', "
    "'informed_by', 'evidence_for', 'discussed_in', 'extracted_from', "
    "'part_of', 'summarized_by', "             # F070 catch-up
    "'happened_before')",                       # F075 addition
    name="ck_edges_relation",
),
```

Style matches `052_f069_document_source_kind.sql:22,34`.

---

## Settings additions (`nous/config.py`)

```python
# F075 — Temporal extraction & date-aware retrieval
# All flags default OFF for dark-launch consistency (F042/F047/F067/F071 pattern).
temporal_extraction_enabled: bool = Field(
    default=False,
    description="F075: include date-anchored event extraction in summarizer + "
                "fact-extractor prompts. Flip to True after measurement confirms "
                "no regression on existing test suite or LME baseline.",
)
date_aware_boost_enabled: bool = Field(
    default=False,
    description="F075 Layer 3: gentle multiplicative boost on facts with "
                "event_date in query's inferred date window. Deferred from v2 "
                "pending Layer 1+2+4 measurement; ship with flag default off.",
)
date_aware_boost_factor: float = Field(
    default=1.20, ge=1.0, le=2.0,
    description="F075: multiplier applied to in-window facts. 1.0 = no boost.",
)
date_aware_boost_window_pad_days: int = Field(
    default=30,
    description="F075: pad days around inferred query date window.",
)
temporal_backfill_default_token_budget: int = Field(
    default=50000,
    description="F075: default Haiku token cap for backfill script.",
)
```

`CLAUDE.md` env-var table gets 5 new rows. Update is part of the impl PR, not separate.

---

## Tests

**`tests/test_temporal_extractor.py`** (new):
- Summarizer prompt produces `event_date` field when transcript explicit (mock LLM)
- Summarizer prompt resolves relative dates against episode_start_timestamp
- Malformed date string fails `date.fromisoformat` → field dropped, fact kept
- Ambiguous date → field omitted, no false positive
- Same event, same date → dedup catches duplicate (existing dedup path unchanged)
- `_store_candidate_facts` reads `event_date` from dict and threads into `FactInput`
- FactExtractor fallback prompt (Layer 1b) also emits event_date

**`tests/test_temporal_edges.py`** (new):
- Two facts same episode, different dates → 1 `happened_before` edge
- Three facts in chronological order → 2 edges (chain, not all pairs)
- Cross-episode facts → no edge
- NULL event_date on either side → no edge
- ON CONFLICT DO NOTHING prevents duplicate edges on re-run

**`tests/test_temporal_backfill.py`** (new):
- Advisory lock (mocked `pg_try_advisory_xact_lock`) prevents concurrent backfills
- Token budget exhausted → clean halt, no partial-row corruption
- Dry-run produces estimate without LLM calls
- NULL-only filter means re-running picks up only unprocessed rows
- Chunk context source (not summary) — verified via mock chunk lookup

**`tests/test_date_aware_boost.py`** (new, Layer 3 deferred but tests stay):
- Query "how many days between" detects as date-arithmetic
- Query "OpenWeather endpoints" produces no window
- Fact with event_date in window: `(score or 0.0) * factor`
- Fact outside window untouched
- Re-sort stable when no boosts applied; missing scores coalesce to 0.0

**Integration test in `tests/test_f075_end_to_end.py`:**
- Ingest a fixture conversation with explicit date references
- Verify extracted facts have correct `event_date`
- Verify `RecallResult.metadata["event_date"]` is populated for retrieved facts
- Verify edges built via sleep cycle
- Confirm baseline retrieval (without Layer 3) still surfaces dated fact at high rank

pytest-asyncio config in `pyproject.toml:59` is already `asyncio_mode = "auto"` (verified by python reviewer); no config change needed. The repo currently has `tests/test_fact_extractor_episode_id.py` (F022-narrow); no general `test_fact_extractor.py` exists yet, so F075 establishes one.

---

## Acceptance criteria

1. **All new tests pass.** 4 unit files + 1 integration file, ~30 tests.
2. **Existing tests pass.** No regression in `tests/test_fact_extractor_episode_id.py`, `tests/test_heart.py`, `tests/test_graph_densifier.py`.
3. **Migration runs cleanly on fresh DB** (`docker compose up` cold start), verified by F074 harness pattern.
4. **LongMemEval N=20 retrieval pre-check** (cheap, ~$5): hit@10 on temporal-reasoning category questions improves by ≥+5% vs current baseline. If LME pre-check fails, do NOT proceed to BEAM.
5. **BEAM Phase 1 re-run (n=5 conv) shows temporal_reasoning ≥ 0.55**, ideally ≥ 0.60. Other ability scores stay within ±0.05 of prod-v3 (no collateral regression like K-bump's abstention -0.167).
6. **Per-failure-class verification** (cheap, $0.30 of synthetic-fact tests): inject hand-crafted dated facts for one PATTERN_MATCH (conv 4 Q1) and one PARTIAL_MATCH (conv 5 Q1), confirm both move from 0.000 to ≥0.5 via the same chain as the conv 2 Q0 baseline. Devil's #2 risk-mitigation: pre-implementation gate.
7. **Retrofit script dry-run** on `nous-default` prod-snapshot reports cost estimate; full run completes within token budget; advisory lock prevents concurrent execution.
8. **Settings docs updated** in `CLAUDE.md` env-var table.

---

## Cost & risk

**Implementation cost:**
- Layer 1 (summarizer + extractor prompts + schema wire path): ~110 LOC across 7 files + migration + 8 tests
- Layer 2 (happened_before edges + flag flip): ~60 LOC in GraphDensifier + 5 tests
- Layer 3 (date-aware boost): ~50 LOC in retrieval_pipeline + 5 tests *(deferred)*
- Layer 4 (backfill script): ~280 LOC + 6 tests + CLI
- Total: ~500 LOC (450 if Layer 3 deferred) + 30 tests. ~3-4 days for one engineer.

**Measurement cost:**
- Synthetic per-failure-class verification (criterion #6): ~$0.30
- LongMemEval N=20 temporal retrieval pre-check: ~$5
- BEAM Phase 1 n=5 re-run: ~$7
- Total: ~$13

**Backfill cost (prod):**
- `nous-default` ~5K facts × Haiku ~10 tokens each ≈ ~$0.30
- Plus chunk-context lookup: ~1 extra DB query per fact, negligible

**Risks:**

1. **Prompt-engineering collateral on summarizer.** Layer 1 modifies the summarizer prompt, which is a high-traffic shared path. Risk: existing fact-extraction behavior shifts on non-event content. **Mitigation**: integration test with known-event corpus; LME hand-label qrels comparison; explicit acceptance criterion #2.
2. **PARTIAL_MATCH class may not move as expected.** Devil's #2 revealed 2 of 5 failures are PARTIAL_MATCH where chunks already surface but LLM doesn't compose. Discrete dated facts SHOULD help (they're easier to combine than prose), but this is unverified. **Mitigation**: acceptance criterion #6 pre-implementation synthetic verification at ~$0.30.
3. **`happened_before` edges may bleed into wrong adjacency reinforcement.** `_apply_graph_adjacency_boost` sums ALL non-`contradicts` relations indistinguishably (`retrieval_pipeline.py:712`). If temporal_anchor neighbors mislead retrieval for non-temporal queries, we'd see collateral. **Mitigation**: monitor non-target ability scores in BEAM re-run; if regression > 0.05, revisit adjacency-boost allowlist (F075.1 candidate).
4. **Same-episode `happened_before` ceiling.** Inherits F070 cross-episode-edge gap. Cross-session date pairs never get edges (F075.1). Acceptable for v1 because BEAM convs are single-haystack.
5. **Source ambiguity (1 of 5)** unfixable by this work. Ceiling at ~0.7 on temporal_reasoning from extractor + edges alone.

---

## Alternatives considered & rejected

### A1. Wire `nous/heart/content_date_extractor.py` into `_format_pipeline_text`

The existing untracked module annotates retrieved snippets with `[event: YYYY-MM-DD (~N months ago)]` markers. Falsified for the PATTERN_MATCH class: the right snippet isn't retrieved, so markers have nothing to annotate. **Partially repurposed in Layer 3** — its `_extract_regex` function is reused by the date-language detector (with commit + tests added per P3).

### A2. Synthetic `nous_system.date_anchors` node table

A separate table where each unique date gets a UUID, then `temporal_anchor` edges target those rows. Enables graph-walk semantics ("find all facts on this date"). Deferred to F075.1 because:
- `WHERE event_date = '2024-03-10'` already does the work via index
- Adds a new table for a use case we don't yet have

### A3. Separate `nous/handlers/temporal_fact_extractor.py` parallel pipeline

Doubles LLM cost per episode. Augmenting summarizer + extractor prompts is simpler with same expressive power.

### A4. Multi-date facts (start + end ranges in one fact)

Reject for v1: requires schema beyond one column, complicates dedup, no rubric tests range queries. F075.2 if needed.

### A5. Read timestamps from chat metadata

Reject: prod doesn't reliably have message timestamps. Couples memory to ingest-time metadata.

### A6. Defer Layer 3 entirely

Adopted in v2. Synthetic validation showed fact embedding alone ranks #3 without boost. Ship Layers 1+2+4; measure; add Layer 3 only if temporal_reasoning lands <0.55.

---

## Open questions (resolved in v2)

| v1 question | v2 resolution |
|---|---|
| Augment vs new module (Layer 1) | Augment. Primary target = EpisodeSummarizer. Defense-in-depth = FactExtractor. |
| Layer 3 phase decision | Defer until Layer 1+2+4 measurement. |
| `event_date` validator stringency | Fail-soft (drop field, keep fact) via `date.fromisoformat` in Pydantic v2 `field_validator`. |
| Backfill batching | Per-row UPDATE copying F047's `actionability_backfill.py:198-216`. |
| Edge build trigger | Synchronous at end-of-backfill-script. |
| Token budget default | Total cap with resume, 50K Haiku tokens default. Matches F047. |

---

## Rollback

1. Set `NOUS_TEMPORAL_EXTRACTION_ENABLED=false` — new ingests stop producing event_date facts. Existing facts retain their event_date.
2. Set `NOUS_DATE_AWARE_BOOST_ENABLED=false` — Layer 3 disabled. (Default off.)
3. Set `NOUS_GRAPH_ADJACENCY_BOOST_ENABLED=false` (revert prod flip) — Layer 2 reranking disabled.
4. Migration is forward-only. To fully revert column: drop via v2 migration. Partial index is harmless to leave.
5. Backfill outputs are idempotent and additive — no rollback path needed.

Feature is **flag-gated end-to-end**. Worst case: turn off flags, behavior reverts to pre-F075. Cost = one-time backfill spend.

---

## Deferred to F075.x

- **F075.1** — `nous_system.date_anchors` node table + true `temporal_anchor` edges (graph-walk); cross-episode `happened_before` edges (F070 ceiling fix)
- **F075.2** — Multi-date facts (range events)
- **F075.3** — Timeline dashboard tab
- **F075.4** — Post-extract `dateparser` fallback (defense-in-depth on LLM extraction misses)
- **F075.5** — Adjacency-boost allowlist (per-relation weighting); only if BEAM measurement shows collateral regression
