# Plan 1.2 — Graph-Leg Score Normalization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put the two remaining score-space-deviant graph legs (spreading activation, decision expansion) onto the same bounded, seed-anchored scale Path A already uses, so graph results merge coherently with the RRF-normalized heart legs.

**Architecture:** Design finalized in FORGE 97ec2098 (user go received 2026-07-12). Two mechanisms: (1) the spreading CTE's cross-path `SUM(activation)` becomes `MAX(activation)` — each individual path's activation is already `seed_score × ∏(weight × decay) ≤ 1`, so only the SUM violates the [0,1] contract (it also makes cycles score-inflating, since traversal is undirected with no visited-set); MAX = bounded best-path, unconditional bug-class fix, no flag (owner directive from PR #556: no dark landing for spreading behavior). (2) Thread the seed's retrieval score onto Stage 2 heart-graph decision neighbors (`seed=hr.score`) and Stage 4 1-hop decision-expansion neighbors (`seed=dec.score`), mirroring Path A's nullable-assignment + best-composed-path pattern, then swap `_heart_graph_to_pipeline` and `_graph_expanded_to_pipeline` onto the shared `_score_memory_neighbor` scorer. Seed-score behavior rides the EXISTING `graph_neighbor_seed_score_enabled` flag (default False in code, ON in prod); when the flag is off or `seed_score` is None the scorer falls back to the exact legacy formula, so flag-off behavior is byte-identical.

**Tech Stack:** Python 3.12+, SQLAlchemy async `text()` SQL, pytest (+ `NOUS_TEST_DB=postgres` for CTE tests).

## Global Constraints

- No new flags, no config changes. `SUM→MAX` is unconditional; seed-score extension rides `graph_neighbor_seed_score_enabled`.
- Flag-off (or seed_score=None) scoring must be byte-identical to today: `_score_memory_neighbor`'s fallback IS the current `_f065_provenance_penalty(n, n.edge_weight, decay, settings)` expression.
- Spreading rows keep `seed_score=None` by design — their activation ALREADY composes the seed score per-hop, so they score via the legacy `activation × decay` path (now bounded ≤ max-seed ≤ 1 by MAX).
- The 0.1 activation floor (`retrieval_pipeline.py:720`) is KEPT: under MAX it is stricter (a node needs one genuine path ≥ 0.1 instead of accumulating weak paths) — that is intended precision behavior; document, don't retune.
- **PR is NOT merged in this session** — it awaits the user's external A/B (their directive). Codex loop runs to clean; merge is the user's call after measurement.
- Branch `feat/plan12-graph-seed-score` (worktree, based on 3a381b2). Local SQLite suite has pre-existing failures; local `NOUS_TEST_DB=postgres` has 6 pre-existing dirty-dev-DB failures — **Postgres CI is the gate** (established in d363b036).

## Current code facts (verified at 3a381b2)

- `spreading_activation.py:150`: `SELECT id, node_type, SUM(activation) AS total_activation ... GROUP BY id, node_type ORDER BY total_activation DESC`.
- Seeds: decisions `(d.id, "decision", d.score or 0.5)` + top-3 heart facts with RRF scores (`retrieval_pipeline.py:631-689`); `settings.spreading_activation_decay = 0.5`, `max_depth = 2`.
- Spreading results → `NeighborResult(edge_relation="spreading_activation", edge_weight=activation, seed_score=None)` (`retrieval_pipeline.py:746-755`) → `_graph_expanded_to_pipeline` scores `_f065_provenance_penalty(n, n.edge_weight, decay)` which for spreading rows is `activation × decay` (`:1191-1192`).
- Stage 2 heart-graph decisions (`:477-503`): `for hr in acc.heart_results[:3]` → `brain.neighbors(..., neighbor_type="decision", limit=2)` → appended with `seen_graph_ids` set, first-seed-wins, **no seed_score assignment** → `_heart_graph_to_pipeline` scores `edge_weight × decay × penalty`.
- Stage 4 1-hop (`:778-797`): `for dec in decision_results[:max_expand]: if dec.score is None: continue` → neighbors appended to `graph_expanded` with `seen_ids` set, first-seed-wins, **no seed_score** → `_graph_expanded_to_pipeline`.
- Path A (Stage 2b, `:515-611`): the pattern to mirror — nullable `n.seed_score = seed_score` assignment (None stays None; comment at `:524-529` explains why never coerce to 0.0), duplicate handling compares full composed `_score_memory_neighbor` and replaces path metadata when a later path wins (`:578-596`).
- `_score_memory_neighbor` (`:1222-1239`): flag+seed path = `seed_score × edge_weight × penalty`; fallback = `_f065_provenance_penalty(n, n.edge_weight, settings.graph_recall_decay, settings)`.
- `NeighborResult.seed_score: float | None = None` exists (`brain/schemas.py:183`); docstring says "None for neighbors built outside that path (e.g. decision expansion, spreading activation)" — Tasks 2-3 change that for decision expansion; update the comment.
- Existing spreading tests assert reachability/exclusions only, never SUM magnitudes (`tests/test_spreading_activation.py`) — SUM→MAX breaks none of them.

---

### Task 1: Spreading CTE `SUM` → `MAX`

**Files:**
- Modify: `nous/brain/spreading_activation.py:150` (+docstring)
- Test: `tests/test_spreading_activation.py` (extend)

**Interfaces:**
- Produces: unchanged signature; `total_activation` is now bounded best-path (`≤ max seed score` when all edge weights ≤ 1).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_spreading_activation.py`:

```python
@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_spreading_activation_is_bounded_best_path(brain, session):
    """Plan 1.2: cross-path aggregation is MAX, not SUM. A diamond
    (seed→A→C, seed→B→C, all weight 1.0, decay 0.5, depth 2) must give C
    activation = one best path (1.0 × 0.5 × 0.5 = 0.25), NOT the 0.5 a
    SUM over both paths would produce. Also pins the global bound: no
    activation may exceed the max seed score (SUM additionally inflated
    seeds themselves via undirected cycle returns)."""
    from sqlalchemy import text

    from nous.brain.schemas import RecordInput

    def _inp(d):
        return RecordInput(description=d, confidence=0.8, category="architecture",
                           stakes="low", reasons=_reasons())

    seed = await brain.record(_inp("Diamond seed decision for bounded path test"), session=session)
    a = await brain.record(_inp("Diamond left intermediate decision node"), session=session)
    b = await brain.record(_inp("Diamond right intermediate decision node"), session=session)
    c = await brain.record(_inp("Diamond sink decision reachable via two paths"), session=session)

    async def _edge(s_id, t_id):
        await session.execute(text(
            "INSERT INTO brain.graph_edges (source_id,target_id,source_type,target_type,"
            "agent_id,relation,weight,auto_linked,extraction_method) "
            "VALUES (:s,:t,'decision','decision',:a,'related_to',1.0,true,'deterministic')"),
            {"s": str(s_id), "t": str(t_id), "a": brain.agent_id})

    await _edge(seed.id, a.id)
    await _edge(seed.id, b.id)
    await _edge(a.id, c.id)
    await _edge(b.id, c.id)
    await session.flush()

    settings = Settings()  # decay=0.5, max_depth=2
    activated = await spreading_activation_search(
        session, brain.agent_id, [(seed.id, "decision", 1.0)], settings,
    )
    by_id = {r[0]: r[2] for r in activated}
    assert by_id[c.id] == pytest.approx(0.25), (
        "C must score its best single path (MAX), not the sum of both paths"
    )
    assert all(act <= 1.0 + 1e-9 for act in by_id.values()), (
        "no activation may exceed the max seed score"
    )
    # Seed's own activation must stay its seed score, not seed + cycle returns.
    assert by_id[seed.id] == pytest.approx(1.0)
```

NOTE: `brain.record()` may auto-link the four decisions to each other (similarity linker) — if that adds edges that change the exact path math, either construct with dissimilar descriptions (current texts are deliberately distinct) or assert `by_id[c.id] <= 0.25 + 1e-9` plus `< 0.5 - 1e-9` (strictly-less-than-SUM). Implementer verifies with the real run and picks the strongest stable assertion; the bound assertions must stay exact.

- [ ] **Step 2: Run test to verify it fails**

Run: `$env:NOUS_TEST_DB='postgres'; uv run pytest tests/test_spreading_activation.py -q -k bounded_best_path`
Expected: FAIL — C's activation is 0.5 (SUM of two 0.25 paths).

- [ ] **Step 3: Implement**

In `spreading_activation.py`, change line 150 and document:

```sql
        SELECT id, node_type, MAX(activation) AS total_activation
```

and extend the function docstring's Returns section:

```
    Returns:
        List of (node_id, node_type, activation) sorted by activation desc.
        Aggregation across paths is MAX (bounded best-path), not SUM:
        each path's activation is seed_score × ∏(weight × decay) ≤ 1 when
        weights ≤ 1, so MAX keeps results on the seeds' [0,1] score scale
        and makes undirected-traversal cycles score-harmless. (Plan 1.2 —
        SUM let multi-path/cyclic nodes exceed 1.0 and dominate the
        RRF-sorted merge.)
```

Keep the alias `total_activation` (callers destructure positionally; renaming would churn the CTE only).

- [ ] **Step 4: Run tests**

Run: `$env:NOUS_TEST_DB='postgres'; uv run pytest tests/test_spreading_activation.py -q`
Expected: all pass (existing reachability/exclusion tests unaffected).

- [ ] **Step 5: Commit**

```bash
git add nous/brain/spreading_activation.py tests/test_spreading_activation.py
git commit -m "fix(retrieval): spreading activation aggregates MAX not SUM - bounded best-path scores"
```

---

### Task 2: Stage 2 heart-graph decisions — seed_score + shared scorer

**Files:**
- Modify: `nous/api/retrieval_pipeline.py:476-503` (Stage 2 loop), `:1198-1219` (`_heart_graph_to_pipeline`)
- Modify: `nous/brain/schemas.py:178-183` (seed_score comment: decision expansion now threads it)
- Test: `tests/test_retrieval_pipeline.py` (extend — find the existing Stage-2/scorer test section and mirror its fixtures)

**Interfaces:**
- Consumes: `_score_memory_neighbor` (existing, shared).
- Produces: Stage 2 neighbors carry `seed_score=hr.score` (nullable); duplicates keep the best composed path.

- [ ] **Step 1: Write the failing tests**

Two unit tests (no DB — construct `NeighborResult` + `Settings` directly, mirroring how `tests/test_f065_pipeline_penalty.py` tests the pipeline scorers):

```python
def test_heart_graph_to_pipeline_uses_seed_score_when_flag_on():
    """Plan 1.2: Stage 2 decision neighbors score seed×edge×penalty when
    graph_neighbor_seed_score_enabled and seed_score is threaded."""
    from nous.api.retrieval_pipeline import _heart_graph_to_pipeline
    from nous.brain.schemas import NeighborResult

    n = NeighborResult(
        id=uuid4(), node_type="decision", description="d",
        edge_relation="related_to", edge_weight=0.8,
        created_at=datetime.now(UTC), extraction_method="inferred",
        seed_score=0.9,
    )
    s = Settings()
    object.__setattr__(s, "graph_neighbor_seed_score_enabled", True)
    object.__setattr__(s, "graph_inferred_edge_penalty", 0.5)
    [res] = _heart_graph_to_pipeline([n], s)
    assert res.score == pytest.approx(0.9 * 0.8 * 0.5)


def test_heart_graph_to_pipeline_legacy_when_flag_off_or_no_seed():
    """Flag off, or seed_score None (legacy callers), must reproduce the
    pre-plan-1.2 formula edge_weight × decay × penalty exactly."""
    from nous.api.retrieval_pipeline import _heart_graph_to_pipeline
    from nous.brain.schemas import NeighborResult

    s = Settings()
    object.__setattr__(s, "graph_neighbor_seed_score_enabled", False)
    n = NeighborResult(
        id=uuid4(), node_type="decision", description="d",
        edge_relation="related_to", edge_weight=0.8,
        created_at=datetime.now(UTC), extraction_method="heuristic",
        seed_score=0.9,
    )
    [res] = _heart_graph_to_pipeline([n], s)
    assert res.score == pytest.approx(0.8 * s.graph_recall_decay)

    object.__setattr__(s, "graph_neighbor_seed_score_enabled", True)
    n2 = NeighborResult(
        id=uuid4(), node_type="decision", description="d",
        edge_relation="related_to", edge_weight=0.8,
        created_at=datetime.now(UTC), extraction_method="heuristic",
        seed_score=None,
    )
    [res2] = _heart_graph_to_pipeline([n2], s)
    assert res2.score == pytest.approx(0.8 * s.graph_recall_decay)
```

Place them beside the existing `_heart_graph_to_pipeline` / scorer tests (grep `tests/test_retrieval_pipeline.py` and `tests/test_f065_pipeline_penalty.py` for the current fixtures and match imports/style; if the F065 file already covers `_heart_graph_to_pipeline`, put them there).

- [ ] **Step 2: Run to verify the first fails**

`uv run pytest tests/test_f065_pipeline_penalty.py tests/test_retrieval_pipeline.py -q -k seed_score`
Expected: first test FAILS (score uses edge_weight × decay, ignoring seed_score); second PASSES (documents the invariant).

- [ ] **Step 3: Implement**

Stage 2 loop (`retrieval_pipeline.py:476-503`) — upgrade `seen_graph_ids` from set to dict and mirror Path A's nullable assignment + best-path compare:

```python
        seen_graph: dict[UUID, "NeighborResult"] = {}
        for hr in acc.heart_results[:3]:
            if hr.type in ("fact", "episode"):
                try:
                    # (existing brain.neighbors call unchanged)
                    neighbors = await brain.neighbors(
                        hr.id,
                        node_type=hr.type,
                        limit=2,
                        neighbor_type="decision",
                    )
                    for n in neighbors:
                        if n.node_type != "decision":
                            continue
                        # Plan 1.2: thread the seed's retrieval score
                        # (nullable — None stays None so the scorer's
                        # fallback fires; never coerce to 0.0).
                        n.seed_score = hr.score
                        if n.id in seen_graph:
                            # Same best-composed-path rule as Stage 2b
                            # (:578-596): replace path metadata only when
                            # the later path genuinely composes higher.
                            prev = seen_graph[n.id]
                            if _score_memory_neighbor(n, settings) > _score_memory_neighbor(prev, settings):
                                prev.seed_score = n.seed_score
                                prev.edge_weight = n.edge_weight
                                prev.edge_relation = n.edge_relation
                                prev.extraction_method = n.extraction_method
                            continue
                        acc.heart_graph_decisions.append(n)
                        seen_graph[n.id] = n
                except Exception:
                    # (existing warning + counter unchanged)
```

`_heart_graph_to_pipeline` (`:1207`) — swap the score expression:

```python
            score=_score_memory_neighbor(n, settings),
```

(`_score_memory_neighbor` is defined AFTER `_heart_graph_to_pipeline` in the file — module-level function referenced at call time, no reordering needed; verify or move `_score_memory_neighbor` above both callers for readability.)

`schemas.py:178-183` comment — replace "None for neighbors built outside that path (e.g. decision expansion, spreading activation)" with "Spreading-activation rows keep None by design (their activation already composes the seed score per hop); Stage 2 decision expansion and Stage 4 1-hop thread it as of plan 1.2."

- [ ] **Step 4: Run tests**

`uv run pytest tests/test_f065_pipeline_penalty.py tests/test_retrieval_pipeline.py -q`
Expected: PASS including all pre-existing scorer tests (flag-off default keeps legacy values).

- [ ] **Step 5: Commit**

```bash
git add nous/api/retrieval_pipeline.py nous/brain/schemas.py tests/
git commit -m "feat(retrieval): thread seed_score into Stage 2 heart-graph decision neighbors"
```

---

### Task 3: Stage 4 1-hop — seed_score + shared scorer

**Files:**
- Modify: `nous/api/retrieval_pipeline.py:778-797` (1-hop loop), `:1294-1328` (`_graph_expanded_to_pipeline`)
- Test: same test files as Task 2

**Interfaces:**
- Produces: 1-hop decision-expansion neighbors carry `seed_score=dec.score`; spreading rows keep `seed_score=None` (bounded activation × decay via legacy path).

- [ ] **Step 1: Write the failing tests**

```python
def test_graph_expanded_to_pipeline_uses_seed_score_for_one_hop():
    """Plan 1.2: 1-hop expansion rows (seed_score threaded) score
    seed×edge×penalty under the flag; spreading rows (seed_score None,
    edge_weight = bounded activation) keep activation × decay."""
    from nous.api.retrieval_pipeline import _graph_expanded_to_pipeline
    from nous.brain.schemas import NeighborResult

    s = Settings()
    object.__setattr__(s, "graph_neighbor_seed_score_enabled", True)

    one_hop = NeighborResult(
        id=uuid4(), node_type="fact", description="f",
        edge_relation="related_to", edge_weight=0.7,
        created_at=datetime.now(UTC), extraction_method="heuristic",
        seed_score=0.6,
    )
    spreading = NeighborResult(
        id=uuid4(), node_type="fact", description="f2",
        edge_relation="spreading_activation", edge_weight=0.4,
        created_at=datetime.now(UTC),
    )
    res = {r.description: r for r in _graph_expanded_to_pipeline([one_hop, spreading], s)}
    assert res["f"].score == pytest.approx(0.6 * 0.7)
    assert res["f2"].score == pytest.approx(0.4 * s.graph_recall_decay)
```

- [ ] **Step 2: Run to verify it fails**

Expected: FAIL on `res["f"]` (currently `0.7 × decay`), PASS branch for spreading.

- [ ] **Step 3: Implement**

1-hop loop (`:780-792`) — dict + assignment + compare (same shape as Task 2; `dec.score` is guaranteed non-None here by the existing `continue` guard, but assign without coercion anyway):

```python
        if not use_spreading:
            # Fall back to 1-hop expansion
            seen_hop: dict[UUID, "NeighborResult"] = {}
            for dec in decision_results[: settings.graph_recall_max_expand]:
                if dec.score is None:
                    continue
                try:
                    neighbors = await brain.neighbors(
                        dec.id,
                        node_type="decision",
                        limit=settings.graph_recall_max_neighbors,
                    )
                    for n in neighbors:
                        # Plan 1.2: thread the expanding decision's score.
                        n.seed_score = dec.score
                        if n.id in seen_ids:
                            prev = seen_hop.get(n.id)
                            if prev is not None and _score_memory_neighbor(n, settings) > _score_memory_neighbor(prev, settings):
                                prev.seed_score = n.seed_score
                                prev.edge_weight = n.edge_weight
                                prev.edge_relation = n.edge_relation
                                prev.extraction_method = n.extraction_method
                            continue
                        graph_expanded.append(n)
                        seen_hop[n.id] = n
                        seen_ids.add(n.id)
                except Exception:
                    # (existing debug + counter unchanged)
```

(NOTE: `seen_ids` also contains the decision_results themselves — a neighbor that IS another seed decision stays skipped with no compare; `seen_hop.get` returning None covers that.)

`_graph_expanded_to_pipeline` (`:1303`) — swap:

```python
            score=_score_memory_neighbor(n, settings),
```

Spreading rows flow through the scorer's fallback (`seed_score=None`) → `_f065_provenance_penalty(n, activation, decay)` → `activation × decay` — byte-identical to today, now bounded by Task 1.

- [ ] **Step 4: Run tests**

`uv run pytest tests/test_f065_pipeline_penalty.py tests/test_retrieval_pipeline.py tests/test_spreading_activation.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add nous/api/retrieval_pipeline.py tests/
git commit -m "feat(retrieval): thread seed_score into Stage 4 one-hop expansion; shared scorer for graph legs"
```

---

### Task 4: Suite, PR, codex — NO merge

- [ ] Run: `$env:NOUS_TEST_DB='postgres'; uv run pytest tests/ -q` — expect only the 6 known dirty-dev-DB failures (density/admission), identical to origin/main; everything else green. CI is the gate.
- [ ] Push `feat/plan12-graph-seed-score`, open PR titled `feat(retrieval): plan 1.2 — bounded spreading scores + seed-score threading for graph legs`. Body: score-space incoherence context (F080 verdict: only chunk+graph legs deviant; chunk leg fixed in #553), the SUM→MAX bound argument, seed-score extension riding the existing prod-ON flag, byte-identical flag-off invariant, and an explicit **"DO NOT MERGE — awaiting external A/B (user runs in separate session)"** header line.
- [ ] Codex loop to clean (👍 or "no major issues"); address every finding.
- [ ] STOP. Leave the PR open; report the branch/PR to the user for their A/B.

## Explicit non-goals

- No retuning of `spreading_activation_decay`, the 0.1 activation floor, or `graph_recall_decay`.
- No change to spreading seed composition (#556 owns that), Path A/Stage 2b (already correct), chunk-leg renorm (#553, own flag), or `rerank_by_score` default.
- No new flags; no A/B in this session (user directive).

---

## Amendments after 3-agent team review (2026-07-12)

Verdicts: correctness APPROVE; devil's-advocate + test-quality APPROVE-WITH-FIXES. All fixes below are binding on the implementation; where they conflict with the task bodies above, the amendment wins.

**A1 (P1, both reviewers): gate the best-path dedup compare on `graph_neighbor_seed_score_enabled`.** Flag-off duplicates take the existing `continue` (first-seed-wins) so flag-off output stays byte-identical (score AND edge_relation) and the external A/B's control arm is uncontaminated. Structure per stage:

```python
                    for n in neighbors:
                        if n.node_type != "decision":
                            continue
                        prev = seen_graph.get(n.id)
                        if prev is n:
                            # Aliasing guard (review F5): the same object
                            # reached again (shared mock instances) must not
                            # have its stored seed_score overwritten.
                            continue
                        # Plan 1.2: thread the seed's retrieval score
                        # (nullable — None stays None; never coerce to 0.0).
                        n.seed_score = hr.score
                        if prev is not None:
                            # Best-composed-path replacement is part of the
                            # seed-score mechanism — flag-gated so flag-off
                            # dedup stays first-seed-wins (byte-identical).
                            if (
                                getattr(settings, "graph_neighbor_seed_score_enabled", False)
                                and _score_memory_neighbor(n, settings) > _score_memory_neighbor(prev, settings)
                            ):
                                prev.seed_score = n.seed_score
                                prev.edge_weight = n.edge_weight
                                prev.edge_relation = n.edge_relation
                                prev.extraction_method = n.extraction_method
                            continue
                        acc.heart_graph_decisions.append(n)
                        seen_graph[n.id] = n
```

Stage 4 mirrors this with `seen_hop.get(n.id)` (plus the existing `seen_ids` membership check for seed decisions: if `n.id in seen_ids` and `prev is None` → plain `continue`, no compare).

**A2 (P3, belt): bound the CTE weight term.** At `spreading_activation.py:137` use `LEAST(COALESCE(e.weight, 1.0), 1.0)` instead of `COALESCE(e.weight, 1.0)` — `brain.graph_edges.weight` has no DB CHECK ≤ 1.0; all writers cap in code today, but the MAX-bound docstring claim should be enforced, not assumed. Docstring states the precondition.

**A3 (test fixes, binding):**
- Use `uuid.uuid4()` (module import style of `test_f065_pipeline_penalty.py`) — `uuid4` bare name is a NameError there.
- Use constructor kwargs, not `object.__setattr__`: `Settings(graph_neighbor_seed_score_enabled=True, graph_inferred_edge_penalty=0.5, graph_recall_decay=0.7)`; diamond test uses `Settings(spreading_activation_decay=0.5, spreading_activation_max_depth=2)` — exact-magnitude tests must not inherit `.env`/env-var drift.
- Task 2 Step 2 command: `-k "seed_score or no_seed"` (the plain `-k seed_score` deselects the invariant test).
- Pin failure REASONS: Task 2 test 1 must fail with `0.28 != approx(0.36)` (current formula edge×decay×penalty), NOT a NameError/collection error. Task 3: fails on `res["f"]` (`0.49 != approx(0.42)`); the spreading assertion only becomes observed at Step 4.
- Diamond test: DELETE the weakened `<=` fallback from the Task 1 NOTE — the `brain` fixture has no embedder, `_auto_link` no-ops (`brain.py:1750-1752`), the 4-edge set is deterministic; keep exact `== approx(0.25)`, and expected SUM-failure values are C=0.5, seed=1.5.
- Delete the now-dead `decay = settings.graph_recall_decay` locals in both converters after the scorer swap.

**A4 (new tests, binding):**
1. **E2E flag-on wiring test** in `tests/test_retrieval_pipeline.py` using the existing `_make_heart`/`_make_brain`/`_make_settings` mock harness (`:162-237`): heart fact seed score s + decision neighbor edge w → the heart_graph PipelineResult scores `s×w`; and a Stage 4 1-hop neighbor from `dec.score=d`, edge w2 → `d×w2`. `_make_settings` must gain `graph_neighbor_seed_score_enabled` and `graph_inferred_edge_penalty` attrs. This is the test that catches "converters swapped but loop never assigns seed_score" (the silent-inert failure class).
2. **Stage 2 duplicate-path tests**: two seeds reaching the same decision via different (seed_score, edge_weight) pairs — flag ON: best composed path wins (score AND edge metadata replaced); flag OFF: first-seed-wins, output byte-identical to today. Fixtures must use per-seed FRESH NeighborResult objects (shared instances hit the `prev is n` guard).
3. **Stage 4 prev-None test**: 1-hop neighbor whose id is already in `seen_ids` as a seed decision → skipped without compare, no crash.
4. **Depth-mixed MAX postgres test**: seed→X (direct, 0.5) plus seed→A→X (0.25) → `by_id[x.id] == approx(0.5)` — pins MAX across different-length paths (diamond covers equal-length only).

**A5 (PR body additions, binding):** state the prod amplifiers explicitly — prod `recall_deep` runs with `rerank_by_score=True` (chunks enabled ⇒ `tools.py:947-959`), so new Stage-2/4 scores globally reorder the merged list, and the prod-ON adjacency boost multiplies + re-sorts on top; spreading itself is inert in prod day-one (auto mode, density 2.745 < 3.0). Also state the A/B measurement demand: report spreading result-count and depth histogram before/after (MAX + the 0.1 floor makes depth-2 survival require `seed×w₁×w₂ > 0.4` — near-1-hop behavior for weak chains is intended, but must be visible in the eval, not assumed).

**Cleared by review (no action):** seed-scale trap (both seed families are 1/k-normalized RRF in [0,1]; top-3 fact seeds ≈0.86–1.0; depth-2 activations ≈0.22–0.25 clear the 0.1 floor); byte-identical scorer-swap float math; `seen_graph_ids` locality; Stage 4 prev-None safety; adjacency-boost >1.0 exposure pre-existing; `hr.score` is never None in practice (Heart coerces to 0.0 — the nullable guard is belt only).
