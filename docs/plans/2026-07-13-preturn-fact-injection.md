# Pre-Turn Fact Injection Fixes — Implementation Plan (v2, post-review)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **Review status:** v1 reviewed by 3 agents (architecture / phantom-API / test-eval) on 2026-07-13. All P1+P2 findings incorporated in this v2: lineage is now a dict parameter (never a mutation — `_ScoredWrapper.__slots__` kills attribute writes), pin capture moved after `_resolve_recency` + superseded-skip, probe replays the intent plan, Task 7 rewritten around `qa_context_ab.py`, `DEFAULT_FETCH_LIMITS` corrected to 15, staleness documented as a phantom gate for facts, `FrameSelection`/`FactManager`/session-idiom/EvalSettings signatures corrected, build()-level golden written on HEAD first.

**Goal:** Make pre-turn ("Block-2") fact injection reliable enough that a stored counterfactual fact reaches the model on question turns — closing the coin-flip failure chain measured in the 6k LongMemEval investigation — plus fix the 200-char render truncation that makes injected facts unreadable.

**Architecture:** Flag-gated changes to the pre-turn context path (`nous/cognitive/context.py`), each defaulting OFF/byte-identical: (1) a diagnostic probe that attributes WHICH pipeline gate drops a gold fact, replaying the real intent-plan gate; (2) configurable fact render depth (`_format_facts`); (3) a top-K "fact pin" that exempts the strongest direct search hits from the demotion pipeline (captured post-recency-resolve); (4) supersession lineage annotation threaded as a dict into rendering (column-based, two phrasings for A/B); (5) an empty-facts recall-backstop instruction. A final A/B via the `qa_context_ab.py` harness (real `ContextEngine.build` path, prod Opus generator) gates any flag flip.

**Tech Stack:** Python 3.12+, pydantic-settings, async SQLAlchemy 2.0, pytest + pytest-asyncio (`asyncio_mode = "auto"` — no `@pytest.mark.asyncio` needed), AsyncMock-style unit tests per `tests/test_f038_context_fixes.py` conventions, eval DB `:5433`.

## Global Constraints

- **Every new setting defaults to current behavior.** With defaults, `ContextEngine.build()` output must be byte-identical to HEAD. Task 2 Step 1 captures a build()-level golden ON UNMODIFIED HEAD before any implementation; Task 6 re-runs it.
- **All new settings use the `NOUS_` env prefix** (automatic via pydantic-settings) and get one row each in the CLAUDE.md env table (Task 6).
- **Tests construct `Settings(_env_file=None, ...)`** — `Settings` loads `.env` by default (config.py:20), which would make default-assertions environment-dependent.
- **No prod-behavior flag flips in this plan.** Flips are gated on Task 7's A/B (prod Opus generator, n≥100 non-regression + counterfactual diagnostic, per `feedback-eval-prod-generator`).
- **Work happens on a fresh branch `feat/preturn-fact-injection` cut from `origin/main`**, in a fresh worktree (create via superpowers:using-git-worktrees at execution start). Do NOT reuse the `plan12-graph-seed-score` worktree. (Verified: current worktree HEAD is identical to `origin/main` on every touched path.)
- **Subagent dispatch preamble:** every subagent must `cd` to the new worktree and verify `git branch --show-current` prints `feat/preturn-fact-injection` before touching files (subagents default to the MAIN checkout — known footgun).
- Commit style: `feat:` / `fix:` / `test:` / `docs:` prefixes, one logical change per commit.
- Run tests with `uv run pytest <file> -v` from the worktree root.

## Design Rationale (context for reviewers)

The 6k investigation showed: for "Who performed Past Masters?" (stored counterfactual: Madonna), the fact exists and `recall_deep` retrieves it in one hop, but pre-turn injection missed it (0 mentions in system prompt), so answering depends on a stochastic tool-call election. Code verification found both paths share the same `hybrid_search` fact leg — the pre-turn miss must come from one of these gates, in `ContextEngine.build()` (nous/cognitive/context.py:619-676) order:

0. **Intent-plan query rewrite + limit** (nous/cognitive/intent.py:199-207, 222) — prod `pre_turn` always builds a `RetrievalPlan`; the fact leg then searches with a *keyword bag* (`" ".join(signals.topic_keywords)` — for "Who performed Past Masters?" something like `"performed masters past"`), NOT the raw question, and with the plan's limit (5 uniform, or 3 when hints point at another type). `recall_deep` searches the raw query. **This rewrite is itself a prime suspect for the 6k miss.**
1. **Fetch limit** — module default `DEFAULT_FETCH_LIMITS["fact"] = 15` (context.py:83-85), but the *effective* prod limit is the intent plan's 5 or 3 (gate 0). recall_deep fetches 10+.
2. ~~Staleness penalty~~ — **phantom gate for facts**: `FactSummary` carries no `created_at` (heart/schemas.py:195-218), so `_apply_staleness_penalty` (context.py:1169-1171) early-continues for every fact. Listed so probe output isn't misread.
3. **Frame boost / diversity (max 2 per subject) / conversation-dedup / usage boost** (:640-649). Conversation-dedup CAN empty the whole list (:1082-1085).
4. **Adaptive relevance filter** (`_apply_relevance_filter`, :1112) — gap-cut at score drops, min 3 / max 12 facts.
5. **Budget truncation** (`_truncate_to_budget`, :1293) — question frame gets 1500 fact tokens; conversation frame only 500 (schemas.py:135-136).
6. **Render truncation** — even a surviving fact renders at most its first 200 chars (`_format_facts`, :1374).

Task 1 identifies which gate(s) actually fired. Tasks 2-5 are the remedies; pin (Task 3) is a *holdout-and-reinsert* around the demotion pipeline — captured AFTER `_resolve_recency` so it can never resurrect a fact the recency resolver demoted as superseded (and `_reinsert_pinned` additionally skips `recency_status == "superseded"` items, belt-and-braces). The pin cannot resurrect write-side-superseded facts either: `apply_supersession_filter` runs inside `search()` before the pin sees anything (nous/heart/facts.py:359-390).

Supersession lineage (Task 4) reads the `heart.facts.superseded_by` **column** (authoritative; graph edges historically lagged it — PR #518: 261 column-writes vs 2 edges) and is threaded into `_format_facts` as a **dict parameter, never an attribute mutation**: after `apply_frame_boost`/`_apply_usage_boost`, facts are `_ScoredWrapper` objects with `__slots__ = ("_item", "_score")` (nous/heart/search.py:441-452) — attribute writes raise `AttributeError` (attribute *reads* delegate to the wrapped item and are safe). The "named" lineage mode injects the stale value into context, which is theoretically an inoculation but carries anchoring risk — hence two modes and an A/B, never a default-on.

Known accepted seam (documented, not fixed here): `recalled_fact_ids` is collected from the post-filter list while `_truncate_to_budget` cuts the rendered *text* from the tail — a tail fact can be recorded-but-not-shown (F071 interaction, pre-existing). The pin itself is safe (pinned facts render at the FRONT), but pin+FULL_TOP_N grows the list/head and can push MORE tail facts past the budget. Task 7 checks this empirically; if it matters, the fix is the B-cog-A discipline already used for procedures (context.py:722-736) as a follow-up. Also: pinned facts recorded in `recalled_ids` every turn accrue retrieved-but-not-referenced usage penalties — self-limiting (the pin bypasses usage boost) but it degrades the fact's *unpinned* ranking over time; acceptable for an A/B-gated feature.

Prior evidence constraining this plan: injection *precision* is a non-lever on the prod model (n=101, delta 0.000) but that investigation's own conclusion was that *coverage* is the real lever; Opus is robust to extra low-relevance injected facts (supports pin's failure mode being cheap); A/B signs flip between Sonnet and Opus generators (mandates prod-generator validation).

