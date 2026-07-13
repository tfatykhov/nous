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

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from nous.cognitive.context import (
    ContextEngine,
    DEFAULT_FETCH_LIMITS,
    TIER1_FACT_CATEGORIES,
)
from nous.heart.search import apply_frame_boost  # defined in heart.search; not re-exported from context
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
    print("  SURVIVES to injected text")


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
