# Memory Storage + Retrieval Fixes — A/B-Gated Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the memory storage/retrieval gaps found in the 2026-07-01 audit, with **every change gated on measured proof of improvement** — no blind changes.

**Architecture:** Two fix classes, two evidence standards (user decision 2026-07-01):
- **Ranking fixes** (R2 score-space, R6 soft-delete leak, coherent_ranking relaxation) → gated on a **retrieval A/B** (MRR / nDCG / R@K on a purpose-built graph-binding qrel set + BEAM/LME guardrail) via the existing `nous_eval` harness.
- **Integrity fixes** (S1 NULL-embed fail-open, S4 unguarded classifier, S5/S6/S7 supersession/atomic-merge) → gated on a **fault-injection before/after probe** (dup-rate under injected embed outage, fact-drop-rate under classifier outage, lineage-completeness count, orphan-after-crash count). These CANNOT move retrieval MRR on a clean corpus, so retrieval A/B is the wrong instrument for them.

**Sequencing (user decision):** **R2 first, prove end-to-end, then expand.** Phase 0 builds the measurement instrument; Phase 1 lands + measures R2 behind a hard gate; Phases 2–3 proceed **only if Phase 1's gate passes** (or are re-scoped if R2 reads null).

**Tech Stack:** Python 3.12+, async SQLAlchemy/asyncpg, pydantic-settings, pytest + pytest-asyncio, `nous_eval` harness (F051), eval DB at `127.0.0.1:5433` (`nous_eval_prod`, agent `nous-default`, prod-shape: ~2899 facts, `superseded_by` preserved, 1536-dim embeds).

## Global Constraints

- **Every new Settings flag lands default-OFF (land-dark).** Flip only after its A/B gate passes. Matches the F050/F067 pattern.
- **Every new boolean Settings field must be pinned `False` in bare-MagicMock test fixtures** (`tests/test_streaming.py::_make_mock_settings` and siblings) — a bare MagicMock returns a truthy child and defeats default-off semantics. (memory: `feedback_f071_shipped`)
- **A/B discipline (memory `feedback_apples_to_apples_eval`, `feedback_eval_prod_generator`):** every A/B changes exactly ONE variable; pin corpus, sleep state, models, scoring, K. Retrieval-quality QA A/Bs use the **prod generator model** (`--gen-model claude-opus-4-8`), never a Sonnet/Haiku proxy — sign flips by generator. No retrieval-quality delta believed below **n ≈ 100** gold-present, snapshot-matched qrels.
- **Eval DB is authoritative for retrieval A/B; NEVER run A/B writes against live prod `192.168.1.141:5432`.** Use `:5433`. (memory `reference_eval_db_topology`)
- **Migrations:** never put `;` inside `-- ...` line comments in `sql/migrations/*.sql`; a fresh-DB `docker compose up` is the real acceptance test. (memory `feedback_migration_semicolons`)
- **Fail-open contract for the learn path:** a background-LLM outage must never drop a fact. Integrity fixes must preserve or strengthen this.
- All new tables/rows are agent-scoped (`agent_id`).

---

## Baseline facts locked at audit time (do not re-derive)

