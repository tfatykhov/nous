# F045 Implementation Plan — CE-Aware Thresholds + Content Guard

**Date:** 2026-04-14
**Spec:** `docs/features/F045-ce-aware-thresholds.md`
**Forge decision:** `7d6fdce9` (lead)

## Why the compressed pipeline

F043 established the pattern (adapter + _ENTITY_CONFIG + threshold lookup). F045 refines that pattern with two small changes that were **empirically validated live** by the 22:01 sleep cycle A/B on `192.168.1.141`. The change is ~112 LOC, touches no new infrastructure, and has zero new dependencies.

Single consolidated review instead of a 3-agent team — the failure modes the 3-agent team would catch (return-contract bugs, signature drift, sum-pollution) don't apply here because we're not changing any return shapes or reducer paths.

## Changes

### 1. `nous/config.py` — 7 new fields

Place them in the F043 config block, after the existing `ce_backfill_*` settings:

```python
# F045: CE-aware relaxed thresholds (apply only when ce_backfill_enabled=True).
# Defaults derived from the 22:01 A/B experiment (fact-fact=0.65 validated at
# 80% LLM-judged precision) and histogram modal-peak analysis for other types.
ce_backfill_threshold_fact_fact: float = 0.65
ce_backfill_threshold_fact_decision: float = 0.55
ce_backfill_threshold_fact_episode: float = 0.55
ce_backfill_threshold_decision_decision: float = 0.60
ce_backfill_threshold_episode_episode: float = 0.58
ce_backfill_threshold_procedure_any: float = 0.55

# F045: content-length guard — drops URL-only / boilerplate facts before CE.
ce_backfill_min_content_chars: int = 80
```

### 2. `nous/brain/graph_densifier.py` — split `_get_threshold`

Find the existing `_get_threshold` helper (module-level function near the top, around line 55) and replace it with this:

```python
def _get_strict_threshold(settings, source_type: str, target_type: str) -> float:
    """Strict per-relation cosine thresholds — used when ce_backfill is disabled."""
    pair = tuple(sorted([source_type, target_type]))
    if "procedure" in pair:
        return float(settings.graph_threshold_procedure_any)
    mapping = {
        ("fact", "fact"): settings.graph_threshold_fact_fact,
        ("decision", "fact"): settings.graph_threshold_fact_decision,
        ("episode", "fact"): settings.graph_threshold_fact_episode,
        ("decision", "decision"): settings.graph_threshold_decision_decision,
        ("episode", "episode"): settings.graph_threshold_episode_episode,
    }
    return float(mapping.get(pair, 0.75))


def _get_ce_mode_threshold(settings, source_type: str, target_type: str) -> float:
    """Relaxed thresholds for when CE is upstream and has already pruned candidates."""
    pair = tuple(sorted([source_type, target_type]))
    if "procedure" in pair:
        return float(settings.ce_backfill_threshold_procedure_any)
    mapping = {
        ("fact", "fact"): settings.ce_backfill_threshold_fact_fact,
        ("decision", "fact"): settings.ce_backfill_threshold_fact_decision,
        ("episode", "fact"): settings.ce_backfill_threshold_fact_episode,
        ("decision", "decision"): settings.ce_backfill_threshold_decision_decision,
        ("episode", "episode"): settings.ce_backfill_threshold_episode_episode,
    }
    return float(mapping.get(pair, 0.70))


def _get_threshold(settings, source_type: str, target_type: str) -> float:
    """Resolve cosine threshold: CE-mode if ce_backfill_enabled, else strict."""
    if getattr(settings, "ce_backfill_enabled", False):
        return _get_ce_mode_threshold(settings, source_type, target_type)
    return _get_strict_threshold(settings, source_type, target_type)
```

All existing callers of `_get_threshold(settings, ...)` stay unchanged — they just start picking the right branch automatically based on the flag.

### 3. `nous/brain/backfill_rerank.py` — content guard

In `ce_rerank_backfill_candidates`, inside the wrap loop:

```python
min_chars = int(getattr(settings, "ce_backfill_min_content_chars", 80))

wrapped: list[RerankCandidate] = []
for cand_id, rrf in candidate_rows:
    content = content_map.get(cand_id, "")
    if not content:
        continue
    if len(content.strip()) < min_chars:
        continue  # F045: skip URL-only / boilerplate facts
    wrapped.append(RerankCandidate(id=cand_id, content=content, score=float(rrf)))
```

