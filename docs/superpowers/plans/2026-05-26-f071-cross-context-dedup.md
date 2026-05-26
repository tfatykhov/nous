# F071 Implementation Plan — Cross-context dedup

**Spec:** `docs/features/F071-cross-context-dedup.md`
**Forge decision:** `911bb083`
**Date:** 2026-05-26
**Branch:** `feat/F071-cross-context-dedup`

This plan is intentionally tactical — exact code to insert, exact line targets, exact test cases. Copy verbatim, don't editorialize.

---

## Order of operations

1. Define `CURRENT_TURN_EXCLUDE_IDS` ContextVar + `_build_exclude_ids` helper in `runner.py`
2. Wire ContextVar set/reset into both `run_turn` and `stream_chat`
3. Add `recall_exclude_context_ids: bool = False` to `Settings`
4. Add `exclude_ids` parameter + filter + stats field to `run_recall_pipeline`
5. Add lazy ContextVar read in `recall_deep` closure
6. Write unit tests for pipeline filter (no app wiring needed)
7. Write integration tests for runner ↔ tools wiring
8. Write concurrency test for contextvar isolation
9. Update CLAUDE.md env table + features INDEX.md
10. Run full test suite; fix; commit; push; codex

---

## Step 1 — `Settings` field

`nous/config.py` — find the section near other `NOUS_RECALL_*` flags (e.g., `recall_include_parent_episodes`). Add:

```python
recall_exclude_context_ids: bool = Field(
    default=False,
    description=(
        "F071: recall_deep filters out items already loaded into the "
        "current turn's system prompt (facts/decisions/episodes/procedures). "
        "Land dark; flip in dev for measurement."
    ),
)
```

`alias` follows existing pattern (uppercase with `NOUS_` prefix is auto from `BaseSettings` config).

---

## Step 2 — `runner.py` ContextVar + helper

Top-of-module imports section: add

```python
from contextvars import ContextVar

CURRENT_TURN_EXCLUDE_IDS: ContextVar[dict[str, set[str]] | None] = ContextVar(
    "CURRENT_TURN_EXCLUDE_IDS", default=None,
)
```

After the imports, before `class AgentRunner`, add the helper:

```python
def _build_exclude_ids(
    settings: "Settings",
    turn_context: "TurnContext | None",
) -> dict[str, set[str]] | None:
    """F071: build per-type exclusion set from a TurnContext.

    Returns None when the feature flag is off or no turn_context is available
    so the pipeline's `if exclude_ids:` short-circuit fires and run_recall_pipeline
    output stays byte-identical.
    """
    if not getattr(settings, "recall_exclude_context_ids", False):
        return None
    if turn_context is None:
        return None
    return {
        "fact":      set(turn_context.recalled_fact_ids or []),
        "decision":  set(turn_context.recalled_decision_ids or []),
        "episode":   set(turn_context.recalled_episode_ids or []),
        "procedure": set(turn_context.recalled_procedure_ids or []),
    }
```

`getattr(settings, ..., False)` is defensive — tests that mock Settings shouldn't need to populate the new field.

---

## Step 3 — Wire into `run_turn` (non-streaming)

`nous/api/runner.py::run_turn`, ~line 351 (after pre_turn returns `turn_context`):

The existing block is:
```python
# after pre_turn at ~line 342-351
turn_context = await self._cognitive.pre_turn(...)

# 3. Append user message  (~line 353)
conversation.messages.append(Message(role="user", content=user_message))
...
return response_text, turn_context, usage  # ~line 494
```

Wrap from line 353 through line 494 in a new outer try/finally:

```python
turn_context = await self._cognitive.pre_turn(...)

# F071: per-turn exclusion-set lifecycle
exclude_ids = _build_exclude_ids(self._settings, turn_context)
_f071_token = CURRENT_TURN_EXCLUDE_IDS.set(exclude_ids)
try:
    # 3. Append user message  (existing code from ~line 353)
    conversation.messages.append(Message(role="user", content=user_message))
    ...
    # Existing inner try/except/else/post_turn/raise stays unchanged
    ...
    return response_text, turn_context, usage
finally:
    CURRENT_TURN_EXCLUDE_IDS.reset(_f071_token)
```