- **R2 site + CORRECTED scope (per reviewed decision F080, `a18e0836`):** `nous/api/retrieval_pipeline.py:282-283` sorts the merged candidate list by `r.score` when `rerank_by_score=True`. **The pool is NOT globally incoherent** — F080 verified that facts/episodes/decisions/procedures already emit normalized RRF `[0,1]` via `_rrf_merge_n` (`heart.py`), so those legs cohere. The un-normalized **deviants** are only four: (a) procedure utility boost >1.0, (b) censor cosine floor ≥0.7, (c) **chunk cosine**, (d) **graph `edge_weight × decay`** (~0.5–0.7). F080 already neutralized (a)+(b) **by exclusion** (`coherent_ranking` drops procedures+censors from the `["all"]` pool — a deliberate, reviewed choice, NOT a band-aid to remove). **F080 explicitly scoped (c)+(d) out to a follow-up "F080.N behind an F051 measurement gate" — that follow-up is exactly this R2 work.** So R2 = surgically map **only chunk + graph scores** onto the same RRF normalizer the coherent legs use; do NOT blanket-renormalize (an advisor rejected blanket rank-norm as "magnitude inflation + graph mis-ranking"), and do NOT disturb the already-coherent facts/episodes/decisions ordering.
- **Prod config (`.env.prod-snapshot`, verified 2026-07-01):** `episode_chunks_enabled=true` → `rerank_by_score=True` in prod, so **R2 is LIVE in prod** (graph IS score-reranked, mixing scales). `coherent_ranking_enabled` defaults True (unset in prod) → censors + procedures are dropped from `["all"]` recall as a band-aid for exactly this scale problem (`retrieval_pipeline.py:372-374`). CE, MMR, spreading are OFF in prod.
- **Measurement infra already exists:** `nous_eval/generate_graph_qrels.py` (double-pass miner: graph-off must miss top-K, graph-on must hit), `QrelSource.GRAPH_TARGETED` (`qrels_loader.py:44`), `retrieval_runner.run_matrix`, `report.decide_gate_f050`. Known limitation: the miner targets `target_type='decision'` bridges only (its comment: "graph-expansion paths ONLY surface DECISION neighbors"), but **prod runs Path A (`heart_graph_all_types_enabled=true`)** which also expands fact/episode/chunk/procedure neighbors — so the miner under-covers the prod graph surface and must be extended for a prod-faithful R2 measurement.
- **Memory caveat:** "graph_targeted eval source unconstructable on current prod" (F044 work) — the miner may yield ~0 rows on the current `:5433` corpus. Phase 0 Task 1 is a smoke-yield check precisely to catch this before investing.

---

# PHASE 0 — Measurement instrument (gates everything)

**Deliverable:** a validated graph-binding qrel set on `:5433` where the gold is only reachable via a reranked graph/Path-A hit, plus a reproducible A/B command that reads R2's effect. If this phase cannot produce ≥ ~40 valid qrels, STOP and reassess with the user — a null R2 result would be uninterpretable without it.

### Task 0.1: Smoke the existing miner on current prod-shape corpus

**Files:**
- Run only: `nous_eval/generate_graph_qrels.py` (no edit yet)

- [ ] **Step 1: Confirm eval DB reachable + has edges**

Run:
```bash
python -m nous_eval.retrieval --smoke   # confirms :5433 reachable
psql "postgresql://nous:nous_eval@127.0.0.1:5433/nous_eval_prod" -c \
  "SELECT extraction_method, target_type, count(*) FROM brain.graph_edges \
   WHERE weight>=0.7 GROUP BY 1,2 ORDER BY 3 DESC LIMIT 20;"
```
Expected: a non-empty edge distribution. Record how many `target_type='decision'` weight≥0.7 edges exist — this bounds the decision-only miner's yield.

- [ ] **Step 2: Run the miner at small sample, decision-only (baseline behavior)**

Run:
```bash
python -m nous_eval.generate_graph_qrels --sample-size 60 --target-size 20 \
  --allow-inferred \
  --out C:/Users/User/AppData/Local/Temp/claude/.../scratchpad/graph_qrels_smoke.jsonl
```
Expected: prints "Wrote N qrels". **Decision gate:** if N ≥ 15, the corpus supports decision-bridge qrels — proceed to 0.2 to extend coverage. If N < 5, the memory caveat holds; **STOP and report to user** (options: use a different corpus snapshot, or broaden bridges in 0.2 before concluding). Do not silently proceed on a dead corpus.

### Task 0.2: Extend the miner to Path-A (all-type) neighbor bridges

**Why:** prod expands non-decision neighbors (Path A); a decision-only qrel set measures a narrower graph surface than prod runs, so an R2 fix could help prod while reading null on decision-only qrels.

**Files:**
- Modify: `nous_eval/generate_graph_qrels.py:170-212` (the `target_type = 'decision'` filter) and `:299-340` (`_validate_query`)
- Test: `tests/nous_eval/test_generate_graph_qrels.py`

**Interfaces:**
- Produces: `fetch_edge_candidates(..., target_types: list[str] = ["decision"])` — new kwarg; when it includes non-decision types, validation must run with `heart_graph_all_types_enabled=True` so the Path-A stage can reach the gold.