---

### Task 1: Diagnostic probe — which gate drops the gold fact?

**Files:**
- Create: `scripts/diag/probe_preturn_fact_gate.py`
- Create: `scripts/diag/README_preturn_probe.md` (3-line usage note)

**Interfaces:**
- Consumes: `nous_eval.retrieval_runner._settings_for_eval_db` (:286), `_build_heart_for_eval` (async context manager, :316) and `_build_brain_for_eval` (:427); `nous.cognitive.intent.IntentClassifier` (`classify(input_text, frame) -> IntentSignals`, `plan_retrieval(signals, input_text) -> RetrievalPlan`); `nous.cognitive.context.ContextEngine` private stage methods; `nous.cognitive.schemas.ContextBudget/FrameSelection`.
- Produces: a per-question stage-attribution table on stdout. No prod code changes. Findings go into the PR description and decide the conditional steps in Task 3.

**Note:** Diagnostic script, not TDD-able prod code — no unit tests; the "test" is running it against the eval DB. Requires `OPENAI_API_KEY` (asserted — FTS-only ranks would silently invalidate the attribution) and a fully-migrated eval DB (the heart builder runs a schema preflight).

- [ ] **Step 1: Write the probe script**

```python
"""Probe: which pre-turn gate drops a gold fact?

For each question in a JSONL file ({"question": ..., "gold": <content substring>}),
replays ContextEngine.build()'s fact pipeline stage by stage against the eval DB —
INCLUDING the intent-plan query rewrite prod actually applies — and reports the
gold fact's rank/score after each stage, or the stage that dropped it. Read-only.

Fidelity caveats (prod build() differs in ways this probe cannot replay):
- usage-boost stage omitted (needs a live per-session tracker);
- frame boost runs WITHOUT live censor names (censor-overlap boost can reorder);
- conversation-dedup is a NO-OP here (no deduplicator, empty history);
- settings.context_budget_overrides not re-applied after plan overrides.
A prod drop caused by any of these will show as SURVIVES here — treat SURVIVES
as "not attributable to the replayed gates", not as proof of prod injection.

Usage:
  uv run python scripts/diag/probe_preturn_fact_gate.py \
      --questions failing_questions.jsonl --frame question --agent-id nous-default

DB comes from NOUS_EVAL_DB_* env (nous_eval.config.EvalSettings defaults:
localhost:5433/nous_eval).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from nous.cognitive.context import (
    ContextEngine,
    DEFAULT_FETCH_LIMITS,
    TIER1_FACT_CATEGORIES,
    apply_frame_boost,
)
from nous.cognitive.intent import IntentClassifier
from nous.cognitive.schemas import ContextBudget, FrameSelection
from nous.config import Settings
from nous_eval.config import EvalSettings
from nous_eval.retrieval_runner import (
    _build_brain_for_eval,
    _build_heart_for_eval,
    _settings_for_eval_db,
)
from nous.storage.database import Database


def _gold_pos(facts: list, gold: str) -> tuple[int, float] | None:
    for i, f in enumerate(facts):
        if gold.lower() in (getattr(f, "content", "") or "").lower():
            return i + 1, float(getattr(f, "score", 0) or 0)
    return None


async def probe_one(engine: ContextEngine, heart, classifier: IntentClassifier,
                    question: str, gold: str, frame: FrameSelection) -> None:
    print(f"\n=== {question!r} (gold: {gold!r}) ===")

    # Gate 0: the intent plan — prod rewrites the query and sets the limit.
    signals = classifier.classify(question, frame)
    plan = classifier.plan_retrieval(signals, question)
    if "fact" in plan.skip_types:
        print("  DROP @ intent-plan: fact retrieval SKIPPED entirely for this input")
        return
    fact_q = next((q for q in plan.queries if q.memory_type == "fact"), None)
    q_text = fact_q.query_text if fact_q else question
    limit = fact_q.limit if fact_q else DEFAULT_FETCH_LIMITS.get("fact", 15)
    budget = ContextBudget.for_frame(frame.frame_id)
    if plan.budget_overrides:
        budget.apply_overrides(plan.budget_overrides)
    print(f"  intent-plan: query_text={q_text!r} limit={limit} "
          f"facts_budget={budget.facts}")

    # Rank under the RAW question (what recall_deep would search) vs the
    # REWRITTEN query (what prod pre-turn searches) — isolates gate 0.
    raw_wide = await heart.search_facts(question, limit=50,
                                        exclude_categories=TIER1_FACT_CATEGORIES)
    rewritten_wide = await heart.search_facts(q_text, limit=50,
                                              exclude_categories=TIER1_FACT_CATEGORIES)
    raw_pos = _gold_pos(raw_wide, gold)
    rw_pos = _gold_pos(rewritten_wide, gold)
    print(f"  raw-query rank(50): {raw_pos}  rewritten-query rank(50): {rw_pos}")
    if rw_pos is None:
        if raw_pos is not None:
            print("  DROP @ query-rewrite: raw query finds gold, keyword-bag rewrite loses it")
        else:
            print("  DROP @ raw-search: gold not findable under either query (write-path problem)")
        return
    if rw_pos[0] > limit:
        print(f"  DROP @ fetch-limit: rewritten rank {rw_pos[0]} > plan limit {limit}")
        # continue with a wide slice to see whether later gates would ALSO kill it
    facts = rewritten_wide[:max(limit, rw_pos[0])]

    stages = [
        ("recency-resolve", lambda fs: engine._resolve_recency(fs)),
        # staleness omitted: phantom for facts (FactSummary has no created_at)
        ("frame-boost", lambda fs: apply_frame_boost(fs, frame.frame_id, [])),
        ("diversity", lambda fs: engine._enforce_diversity(fs, "subject", max_per_subject=2)),
    ]
    for name, fn in stages:
        facts = fn(facts)
        pos = _gold_pos(facts, gold)
        if pos is None:
            print(f"  DROP @ {name}")
            return
        print(f"  after {name}: rank={pos[0]} score={pos[1]:.4f}")

    # conversation-dedup is a NO-OP here (no deduplicator, empty history) —
    # a prod drop at that stage is NOT attributable by this probe.
    facts = await engine._apply_dedup(facts, [], "content")

    facts = engine._apply_relevance_filter(facts, "fact")
    pos = _gold_pos(facts, gold)
    if pos is None:
        print("  DROP @ relevance-filter (gap-cut / max_k=12)")
        return
    print(f"  after relevance-filter: rank={pos[0]}")

    text = engine._format_facts(facts)
    text = engine._truncate_to_budget(text, engine._scaled_budget(budget.facts))
    if gold.lower() not in text.lower():
        print("  DROP @ budget-or-render-truncation (survived pipeline, cut from text)")
        return
    print("  SURVIVES to injected text ✓")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--questions", required=True)
    ap.add_argument("--frame", default="question",
                    choices=["question", "conversation", "task", "decision"])
    ap.add_argument("--agent-id", default=None)
    args = ap.parse_args()

    settings = _settings_for_eval_db(EvalSettings(), Settings())
    if args.agent_id:
        settings.agent_id = args.agent_id
    db = Database(settings)
    await db.connect()
    try:
        async with _build_heart_for_eval(db, settings) as heart:
            assert heart._embeddings is not None, (
                "OPENAI_API_KEY required — FTS-only ranks would invalidate attribution"
            )
            brain = _build_brain_for_eval(db, settings, heart._embeddings)
            engine = ContextEngine(brain, heart, settings, identity_prompt="")
            classifier = IntentClassifier(settings)
            frame = FrameSelection(
                frame_id=args.frame, frame_name=args.frame.title(),
                description="probe", confidence=1.0, match_method="probe",
            )
            print(f"flags: recency_resolver={settings.recency_resolver_enabled} "
                  f"relevance_floor={settings.relevance_floor_enabled} "
                  f"drop_ratio={settings.relevance_drop_ratio} "
                  f"staleness={settings.staleness_penalty_enabled} (phantom for facts)")
            for line in Path(args.questions).read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                q = json.loads(line)
                await probe_one(engine, heart, classifier, q["question"], q["gold"], frame)
    finally:
        await db.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
```