**Critical:** the outer try wraps EVERYTHING after pre_turn including the re-raise at line 492. The `finally` fires even on the re-raise. Use a unique local name like `_f071_token` to avoid collision with any future locals named `token`.

---

## Step 4 — Wire into `stream_chat` (streaming)

`nous/api/runner.py::stream_chat`, ~line 894 (after pre_turn).

Same pattern — wrap from the user-message append (~line 896) through the end of the generator body in a new outer try/finally:

```python
turn_context = await self._cognitive.pre_turn(...)  # ~line 887

# F071: per-turn exclusion-set lifecycle (streaming path)
exclude_ids = _build_exclude_ids(self._settings, turn_context)
_f071_token = CURRENT_TURN_EXCLUDE_IDS.set(exclude_ids)
try:
    conversation.messages.append(Message(role="user", content=user_message))
    ...
    # existing generator body
    ...
finally:
    CURRENT_TURN_EXCLUDE_IDS.reset(_f071_token)
```

If the existing `try:` at ~line 993 already wraps the generator body, the F071 wrapper goes OUTSIDE it — F071's `finally` must fire even when the inner generator raises.

**Verify locally before push:** the `stream_chat` is an async generator (`async def stream_chat ... yield ...`). ContextVar values are preserved across `yield` boundaries in the same Task. No special handling needed.

---

## Step 5 — `recall_deep` closure reads ContextVar

`nous/api/tools.py::recall_deep`, after line 605 (`from nous.api.retrieval_pipeline import run_recall_pipeline`), add:

```python
# F071: lazy import to avoid runner.py <-> tools.py circular dependency
from nous.api.runner import CURRENT_TURN_EXCLUDE_IDS
_f071_exclude_ids = CURRENT_TURN_EXCLUDE_IDS.get()
```

In the `run_recall_pipeline(...)` call at ~line 653, add `exclude_ids=_f071_exclude_ids`:

```python
results, stats = await run_recall_pipeline(
    query=query,
    heart=heart,
    brain=brain,
    settings=settings,
    limit=limit,
    memory_types=memory_types,
    residual_activations=residual_activations or None,
    rerank_by_score=chunks_rerank,
    exclude_ids=_f071_exclude_ids,  # F071
)
```

In the existing INFO log at ~line 680, add `excluded_in_context=%d` and `stats.excluded_in_context` so we can grep prod logs:

```python
logger.info(
    "recall_deep agent=%s query_chars=%d limit=%d "
    "chunks_enabled=%s chunks_searched=%s "
    "n_chunks_total=%d n_chunks_top10=%d first_chunk_rank=%s "
    "excluded_in_context=%d "  # F071
    "n_total=%d",
    ...,
    stats.excluded_in_context,  # F071
    len(results),
)
```

---

## Step 6 — `run_recall_pipeline` filter + stats field

`nous/api/retrieval_pipeline.py:73` — add field to `PipelineStats`:

```python
@dataclass(frozen=True)
class PipelineStats:
    ...
    contradiction_edges: list[tuple[UUID, str, UUID, str]] = field(
        default_factory=list
    )
    # F071: count of results dropped because they were already in the
    # current turn's system prompt. 0 when feature flag is off.
    excluded_in_context: int = 0
```

`run_recall_pipeline` signature — add `exclude_ids` param. Find the function definition (around line 175) and add:

```python
async def run_recall_pipeline(
    query: str,
    heart: "Heart",
    brain: "Brain",
    settings: "Settings",
    limit: int = 10,
    ...
    rerank_by_score: bool = False,
    exclude_ids: dict[str, set[str]] | None = None,  # F071
) -> tuple[list[PipelineResult], PipelineStats]:
```

Filter insertion — between `retrieval_pipeline.py:265` (`results.sort(...)`) and `:267` (`stats = PipelineStats(...)`):