- [ ] **Step 1: Write the failing test**

```python
# tests/nous_eval/test_generate_graph_qrels.py
import pytest
from nous_eval.generate_graph_qrels import fetch_edge_candidates

@pytest.mark.asyncio
async def test_fetch_candidates_honors_target_types(eval_db, seeded_all_type_edges):
    # seeded_all_type_edges inserts a fact->fact evidence edge weight 0.8
    cands = await fetch_edge_candidates(
        eval_db, agent_id="nous-default", sample_size=50,
        target_types=["fact", "episode", "decision"],
    )
    assert any(c.target_type == "fact" for c in cands), \
        "non-decision bridges must be selectable when requested"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/nous_eval/test_generate_graph_qrels.py::test_fetch_candidates_honors_target_types -v`
Expected: FAIL — `fetch_edge_candidates` has no `target_types` kwarg (TypeError).

- [ ] **Step 3: Implement — parametrize the target_type filter**

In `fetch_edge_candidates`, replace the hardcoded `AND e.target_type = 'decision'` with a bound `IN` list from the new `target_types` kwarg (default `["decision"]` to preserve current behavior), and in `_validate_query` set `settings_on = settings.model_copy(update={"graph_recall_enabled": True, "heart_graph_all_types_enabled": True})` so Path-A neighbors are reachable in the on-pass. Keep `settings_off` with both False.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/nous_eval/test_generate_graph_qrels.py -v`
Expected: PASS.

- [ ] **Step 5: Regenerate the extended qrel set**

Run:
```bash
python -m nous_eval.generate_graph_qrels --sample-size 200 --target-size 120 \
  --allow-inferred \
  --out E:/Projects/nous-eval-fixtures/v2026-Q2/qrels_graph_targeted.jsonl
```
Expected: "Wrote N qrels" with **N ≥ 40** (target 80–120). Record N and the per-`bridge_source_type` breakdown. If N < 40 after broadening, STOP and report.

- [ ] **Step 6: Register the source + commit**

Confirm `nous_eval/config/sources.yaml` has a `graph_targeted` entry (gate-eligible, `review_filter` off since rows are `reviewed_by="auto"`). Commit the miner change + regenerated qrels.
```bash
git add nous_eval/generate_graph_qrels.py tests/nous_eval/test_generate_graph_qrels.py nous_eval/config/sources.yaml
git commit -m "feat(eval): extend graph-qrel miner to Path-A all-type bridges for R2 measurement"
```

### Task 0.3: Establish the R2 A/B harness command + baseline

**Files:**
- Verify/Modify: `nous_eval/retrieval_runner.py` — confirm it threads `rerank_by_score` into `run_recall_pipeline` so A/B configs run **prod-faithful** (rerank ON). If it does not, add a `RuntimeConfig`/matrix flag so the A/B can pin `rerank_by_score=True`.
- Test: `tests/nous_eval/test_retrieval_runner.py`

- [ ] **Step 1: Verify rerank is threaded (read + assert)**

Grep `retrieval_runner.py` for `rerank_by_score`. If absent, the harness runs R2 dark (default False) and the whole A/B is invalid. Add a test asserting the runner passes `rerank_by_score=True` when the matrix config requests it; implement the threading minimally if missing.

- [ ] **Step 2: Capture the baseline (control) run**

Run (prod generator, rerank ON, current `main` = no normalization):
```bash
python -m nous_eval.retrieval --sources graph_targeted,probes,nous_prod \
  --top-k 10 --rerank-by-score --report-dir reports/
