# F075 Implementation Plan — Temporal Fact Extraction

**Spec:** [`docs/features/F075-temporal-fact-extraction.md`](../../features/F075-temporal-fact-extraction.md) (v2.17, merged in PR #460 / squash `0115568`)
**Reviews:**
- Spec-side (already incorporated into spec): arch + python-pro + devil's-advocate + 17 codex rounds. See [[f075-codex-iteration-pattern]] memory for the iteration breakdown.
- Impl-side (this plan): `docs/reviews/F075-plan-arch-review.md` + `F075-plan-code-review.md` + `F075-plan-devil-review.md`. **v2 of this plan incorporates 5 P1s + multiple P2s/P3s** — see §v2 changelog below.
**Forge:** to be created during Phase 1.
**Date:** 2026-05-28 (plan v2 — same day; 3-agent review same day)

This plan is **tactical**, not architectural. The spec already pins every wire-path hop (15 rows) to a file:line. The implementer's job is to copy from the spec verbatim, not to redesign. Where the spec is incomplete or ambiguous, this plan resolves it inline.

## v4 changelog (scope split for shippability)

After completing Phases 1-7 during impl, recognized that Phase 8 (backfill script — 250+ LOC, the piece that ate ~14 codex rounds in the spec) warrants its own dedicated PR for review focus. Live-path F075 (extraction + edges + retrieval surfacing) stands alone — new facts get F075-classified, old facts simply stay `event_date_classified_at IS NULL` until F075.1 ships the backfill.

**v4 changes:**
- **Phase 8 deferred to F075.1 follow-up PR.** This impl PR ships Phases 1-7 + 9 + 10. Justification: (a) live-path is the primary user value; (b) backfill is a one-time operator action, not blocking; (c) the spec already split Layer 3 to F075.x — same pattern; (d) PR review burden bounded.
- Phase 12 PR body documents the follow-up requirement and links to F075.1 tracking.
- Tests for `test_f075_backfill.py` and the multi-batch lock-hold test ship with F075.1, not this PR.

## v3 changelog (Phase 0 source-read recalibration)

Phase 0 prep (reading BEAM source files for conv 4 + conv 5) revealed two of the three planned verification cases are **not actually date-arithmetic problems**:

- **Conv 4 Q1** rubric is `'2 problems'`. Ideal explicitly says "This is **inferred**" from problem-count chronology, not a stated date. F075's `<entity> <action> on <date>` extraction mechanism doesn't apply.
- **Conv 5 Q1** rubric is `'10 days'`. Source contains "april 5, 2024" (start anchor) but does NOT contain the second event date — the rubric infers "10 days" from chat position. F075 can't extract a date that isn't in the source.

This means the spec's `f074-temporal-diagnosis-2026-05-27` failure-mode classification (PATTERN_MATCH/PARTIAL_MATCH for these cases) was **misattributed** — the missing canonical detail wasn't a date in either case.

**Realistic F075 lift on the 5 BEAM temporal_reasoning failures:**

| Q | Class | F075 addresses? |
|---|---|---|
| Conv 2 Q0 (API key March 10) | PATTERN_MATCH genuine date | ✓ validated |
| Conv 2 Q1 (April 1 vs April 5) | source ambiguity | ✗ |
| Conv 3 Q1 (peer review April 2) | PARTIAL_MATCH date | ~ partial |
| Conv 4 Q1 (8/10 → 2 problems) | problem-count arithmetic | ✗ |
| Conv 5 Q1 (10 days, undated event) | missing date in source | ✗ |

Best case: 1 clean + 1 partial. Recalibrated acceptance target accordingly (see Phase 0 + acceptance updates below).

**v3 changes:**
- Phase 0 narrowed to conv 2 Q0 only (re-validation at ~$0.05). Conv 4 Q1 + conv 5 Q1 dropped — F075's mechanism doesn't address them.
- Acceptance criterion #5 (BEAM temporal_reasoning ≥ 0.55) **relaxed to ≥ 0.45**, with explicit note that the realistic ceiling for F075 alone is ~0.50-0.55. The 0.65-0.70 estimate in the spec was based on the misclassified failure modes.
- Phase 12 PR body must document the calibrated expectation and acknowledge BEAM acceptance is a "best-effort" measurement, not a strict gate.
- **F075 is still worth shipping**: it's a real product improvement (better fact extraction for date-anchored events that DO exist in source content). User confirmed.
- New deliverable: this impl PR also includes a spec amendment bumping F075 spec to v2.18 with the Phase 0 / acceptance recalibration documented.

## v2 changelog (3-agent review)

Three parallel reviews on plan v1 found 5 P1s. All addressed in v2:

- **Arch P1 #1**: Layer 2 ships dark without `NOUS_GRAPH_ADJACENCY_BOOST_ENABLED=true` flip. Added to Phase 7 + Phase 10.
- **Arch P1 #2**: `_supersede_by_subject` bypass missing — Phase 5.2 only covered cosine path. For "API key obtained March 10" / "March 12" pairs, same-subject supersession destroys the date pair. Added Phase 5.2b.
- **Code P1 #1**: `self._db.session()` is wrong — `GraphDensifier` exposes `self.db`. Phase 7 snippet would AttributeError. Fixed.
- **Code P1 #2**: `_to_recall_result` is in `nous/heart/heart.py:1099-1110`, NOT `facts.py`. Spec wire-path row 10 has the same drift; survived 17 codex rounds. Fixed in Phase 5.5.
- **Code P1 #3**: `GraphEdge` has F065 `extraction_method` CHECK; Phase 7 INSERT must populate it. Fixed.
- **Devil's Critical**: Phase 8 likely re-introduces the lock-leak fix because F047 (single-session) is the mental anchor cited ~10× in the spec. Added anti-pattern callout + multi-batch lock-hold test. Phase 11 review budget bumped to 3-5 rounds.
- **Devil's High**: Phase 0 + Phase 9 don't validate BEAM acceptance #5. Added boost-on/off ranking assertion in Phase 9; documented residual risk for acceptance #5 explicitly.
- **Devil's High**: Phase 12 `git add -A` dangerous. Replaced with explicit pathspec listing each F075 file.
- Phase 0 gate criterion was self-contradictory (table ≥0.5, prose "all 3 pass"). Diagnostic is binary → table corrected.
- Phase 0 adaptation scope upgraded from "minor adapt" to ~50-80 LOC.
- Phase 4 `hasattr(existing[0], "event_date")` dropped (dead code after Phase 2).
- Phase 5.2 references explicit dedup call site at `_find_duplicate → _confirm` (`facts.py:376-381`).
- Phase 7 `result.fetchall()` → `result.rowcount`.
- Test files renamed to `test_f075_*.py` (`test_temporal_recall.py` already exists, unrelated to F075).
- Open Question 3 resolved (`call_background_llm_structured` confirmed at `nous/handlers/__init__.py:86`).

---

## Order of operations

Tasks run in strict order. Each gate must pass before the next phase starts. Phase 0 is a pre-impl empirical verification gate per spec acceptance criterion #6 — if it fails, the entire plan is suspended and the spec needs revisiting.

| # | Phase | Files / Action | Gate |
|---|---|---|---|
| 0 | Re-validate conv 2 Q0 (only) | `diag_synthetic_temporal_validate.py` unchanged from session use | **CONFIRMED verdict** on conv 2 Q0. (Conv 4 Q1 + Conv 5 Q1 dropped per v3 source-read finding — see v3 changelog.) |
| 1 | Schema foundation | `sql/migrations/053_temporal_fact_extraction.sql` + `nous/storage/models.py` | `docker compose down -v && up -d` cold start; `_run_migrations()` exits 0; schema check confirms columns + indexes + CHECK constraints |
| 2 | Pydantic schemas | `nous/heart/schemas.py` | `tests/test_temporal_schemas.py` passes (validator unit tests) |
| 3 | Layer 1a — summarizer | `nous/handlers/episode_summarizer.py` | `tests/test_temporal_extractor.py::test_summarizer_*` passes |
| 4 | Layer 1b — FactExtractor | `nous/handlers/fact_extractor.py` | `tests/test_temporal_extractor.py::test_fact_extractor_*` passes |
| 5 | Heart | `nous/heart/facts.py` | `tests/test_temporal_extractor.py::test_heart_*` passes; existing `test_heart.py` unaffected |
| 6 | Retrieval pipeline | `nous/api/retrieval_pipeline.py` + `nous/config.py` | `tests/test_temporal_extractor.py::test_metadata_surfacing` passes |
| 7 | Layer 2 — happened_before edges | `nous/brain/graph_densifier.py` | `tests/test_temporal_edges.py` passes |
| 8 | Layer 4 — backfill script | `scripts/backfill_temporal_facts.py` + helper module | `tests/test_temporal_backfill.py` passes; `--dry-run` works on eval-DB fixture |
| 9 | Integration | `tests/test_f075_end_to_end.py` | Real ingest fixture: dated facts persist, `event_date_classified_at` set when flag on, retrieval `RecallResult.metadata["event_date"]` populated |
| 10 | CLAUDE.md + INDEX.md | `CLAUDE.md` env-var table + `docs/features/INDEX.md` row | Manual diff review |
| 11 | Code review | 3-agent review of the impl diff | All P1s resolved |
| 12 | PR | branch + commit + push + `gh pr create` | PR URL returned |

---

## Phase 0 — Re-validate conv 2 Q0 (acceptance criterion #6, recalibrated)

**Goal (v3-recalibrated):** re-confirm the single legitimate F075 verification case — conv 2 Q0 ("API key obtained on March 10") — still moves under synthetic fact injection. The v2 plan tried to verify conv 4 Q1 + conv 5 Q1 as well, but the Phase 0 source-read found those cases aren't actually date-arithmetic problems F075 addresses (see v3 changelog).

**Cost:** ~$0.05 (1 synthetic Anthropic call against `beam-100K-conv-002`).

**Steps:**
1. Run `diag_synthetic_temporal_validate.py` unchanged (no script refactor needed — it's already configured for conv 2 Q0).
2. Verify CONFIRMED verdict (all 3 auto-checks pass: 'March 10', 'March 12', '2 days').

**Gate:** CONFIRMED verdict. The mechanism was already validated at v2.16 of the spec; this is a sanity re-run after the spec merged to main to catch any regression from main's current state.

**If gate fails:** halt, investigate why the previously-validated case stopped working (most likely cause: someone changed something on main between spec merge and impl start).

**Documented out-of-scope cases:** conv 4 Q1 (problem-count arithmetic) and conv 5 Q1 (undated second event) need a different mechanism than F075. Candidate for a follow-up F075.x or separate feature. Memory entry to be added during impl: "F075 scope vs BEAM failure modes."

---

## Phase 1 — Schema foundation

**Files:**
- `sql/migrations/053_temporal_fact_extraction.sql` (new, ~30 LOC)
- `nous/storage/models.py` (`Fact` ORM lines 469-511; `GraphEdge` ORM lines 235-260)

### 1.1 Migration 053

Copy the SQL verbatim from spec §Schema migration. The full content:

```sql
BEGIN;

-- F075: add event_date + classification-state columns to heart.facts
ALTER TABLE heart.facts
    ADD COLUMN IF NOT EXISTS event_date DATE DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS event_date_classified_at TIMESTAMPTZ DEFAULT NULL;

CREATE INDEX IF NOT EXISTS idx_facts_event_date_agent
    ON heart.facts(agent_id, event_date)
    WHERE event_date IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_facts_event_date_unclassified_agent
    ON heart.facts(agent_id, learned_at)
    WHERE event_date_classified_at IS NULL;

COMMENT ON COLUMN heart.facts.event_date IS
    'F075: ISO date of the event this fact describes. NULL = stable fact (not event-anchored) OR pre-F075 row pending backfill.';
COMMENT ON COLUMN heart.facts.event_date_classified_at IS
    'F075: timestamp the backfill (or live extractor) classified this row for event_date. NULL = never classified, eligible for backfill. NOT NULL with event_date IS NULL = classified but no date found (terminal state, do NOT re-classify).';

-- F075 Layer 2: extend brain.graph_edges relation CHECK to allow 'happened_before'.
ALTER TABLE brain.graph_edges DROP CONSTRAINT IF EXISTS ck_edges_relation;
ALTER TABLE brain.graph_edges DROP CONSTRAINT IF EXISTS graph_edges_relation_check;
ALTER TABLE brain.graph_edges
    ADD CONSTRAINT ck_edges_relation CHECK (
        relation IN (
            'supports', 'contradicts', 'supersedes', 'related_to', 'caused_by',
            'informed_by', 'evidence_for', 'discussed_in', 'extracted_from',
            'part_of', 'summarized_by',
            'happened_before'
        )
    );

COMMIT;
```

### 1.2 ORM updates

In `nous/storage/models.py`:

**A. `Fact` class** (lines 469-511): add two `Mapped` columns:

```python
event_date: Mapped[date | None] = mapped_column(Date, nullable=True)
event_date_classified_at: Mapped[datetime | None] = mapped_column(
    DateTime(timezone=True), nullable=True
)
```

`date` and `datetime` must be imported from `datetime` if not already.

**B. `GraphEdge.__table_args__`** (lines 235-260): bring ALL THREE `CheckConstraint` declarations current with migrations 051 + 053:

```python
CheckConstraint(
    "relation IN ('supports', 'contradicts', 'supersedes', 'related_to', 'caused_by', "
    "'informed_by', 'evidence_for', 'discussed_in', 'extracted_from', "
    "'part_of', 'summarized_by', "             # F070 catch-up
    "'happened_before')",                       # F075
    name="ck_edges_relation",
),
CheckConstraint(
    "source_type IN ('decision', 'fact', 'episode', 'procedure', 'chunk')",  # F070 catch-up
    name="ck_edges_source_type",
),
CheckConstraint(
    "target_type IN ('decision', 'fact', 'episode', 'procedure', 'chunk')",  # F070 catch-up
    name="ck_edges_target_type",
),
```

### 1.3 Verification

```powershell
# Fresh DB
docker compose --profile eval down -v
docker compose --profile eval up -d
# Wait for healthy
docker logs nous-eval-scratch 2>&1 | grep "database system is ready"
# Apply migrations via the existing migrator
uv run python -c "import asyncio; from nous.storage.database import Database; from nous.storage.migrator import run_migrations; from nous.config import Settings; asyncio.run(run_migrations(Database(Settings()).engine))"
# Verify schema
psql -h localhost -p 5433 -U nous -d nous_eval_scratch -c "\d+ heart.facts" | grep event_date
```

**Gate:** `event_date DATE` and `event_date_classified_at TIMESTAMPTZ` columns exist; both partial indexes exist; `ck_edges_relation` includes `'happened_before'`.

### 1.4 Tests

`tests/test_migration_053.py`:
- Cold-DB up + migrate → columns exist
- Re-running migration is idempotent (`IF NOT EXISTS`)
- ORM `Base.metadata.create_all` produces same schema (catches ORM drift)

---

## Phase 2 — Pydantic schemas

**Files:** `nous/heart/schemas.py` (`FactInput` lines 85-98; `FactDetail` + `FactSummary` lines 114-165)

### 2.1 Module-scope additions (above `class FactInput`)

```python
import re
from datetime import date, datetime
from pydantic import field_validator

_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
```

### 2.2 `FactInput` updates (verbatim from spec)

Add fields + validator:

```python
event_date: date | None = None
event_date_classified_at: datetime | None = None  # set by F075 producers only

@field_validator("event_date", mode="before")
@classmethod
def _parse_event_date(cls, v):
    if v is None or isinstance(v, date):
        return v
    if isinstance(v, str):
        if not _DATE_PATTERN.fullmatch(v):
            return None
        try:
            return date.fromisoformat(v)
        except ValueError:
            return None
    return None
```

### 2.3 `FactDetail` and `FactSummary` updates

Add `event_date: date | None = None` to BOTH classes. (Per spec wire-path row 7.)

### 2.4 Tests

`tests/test_temporal_schemas.py`:
- Valid `YYYY-MM-DD` accepted → `date` object
- `'20240310'` rejected (alternate ISO form)
- `'2024-W10-7'` rejected (ISO week date)
- `'2024-02-30'` rejected (impossible day)
- Empty string → None
- `None` passthrough
- `event_date_classified_at` accepts `datetime` and `None`

---

## Phase 3 — Layer 1a (EpisodeSummarizer)

**Files:** `nous/handlers/episode_summarizer.py` (lines 50-77, 135, 361, 377, 383, 394, 445-468)

### 3.1 Thread `started_at` (spec wire-path supplement: 5 hops)

| Site | Change |
|---|---|
| `summarize_episode` line 135 | `summary = await self._generate_summary(transcript, decision_context, started_at=episode.started_at)` |
| `_generate_summary` signature line 361 | Add `started_at: datetime \| None = None` kwarg |
| `_generate_summary` lines 377, 383 | Pass `started_at=started_at` to `_summarize_single` calls |
| `_summarize_single` signature line 394 | Add `started_at: datetime \| None = None` kwarg |

### 3.2 Prompt template (gated)

In `_summarize_single`'s prompt construction, gate on `settings.temporal_extraction_enabled`. When TRUE and `started_at is not None`, prepend an `EPISODE_START_TIMESTAMP: {iso}` block above the transcript AND append the date-anchored extraction instruction (spec §Layer 1a).

The candidate_facts schema in the prompt extends from `{subject, content, category}` to optional 4th field `event_date: "YYYY-MM-DD"`.

When the flag is OFF, the prompt is unchanged from current main.

### 3.3 `_merge_summaries` fix (lines 445-468)

Replace the existing `merged_candidate_facts[:5]` with separate caps:

```python
dated = [c for c in merged_candidate_facts if c.get("event_date") is not None]
stable = [c for c in merged_candidate_facts if c.get("event_date") is None]
merged_candidate_facts = (
    dated[:self._settings.candidate_facts_event_limit]
    + stable[:5]
)
```

The `:5` is the existing stable cap (matches current behavior). The `candidate_facts_event_limit` default is 30 (Phase 6 settings).

### 3.4 Tests

`tests/test_temporal_extractor.py::test_summarizer_*`:
- `test_summarizer_prompt_includes_date_block_when_flag_on`
- `test_summarizer_prompt_unchanged_when_flag_off`
- `test_summarizer_threads_started_at_through_all_hops` (mock LLM, capture the prompt; assert `EPISODE_START_TIMESTAMP` appears)
- `test_merge_summaries_preserves_dated_facts_beyond_5` (synthesize 8 chunks with 10 dated + 8 stable candidates; verify all 10 dated + 5 stable survive when `candidate_facts_event_limit=30`)
- `test_merge_summaries_caps_stable_at_5_dated_at_event_limit` (50 dated → 30 kept; 10 stable → 5 kept)

---

## Phase 4 — Layer 1b (FactExtractor)

**Files:** `nous/handlers/fact_extractor.py` (lines 176-179, 189-196, 249, 251-256, 263-269)

### 4.1 `_store_candidate_facts` (lines 249, 251-256)

Replace the `candidates[:5]` cap with the same split as Phase 3:

```python
# Codex round-12 P1: must mirror _merge_summaries cap or 6th+ dated facts
# from chunks are dropped at this gate too.
dated = [c for c in candidates if isinstance(c, dict) and c.get("event_date")]
stable = [c for c in candidates if not (isinstance(c, dict) and c.get("event_date"))]
candidates = (
    dated[:self._settings.candidate_facts_event_limit]
    + stable[:5]
)
```

In the loop body, read `event_date` from the dict:

```python
event_date = item.get("event_date") if isinstance(item, dict) else None
```

And pass to `FactInput(...)` construction along with conditional classification marker:

```python
classified_at = (
    datetime.now(UTC)
    if self._settings.temporal_extraction_enabled
    else None
)
fact_input = FactInput(
    subject=...,
    content=...,
    ...,
    event_date=event_date,
    event_date_classified_at=classified_at,
)
```

### 4.2 Pre-learn dedup bypass (lines 176-179, 263-269)

Both pre-learn dedup sites do `existing = await self._heart.search_facts(content, limit=1)` then skip if `existing[0].score > self._settings.fact_dedup_threshold`. After Phase 2, `FactSummary` always has `event_date` — drop the `hasattr` guard from v1's snippet (dead code):

```python
if existing and existing[0].score is not None and existing[0].score > self._settings.fact_dedup_threshold:
    # F075 dedup-bypass: distinct event_dates = distinct events.
    # FactSummary.event_date guaranteed present by Phase 2 — no hasattr needed.
    candidate_event_date = ...  # from item.get("event_date") for store_candidate; or extracted upstream for direct path
    existing_event_date = existing[0].event_date
    if not (candidate_event_date is not None
            and existing_event_date is not None
            and candidate_event_date != existing_event_date):
        # Original skip path
        stored_ids.append(existing[0].id)
        continue
    # Else: fall through to learn — both have dates and they differ
```

### 4.3 Direct extractor `FactInput` (lines 189-196)

The direct LLM extraction path constructs `FactInput(...)` separately. Add `event_date=fact.get("event_date")` and the same conditional `event_date_classified_at` kwarg.

### 4.4 Tests

`tests/test_temporal_extractor.py::test_fact_extractor_*`:
- `test_store_candidate_facts_reads_event_date_from_dict`
- `test_store_candidate_facts_sets_classified_at_when_flag_on`
- `test_store_candidate_facts_leaves_classified_at_null_when_flag_off`
- `test_store_candidate_facts_caps_dated_at_event_limit_and_stable_at_5`
- `test_pre_learn_dedup_bypasses_when_dates_differ`
- `test_pre_learn_dedup_engages_when_one_side_missing_date` (existing behavior preserved)
- `test_direct_extractor_path_threads_event_date` (Layer 1b)

---

## Phase 5 — Heart (`nous/heart/facts.py`)

**Files:** `nous/heart/facts.py` (lines 428-449, 1046, 1160, 1256, 1355, 1459, recall result conversion)

### 5.1 `FactManager._learn` — sink-only (lines 428-449)

In the `Fact(...)` ORM construction inside `_learn`, pass `event_date` and `event_date_classified_at` from `fact_input` directly:

```python
fact = Fact(
    ...,
    event_date=fact_input.event_date,
    event_date_classified_at=fact_input.event_date_classified_at,
)
```

NO conditional, NO `.now()` injection. Pure sink. The producer's value is persisted as-is.

### 5.2 `_learn` cosine dedup-bypass (`facts.py:376-381`)

The cosine dedup path is `_find_duplicate(...)` returning a `dupe`, then `return await self._confirm(dupe.id, session)` at `facts.py:381`. The `_confirm` short-circuit must be guarded by the event_date rule. Concretely, immediately before line 381:

```python
# F075: distinct event_dates → distinct events; skip the _confirm return.
if (
    fact_input.event_date is not None
    and dupe.event_date is not None
    and fact_input.event_date != dupe.event_date
):
    pass  # fall through to session.add(fact) at line 450
else:
    return await self._confirm(dupe.id, session)
```

This is the cosine path only. Subject-based supersession requires a separate guard — see Phase 5.2b.

### 5.2b `_supersede_by_subject` bypass (`facts.py:453-458`) — ADDED IN v2 PER ARCH P1 #2

After Phase 5.2's cosine bypass fires, execution falls through to `session.add(fact)` at `facts.py:450`, then hits `_supersede_by_subject` at lines 453-458. For canonical temporal pairs ("API key obtained March 10" + "API key obtained March 12"), subjects are identical and the function deactivates the older fact unless guarded. The date pair temporal_reasoning needs is destroyed.

Two equivalent fixes:

**Option A (preferred — guard at call site):** wrap the call:

```python
if check_contradictions and input.subject and embedding is not None:
    # F075: skip subject-based supersession for date-anchored events with
    # distinct dates. The new fact + any subject-matched existing fact
    # whose event_dates differ are distinct events, not supersession.
    await self._supersede_by_subject(
        fact.id, input.subject, embedding, session,
        new_content=input.content,
        new_event_date=input.event_date,  # NEW kwarg
    )
```

And inside `_supersede_by_subject`, skip any candidate where both candidate.event_date and new_event_date are non-NULL and differ.

**Option B:** keep call site untouched, push the guard inside `_supersede_by_subject`. Simpler at call sites but adds a kwarg-default-None at the function signature.

**Pick Option A** — explicit at the call site makes the F075 rule visible to anyone reading `_learn`. Function signature gets one new kwarg `new_event_date: date | None = None`.

### 5.3 `FactSummary(...)` constructors (lines 1046, 1160, 1256, 1355)

At each of the 4 sites, add `event_date=fact.event_date` to the constructor call. Verify by grep that the search returns exactly 4 occurrences post-edit.

### 5.4 `_to_detail` (line 1459)

Add `event_date=fact.event_date` to the `FactDetail(...)` construction.

### 5.5 `_to_recall_result` — FIXED IN v2 (was Phase 5.5 in v1)

**Spec drift caught by Code P1 #2:** v1 of the plan and spec wire-path row 10 both say `nous/heart/facts.py`. The actual location is `nous/heart/heart.py:1085` (method on `Heart` class, not `FactManager`). FactSummary branch at `heart.py:1099-1110`. There is no `_to_recall_result` in `facts.py` at any line. The drift survived 17 codex rounds.

Edit `nous/heart/heart.py:1099-1110` (the FactSummary branch). Add to the metadata dict construction:

```python
"event_date": item.event_date.isoformat() if item.event_date else None,
```

`item` is a `FactSummary` here (per the surrounding code), and `FactSummary.event_date` is guaranteed present by Phase 2.

### 5.6 Tests

`tests/test_f075_heart.py`:
- `test_learn_persists_event_date_and_classified_at_unchanged` (sink semantics; non-F075 caller leaves classified_at NULL)
- `test_learn_cosine_dedup_bypasses_when_dates_differ` (Phase 5.2 cosine path)
- `test_learn_cosine_dedup_engages_when_dates_match` (regression — same event same date still dedupes)
- **`test_supersession_skipped_when_both_facts_have_different_dates`** (NEW per Arch P1 #2 — Phase 5.2b: ingest fact A subject="API key event" event_date=2024-03-10, then fact B same subject event_date=2024-03-12 with high embedding similarity; assert BOTH rows `active=True`, NO `supersedes` edge written)
- `test_recall_surfaces_event_date_in_metadata` (full path: learn → recall → metadata["event_date"] is ISO string)
- `test_factsummary_carries_event_date_at_all_construction_sites` (parametrized over the 4 sites)
- `test_non_f075_callers_leave_classified_at_null` (regression: simulating `tools.py`-style `Heart.learn(FactInput(...))` produces a row with `event_date_classified_at IS NULL`, eligible for backfill)

---

## Phase 6 — Retrieval pipeline + settings

**Files:** `nous/api/retrieval_pipeline.py` (lines 659-669); `nous/config.py`

### 6.1 `_heart_results_to_pipeline` metadata copy (lines 659-669)

Currently `PipelineResult` is built with empty metadata. Add:

```python
metadata = {}
if "event_date" in r.metadata:
    metadata["event_date"] = r.metadata["event_date"]
results.append(PipelineResult(..., metadata=metadata, ...))
```

### 6.2 Settings additions (spec §Settings additions)

In `nous/config.py`:

```python
# F075
temporal_extraction_enabled: bool = Field(default=False, description="...")
candidate_facts_event_limit: int = Field(default=30, ge=0, description="...")
temporal_backfill_default_token_budget: int = Field(default=50000, description="...")
# Layer 3 deferred
date_aware_boost_enabled: bool = Field(default=False, description="...")
date_aware_boost_factor: float = Field(default=1.20, ge=1.0, le=2.0, description="...")
date_aware_boost_window_pad_days: int = Field(default=30, description="...")
```

Match descriptions from spec §Settings additions verbatim.

### 6.3 Tests

`tests/test_temporal_extractor.py::test_metadata_surfacing`:
- Recall returns `RecallResult` with `event_date` in metadata → `PipelineResult.metadata["event_date"]` populated
- Recall returns `RecallResult` without `event_date` → `PipelineResult.metadata` excludes the key (no `None` placeholder)

---

## Phase 7 — Layer 2 (happened_before edges)

**Files:** `nous/brain/graph_densifier.py`

### 7.1 New method `_build_happened_before_edges()` — FIXED IN v2

Add to `GraphDensifier` class. Three corrections vs v1:

1. **Code P1 #1**: `GraphDensifier` exposes `self.db` (no underscore), not `self._db`. Verify by reading `nous/brain/graph_densifier.py:115`.
2. **Code P1 #3**: `GraphEdge.__table_args__` has a `ck_edges_extraction_method` CHECK constraint requiring `extraction_method IN ('deterministic', 'heuristic', 'inferred')`. INSERT must populate this column or the constraint fires.
3. **Code P2**: prefer `result.rowcount` over `result.fetchall()` for INSERT … RETURNING — same information, lower memory.

```python
async def _build_happened_before_edges(self) -> int:
    """F075 Layer 2: chain temporal events within episode boundaries."""
    async with self.db.session() as session:   # NOTE: self.db, not self._db
        result = await session.execute(
            text("""
                INSERT INTO brain.graph_edges
                    (source_id, source_type, target_id, target_type,
                     agent_id, relation, weight, auto_linked,
                     extraction_method)        -- F065 NOT-NULL CHECK
                SELECT a.id, 'fact', b.id, 'fact',
                       a.agent_id, 'happened_before', 1.0, TRUE,
                       'deterministic'         -- chain is structurally derived
                FROM heart.facts a
                JOIN LATERAL (
                    SELECT b.id
                    FROM heart.facts b
                    WHERE b.agent_id = a.agent_id
                      AND b.source_episode_id = a.source_episode_id
                      AND b.event_date IS NOT NULL
                      AND b.event_date > a.event_date
                      AND b.active = TRUE
                    ORDER BY b.event_date ASC, b.id ASC
                    LIMIT 1
                ) b ON TRUE
                WHERE a.agent_id = :agent_id
                  AND a.event_date IS NOT NULL
                  AND a.active = TRUE
                ON CONFLICT (source_id, target_id, relation) DO NOTHING
            """),
            {"agent_id": self._agent_id},
        )
        await session.commit()
        return result.rowcount or 0
```

### 7.2 Wire into `run_backfill_cycle`

Call `_build_happened_before_edges()` as part of the backfill cycle. Update the return dict to include the count.

### 7.3 Flip `NOUS_GRAPH_ADJACENCY_BOOST_ENABLED=true` — ADDED IN v2 PER ARCH P1 #1

**Without this flip, every edge built by 7.1 is dead weight.** The consumer `_apply_graph_adjacency_boost` (`retrieval_pipeline.py:243-247`) is already-shipped code but gated by `NOUS_GRAPH_ADJACENCY_BOOST_ENABLED` which defaults to `False`. Spec line 443 mandates the flip as part of the impl plan.

Actions:
- Add the env var to the deployment `.env.example` or operator runbook
- Add a row to CLAUDE.md env-var table (covered in Phase 10)
- Phase 9 integration test verifies the boost actually changes ranking when on vs off (see Phase 9 update)

The flip itself is a deploy-time config change, NOT a code change in this PR. The plan's responsibility is to document the requirement; the operator's responsibility is to flip it.

### 7.4 Tests

`tests/test_f075_edges.py`:
- 2 facts same episode, different dates → 1 edge
- 3 facts chronological → 2 edges (chain, not pairs)
- Cross-episode facts → no edge
- NULL event_date on either side → no edge
- Inactive fact never a source or target
- Multiple facts same date → not linked to each other; one outgoing edge per source
- Re-running backfill is idempotent (ON CONFLICT DO NOTHING)

---

## Phase 8 — Layer 4 (backfill script)

**Files:** `scripts/backfill_temporal_facts.py` (new, ~250 LOC); `nous/handlers/temporal_backfill.py` (new helper module, ~150 LOC)

The backfill script is the riskiest piece per the spec's iteration history. Copy the pseudo-code from spec §Layer 4 with extreme care — every line was earned by a codex round. Key points:

- Two-connection pattern (`engine.connect()` for lock + `session_factory()` for batches)
- Session-scoped `pg_try_advisory_lock` with `_LOCK_NAMESPACE = "f075-temporal"` prefix in the SHA-256 key
- `BudgetTracker` class with call-count semantics (`consume()` no-arg)
- Per-row chunk fetch via `_fetch_chunk_context` with `embedding_str` serialization + null-guards
- Per-batch commit (not per-row); commit-before-early-return on budget exhaustion
- `_classify_event_date(row, chunk_context=...)` via `call_background_llm_structured` from `nous/handlers/__init__.py:86`
- CLI: `--agent-id`, `--batch-size`, `--token-budget`, `--dry-run`

### 8.1 Module split

- `nous/handlers/temporal_backfill.py` — `BudgetTracker`, `_advisory_lock_key`, `_fetch_chunk_context`, `_classify_event_date`, `_process_batch`, `_run_with_lock`. These are all reusable / testable.
- `scripts/backfill_temporal_facts.py` — thin CLI wrapper (argparse, settings load, calls into the handler module).

This separation makes unit testing the script's internals tractable without `python scripts/...` subprocess complexity.

### 8.2 Tests

`tests/test_f075_backfill.py`:
- `BudgetTracker.consume()` decrements by 1; `ok()` correctly transitions to False
- `_advisory_lock_key` differs from F047's (cross-feature isolation)
- `_advisory_lock_key` stable across processes for same agent_id
- Concurrent invocations: second one sees lock held, exits cleanly
- `_fetch_chunk_context` returns None on null fact.embedding / null source_episode_id
- `_fetch_chunk_context` filters chunk-side NULL embedding (mock chunks)
- `_fetch_chunk_context` serializes pgvector text literal correctly
- `_process_batch` calls `_fetch_chunk_context` BEFORE classifier (not after)
- `_process_batch` commits on budget-exhausted early return
- `_process_batch` returns `(updated, stop_requested)` correctly
- Dry-run mode produces estimate without LLM calls
- `event_date_classified_at IS NULL` predicate filters NEVER-tried rows only (rows with NULL event_date but classified_at IS NOT NULL are NOT picked up)
- **`test_lock_survives_multi_batch_commits`** (NEW per Devil's Critical): force the script to run ≥3 batches against a fixture with enough NULL-classified rows; after each batch's commit, inspect `pg_locks` and assert the F075 advisory lock is still held on the same backend PID. Regression test for the two-connection pattern — if a future maintainer reverts to single-session, this test fails because per-batch commits release the connection.

### 8.3 Smoke

After unit tests pass, run a real smoke against `nous-eval-scratch` with synthetic facts inserted (10 NULL-classified-at facts, run script with `--token-budget 10000`, verify all classified, no leaked locks via `pg_locks` check).

---

## Phase 9 — Integration test

`tests/test_f075_end_to_end.py`:

End-to-end fixture flow with `temporal_extraction_enabled=True`:

1. Ingest a synthetic conversation with 3 explicit dates ("I started on March 1", "Then on March 5", "Finished March 10")
2. Episode summarizer extracts → fact extractor stores → 3 facts with event_date populated
3. Sleep cycle runs GraphDensifier → 2 `happened_before` edges
4. Recall the query "when did I start and finish?" → top results include both dated facts; `RecallResult.metadata["event_date"]` populated on both
5. Assert: 3 dated facts in heart.facts; `event_date_classified_at` set on all; 2 happened_before edges in brain.graph_edges; recall surfaces both via metadata

**Boost-on/off ranking assertion** (added in v2 per Arch P3 #2): with `NOUS_GRAPH_ADJACENCY_BOOST_ENABLED=True`, inject a 4th unrelated fact (no temporal context, no `happened_before` neighbor) at similar base score to the chain. Assert that the later-dated chain fact ranks above the unrelated fact. With `NOUS_GRAPH_ADJACENCY_BOOST_ENABLED=False`, repeat — assert the ranking does NOT favor the chain fact. This closes the full consumer path from edge write to ranking change and verifies the Phase 7.3 flip actually matters.

Same fixture re-ingested with `temporal_extraction_enabled=False`: 3 facts stored, ZERO have event_date (legacy prompt), ZERO have classified_at, ZERO edges built. Confirms flag-gating is end-to-end.

**Residual acceptance-criterion #5 risk:** Phase 9 uses a synthetic 3-message fixture. Acceptance #5 requires BEAM-100K temporal_reasoning ≥ 0.55 — a measurement on a 100K-token haystack. Phase 9 passing says nothing about acceptance #5. The BEAM re-measurement is a follow-up step after impl PR merges (per spec acceptance #5). Document this explicitly in the PR body so it's not mistaken for a green light.

---

## Phase 10 — CLAUDE.md + INDEX.md

### 10.1 `CLAUDE.md` env-var table

Add 6 new rows for the F075 settings (per Phase 6.2). Match the existing table format (variable | default | description).

**Plus** (per Arch P1 #1): add an explicit note on `NOUS_GRAPH_ADJACENCY_BOOST_ENABLED`. The variable already exists in CLAUDE.md (default `false`), but add a comment/footnote that **F075 Layer 2 requires this flag be `true` to take effect**. Without it, `happened_before` edges are written but never consulted by retrieval ranking.

### 10.2 `docs/features/INDEX.md`

Add F075 row marking it as Phase-1 shipped (impl PR). Status: 🚀 Shipped (impl PR #TBD).

### 10.3 `docs/features/F075-temporal-fact-extraction.md`

Update status line from "📝 Draft v2.17" to "🚀 Shipped v2.17". Add an "Impl PR" line.

---

## Phase 11 — Code review

Dispatch 3-agent code review on the diff:
- `feature-dev:code-architect` — verify spec-to-impl fidelity per wire path; flag any deviations
- `pr-review-toolkit:code-reviewer` — Python idioms, async patterns, error handling, test coverage
- `pr-review-toolkit:silent-failure-hunter` — find hidden no-ops, swallowed exceptions, dropped values

Save reviews to `docs/reviews/F075-impl-{arch,code,sfh}-review.md`. Address all P1s before PR.

**Iteration budget (Devil's review):** the spec required 17 codex rounds even after the same 3-agent review. F075 touches many integration surfaces (schemas, ORM, summarizer, fact extractor, Heart, retrieval pipeline, GraphDensifier, backfill script + tests). Budget **3-5 review cycles** for the impl, not 1. Each cycle: fix → re-grep changed files for the lesson → re-trigger reviews. Halt iteration when a round returns zero P1/P2 findings.

---

## Phase 12 — PR submission

**Explicit pathspec** (Devil's High): the working tree has ~40 untracked files at session start (identity snapshots, scratch scripts, log dirs, recovered files, eval reports). `git add -A` would pull non-F075 garbage into the PR. List each F075 file explicitly:

```bash
git checkout -b feat/F075-temporal-fact-extraction
git add \
    sql/migrations/053_temporal_fact_extraction.sql \
    nous/storage/models.py \
    nous/heart/schemas.py \
    nous/heart/facts.py \
    nous/heart/heart.py \
    nous/handlers/episode_summarizer.py \
    nous/handlers/fact_extractor.py \
    nous/handlers/temporal_backfill.py \
    nous/api/retrieval_pipeline.py \
    nous/brain/graph_densifier.py \
    nous/config.py \
    scripts/backfill_temporal_facts.py \
    tests/test_f075_*.py \
    tests/test_migration_053.py \
    CLAUDE.md \
    docs/features/F075-temporal-fact-extraction.md \
    docs/features/INDEX.md \
    docs/reviews/F075-plan-*.md \
    docs/reviews/F075-impl-*.md \
    docs/superpowers/plans/2026-05-28-f075-temporal-fact-extraction.md
git status  # MANUAL VERIFICATION before commit — no extraneous files staged
git diff --cached --stat  # confirm diff size matches expectations
git commit -m "feat(F075): temporal fact extraction + date-aware retrieval"
git push -u origin feat/F075-temporal-fact-extraction
gh pr create --title "feat(F075): temporal fact extraction + date-aware retrieval" --body "..."
```

PR body sections:
- Summary
- Reference to spec PR #460 (commit `0115568`) + this plan + 3 plan reviews + N impl reviews
- Acceptance criteria table — explicitly mark #5 (BEAM ≥ 0.55) as **deferred** to a follow-up measurement PR, with link to where the BEAM re-run will happen
- Operator action required after merge: flip `NOUS_GRAPH_ADJACENCY_BOOST_ENABLED=true` for Layer 2 to take effect (Phase 7.3)
- Backfill action required after merge: run `scripts/backfill_temporal_facts.py --agent-id <agent> --dry-run` first, then real run for each existing agent.

---

## Open questions to resolve during impl

1. **Phase 0 dates for conv 4 Q1 and conv 5 Q1** — need to read BEAM source chats to find the canonical date strings before writing the synthetic facts. If the source doesn't explicitly date the events, the PATTERN_MATCH/PARTIAL_MATCH classification may be wrong and the gate fails (good — we learn this before paying for impl).

2. **F074 BEAM harness still un-PR'd on main** — the F075 impl plan does NOT include BEAM measurement (per spec acceptance #5 it ships in a follow-up after this impl PR lands). Phase 9's integration test uses a synthetic fixture, not BEAM. The BEAM re-measurement happens after impl PR merges.

3. ~~`call_background_llm_structured` exact import path~~ — RESOLVED: confirmed at `nous/handlers/__init__.py:86`. No impl-time investigation needed.

4. **Layer 3 (date-aware boost) deferred** — confirmed not in this impl PR. Setting fields added (default off) so the plumbing exists for a future F075.x. Tests for boost shipped with F075.x, NOT here.

---

## Acceptance criteria recalibration (v3)

Spec v2.17 §Acceptance criteria #5 originally required:

> **BEAM Phase 1 re-run (n=5 conv) shows temporal_reasoning ≥ 0.55**, ideally ≥ 0.60.

Recalibrated for v3 based on the Phase 0 source-read finding (only 1 of 5 BEAM failures is genuinely addressable by F075's `<entity> <action> on <date>` extraction mechanism):

> **BEAM Phase 1 re-run (n=5 conv) shows temporal_reasoning ≥ 0.45**, with an explicit understanding that the realistic ceiling for F075 ALONE is ~0.50-0.55. The 0.65-0.70 estimate in the original spec was based on misclassified failure modes — conv 4 Q1 (problem-count arithmetic) and conv 5 Q1 (undated second event) need a different mechanism than F075. Other ability scores stay within ±0.05 of prod-v3 (no collateral regression like K-bump's abstention -0.167).

Spec amendment plan: bump spec to v2.18 in this impl PR with a new changelog entry documenting the Phase 0 source-read finding and the acceptance recalibration. This is a documentation-only change to spec content; the design itself is unchanged. F075 is still worth shipping — better fact extraction for date-anchored events is real product value beyond BEAM (user confirmed).

## Acceptance gate to enter Phase 11 (review)

Before requesting impl review, the implementer asserts:
- [ ] All phase gates 1-10 passed
- [ ] `uv run pytest tests/test_temporal_*.py tests/test_migration_053.py tests/test_f075_end_to_end.py -v` exits 0
- [ ] Fresh DB cold start + migrate succeeds
- [ ] Existing test suite unaffected (`uv run pytest tests/ -v` exits 0, modulo pre-existing flakes)
- [ ] CLAUDE.md updated
- [ ] Phase 0 gate passed (cheap synthetic verification)