```python
    if rerank_by_score:
        results.sort(key=lambda r: r.score or 0.0, reverse=True)

    # F071: drop results whose id is already in the system prompt for this
    # turn. Applied AFTER all scoring (rerank, MMR, CE inside heart.recall)
    # so the LLM sees the next-best alternatives — not the items below
    # the now-excluded head. Type-keyed so a UUID collision across types
    # (defensive, won't happen in practice) doesn't cross-filter.
    excluded_in_context = 0
    if exclude_ids:
        before = len(results)
        results = [
            r for r in results
            if str(r.id) not in exclude_ids.get(r.type, set())
        ]
        excluded_in_context = before - len(results)

    stats = PipelineStats(
        ce_reranked=False,
        mmr_applied=False,
        graph_expansion_used=acc.graph_expansion_used,
        spreading_activation_used=acc.spreading_activation_used,
        contradiction_checks_ran=acc.contradiction_checks_ran,
        chunks_searched=acc.chunks_searched,
        n_heart_results=len(acc.heart_results),
        n_brain_results=len(acc.decision_results),
        n_graph_expanded=len(acc.graph_expanded),
        n_stage_errors=dict(acc.stage_errors),
        contradiction_edges=list(acc.contradictions),
        excluded_in_context=excluded_in_context,  # F071
    )
    return results, stats
```

---

## Step 7 — Unit tests

**Snapshot test that MUST stay byte-identical:** the existing test is
`tests/test_retrieval_pipeline.py::TestFormatPipelineTextSnapshot::test_format_matches_committed_snapshot`
(fixture: `tests/fixtures/recall_deep_text_snapshot.txt`). It calls
`run_recall_pipeline` without `exclude_ids` — the new default `None` will
short-circuit, leaving output byte-identical. Verify by running it explicitly
post-impl; no test changes needed there.

**New file:** `tests/test_retrieval_pipeline_exclusion.py`