```
Expected: writes `reports/<ts>_baseline.{md,json}`. Record aggregate MRR + per-source MRR/nDCG/R@10 on `graph_targeted`. **This is the control arm A.** Persist the report path in the plan-execution notes.

---

# PHASE 1 — R2: renormalize the two deviant score-spaces (graph + chunk) — the F080.N follow-up

**Deliverable:** a flag-gated step that maps **only chunk-cosine and graph `edge_weight×decay` scores** onto the RRF `[0,1]` scale the coherent legs already use, applied before the rerank sort, proven to lift `graph_targeted` MRR/nDCG with no BEAM/LME regression and **byte-identical output when OFF**. **HARD GATE at the end.**

> **Design constraint from F080 (`a18e0836`), do not violate:** facts/episodes/decisions/procedures already share the RRF `1/(K+rank)` normalizer and are comparable. **Only chunk + graph are deviant.** Renormalize those two legs to the SAME normalizer; leave the coherent legs' relative order untouched. Blanket per-type min-max / global rank-norm was advisor-rejected (magnitude inflation, graph mis-ranking) — it is included below ONLY as a documented losing arm, never the default.

### Task 1.1: Add the deviant-leg renormalizer (pure function, unit-tested)

**Files:**
- Create: `nous/api/score_normalization.py`
- Test: `tests/api/test_score_normalization.py`
- Modify: `nous/config.py` (new setting)

**Interfaces:**
- Produces: `renormalize_deviant_legs(results: list[PipelineResult], mode: str) -> list[PipelineResult]` — mutates/returns the list with `.score` rewritten **only for `type in {"chunk"}` and graph-origin items**, mapping them onto RRF `1/(K+rank)` computed within their own leg (K=`NOUS_RRF_K`, default 60 — the SAME family the coherent legs use). Coherent-leg scores (fact/episode/decision/procedure that arrived via `_rrf_merge_n`) are left unchanged. `mode="off"` → identity. `mode="rrf_deviant"` → the fix above. `mode="rrf_all"` → the losing-arm blanket re-rank (kept only so the A/B can demonstrate it regresses, per F080's advisor note).
- Consumes: `PipelineResult.type`; a graph-origin marker. **Step 0 below establishes how a graph-expanded item is identified** — this is a prerequisite, not an assumption.

- [ ] **Step 0: Determine how graph-origin items are marked (READ FIRST, no code)**

Read `nous/api/retrieval_pipeline.py` where graph-expanded items are appended to `results` (the `acc.graph_expanded` merge) and inspect `PipelineResult` fields. Confirm whether a graph item is distinguishable (e.g. a `provenance`/`via_graph` field, or membership in `acc.graph_expanded`). If there is NO marker, add Task 1.1a: thread a boolean `via_graph` onto `PipelineResult` at the graph-append site (small, mechanical) — the renormalizer needs it. Record the exact field name here before writing the function.

- [ ] **Step 1: Write failing tests for the renormalizer math**

```python
# tests/api/test_score_normalization.py
from nous.api.score_normalization import renormalize_deviant_legs
from nous.api.retrieval_pipeline import PipelineResult

def _r(id, type, score, via_graph=False):
    return PipelineResult(id=id, type=type, score=score, summary="", via_graph=via_graph)

def test_off_is_identity():
    rs = [_r("a","fact",0.9), _r("c","chunk",0.72)]
    out = renormalize_deviant_legs(rs, "off")
    assert [r.score for r in out] == [0.9, 0.72]

def test_coherent_legs_untouched():
    # facts already RRF-normalized: their scores must NOT change under rrf_deviant
    rs = [_r("f1","fact",0.30), _r("f2","fact",0.20)]
    out = {r.id: r.score for r in renormalize_deviant_legs(rs, "rrf_deviant")}
    assert out["f1"] == 0.30 and out["f2"] == 0.20

def test_chunk_leg_mapped_to_rrf_scale():
    # two chunks at raw cosine 0.72/0.55 -> RRF 1/61, 1/62 (rank-based, magnitude discarded)
    rs = [_r("c1","chunk",0.72), _r("c2","chunk",0.55)]
    out = {r.id: r.score for r in renormalize_deviant_legs(rs, "rrf_deviant")}
    assert out["c1"] == 1/61 and out["c2"] == 1/62