### 4. Tests

**`tests/test_backfill_rerank.py`** — add:

- `test_content_guard_drops_short` — 40-char content dropped, long content proceeds (and CE runs on it).
- `test_content_guard_respects_whitespace` — `"   short   "` (10 chars after strip) dropped.
- `test_content_guard_configurable` — `ce_backfill_min_content_chars=200` drops medium-length content.
- `test_content_guard_runs_before_ce` (**P2-1**) — 2 candidates, one 40-char and one 200-char; install the fake CE model; run `ce_rerank_backfill_candidates`; assert the 40-char candidate's id is **not** in `fake.pairs_seen` — proving the guard runs before `cross_encoder_rerank` inference, not just that it filters the output.

All use `make_settings()` (SimpleNamespace kwargs) + `install_fake` (correct fixture name) from the F043 test file.

**`tests/test_graph_densifier.py`** — add:

- `test_get_threshold_ce_mode` — set `ce_backfill_enabled=True` on a settings copy, call `_get_threshold(settings, "fact", "fact")`, assert result equals `settings.ce_backfill_threshold_fact_fact` (0.65). Repeat for each relation pair.
- `test_get_threshold_strict_mode` — with `ce_backfill_enabled=False`, assert result equals `settings.graph_threshold_fact_fact` (0.82). Regression guard.
- `test_backfill_uses_ce_mode_threshold_end_to_end` (**P2-3**) — `@pytest.mark.postgres_only` integration: seed 2 near-duplicate facts whose cosine similarity is `~0.70` (below strict 0.82, above CE-mode 0.65), enable `ce_backfill_enabled=True`, install the fake CE model that returns high raw logits for both, call `backfill_orphan_facts(max_count=1)`, assert exactly one edge is created. This catches any future refactor that bypasses `_get_threshold`.

Import helpers: `from nous.brain.graph_densifier import _get_threshold, _get_ce_mode_threshold, _get_strict_threshold`.

### 5. Docs

- **`CLAUDE.md`** — add 7 env var rows (`NOUS_CE_BACKFILL_THRESHOLD_*` + `NOUS_CE_BACKFILL_MIN_CONTENT_CHARS`) after the existing `NOUS_CE_BACKFILL_*` rows. **Migration note (P2-2)**: add a bold paragraph immediately under the new rows stating: "When `NOUS_CE_BACKFILL_ENABLED=true`, `NOUS_GRAPH_THRESHOLD_*` env overrides are ignored — `_get_threshold` routes to the `ce_backfill_threshold_*` set instead. Operators upgrading from an F043-only deployment with `NOUS_GRAPH_THRESHOLD_FACT_FACT` overridden must re-set the equivalent `NOUS_CE_BACKFILL_THRESHOLD_FACT_FACT` to keep their value in effect." Add F045 shipped-table row.
- **`docs/features/INDEX.md`** — F045 row matching F043/F044 format.
- **`docs/features/F045-*.md`** — Status: Draft → Shipped after merge.

## Sequencing (single session, lead-only)

1. Branch `feat/f045-ce-aware-thresholds` ✓ (already created)
2. Commit spec + plan
3. Implement in order: config.py → graph_densifier.py → backfill_rerank.py
4. Add tests
5. Run tests locally (venv python)
6. Run code-reviewer subagent on the diff
7. Address P1/P2 findings inline
8. Commit implementation
9. Update CLAUDE.md + INDEX.md + spec Status
10. Commit docs
11. Push, open PR, wait for CI
12. Merge, sync main, finalize forge outcome

## Risks

Only one I'm worried about: **the other 5 non-fact-fact thresholds are not empirically validated** — I picked them from histogram-estimate and the fact that they're ~17pp below the strict defaults (same offset the empirically-validated fact_fact was). If fact↔episode linking starts producing bad edges at 0.55, operators can override via env var. Phase 2 would run the same A/B experiment per relation type and tune each one.

## Out-of-scope

- Runtime config override (follow-up, mirror F042's pattern if needed)
- Auto-tuning threshold based on LLM-judge precision (observability work)
- F042 Phase 3 CE-distilled embedding model (multi-week effort)