```python
"""F071: cross-context dedup exclusion-set unit tests for run_recall_pipeline."""
import pytest
from uuid import uuid4
from unittest.mock import AsyncMock, MagicMock
from nous.api.retrieval_pipeline import run_recall_pipeline, PipelineResult, PipelineStats


# (set up minimal heart/brain/settings mocks here. Pattern: copy from
#  existing tests/test_retrieval_pipeline*.py if it exists; otherwise mock
#  the Heart.recall / Brain.query surface to return fixed PipelineResults.)


@pytest.mark.asyncio
async def test_exclude_ids_none_byte_identical_baseline(
    heart_mock, brain_mock, settings_mock,
):
    """exclude_ids=None must yield the exact same (results, stats) shape
    as the pre-F071 baseline."""
    r1, s1 = await run_recall_pipeline(
        query="x", heart=heart_mock, brain=brain_mock, settings=settings_mock,
        exclude_ids=None,
    )
    # Snapshot: no items dropped, counter == 0
    assert s1.excluded_in_context == 0
    # Output preserves the canonical id list expected by the test fixture
    assert [str(r.id) for r in r1] == EXPECTED_BASELINE_IDS


@pytest.mark.asyncio
async def test_exclude_ids_empty_dict_short_circuits(
    heart_mock, brain_mock, settings_mock,
):
    """exclude_ids={} (no keys at all) behaves identically to None."""
    r_none, s_none = await run_recall_pipeline(
        ..., exclude_ids=None,
    )
    r_empty, s_empty = await run_recall_pipeline(
        ..., exclude_ids={},
    )
    assert [r.id for r in r_none] == [r.id for r in r_empty]
    assert s_empty.excluded_in_context == 0


@pytest.mark.asyncio
async def test_exclude_single_fact_drops_only_that_fact(
    heart_mock, brain_mock, settings_mock,
):
    """exclude_ids={"fact": {<uuid>}} drops just that fact, keeps everything else."""
    target_fact_id = SEEDED_FACT_ID  # one of the known fixture facts
    results, stats = await run_recall_pipeline(
        ..., exclude_ids={"fact": {str(target_fact_id)}},
    )
    assert target_fact_id not in [r.id for r in results]
    assert stats.excluded_in_context == 1
    # Other types untouched
    assert any(r.type == "decision" for r in results)
    assert any(r.type == "episode" for r in results)


@pytest.mark.asyncio
async def test_exclude_type_keyed_no_cross_filter(
    heart_mock, brain_mock, settings_mock,
):
    """Same UUID exists as a fact AND as an episode (test fixture). Excluding
    via "fact" key must drop the fact but keep the episode."""
    shared = uuid4()
    # ... arrange fixture so a fact and an episode both have id=shared
    results, stats = await run_recall_pipeline(
        ..., exclude_ids={"fact": {str(shared)}},
    )
    assert (shared, "fact") not in [(r.id, r.type) for r in results]
    assert (shared, "episode") in [(r.id, r.type) for r in results]
    assert stats.excluded_in_context == 1


@pytest.mark.asyncio
async def test_exclude_total_overlap_returns_empty(
    heart_mock, brain_mock, settings_mock,
):
    """All result IDs are in exclude_ids → returns empty list, counter == n."""
    all_fact_ids = {str(r.id) for r in BASELINE_RESULTS if r.type == "fact"}
    all_decision_ids = {str(r.id) for r in BASELINE_RESULTS if r.type == "decision"}
    results, stats = await run_recall_pipeline(
        ..., exclude_ids={
            "fact": all_fact_ids,
            "decision": all_decision_ids,
        },
    )
    assert results == []
    assert stats.excluded_in_context == len(BASELINE_RESULTS)


@pytest.mark.asyncio
async def test_exclude_ids_with_unknown_type_no_op(
    heart_mock, brain_mock, settings_mock,
):
    """exclude_ids={"chunk": {...}} — F072 territory; v1 just no-ops because
    chunks aren't in TurnContext. Verify no error."""
    results, stats = await run_recall_pipeline(
        ..., exclude_ids={"chunk": {str(uuid4())}},
    )
    # Chunk type doesn't match any tracked TurnContext type → no drops
    # (or: if a chunk with that id existed it would be dropped; pin
    # the test to the no-collision case)
    assert stats.excluded_in_context == 0


@pytest.mark.asyncio
async def test_untyped_result_survives_filter(
    heart_mock, brain_mock, settings_mock,
):
    """Result types `censor` and `chunk` are not in exclude_ids keys — they
    must pass through untouched. Defends against future refactor to a
    stricter lookup. Spec Risks "Type-key mismatch" row."""
    # Seed a chunk-type result; exclude_ids only has fact key
    chunk_id = uuid4()
    seed_chunk_result(chunk_id)  # fixture helper
    results, stats = await run_recall_pipeline(
        ..., exclude_ids={"fact": {str(chunk_id)}},  # same UUID, wrong type
    )
    assert chunk_id in [r.id for r in results if r.type == "chunk"]
    assert stats.excluded_in_context == 0


@pytest.mark.asyncio
async def test_graph_expanded_neighbor_survives_when_seed_excluded(
    heart_mock, brain_mock, settings_mock,
):
    """F022 graph-expanded neighbor has its OWN id, distinct from the seed.
    Excluding the seed must NOT drop the neighbor. Spec Risks row 3."""
    seed_decision = uuid4()
    neighbor_decision = uuid4()
    seed_graph_edge(seed_decision, neighbor_decision)  # fixture helper
    results, stats = await run_recall_pipeline(
        ..., exclude_ids={"decision": {str(seed_decision)}},
    )
    # The neighbor (different id, source=graph_expanded) must remain
    neighbors = [r for r in results if r.source == "graph_expanded"]
    assert any(r.id == neighbor_decision for r in neighbors)
    # The seed itself is gone
    assert all(r.id != seed_decision for r in results)


@pytest.mark.asyncio
async def test_type_collision_stats_counter_correct(
    heart_mock, brain_mock, settings_mock,
):
    """Same UUID present as both fact AND episode. exclude_ids={"fact":{uuid}}
    drops fact only (count=1), not both (would be 2). Confirms type-keying
    of the counter, not just the filter."""
    shared = uuid4()
    seed_fact_and_episode_with_id(shared)  # fixture helper
    results, stats = await run_recall_pipeline(
        ..., exclude_ids={"fact": {str(shared)}},
    )
    assert stats.excluded_in_context == 1
    assert (shared, "episode") in [(r.id, r.type) for r in results]


@pytest.mark.parametrize("variant", [
    "11111111-1111-1111-1111-111111111111",  # canonical lowercase with dashes
    "11111111111111111111111111111111",       # no dashes
    "11111111-1111-1111-1111-111111111111".upper(),  # uppercase
])
@pytest.mark.asyncio
async def test_uuid_canonicalization_via_str_call(
    variant, heart_mock, brain_mock, settings_mock,
):
    """recalled_*_ids carries strings from elsewhere; `str(r.id)` is canonical
    (lowercase with dashes from UUID.__str__). Document by test which forms
    DO and DO NOT match. Spec assumes inputs are canonical; if they aren't,
    set membership silently misses."""
    target = UUID("11111111-1111-1111-1111-111111111111")
    seed_fact_with_id(target)
    results, stats = await run_recall_pipeline(
        ..., exclude_ids={"fact": {variant}},
    )
    canonical = "11111111-1111-1111-1111-111111111111"
    if variant == canonical:
        assert stats.excluded_in_context == 1
    else:
        # Non-canonical forms do NOT match — documented as a precondition,
        # not silently corrected. Callers must pass canonical strings.
        assert stats.excluded_in_context == 0


@pytest.mark.asyncio
async def test_empty_results_after_filter_format_graceful(
    heart_mock, brain_mock, settings_mock,
):
    """When 0 results survive, the formatter must produce a non-empty,
    LLM-comprehensible "no additional results" block — not a bare empty
    string that breaks the tool result content array."""
    from nous.api.tools import _format_pipeline_text
    all_ids = {str(r.id) for r in BASELINE_RESULTS}
    results, stats = await run_recall_pipeline(
        ..., exclude_ids={"fact": all_ids, "decision": all_ids},
    )
    text = _format_pipeline_text(results, stats, ["all"])
    assert text  # not empty
    assert "No results" in text or "no results" in text  # graceful sentinel
```