def test_graph_item_mapped_by_flag_not_type():
    # a fact that arrived via graph expansion is deviant and must be renormalized
    rs = [_r("g1","fact",0.65, via_graph=True)]
    out = {r.id: r.score for r in renormalize_deviant_legs(rs, "rrf_deviant")}
    assert out["g1"] == 1/61   # rank-1 in the graph leg
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/api/test_score_normalization.py -v`
Expected: FAIL — module does not exist (ImportError).

- [ ] **Step 3: Implement `renormalize_deviant_legs`**

```python
# nous/api/score_normalization.py
"""Renormalize the two deviant recall score-spaces onto the RRF scale (audit R2 / F080.N).

Per reviewed decision F080 (a18e0836): fact/episode/decision/procedure already
share the RRF 1/(K+rank) normalizer and are comparable. Chunk cosine and
graph edge_weight*decay are the ONLY deviants. This maps just those two legs
onto the same RRF scale before the rerank sort — the coherent legs are left
byte-identical. Blanket re-normalization is a documented losing arm only.
"""
from __future__ import annotations
from collections import defaultdict

def _rrf(group, k):
    ordered = sorted(group, key=lambda r: r.score or 0.0, reverse=True)
    for rank, r in enumerate(ordered, 1):
        r.score = 1.0 / (k + rank)