**Implementer notes (verify at execution, adjust surgically):**
- All imports/signatures above were verified against HEAD by the v1 review (`_settings_for_eval_db` retrieval_runner.py:286, `Database(settings)` database.py:17, `heart._embeddings` heart.py:83, `FrameSelection` required fields schemas.py:89-99, `IntentClassifier` intent.py:95-155). If anything drifted since, fix names surgically — do not restructure.
- Match prod flag state when probing (e.g. `NOUS_RECENCY_RESOLVER_ENABLED=true` if prod has it on) — the printed `flags:` line is there to catch mismatches.

- [ ] **Step 2: Run it against the eval DB with the failing counterfactual questions**

The questions file comes from the 6k-investigation failure list (operator provides; e.g. `{"question": "Who performed Past Masters?", "gold": "Madonna"}` plus the other ~11 flips). Run once with `--frame question` and once with `--frame conversation`.

Run: `uv run python scripts/diag/probe_preturn_fact_gate.py --questions <file> --frame question`
Expected: a `DROP @ <stage>` or `SURVIVES` line per question. Record the distribution.

- [ ] **Step 3: Write findings + decision gate into the PR description**

Map outcomes → remedies:
- `DROP @ query-rewrite` → enable Task 3's conditional Step 6b (`fact_query_use_raw_input`). **The pin alone cannot fix this** — the gold fact is never in the fetched set.
- `DROP @ fetch-limit` → enable Task 3's conditional Step 6a (`fact_fetch_limit_override`).
- `DROP @ diversity/relevance-filter` → Task 3 pin covers it.
- `DROP @ budget-or-render` → Task 2 covers it.
- `DROP @ intent-plan (skipped)` / `DROP @ raw-search` → out of scope here (frame/intent or write-path issue — file a finding, don't expand this plan).
- Prod drops at conversation-dedup are NOT attributable by this probe (no-op offline) — if all stages SURVIVE locally but prod misses, dedup is the residual suspect; note it rather than guessing.

- [ ] **Step 4: Commit**

```bash
git add scripts/diag/probe_preturn_fact_gate.py scripts/diag/README_preturn_probe.md
git commit -m "feat(diag): pre-turn fact-gate attribution probe (replays intent plan)"
```

---

### Task 2: Configurable fact render depth (+ HEAD golden first)

**Files:**
- Create: `tests/test_preturn_fact_injection.py` (used by Tasks 2-5)
- Modify: `nous/config.py` (add 2 settings near `relevance_drop_ratio`, config.py:191)
- Modify: `nous/cognitive/context.py:1361-1387` (`_format_facts`) + call site `:664`
- Modify: `tests/test_context.py` `_make_context_engine_light` (:442-448) — **required**: it builds the engine with `settings = MagicMock()`; the new `getattr(self._settings, "fact_format_max_chars", 200)` would return a MagicMock and `len(content) > max_len` raises TypeError, erroring every `test_format_facts_*` test. Switch it to a real `Settings(_env_file=None)` (pattern: `tests/test_f038_context_fixes.py:50`). This edit is licensed by the plan; note it in the commit message.

**Interfaces:**
- Consumes: existing `_format_facts(self, facts: list) -> str`.
- Produces: `_format_facts(self, facts: list, *, full_top_n: int = 0, lineage: dict[str, list[str]] | None = None) -> str` (the `lineage` param is added here as a no-op placeholder consumed by Task 4 — declare it now so the signature changes once); settings `fact_format_max_chars: int = 200`, `fact_format_full_top_n: int = 0`. Tasks 3-5 tests reuse `FakeFact` + `_make_engine` defined here.

- [ ] **Step 1: On UNMODIFIED HEAD, write the build()-level golden + unit tests**

Create `tests/test_preturn_fact_injection.py`. The golden test MUST be written, run, and committed against HEAD **before** any implementation — it is the byte-identity oracle for Tasks 2-5.

```python
"""Tests for pre-turn fact-injection fixes (render depth, pin, lineage, backstop).

The build()-level golden below was captured on unmodified HEAD and guards the
Global Constraint that all new flags default to byte-identical output.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from nous.cognitive.context import ContextEngine
from nous.cognitive.schemas import FrameSelection
from nous.config import Settings


class FakeFact:
    def __init__(self, content="", subject=None, confidence=1.0, score=None,
                 id=None, superseded_by=None, category=None, source=None):
        self.content = content
        self.subject = subject
        self.confidence = confidence
        self.score = score
        self.id = id or ""
        self.superseded_by = superseded_by
        self.category = category
        self.source = source
        self.recency_status = None
        self.recency_date = None


def _make_engine(**settings_kwargs) -> ContextEngine:
    brain = AsyncMock()
    brain.embeddings = MagicMock()
    heart = AsyncMock()
    settings = Settings(_env_file=None, **settings_kwargs)
    return ContextEngine(brain, heart, settings, identity_prompt="")


def _frame(frame_id="question"):
    return FrameSelection(
        frame_id=frame_id, frame_name=frame_id.title(),
        description="test", confidence=0.9, match_method="pattern",
    )


def _stub_heart_for_build(engine, facts):
    heart = engine._heart
    heart.search_facts.return_value = facts
    heart.list_censors.return_value = []
    heart.list_facts_by_category.return_value = []
    heart.get_working_memory.return_value = None
    heart.search_episodes.return_value = []
    heart.list_episodes.return_value = []
    heart.list_procedures.return_value = ([], 0)
    engine._brain.query.return_value = []


GOLDEN_FACTS = [
    FakeFact(content="A" * 150 + " midpoint marker " + "B" * 150,
             subject="long-one", confidence=1.0, score=0.9, id="f1"),
    FakeFact(content="short fact two", subject="short-two", confidence=0.8,
             score=0.5, id="f2"),
    FakeFact(content="short fact three", subject=None, confidence=0.7,
             score=0.4, id="f3"),
]

# Captured on unmodified HEAD (see Step 2). Paste the printed value here.
GOLDEN_RELEVANT_FACTS = "<CAPTURE-ON-HEAD>"


async def test_build_relevant_facts_golden_default_settings():
    """Byte-identity oracle: default Settings must reproduce HEAD's exact
    Relevant Facts section content for a fixed fact set."""
    engine = _make_engine()
    _stub_heart_for_build(engine, list(GOLDEN_FACTS))
    result = await engine.build(
        agent_id="a", session_id="s",
        input_text="what do we know about the long one?", frame=_frame(),
    )
    section = next(s for s in result.sections if s.label == "Relevant Facts")
    assert section.content == GOLDEN_RELEVANT_FACTS
```

Then the Task 2 unit tests in the same file:

```python
LONG = "A" * 150 + " midpoint marker " + "B" * 150  # 317 chars


class TestFactRenderDepth:
    def test_default_truncates_at_200_word_boundary(self):
        engine = _make_engine()
        out = engine._format_facts([FakeFact(content=LONG)])
        assert "..." in out
        assert "midpoint marker" in out          # 200 chars reaches past the As
        assert "B" * 100 not in out              # tail cut

    def test_max_chars_setting_raises_cap(self):
        engine = _make_engine(fact_format_max_chars=1000)
        out = engine._format_facts([FakeFact(content=LONG)])
        assert "B" * 150 in out                  # full content survives
        assert "..." not in out

    def test_full_top_n_renders_head_untruncated(self):
        engine = _make_engine()  # max_chars stays 200
        facts = [FakeFact(content=LONG, subject="first"),
                 FakeFact(content=LONG, subject="second")]
        out = engine._format_facts(facts, full_top_n=1)
        first_line, second_line = out.splitlines()
        assert "B" * 150 in first_line           # rank 1: full
        assert "..." in second_line              # rank 2: default cap

    def test_default_output_byte_identical_to_legacy(self):
        engine = _make_engine()
        f = FakeFact(content="short fact", subject="subj", confidence=0.93)
        assert engine._format_facts([f]) == "- [subj] short fact [confidence: 0.93]"
```

- [ ] **Step 2: Capture the golden ON HEAD, verify it passes, commit the test file alone**

Temporarily set `GOLDEN_RELEVANT_FACTS = ""`, run the golden test with `-s` and a `print(repr(section.content))` before the assert, paste the printed value into the constant, remove the print, re-run.

Run: `uv run pytest tests/test_preturn_fact_injection.py -v`
Expected: golden test PASSES on HEAD; `test_max_chars_setting_raises_cap` FAILS (setting doesn't exist yet — note `Settings` has `extra="ignore"`, so the unknown kwarg is silently dropped and the failure is the still-truncated output, NOT a ValidationError); `test_full_top_n_renders_head_untruncated` FAILS with `TypeError: unexpected keyword argument`.

```bash
git add tests/test_preturn_fact_injection.py
git commit -m "test(context): HEAD golden + failing tests for fact render depth"
```

- [ ] **Step 3: Add the settings to `nous/config.py`**

Insert after `relevance_max_results` (config.py:196):

```python
    # Pre-turn fact render depth (2026-07-13 plan). Defaults preserve the
    # legacy hardcoded 200-char cap byte-for-byte. NOTE: max_chars is read
    # inside _format_facts, so raising it also affects the User Profile
    # section (shared formatter) — intended.
    fact_format_max_chars: int = Field(
        default=200, ge=50,
        description="Per-fact char cap in pre-turn context rendering (_format_facts). Was hardcoded 200.",
    )
    fact_format_full_top_n: int = Field(
        default=0, ge=0,
        description="Render the top-N facts in the Relevant Facts section untruncated (0 = all capped).",
    )
```

- [ ] **Step 4: Modify `_format_facts` (context.py:1361) and the call site**

```python
    def _format_facts(
        self,
        facts: list,
        *,
        full_top_n: int = 0,
        lineage: dict[str, list[str]] | None = None,
    ) -> str:
        """Format facts for context.

        Format: - [subject]: content_truncated [confidence: N.NN]
        Truncates content at fact_format_max_chars (word boundary); the first
        ``full_top_n`` facts render untruncated. ``lineage`` maps str(fact.id)
        -> superseded contents (consumed by the supersession-lineage feature;
        passed as a dict because pipeline items may be _ScoredWrapper objects
        whose __slots__ forbid attribute writes).
        """
        lines = []
        max_len = getattr(self._settings, "fact_format_max_chars", 200)
        for idx, f in enumerate(facts):
            content = getattr(f, "content", "")
            conf = getattr(f, "confidence", 1.0)
            subject = getattr(f, "subject", None)

            # Truncate at word boundary (top-N exempt)
            if idx >= full_top_n and len(content) > max_len:
                truncated = content[:max_len].rsplit(" ", 1)[0]
                content = truncated + "..."

            # Gap-2: recency tag (current/superseded) when the pre-turn resolver ran.
            status = getattr(f, "recency_status", None)
            rtag = f" [{status} {getattr(f, 'recency_date', '') or ''}]".rstrip() if status else ""

            if subject:
                lines.append(f"- [{subject}] {content}{rtag} [confidence: {conf:.2f}]")
            else:
                lines.append(f"- {content}{rtag} [confidence: {conf:.2f}]")
        return "\n".join(lines)
```

(The `lineage` parameter is intentionally unused until Task 4 — declared now so the signature changes once.)

Update the Relevant Facts call site (context.py:664):

```python
                    facts_text = self._format_facts(
                        facts,
                        full_top_n=getattr(self._settings, "fact_format_full_top_n", 0),
                    )
```

The User Profile call site (context.py:494) stays `self._format_facts(profile_facts)`.

- [ ] **Step 5: Fix `tests/test_context.py::_make_context_engine_light`**

Replace `settings = MagicMock()` with `settings = Settings(_env_file=None)` (add the import if missing). Do not touch anything else in that file.

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_preturn_fact_injection.py tests/test_context.py tests/test_f038_context_fixes.py -v`
Expected: all PASS, including the HEAD golden (defaults unchanged) and the pre-existing `test_format_facts_*` tests (now on real Settings).

- [ ] **Step 7: Commit**

```bash
git add nous/config.py nous/cognitive/context.py tests/test_preturn_fact_injection.py tests/test_context.py
git commit -m "feat(context): configurable fact render depth (NOUS_FACT_FORMAT_MAX_CHARS, NOUS_FACT_FORMAT_FULL_TOP_N)"
```

---

### Task 3: Fact pin — top-K direct hits bypass the demotion pipeline

**Files:**
- Modify: `nous/config.py` (1 setting, same block as Task 2's)
- Modify: `nous/cognitive/context.py:619-676` (fact section of `build()`)
- Test: `tests/test_preturn_fact_injection.py` (extend)

**Interfaces:**
- Consumes: the fact section pipeline in `build()`; `FakeFact`/`_make_engine`/`_frame`/`_stub_heart_for_build` from Task 2.
- Produces: setting `fact_pin_top_k: int = 0`; method `_reinsert_pinned(pinned: list, survivors: list) -> list`. Behavior: the top-K facts by post-recency-resolve order are guaranteed to appear in the injected list (at the front if the pipeline dropped them), EXCEPT facts tagged `recency_status == "superseded"`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_preturn_fact_injection.py`:

```python
class TestFactPin:
    """fact_pin_top_k guarantees top-K search hits survive the pipeline.

    NOTE: the relevance filter alone can't drop rank-1/2 facts (min_k=3 floor);
    the drops the pin repairs come from diversity, conversation-dedup, and the
    gap-cut at deeper ranks (staleness is a phantom for facts — FactSummary has
    no created_at). These tests exercise _reinsert_pinned directly with
    explicit dropped-survivor configurations, plus a build()-level wiring test.
    """

    def test_pin_reinserts_dropped_facts_at_front(self):
        engine = _make_engine(fact_pin_top_k=2)
        raw = [FakeFact(content=f"fact {i}", id=str(i)) for i in range(5)]
        pinned = raw[:2]
        survivors = raw[2:]  # pipeline dropped BOTH pinned facts
        merged = engine._reinsert_pinned(pinned, survivors)
        assert [f.id for f in merged] == ["0", "1", "2", "3", "4"]

    def test_pin_partial_drop_keeps_survivor_position(self):
        engine = _make_engine(fact_pin_top_k=2)
        raw = [FakeFact(content=f"fact {i}", id=str(i)) for i in range(4)]
        pinned = raw[:2]
        survivors = [raw[2], raw[0], raw[3]]  # "0" survived mid-list, "1" dropped
        merged = engine._reinsert_pinned(pinned, survivors)
        assert [f.id for f in merged] == ["1", "2", "0", "3"]  # only "1" re-inserted

    def test_pin_never_resurrects_superseded_fact(self):
        engine = _make_engine(fact_pin_top_k=2)
        stale = FakeFact(content="old value", id="stale")
        stale.recency_status = "superseded"   # tagged by _resolve_recency
        fresh = FakeFact(content="new value", id="fresh")
        merged = engine._reinsert_pinned([stale, fresh], [])  # pipeline dropped both
        assert [f.id for f in merged] == ["fresh"]            # stale NOT re-inserted

    def test_pin_preserves_pipeline_order_when_nothing_dropped(self):
        engine = _make_engine(fact_pin_top_k=1)
        raw = [FakeFact(content="a", id="a"), FakeFact(content="b", id="b")]
        merged = engine._reinsert_pinned(raw[:1], list(raw))
        assert [f.id for f in merged] == ["a", "b"]  # unchanged, no duplicate

    def test_pin_zero_is_inert(self):
        engine = _make_engine()  # fact_pin_top_k defaults 0
        assert engine._settings.fact_pin_top_k == 0


async def test_pin_build_wiring_records_pinned_ids():
    """build()-level: pinned facts flow through to the section AND recalled ids."""
    engine = _make_engine(fact_pin_top_k=2)
    facts = [FakeFact(content=f"pinnable fact {i}", subject=f"s{i}",
                      id=f"id-{i}", score=0.9 - i * 0.1) for i in range(4)]
    _stub_heart_for_build(engine, facts)
    result = await engine.build(
        agent_id="a", session_id="s", input_text="pinnable?", frame=_frame(),
    )
    section = next(s for s in result.sections if s.label == "Relevant Facts")
    assert "pinnable fact 0" in section.content
    assert "id-0" in result.recalled_ids["fact"]
    assert "id-1" in result.recalled_ids["fact"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_preturn_fact_injection.py -v -k pin`
Expected: the `_reinsert_pinned` unit tests FAIL (`ContextEngine` has no `_reinsert_pinned`); `test_pin_zero_is_inert` fails (`Settings` has no `fact_pin_top_k`). NOTE: `test_pin_build_wiring_records_pinned_ids` PASSES even pre-implementation — with these scores/subjects the pipeline drops nothing, so it is a wiring smoke test (regression guard for the capture/reinsert/recalled-ids seams), not a red-green TDD test. Expected: 1 green, rest red.

- [ ] **Step 3: Add the setting**

In `nous/config.py`, same block as Task 2's settings:

```python
    fact_pin_top_k: int = Field(
        default=0, ge=0,
        description=(
            "Pin the top-K post-recency-resolve fact search hits into pre-turn "
            "context, bypassing diversity/dedup/relevance demotion (0 = off). "
            "Facts tagged superseded by the recency resolver are never pinned. "
            "Remedy for the counterfactual-fact injection miss (2026-07-13 plan)."
        ),
    )
```

- [ ] **Step 4: Implement `_reinsert_pinned` + wire into `build()`**

Add method to `ContextEngine` (place after `_apply_relevance_filter`, context.py:~1156):

```python
    def _reinsert_pinned(self, pinned: list, survivors: list) -> list:
        """Guarantee pinned facts appear in the injected list.

        Pinned facts the pipeline dropped are re-inserted AT THE FRONT (they
        are the strongest direct hits, and front position also protects them
        from budget truncation, which cuts from the tail). Survivors keep
        their pipeline order. Facts the recency resolver tagged superseded
        are never re-inserted — the pin must not resurrect a stale value the
        resolver demoted (c12 failure class).
        """
        surviving_ids = {str(getattr(f, "id", "")) for f in survivors}
        dropped = [
            p for p in pinned
            if str(getattr(p, "id", "")) not in surviving_ids
            and getattr(p, "recency_status", None) != "superseded"
        ]
        return dropped + survivors
```

In the fact section of `build()` — capture the pin set **immediately AFTER `_resolve_recency`** (context.py:636, inside the `if facts:` branch), so resolver tags exist before capture:

```python
                    facts = self._resolve_recency(facts)
                    pin_k = getattr(self._settings, "fact_pin_top_k", 0)
                    pinned_facts = list(facts[:pin_k]) if pin_k > 0 else []
```

...existing pipeline unchanged... then directly after `facts = self._apply_relevance_filter(facts, "fact")` (:651):

```python
                    if pinned_facts:
                        facts = self._reinsert_pinned(pinned_facts, facts)
```

(The reinsert lands before the recalled-IDs loop at :656, so pinned facts are recorded in `recalled_ids`/`recalled_content_map` — required for the F071 exclude contract.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_preturn_fact_injection.py tests/test_context.py tests/test_context_smart.py -v`
Expected: PASS, including the HEAD golden (pin defaults 0 → `pinned_facts` empty → byte-identical).

- [ ] **Step 6a (CONDITIONAL — only if Task 1 showed `DROP @ fetch-limit`): fetch-limit override**

Add setting `fact_fetch_limit_override: int = 0` (`ge=0`; `0` = keep plan/default limits) in the same config block, and change the limit line (context.py:621) to:

```python
                limit = _limits.get("fact", DEFAULT_FETCH_LIMITS.get("fact", 5))
                _override = getattr(self._settings, "fact_fetch_limit_override", 0)
                if _override > 0:
                    limit = max(limit, _override)
```

With test:

```python
    def test_fetch_limit_override_setting_defaults_zero(self):
        engine = _make_engine()
        assert engine._settings.fact_fetch_limit_override == 0
```

- [ ] **Step 6b (CONDITIONAL — only if Task 1 showed `DROP @ query-rewrite`): raw-input fact query**

Add setting `fact_query_use_raw_input: bool = False` in the same config block, and change the fact query line (context.py:622) to:

```python
                q_text = _query_texts.get("fact", _default_query)
                if getattr(self._settings, "fact_query_use_raw_input", False):
                    q_text = _default_query
```

With test:

```python
    def test_raw_input_query_setting_defaults_false(self):
        engine = _make_engine()
        assert engine._settings.fact_query_use_raw_input is False
```

If the probe showed neither drop, skip both steps and note the skips in the PR description.

- [ ] **Step 7: Commit**

```bash
git add nous/config.py nous/cognitive/context.py tests/test_preturn_fact_injection.py
git commit -m "feat(context): fact pin — top-K direct hits bypass demotion pipeline (NOUS_FACT_PIN_TOP_K)"
```

---

### Task 4: Supersession lineage annotation (dict-threaded, no mutation)

**Files:**
- Modify: `nous/heart/facts.py` (new method on `FactManager` — the class at facts.py:134; place it near `find_similar_for_dedup`)
- Modify: `nous/heart/heart.py` (facade method after `find_similar_facts`, heart.py:374-385)
- Modify: `nous/config.py` (1 setting)
- Modify: `nous/cognitive/context.py` (`build()` fact section + `_format_facts` lineage rendering)
- Test: `tests/test_preturn_fact_injection.py` (extend) + `tests/test_facts.py` (real-PG tests for the new SQL — follow that file's existing fixture pattern)

**Interfaces:**
- Consumes: `heart.facts.superseded_by` column (UUID self-FK, models.py:564); the `lineage` parameter Task 2 already added to `_format_facts`.
- Produces:
  - `FactManager.get_superseded_contents(self, fact_ids: list[UUID], session: AsyncSession | None = None) -> dict[UUID, list[str]]` — map superseder-id → up to 2 superseded contents, newest first, agent-scoped.
  - `Heart.get_superseded_contents(...)` — pass-through facade.
  - Setting `supersession_lineage_mode: Literal["off", "tag", "named"] = "off"`.
  - **No new `FactSummary` field** — lineage never touches result objects (they may be `_ScoredWrapper` with `__slots__`; reads delegate, writes raise).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_preturn_fact_injection.py`:

```python
from nous.heart.search import _wrap_with_score


class TestSupersessionLineage:
    def test_mode_validates(self):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, supersession_lineage_mode="bogus")

    def test_tag_mode_appends_generic_marker(self):
        engine = _make_engine(supersession_lineage_mode="tag")
        f = FakeFact(content="Past Masters was performed by Madonna",
                     subject="Past Masters", id="f1")
        out = engine._format_facts(
            [f], lineage={"f1": ["Past Masters was performed by The Beatles"]})
        assert "[current — supersedes an earlier belief]" in out
        assert "Beatles" not in out               # tag mode never names the stale value

    def test_named_mode_quotes_stale_value(self):
        engine = _make_engine(supersession_lineage_mode="named")
        f = FakeFact(content="Past Masters was performed by Madonna",
                     subject="Past Masters", id="f1")
        out = engine._format_facts(
            [f], lineage={"f1": ["Past Masters was performed by The Beatles"]})
        assert 'supersedes earlier belief: "Past Masters was performed by The Beatles"' in out

    def test_named_mode_truncates_stale_value_at_120(self):
        engine = _make_engine(supersession_lineage_mode="named")
        f = FakeFact(content="new", subject="s", id="f1")
        out = engine._format_facts([f], lineage={"f1": ["X" * 500]})
        assert "X" * 120 in out
        assert "X" * 121 not in out

    def test_off_mode_renders_nothing_even_with_lineage(self):
        engine = _make_engine()  # mode defaults "off"
        f = FakeFact(content="new", subject="s", id="f1")
        out = engine._format_facts([f], lineage={"f1": ["old"]})
        assert "supersede" not in out.lower()

    def test_lineage_renders_through_scored_wrapper(self):
        """The pipeline wraps facts in _ScoredWrapper (__slots__ forbids attribute
        writes) — lineage must render via the dict, reading id through the wrapper."""
        engine = _make_engine(supersession_lineage_mode="tag")
        f = _wrap_with_score(FakeFact(content="new", subject="s", id="f1"), 0.9)
        out = engine._format_facts([f], lineage={"f1": ["old"]})
        assert "[current — supersedes an earlier belief]" in out
```

And the build()-level wiring test:

```python
async def test_lineage_build_wiring_fetches_and_renders():
    engine = _make_engine(supersession_lineage_mode="tag")
    facts = [FakeFact(content="current value", subject="cv", id="f1", score=0.9)]
    _stub_heart_for_build(engine, facts)
    engine._heart.get_superseded_contents.return_value = {"f1": ["old value"]}
    result = await engine.build(
        agent_id="a", session_id="s", input_text="current?", frame=_frame(),
    )
    section = next(s for s in result.sections if s.label == "Relevant Facts")
    assert "[current — supersedes an earlier belief]" in section.content


async def test_lineage_fetch_failure_degrades_to_plain_rendering():
    engine = _make_engine(supersession_lineage_mode="tag")
    facts = [FakeFact(content="current value", subject="cv", id="f1", score=0.9)]
    _stub_heart_for_build(engine, facts)
    engine._heart.get_superseded_contents.side_effect = Exception("db down")
    result = await engine.build(
        agent_id="a", session_id="s", input_text="current?", frame=_frame(),
    )
    section = next(s for s in result.sections if s.label == "Relevant Facts")
    assert "current value" in section.content
    assert "supersede" not in section.content.lower()
```

**Note on the wiring test's id types:** in prod, `get_superseded_contents` returns UUID keys and `build()` stringifies them into the `str(fact.id)`-keyed dict it passes to `_format_facts`; with FakeFact string ids, `str("f1") == "f1"` keeps the test faithful to the same conversion path.

Real-PG tests in `tests/test_facts.py` (follow that file's existing fixture/setup pattern for constructing the store and inserting facts — the assertions below are normative):

```python
async def test_get_superseded_contents_maps_and_caps(...existing fixture...):
    # Arrange: fact NEW supersedes OLD1, OLD2, OLD3 (superseded_by = NEW.id on all
    # three, created_at ordered OLD3 newest). OLDs may be inactive — that's the
    # normal supersession end-state and MUST still be returned.
    result = await store.get_superseded_contents([new_id])
    assert set(result.keys()) == {new_id}
    assert len(result[new_id]) == 2                      # cap 2
    assert result[new_id][0] == "old3 content"           # newest first
    # Empty input short-circuits without SQL:
    assert await store.get_superseded_contents([]) == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_preturn_fact_injection.py -v -k lineage`
Expected: FAIL — unknown setting rejected? No: `Settings` ignores extra kwargs, so the mode tests fail on missing rendering; `test_mode_validates` fails because no `ValidationError` is raised yet; wiring tests fail on missing facade method.

- [ ] **Step 3: Add the setting, the store method, and the facade**

`nous/config.py` (same block; add `from typing import Literal` only if not already imported — config.py already imports Literal for other fields, verify):

```python
    supersession_lineage_mode: Literal["off", "tag", "named"] = Field(
        default="off",
        description=(
            "Annotate pre-turn injected facts that supersede an earlier fact: "
            "'tag' = generic [current — supersedes an earlier belief] marker; "
            "'named' = quotes the superseded content (anchoring risk — A/B before prod); "
            "'off' = byte-identical legacy rendering."
        ),
    )
```

`nous/heart/facts.py` — new method on `FactManager` (near `find_similar_for_dedup`; `select`, `Fact`, `UUID`, `AsyncSession` already imported):

```python
    async def get_superseded_contents(
        self,
        fact_ids: list[UUID],
        session: AsyncSession | None = None,
    ) -> dict[UUID, list[str]]:
        """Map superseder fact id -> contents of facts it superseded (max 2, newest first).

        Reads the authoritative ``superseded_by`` column (NOT graph edges, which
        historically lag it). Includes inactive rows on purpose — supersession
        deactivates the old fact, and the old content is exactly what the
        lineage annotation needs. Agent-scoped like every FactManager read.
        """
        if not fact_ids:
            return {}
        if session is None:
            async with self.db.session() as session:
                return await self._get_superseded_contents_impl(fact_ids, session)
        return await self._get_superseded_contents_impl(fact_ids, session)

    async def _get_superseded_contents_impl(
        self, fact_ids: list[UUID], session: AsyncSession
    ) -> dict[UUID, list[str]]:
        stmt = (
            select(Fact.superseded_by, Fact.content)
            .where(
                Fact.agent_id == self.agent_id,
                Fact.superseded_by.in_(fact_ids),
            )
            .order_by(Fact.created_at.desc())
        )
        rows = (await session.execute(stmt)).all()
        out: dict[UUID, list[str]] = {}
        for superseder_id, content in rows:
            bucket = out.setdefault(superseder_id, [])
            if len(bucket) < 2:
                bucket.append(content or "")
        return out
```

**Implementer note:** the two-branch session idiom above matches the file's read methods (e.g. `find_similar_for_dedup`, facts.py:1556-1559); confirm `self.db` / `self.agent_id` attribute names against the class `__init__` and copy exactly.

`nous/heart/heart.py` — facade after `find_similar_facts` (:385):

```python
    async def get_superseded_contents(
        self,
        fact_ids: list[UUID],
        session: AsyncSession | None = None,
    ) -> dict[UUID, list[str]]:
        """Contents of facts superseded by each given fact (lineage annotation)."""
        return await self.facts.get_superseded_contents(fact_ids, session)
```

- [ ] **Step 4: Wire into `build()` and `_format_facts`**

In the fact section of `build()`, after the pin re-insert from Task 3 and before `facts_text = self._format_facts(...)` (context.py:~663). Build a **local dict** — never mutate the (possibly `_ScoredWrapper`-wrapped) fact objects:

```python
                    _lineage_by_id: dict[str, list[str]] = {}
                    lineage_mode = getattr(self._settings, "supersession_lineage_mode", "off")
                    if lineage_mode != "off" and facts:
                        try:
                            _fact_uuids = [f.id for f in facts if getattr(f, "id", None)]
                            _lineage_raw = await self._heart.get_superseded_contents(
                                _fact_uuids, session=session
                            )
                            _lineage_by_id = {str(k): v for k, v in _lineage_raw.items()}
                        except Exception:
                            logger.debug("Supersession lineage fetch failed", exc_info=True)
```

and pass it at the (Task 2-updated) format call:

```python
                    facts_text = self._format_facts(
                        facts,
                        full_top_n=getattr(self._settings, "fact_format_full_top_n", 0),
                        lineage=_lineage_by_id or None,
                    )
```

In `_format_facts`, consume the parameter (directly after the `rtag` line):

```python
            # Supersession lineage (off|tag|named): dict-threaded, keyed str(id).
            olds = (lineage or {}).get(str(getattr(f, "id", "")))
            mode = getattr(self._settings, "supersession_lineage_mode", "off")
            ltag = ""
            if olds and mode == "tag":
                ltag = " [current — supersedes an earlier belief]"
            elif olds and mode == "named":
                ltag = f' (supersedes earlier belief: "{olds[0][:120]}")'
```

and change both append lines to include `{ltag}` after `{rtag}`:

```python
            if subject:
                lines.append(f"- [{subject}] {content}{rtag}{ltag} [confidence: {conf:.2f}]")
            else:
                lines.append(f"- {content}{rtag}{ltag} [confidence: {conf:.2f}]")
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_preturn_fact_injection.py tests/test_context.py tests/test_facts.py -v`
Expected: PASS, including the HEAD golden (mode `off` → `_lineage_by_id` empty → `ltag` always `""` → byte-identical).

- [ ] **Step 6: Commit**

```bash
git add nous/config.py nous/heart/facts.py nous/heart/heart.py nous/cognitive/context.py tests/test_preturn_fact_injection.py tests/test_facts.py
git commit -m "feat(heart,context): supersession lineage annotation on injected facts (NOUS_SUPERSESSION_LINEAGE_MODE)"
```

---

### Task 5: Empty-facts recall backstop instruction

**Files:**
- Modify: `nous/config.py` (1 setting)
- Modify: `nous/cognitive/context.py` (fact section of `build()`, directly after the facts block's `except` at :676)
- Test: `tests/test_preturn_fact_injection.py` (extend)

**Interfaces:**
- Consumes: `ContextSection` (schemas.py:158); `RetrievalPlan` (nous/cognitive/intent.py:47) for the skip_types test; helpers from Task 2.
- Produces: setting `recall_backstop_enabled: bool = False`; method `_recall_backstop_text() -> str`. When ON and the final fact list is empty (search failed, returned nothing, or the pipeline dropped everything — including conversation-dedup emptying it), inject a static instruction section telling the agent to call `recall_deep` before answering memory questions.

**Design notes:**
- Trigger is *zero facts in the final list* only. Do NOT threshold on scores — pre-turn fact scores are rank-encoded RRF values; thresholding them repeats the audit-S1 mistake.
- `facts_injected = bool(facts)` is evaluated on the FINAL list (after pipeline + pin), not at section-append: conversation-dedup can empty the list mid-branch while an empty section still gets appended (pre-existing wart, byte-identity requires keeping the empty append).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_preturn_fact_injection.py`:

```python
from nous.cognitive.intent import RetrievalPlan


class TestRecallBackstop:
    def test_backstop_text_mentions_recall_deep(self):
        engine = _make_engine(recall_backstop_enabled=True)
        assert "recall_deep" in engine._recall_backstop_text()

    def test_backstop_setting_defaults_off(self):
        engine = _make_engine()
        assert engine._settings.recall_backstop_enabled is False


async def test_backstop_section_appears_when_no_facts():
    engine = _make_engine(recall_backstop_enabled=True)
    _stub_heart_for_build(engine, [])
    result = await engine.build(
        agent_id="a", session_id="s",
        input_text="who performed Past Masters?", frame=_frame(),
    )
    assert any(s.label == "Memory Retrieval Notice" for s in result.sections)


async def test_backstop_fires_on_search_exception():
    engine = _make_engine(recall_backstop_enabled=True)
    _stub_heart_for_build(engine, [])
    engine._heart.search_facts.side_effect = Exception("search down")
    result = await engine.build(
        agent_id="a", session_id="s", input_text="anything?", frame=_frame(),
    )
    assert any(s.label == "Memory Retrieval Notice" for s in result.sections)


async def test_backstop_absent_when_facts_present():
    engine = _make_engine(recall_backstop_enabled=True)
    _stub_heart_for_build(engine, [FakeFact(content="a stored fact about things",
                                            subject="s", id="f1", score=0.9)])
    result = await engine.build(
        agent_id="a", session_id="s", input_text="things?", frame=_frame(),
    )
    assert not any(s.label == "Memory Retrieval Notice" for s in result.sections)


async def test_backstop_absent_when_fact_type_skipped():
    engine = _make_engine(recall_backstop_enabled=True)
    _stub_heart_for_build(engine, [])
    plan = RetrievalPlan(skip_types={"fact"})
    result = await engine.build(
        agent_id="a", session_id="s", input_text="hi there", frame=_frame(),
        retrieval_plan=plan,
    )
    assert not any(s.label == "Memory Retrieval Notice" for s in result.sections)


async def test_backstop_absent_by_default():
    engine = _make_engine()  # flag off
    _stub_heart_for_build(engine, [])
    result = await engine.build(
        agent_id="a", session_id="s", input_text="anything", frame=_frame(),
    )
    assert not any(s.label == "Memory Retrieval Notice" for s in result.sections)
```

**Implementer note:** `RetrievalPlan` field names (`skip_types`, `budget_overrides`) verified at intent.py:47-52; if `build()` derives `skip_types` differently (see context.py:182), adjust the skip test to whatever mechanism `build()` actually reads — the assertion is the contract, the construction is not.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_preturn_fact_injection.py -v -k backstop`
Expected: FAIL — missing `_recall_backstop_text`; no "Memory Retrieval Notice" section.

- [ ] **Step 3: Add setting + implementation**

`nous/config.py` (same block):

```python
    recall_backstop_enabled: bool = Field(
        default=False,
        description=(
            "When pre-turn fact retrieval yields ZERO surviving facts, inject a "
            "system-prompt instruction to call recall_deep before answering "
            "memory questions. Deterministic trigger (empty set), no score thresholds."
        ),
    )
```

`nous/cognitive/context.py` — helper method (near `_format_facts`):

```python
    def _recall_backstop_text(self) -> str:
        """Instruction injected when pre-turn fact retrieval came back empty."""
        return (
            "Pre-turn memory retrieval found no relevant stored facts for this input. "
            "Before answering any question about prior conversations, stored knowledge, "
            "or user-specific information, call recall_deep with a focused query — "
            "do not answer such questions from general knowledge alone."
        )
```

In `build()`: introduce `facts_injected = False` immediately before the facts block (`# 6. Facts`, context.py:618); inside the `if facts:` branch, set it from the FINAL list right before formatting (after pipeline + pin + lineage fetch):

```python
                    facts_injected = bool(facts)
```

then append the backstop right after the facts block's `except` (after :676):

```python
        # 6b. Recall backstop (2026-07-13 plan): empty final fact list => tell the
        # agent to recall before answering. Fires on search failure too (the
        # except path leaves facts_injected False) and when dedup/filters empty
        # the list (facts_injected evaluated on the FINAL list) — both desired.
        if (
            getattr(self._settings, "recall_backstop_enabled", False)
            and budget.facts > 0
            and "fact" not in skip_types
            and not facts_injected
        ):
            _bs_text = self._recall_backstop_text()
            sections.append(
                ContextSection(
                    priority=2,
                    label="Memory Retrieval Notice",
                    content=_bs_text,
                    token_estimate=self._estimate_tokens(_bs_text),
                    tier="dynamic",
                )
            )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_preturn_fact_injection.py -v`
Expected: PASS, including the HEAD golden (flag off → no new section).

- [ ] **Step 5: Commit**

```bash
git add nous/config.py nous/cognitive/context.py tests/test_preturn_fact_injection.py
git commit -m "feat(context): recall_deep backstop instruction on empty fact retrieval (NOUS_RECALL_BACKSTOP_ENABLED)"
```

---

### Task 6: Flags-off invariant, full suite, docs

**Files:**
- Test: `tests/test_preturn_fact_injection.py` (one more test class)
- Modify: `CLAUDE.md` (env var table — 5-7 rows)

- [ ] **Step 1: Write the settings-default invariant test**

```python
class TestFlagsOffInvariant:
    """With default Settings, the new code paths must be provably inert.
    (The build()-level byte-identity oracle is the HEAD golden at the top
    of this file — this class covers the settings surface.)"""

    def test_all_new_settings_default_off(self):
        s = Settings(_env_file=None)
        assert s.fact_format_max_chars == 200
        assert s.fact_format_full_top_n == 0
        assert s.fact_pin_top_k == 0
        assert s.supersession_lineage_mode == "off"
        assert s.recall_backstop_enabled is False
```

(Add asserts for `fact_fetch_limit_override == 0` / `fact_query_use_raw_input is False` only if Task 3's conditional steps were built.)

- [ ] **Step 2: Run the invariant + the HEAD golden + the full suite**

Run: `uv run pytest tests/test_preturn_fact_injection.py -v` then `uv run pytest tests/ -x -q`
Expected: all PASS. If any pre-existing test fails, STOP and fix the regression — the only pre-licensed pre-existing-test edit is `_make_context_engine_light` (Task 2 Step 5).

- [ ] **Step 3: Add CLAUDE.md env rows**

Append to the env table (near the other context/recall flags):

```markdown
| `NOUS_FACT_FORMAT_MAX_CHARS` | `200` | Per-fact char cap in pre-turn context rendering (was hardcoded 200). Shared by Relevant Facts AND User Profile sections. |
| `NOUS_FACT_FORMAT_FULL_TOP_N` | `0` | Render the top-N Relevant Facts untruncated (0 = all capped). |
| `NOUS_FACT_PIN_TOP_K` | `0` | Pin top-K post-recency-resolve fact hits into pre-turn context past diversity/dedup/relevance demotion (0 = off; superseded-tagged facts never pinned). Counterfactual-injection fix, 2026-07-13 plan; flip gated on prod-generator A/B. |
| `NOUS_SUPERSESSION_LINEAGE_MODE` | `off` | Annotate injected facts that supersede an earlier fact: `tag` (generic marker) / `named` (quotes stale value — anchoring risk, A/B first) / `off`. |
| `NOUS_RECALL_BACKSTOP_ENABLED` | `false` | Inject a "call recall_deep before answering" instruction when pre-turn fact retrieval returns zero facts. |
```

(Include `NOUS_FACT_FETCH_LIMIT_OVERRIDE` / `NOUS_FACT_QUERY_USE_RAW_INPUT` rows only if built.)

- [ ] **Step 4: Commit**

```bash
git add tests/test_preturn_fact_injection.py CLAUDE.md
git commit -m "test(context): flags-off invariant for pre-turn injection fixes + env docs"
```

---

### Task 7: A/B validation gate (eval run — no prod code)

**Files:**
- Port: `scripts/eval/qa_context_ab.py` (+ its qrels/companion files) from the local unmerged branch `eval/h2-injection-precision` in the MAIN repo (E:\Projects\nous) onto the feature branch. This harness runs QA-accuracy A/B **over the real `ContextEngine.build` path** with `--gen-model` (prod Opus) and LLM judging — it ran the definitive n=101 H2 eval. `nous_eval.multi_turn_eval` is NOT usable here: it calls `run_recall_pipeline` directly and never constructs `ContextEngine`, so every arm would be a byte-identical null; it also has no accuracy metric and no generator.

**Interfaces:**
- Consumes: merged Tasks 2-5 code; eval DB (`127.0.0.1:5433`, prod-shape corpus); the 6k-investigation counterfactual question set (~12 questions); the h2 prod-shape snapshot-matched gold-present qrels (n=101) for the non-regression gate.
- Produces: per-arm accuracy table written into the PR description; a flip/no-flip recommendation per flag; deletion list for losing arms.

- [ ] **Step 1: Port and smoke the harness**

```bash
git -C E:\Projects\nous show eval/h2-injection-precision --stat   # locate harness + qrels files
git checkout eval/h2-injection-precision -- scripts/eval/qa_context_ab.py  # plus companions it needs
uv run python scripts/eval/qa_context_ab.py --help
```

Verify it exposes: generator model selection (`--gen-model`), config/arm selection, `--n`. Extend its config set with the new flags (settings overlays for `fact_pin_top_k`, `fact_format_full_top_n`, `supersession_lineage_mode`, `recall_backstop_enabled`). If the harness expects prod-data files that are gitignored, regenerate per its README/docstring. If anything is missing that requires >1 hour of harness surgery, STOP and surface to the user with what was found.

- [ ] **Step 2: Define the arms (one variable per step past L1)**

| Arm | Settings overlay |
|-----|------------------|
| baseline | all new flags at defaults |
| L1 | `fact_pin_top_k=3, fact_format_full_top_n=3` |
| L1+tag | L1 + `supersession_lineage_mode="tag"` |
| L1+named | L1 + `supersession_lineage_mode="named"` |
| L1+backstop | L1 + `recall_backstop_enabled=True` (only if L1 leaves residual misses; note the backstop's effect is a *tool-call nudge* — measurable only if the harness executes the agent loop, else record as not-measurable) |
| (conditional) L1+rawq | L1 + `fact_query_use_raw_input=True` (only if Task 3 Step 6b was built) |

- [ ] **Step 3: Run and evaluate**

Two tiers:
1. **Counterfactual diagnostic (directional, n≈12):** the 6k flip set. Report per-question flips; no statistical claim at this n.
2. **Non-regression + win gate (n≥100):** the h2 prod-shape qrels on the prod Opus generator. Gate: no aggregate accuracy regression; report any per-tier movement.

Per `feedback-eval-prod-generator`: never conclude from a Sonnet proxy; small-n signs flip.

- [ ] **Step 4: Record the verdict + flag hygiene**

Write the arm table + verdict into the PR description; recommend flag values for prod (or keep dark). **Delete the settings/code for losing arms** (e.g. if `named` loses to `tag`, remove the `named` branch) rather than leaving dead flags — the 2026-07-11 simplification audit counted ~19 removable flags; don't add to that pile. Also check the F071 recorded-vs-shown seam empirically (compare `recalled_fact_ids` against rendered section content in a few L1-arm turns); if the pin measurably aggravates it, file the B-cog-A-style sync as a follow-up. Update the FORGE decision with the outcome.

---

## Execution Workflow (per user mandate)

1. ~~Plan review~~ — DONE 2026-07-13 (3 agents; all P1/P2 findings incorporated in this v2).
2. **Implementation:** fresh worktree + branch `feat/preturn-fact-injection` cut from `origin/main`; subagent-driven task-by-task with per-task verification (each task's test steps must actually run green before moving on). Task order: 2 (golden first) → 1 (probe; can run in parallel with 3-5 implementation but its findings gate Task 3 Steps 6a/6b) → 3 → 4 → 5 → 6 → 7.
3. **Post-implementation team review** (3 agents, fresh eyes) + fix findings.
4. **PR** with probe findings + A/B results; **codex review**; address findings; merge when clean.
5. Flag flips remain a separate post-merge decision gated on Task 7 results.