---

## Step 8 — Integration tests

**Fixture story:** there is **no existing `runner_fixture` or `seed_facts` in `tests/conftest.py`**. The plan's original snippet was a sketch. Two acceptable paths — pick **(B)** for v1:

**(A) Full `AgentRunner` fixture.** Mirror the pattern in `tests/test_runner.py` (whatever it does for runner setup). Cost: ~150 LOC of fixture wiring; covers the real tool-loop dispatch path.

**(B) Synthetic TurnContext path.** Skip the real `AgentRunner.run_turn` and directly call `_build_exclude_ids(synthetic_turn_context)` + `CURRENT_TURN_EXCLUDE_IDS.set(...)` + `run_recall_pipeline(...)`. Captures the wire-level invariants without the fixture cost. **Recommended.**

**Brittle-assertion fix:** never hardcode "loads exactly 3 of 5 facts" — `pre_turn` ranking is non-deterministic across token-budget / staleness boundaries. **Always read `turn_context.recalled_fact_ids` and assert relative to that.**

**New file:** `tests/test_runner_dedup_integration.py`

```python
"""F071 integration: contextvar set → recall_deep → filter wired through."""
import pytest
from uuid import uuid4
from unittest.mock import MagicMock
from nous.api.runner import CURRENT_TURN_EXCLUDE_IDS, _build_exclude_ids
from nous.api.retrieval_pipeline import run_recall_pipeline


def _make_turn_context(fact_ids, decision_ids=None, episode_ids=None, procedure_ids=None):
    """Tiny stand-in for a real TurnContext — only the four list fields the
    feature reads. Avoids pulling in pydantic + the full cognitive stack."""
    tc = MagicMock()
    tc.recalled_fact_ids = list(fact_ids)
    tc.recalled_decision_ids = list(decision_ids or [])
    tc.recalled_episode_ids = list(episode_ids or [])
    tc.recalled_procedure_ids = list(procedure_ids or [])
    return tc


@pytest.mark.asyncio
async def test_flag_off_returns_none(heart_mock, brain_mock):
    """recall_exclude_context_ids=False → _build_exclude_ids returns None →
    pipeline short-circuits → recall_deep result count unchanged."""
    settings = MagicMock(); settings.recall_exclude_context_ids = False
    tc = _make_turn_context([str(uuid4()) for _ in range(3)])
    assert _build_exclude_ids(settings, tc) is None


@pytest.mark.asyncio
async def test_flag_on_drops_overlap_via_contextvar(
    heart_mock, brain_mock, settings_mock,
):
    """End-to-end through contextvar: set CURRENT_TURN_EXCLUDE_IDS, call
    run_recall_pipeline (mimics what recall_deep does), assert overlap dropped."""
    # Read what would-be-in-context first by running pipeline with no filter
    baseline_results, _ = await run_recall_pipeline(
        ..., exclude_ids=None,
    )
    # Take the actual fact IDs that surfaced — guaranteed deterministic relative
    # to themselves; never hardcode N=3 etc.
    overlapping_fact_ids = {
        str(r.id) for r in baseline_results if r.type == "fact"
    }[:2]  # take any 2

    # Set contextvar, run pipeline with those excluded
    settings_mock.recall_exclude_context_ids = True
    tc = _make_turn_context(overlapping_fact_ids)
    token = CURRENT_TURN_EXCLUDE_IDS.set(_build_exclude_ids(settings_mock, tc))
    try:
        filtered_results, stats = await run_recall_pipeline(
            ..., exclude_ids=CURRENT_TURN_EXCLUDE_IDS.get(),
        )
    finally:
        CURRENT_TURN_EXCLUDE_IDS.reset(token)

    surviving = {str(r.id) for r in filtered_results}
    assert overlapping_fact_ids & surviving == set()
    assert stats.excluded_in_context == len(overlapping_fact_ids)


@pytest.mark.asyncio
async def test_recall_deep_closure_reads_contextvar(
    heart_mock, brain_mock, settings_mock,
):
    """recall_deep closure does `from nous.api.runner import
    CURRENT_TURN_EXCLUDE_IDS` lazily, then passes the value to
    run_recall_pipeline. Confirms the wire."""
    from nous.api.tools import register_memory_tools
    settings_mock.recall_exclude_context_ids = True
    # Construct dispatcher with the recall_deep tool registered, invoke it
    # with a known contextvar value, assert run_recall_pipeline received it.
    # Implementation: patch run_recall_pipeline to capture the kwarg and
    # assert it's the expected dict.
    ...
```