def renormalize_deviant_legs(results, mode, k=60):
    if mode == "off" or not results:
        return results
    if mode == "rrf_all":  # losing arm — renormalize EVERY type by rank (advisor-rejected)
        by_type = defaultdict(list)
        for r in results:
            by_type[r.type].append(r)
        for group in by_type.values():
            _rrf(group, k)
        return results
    if mode == "rrf_deviant":
        chunk_leg = [r for r in results if r.type == "chunk"]
        graph_leg = [r for r in results if getattr(r, "via_graph", False)]
        if chunk_leg:
            _rrf(chunk_leg, k)
        if graph_leg:
            _rrf(graph_leg, k)
        return results
    raise ValueError(f"unknown normalization mode: {mode!r}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/api/test_score_normalization.py -v`
Expected: PASS (all 4).

- [ ] **Step 5: Add the setting (default OFF)**

In `nous/config.py`, add:
```python
score_normalization_mode: Literal["off", "rrf_deviant", "rrf_all"] = Field(
    default="off",
    description="Audit R2 / F080.N: renormalize the two deviant recall "
    "score-spaces (chunk cosine, graph edge-weight) onto the RRF scale "
    "before the rerank_by_score sort. off=today. rrf_deviant=the fix. "
    "rrf_all=losing-arm control (advisor-rejected blanket re-rank). "
    "Land-dark; flip after the graph_targeted A/B gate passes.",
)
```
Pin `score_normalization_mode="off"` in the bare-MagicMock fixtures (Global Constraints).

- [ ] **Step 6: Commit**

```bash
git add nous/api/score_normalization.py tests/api/test_score_normalization.py nous/config.py tests/test_streaming.py
git commit -m "feat(retrieval): renormalize deviant chunk+graph score-legs onto RRF scale (R2/F080.N), land-dark"
```

### Task 1.2: Wire the renormalizer into the pipeline before the rerank sort

**Files:**
- Modify: `nous/api/retrieval_pipeline.py:282-283`
- Test: `tests/api/test_retrieval_pipeline.py`

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_deviant_renorm_runs_before_rerank(monkeypatch, pipeline_fixture):
    # A graph-expanded fact at raw 0.65 currently outranks a coherent-leg fact at
    # RRF 1/61(~0.0164) ONLY because 0.65 is on the wrong scale. After rrf_deviant
    # the graph item drops to its rank-based RRF score and the coherent fact leads.
    settings = pipeline_fixture.settings(rerank_by_score=True, score_normalization_mode="rrf_deviant")
    results, _ = await run_recall_pipeline("q", heart, brain, settings, limit=10)
    assert results[0].via_graph is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/api/test_retrieval_pipeline.py::test_deviant_renorm_runs_before_rerank -v`
Expected: FAIL — graph item still ranks first (renormalizer not wired).

- [ ] **Step 3: Wire it in**

At `nous/api/retrieval_pipeline.py`, immediately before the `if rerank_by_score:` block (line ~282):
```python
    mode = getattr(settings, "score_normalization_mode", "off")
    if rerank_by_score and mode != "off":
        from nous.api.score_normalization import renormalize_deviant_legs
        k = getattr(settings, "rrf_k", 60)
        results = renormalize_deviant_legs(results, mode, k=k)
    if rerank_by_score:
        results.sort(key=lambda r: r.score or 0.0, reverse=True)
```
Note: gated on `rerank_by_score` because that is the only path where the merged sort happens; when rerank is off, stage order is preserved and renormalization is moot.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/api/test_retrieval_pipeline.py -v`
Expected: PASS. Also run the byte-identical recall_deep snapshot test — with `mode="off"` (default) output MUST be unchanged.

- [ ] **Step 5: Commit**

```bash
git add nous/api/retrieval_pipeline.py tests/api/test_retrieval_pipeline.py
git commit -m "feat(retrieval): apply deviant-leg renormalization before rerank sort (R2/F080.N)"
```

### Task 1.3: A/B measure R2 — the gate

**Files:**
- Run only (no code): `nous_eval` harness against `:5433`

- [ ] **Step 1: Run both arms vs the Phase-0 baseline**

Run (each changes exactly ONE variable = `score_normalization_mode`):
```bash
# Arm B: the fix — renormalize only the deviant legs
NOUS_SCORE_NORMALIZATION_MODE=rrf_deviant python -m nous_eval.retrieval \
  --sources graph_targeted,probes,nous_prod --top-k 10 --rerank-by-score --report-dir reports/
# Arm C: the losing-arm control — blanket re-rank (expected to regress, per F080 advisor)
NOUS_SCORE_NORMALIZATION_MODE=rrf_all python -m nous_eval.retrieval \
  --sources graph_targeted,probes,nous_prod --top-k 10 --rerank-by-score --report-dir reports/
```
Arm C is a **control**, not a candidate: if `rrf_all` matches or beats `rrf_deviant`, that's a red flag the deviant-marking (Task 1.1 Step 0) is wrong — investigate before trusting Arm B.

- [ ] **Step 2: Apply the gate (`decide_gate_f050` thresholds)**

Compare each arm vs the Phase-0 baseline. **PASS requires:**
1. `graph_targeted` MRR uplift **≥ +0.07** (the `NOUS_EVAL_F050_GATE_THRESHOLD` default), AND
2. **no single source regresses > 0.03** (`..._MAX_SINGLE_REGRESSION`) — especially `probes` and `nous_prod` (the all-vector-findable sources must not degrade), AND
3. majority of gate-eligible sources positive.

Record the winning mode (or "null"). n must be ≥ ~40 on `graph_targeted` (Phase 0 target).

- [ ] **Step 3: BEAM/LME guardrail (prod-generator QA)**

Run a BEAM (or LongMemEval) QA A/B with `--gen-model claude-opus-4-8`, n ≥ 100, changing only `score_normalization_mode` (winning mode vs off). **PASS requires no QA-accuracy regression** (within noise; a positive delta is a bonus, not required — the graph_targeted retrieval uplift is the primary signal).

- [ ] **Step 4: DECISION GATE**

- **If a mode PASSES both** (retrieval uplift + no QA regression): set `NOUS_SCORE_NORMALIZATION_MODE=<winner>` in `.env.prod-snapshot` + prod, record a Brain/FORGE decision with the numbers, and **proceed to Phase 2**.
- **If NULL** (no uplift): do NOT flip. Record the null with numbers (this is a real result — memory shows graph reweighting is often null on current prod). Reassess with the user before Phase 2: the score-space fix may be inert because the graph leg isn't binding even on graph_targeted, in which case Phase 2's coherent_ranking relaxation is also unlikely to help and should be reconsidered.

---

# PHASE 2 — Ranking follow-ups (ONLY if Phase 1 gate passed)

Each task is independently A/B-gated on the Phase-0 harness (one variable, same thresholds).

### Task 2.1: R6 — stop chunk recall leaking soft-deleted episodes
- **Fix:** `nous/api/retrieval_pipeline.py:971-1004` `_search_episode_chunks` — join `heart.episodes` and filter `episodes.active = true` (and `outcome` not abandoned). Also `nous/handlers/episode_summarizer.py:~468` `_link_similar_episodes` (write-side leak).
- **Measurement (A/B):** a **leak probe** — count results in the top-K whose parent episode is `active=false`, before vs after (target: 0 after). PLUS retrieval-neutrality: `graph_targeted`/`probes` MRR must not regress > 0.03. Precision-positive if it lifts a gold item that a leaked soft-deleted chunk was displacing.
- **TDD:** failing test seeds one soft-deleted episode + its chunk, asserts the chunk is absent from `_search_episode_chunks` output; then add the join; then assert green. Commit.

### Task 2.2: R3 — surface fact↔fact contradictions at recall
- **Fix:** `nous/api/retrieval_pipeline.py:687-711` Stage 5 — add heart fact/chunk/episode result IDs to `all_ids` (currently decisions + decision-neighbors only) so fact↔fact `contradicts` edges attach.
- **Measurement (A/B):** a **contradiction-surfacing probe** set (seed N known contradicting fact pairs; query one, assert the contradiction link is attached to the result). Metric = % of seeded contradictions surfaced, before (≈0) vs after. Guardrail: `graph_targeted`/`probes` MRR neutral (this adds links, shouldn't reorder). This probe set is small/hand-built — acceptable because the metric is presence, not rank.
- **TDD:** failing test → add IDs to `all_ids` → green → commit.

### Task 2.3: coherent_ranking re-examination (EVIDENCE-ONLY — do not assume it's a bug)
- **F080 correction:** `coherent_ranking`'s drop of censors + procedures is a **deliberate, reviewed architectural decision** (F080, `a18e0836`), not a band-aid: F080's pattern is "capabilities (procedures) and guardrails (censors) are categorically different from knowledge and should be surfaced via dedicated channels (catalog + `get_procedure`), NOT relevance-ranked against facts." Even with scales normalized, that rationale still holds. **Default assumption: keep the drop.**
- **Only proceed if** the Phase 1 result plus a procedure/censor-recall probe shows the drop is actively costing recall that the dedicated channels don't recover. Frame any change as re-opening F080, not fixing a bug — record a new decision that supersedes/links `a18e0836` with fresh evidence.
- **Measurement (A/B):** if pursued, run procedures/censors re-included (on the normalized scale) vs dropped, `score_normalization_mode=rrf_deviant`. PASS requires no `graph_targeted`/`probes`/`nous_prod` regression > 0.03 AND a positive delta on a **procedure/censor-targeted** qrel slice (or `nous_prod_procedures` source). If it regresses or is flat, the F080 exclusion stands — close the task.

---

# PHASE 3 — Integrity fixes (fault-injection gated; independent of Phase 1)

These protect the corpus from silent corruption and are **not** blocked by Phase 1's retrieval gate (they use a different instrument). They may run in parallel with Phase 1/2. Each is gated on a **before/after fault-injection probe**, not retrieval MRR.

### Task 3.1: S1 — close the NULL-embed fail-open (data-loss)
- **Site:** `nous/heart/facts.py:463-491` (`_embed_with_retry` persists `embedding=NULL` after 2 failed retries) + insert at `:532`.
- **Fix:** on persistent embed outage, fail **closed** — do not insert a NULL-embed, undedupable fact; instead enqueue for re-embed (a `pending_embed` queue row / `embedding_status` column) and retry on the next sleep cycle. Preserve the fail-open-for-CLASSIFIER contract (S4) — this is specifically the embed path.
- **Measurement (fault-injection A/B):** integration test that monkeypatches the embedding provider to raise, learns the SAME fact 5×, and asserts: **before** = 5 NULL-embed rows (all undedupable duplicates); **after** = 0 committed rows + 1 queued (or 1 row max, deduped on retry). Metric = duplicate-fact count under injected outage (5 → ≤1). Also add a prod read-only probe query counting existing `embedding IS NULL AND active` facts to quantify current damage (report only; no prod write).
- **TDD:** write the failing outage-injection test → implement fail-closed + queue → green → commit. Migration for the queue column if needed (respect the comment-semicolon rule; fresh-DB `docker compose up` acceptance).

### Task 3.2: S4 — guard the consolidation classifier (fail-open)
- **Site:** `nous/heart/facts.py:285-311` `_classify_fact_pair`, called unguarded at `:776` (`_find_contradiction`) and `:1033` (`_classify_dupe_in_band`).
- **Fix:** wrap both call sites in try/except that, on classifier raise, falls open per the documented contract (treat as distinct / keep-both — never abort `_learn`, never drop the fact). Mirror the existing guard at `:333` (`is_distinct_fact`).
- **Measurement (fault-injection A/B):** test that monkeypatches `call_background_llm_structured` to raise during a learn that routes through `_find_contradiction`; assert **before** = fact dropped (exception propagates), **after** = fact stored. Metric = fact-drop-rate under classifier outage (1.0 → 0.0).
- **TDD:** failing test → add guards → green → commit.

### Task 3.3: S6 — make consolidation MERGE atomic
- **Site:** `nous/handlers/sleep_handler.py:1145-1230` (F031 MERGE) + `:1544-1607` (F027 cluster) — merged fact committed in txn 1, sources deactivated in a separate txn 2.
- **Fix:** perform the merged-fact insert + source deactivation in one transaction (pass the same session into `heart.learn(..., session=...)` and the deactivation, commit once). If `learn()` can't accept an external session, add that capability.
- **Measurement (crash-injection A/B):** test that injects an exception between the insert and the deactivation; assert **before** = merged fact active AND sources active (duplicate), **after** = transaction rolls back atomically (neither committed) OR both committed. Metric = orphan-duplicate count after injected mid-merge crash (1 → 0).
- **TDD:** failing crash-injection test → single-transaction refactor → green → commit.

### Task 3.4: S5 + S7 — supersession lineage on REMOVE + stale_scan guard
- **Sites:** `nous/handlers/sleep_handler.py:1244-1251` (REMOVE_A/B call `deactivate_fact` only — no `superseded_by`, no `supersedes` edge); `:1336-1357` (`stale_scan` lacks a `superseded_by IS NULL` / merged-head guard).
- **Fix:** REMOVE writes `superseded_by` + a `supersedes` edge via the existing `_apply_supersede` helper (`:974-1004`, already used by SUPERSEDE_A/B). `stale_scan` SELECT gains `AND superseded_by IS NULL AND id NOT IN (SELECT superseded_by FROM heart.facts WHERE superseded_by IS NOT NULL)` (don't retire a live merged head).
- **Measurement (before/after probe):** lineage-completeness count — seed a REMOVE consolidation, assert **before** = 0 `supersedes` edges written for removed facts, **after** = 1 per removal; and a stale_scan test that a live merged head is **not** retired after the guard. Metric = removed-fact lineage-completeness (0% → 100%) + merged-head-retirement count (>0 → 0). Cross-check against the prod edge-audit script (`scripts/diag/edge_audit.py`) read-only.
- **TDD:** failing lineage test → wire `_apply_supersede` into REMOVE + add stale_scan guard → green → commit.

---

## Self-Review

**Spec coverage vs the audit's memory-gap list:**
- R2 (score-space) → Phase 1 ✅ (the headline, gated).
- R3 (fact contradictions) → Task 2.2 ✅.
- R6/S8 (soft-delete leak) → Task 2.1 ✅.
- coherent_ranking type-drop → Task 2.3 ✅ (reframed as an R2 band-aid, relaxation gated).
- S1 (NULL-embed) → Task 3.1 ✅. S4 (classifier) → 3.2 ✅. S6 (atomic merge) → 3.3 ✅. S5/S7 (lineage/stale_scan) → 3.4 ✅.
- S2 (dedup top-20 horizon) → **deliberately deferred**: lower severity, and raising the HNSW limit is itself an A/B (recall vs latency) worth a separate small task once the above land. Noted, not silently dropped.
- R4 (relation-exclusion unification) → **deferred**: inert in prod (adjacency boost consumes it but the audit rated it P2/low-impact); fold into Phase 2 only if Task 2.x touches the same code.

**Measurement instrument coverage:** every ranking fix routes through the `:5433` graph_targeted/probes/nous_prod A/B with the F050 gate thresholds + BEAM/LME Opus guardrail; every integrity fix routes through a named fault-injection before/after probe with a concrete metric. No fix ships on assertion alone.

**Key risk surfaced (not hidden):** if Phase 0 can't build ≥40 graph_targeted qrels on the current corpus (memory: "unconstructable on current prod"), R2 is unmeasurable and the plan STOPS at the 0.2/0.1 gates for user reassessment — rather than shipping R2 blind, which is the exact failure mode the user prohibited.