---

## Step 9 — Concurrency test

**New file:** `tests/test_runner_dedup_concurrency.py`

```python
"""F071 concurrency: ContextVar isolation across concurrent run_turn calls."""
import asyncio
import pytest
from unittest.mock import MagicMock
from nous.api.runner import CURRENT_TURN_EXCLUDE_IDS, _build_exclude_ids


@pytest.mark.asyncio
async def test_contextvar_per_task_isolation():
    """Two concurrent Tasks each setting CURRENT_TURN_EXCLUDE_IDS don't bleed
    into each other. Uses asyncio.Event for deterministic interleave instead
    of fragile asyncio.sleep granularity on slow CI."""
    observed = {}
    both_set = asyncio.Event()
    second_set_count = {"n": 0}

    async def turn(session_id: str, fact_ids: set[str]):
        token = CURRENT_TURN_EXCLUDE_IDS.set({"fact": fact_ids})
        try:
            # Force interleaving: each turn signals it has set, then waits
            # until BOTH have set before observing.
            second_set_count["n"] += 1
            if second_set_count["n"] == 2:
                both_set.set()
            await both_set.wait()
            observed[session_id] = CURRENT_TURN_EXCLUDE_IDS.get()
        finally:
            CURRENT_TURN_EXCLUDE_IDS.reset(token)

    await asyncio.gather(
        turn("s1", {"a", "b"}),
        turn("s2", {"x", "y", "z"}),
    )

    assert observed["s1"] == {"fact": {"a", "b"}}
    assert observed["s2"] == {"fact": {"x", "y", "z"}}
    assert CURRENT_TURN_EXCLUDE_IDS.get() is None


@pytest.mark.asyncio
async def test_contextvar_reset_on_sync_exception():
    """sync RuntimeError → .reset() fires → var restored to default."""
    async def failing_turn():
        token = CURRENT_TURN_EXCLUDE_IDS.set({"fact": {"x"}})
        try:
            raise RuntimeError("boom")
        finally:
            CURRENT_TURN_EXCLUDE_IDS.reset(token)

    with pytest.raises(RuntimeError):
        await failing_turn()
    assert CURRENT_TURN_EXCLUDE_IDS.get() is None


@pytest.mark.asyncio
async def test_contextvar_reset_on_cancellation():
    """asyncio.CancelledError mid-turn → .reset() still fires. Defends the
    spec's "contextvar leaks across turns" risk for the async path."""
    started = asyncio.Event()

    async def long_turn():
        token = CURRENT_TURN_EXCLUDE_IDS.set({"fact": {"x"}})
        try:
            started.set()
            await asyncio.sleep(10)  # cancellation point
        finally:
            CURRENT_TURN_EXCLUDE_IDS.reset(token)

    task = asyncio.create_task(long_turn())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert CURRENT_TURN_EXCLUDE_IDS.get() is None


def test_build_exclude_ids_flag_off_returns_none():
    """_build_exclude_ids returns None when flag is off — guarantees the
    pipeline `if exclude_ids:` short-circuit fires."""
    s = MagicMock(); s.recall_exclude_context_ids = False
    tc = MagicMock()
    tc.recalled_fact_ids = ["a", "b"]
    tc.recalled_decision_ids = []
    tc.recalled_episode_ids = []
    tc.recalled_procedure_ids = []
    assert _build_exclude_ids(s, tc) is None


def test_build_exclude_ids_flag_on_packs_4_types():
    s = MagicMock(); s.recall_exclude_context_ids = True
    tc = MagicMock()
    tc.recalled_fact_ids = ["a", "b"]
    tc.recalled_decision_ids = ["c"]
    tc.recalled_episode_ids = ["d"]
    tc.recalled_procedure_ids = []
    out = _build_exclude_ids(s, tc)
    assert set(out.keys()) == {"fact", "decision", "episode", "procedure"}
    assert out["fact"] == {"a", "b"}
    assert out["decision"] == {"c"}
    assert out["episode"] == {"d"}
    assert out["procedure"] == set()


def test_build_exclude_ids_no_turn_context_returns_none():
    s = MagicMock(); s.recall_exclude_context_ids = True
    assert _build_exclude_ids(s, None) is None
```

---

## Step 10 — Docs

`CLAUDE.md` — find the env-var table near other `NOUS_RECALL_*` entries, add:

```markdown
| `NOUS_RECALL_EXCLUDE_CONTEXT_IDS` | `false` | F071 — `recall_deep` filters out facts/decisions/episodes/procedures already in the current turn's system prompt (cross-context dedup). Land dark; flip in dev for measurement. |
```

`docs/features/INDEX.md` — add F071 row:

```markdown
| F071 | Cross-context dedup (recall_deep ↔ system prompt) | 📝 Draft | — |
```

---

## Sanity checks before commit

```powershell
# Lint
uv run ruff check nous/ tests/

# Targeted tests
uv run pytest tests/test_retrieval_pipeline_exclusion.py -v
uv run pytest tests/test_runner_dedup_integration.py -v
uv run pytest tests/test_runner_dedup_concurrency.py -v

# Full suite (catch regressions)
uv run pytest tests/ -x --timeout=60

# Snapshot regression: this MUST stay byte-identical post-F071
uv run pytest tests/test_retrieval_pipeline.py::TestFormatPipelineTextSnapshot::test_format_matches_committed_snapshot -v
```

The recall_deep text snapshot at `tests/fixtures/recall_deep_text_snapshot.txt` MUST be byte-identical post-F071. The `exclude_ids=None` default + the `if exclude_ids:` short-circuit guarantee this.

---

## Risk callouts during impl

- **`_caught_exc` re-raise (`runner.py:491`)** is INSIDE the existing inner try/except/else. The F071 outer try wraps that whole block — `.reset()` fires before the re-raise propagates.
- **Subtask / heartbeat paths** route through `AgentRunner.run_turn` (the non-streaming path) — they get coverage from Step 3 automatically.
- **F051 eval harness** calls `run_recall_pipeline` directly outside any `run_turn` (`nous_eval/retrieval_runner.py:504`). `CURRENT_TURN_EXCLUDE_IDS.get()` returns `None` there; pipeline short-circuits; output byte-identical. Verified by `test_exclude_ids_none_byte_identical_baseline`.
- **F055 residual activation** runs `record_surfaced` after `run_recall_pipeline` returns. F071 filters happen before; if all results are excluded, `record_surfaced` records nothing — correct behavior.

---

## Acceptance gate

- [ ] All unit tests green
- [ ] All integration tests green
- [ ] Concurrency tests green
- [ ] Full suite green
- [ ] Lint clean
- [ ] CLAUDE.md env var added
- [ ] INDEX.md row added
- [ ] F072 stub spec exists (already shipped in this branch)
- [ ] Single commit (squashable) with subject `feat(F071): cross-context dedup for recall_deep`
