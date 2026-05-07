"""End-to-end context packing eval (cognitive-layer plan item 6).

The umbrella eval. For each (user_message, gold_answer) pair, runs the
production retrieval pipeline (`run_recall_pipeline`) against the prod
snapshot and asks Sonnet: "given this assembled context, could the
gold answer be produced?"

Why it matters: this measures the cumulative damage from frame, intent,
retrieval, dedup, and budget — together. F051 measures retrieval at the
top-K level; this one measures whether the LLM downstream actually has
what it needs, regardless of where in top-K the supporting memory sits.

Cost: ~$3 (10-15 scenarios × pipeline + judge). Rate-limit sensitive.

Usage:
    NOUS_EVAL_DB_NAME=nous_eval_scratch \
    NOUS_EVAL_AGENT_ID=nous-prod-snapshot \
      uv run python scripts/eval/eval_context_packing.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

from nous.api.anthropic_client import create_client
from nous.api.retrieval_pipeline import run_recall_pipeline
from nous_eval._oat_preamble import (
    RateLimiter, call_with_retries, with_oat_preamble,
)
from nous.brain.brain import Brain
from nous.brain.embeddings import EmbeddingProvider
from nous.config import Settings
from nous.heart.heart import Heart
from nous.storage.database import Database
from nous_eval.config import EvalSettings
from nous_eval.retrieval_runner import _settings_for_eval_db


_DEFAULT_MODEL = "claude-sonnet-4-6"


@dataclass
class PackingScenario:
    """One end-to-end packing scenario.

    ``bucket`` separates retrieval-quality probes from documentation-only
    probes. Set ``bucket="docs"`` when the gold_answer_hint references
    information that lives in CLAUDE.md / source code (env-var defaults,
    internal classnames) rather than something the agent would naturally
    memorize as a fact/episode/decision. Headline sufficiency is reported
    over ``bucket="memory"`` only — docs scenarios are surfaced separately
    as a known-limitation aside so they don't depress the metric we
    actually use to judge retrieval quality.
    """

    name: str
    user_message: str
    gold_answer_hint: str
    bucket: str = "memory"  # "memory" or "docs"
    notes: str = ""


# Hand-curated scenarios. Queries reference content that EXISTS in the
# nous-prod-snapshot (taken before this session's PRs). Avoid topics
# that landed today (calibration, F022 audit, F031 fixes) since the
# snapshot pre-dates them.
#
# Bucketing rationale (2026-05-02 audit, see PR #399 follow-up):
#   - subtask_workers: gold expects NOUS_SUBTASK_WORKERS env var default.
#     Pure configuration — corpus probe found 0 verbatim matches. docs.
#   - skill_management: gold expects internal classnames (SkillParser,
#     bootstrap, auto-activation via RECALL). Source-code-level detail
#     never verbalized as a fact — corpus probe found 0 hits for those
#     exact terms. docs.
#   - All others: gold corresponds to memorizable events / decisions /
#     architectural facts that the agent has stored. memory.
SCENARIOS: list[PackingScenario] = [
    PackingScenario(
        "telegram_email",
        "How does the Telegram bot integration work?",
        "Bot token + chat ID env vars; subtask completion notifications.",
    ),
    PackingScenario(
        "heartbeat_overview",
        "Tell me about the heartbeat system.",
        "F034 proactive monitoring — health/email/self-initiated checks; runs on tick interval.",
    ),
    PackingScenario(
        "skill_management",
        "How do skills get registered in Nous?",
        "F011 skill discovery — learn_skill tool, SkillParser, bootstrap, auto-activation via RECALL.",
        bucket="docs",
    ),
    PackingScenario(
        "subtask_workers",
        "How many subtask workers run by default?",
        "Default is 2; configured via NOUS_SUBTASK_WORKERS env var.",
        bucket="docs",
    ),
    PackingScenario(
        "rubric_evolution",
        "What does the rubric evolver do?",
        "F024-3b — outcome signals → dimension proposals → rubric weight evolution.",
    ),
    PackingScenario(
        "procedure_learning",
        "How are procedures created automatically?",
        "F012 K-line procedure learning — auto-creates from decision clusters during sleep.",
    ),
    PackingScenario(
        "graph_densification",
        "What is graph densification?",
        "F040 — orphan backfill, reverse linking, per-relation thresholds during sleep cycle.",
    ),
    PackingScenario(
        "cognitive_loop",
        "What are the steps of the cognitive loop?",
        "Sense, Frame, Recall, Deliberate, Act, Monitor, Learn — 7 steps.",
    ),
]


def aggregate_by_bucket(results: list[dict]) -> dict:
    """Compute headline (memory) + docs sufficiency separately.

    Returns a dict with both rates so the report can show them side-by-side.
    Headline sufficiency is what should drive the verdict on whether
    retrieval is healthy; docs sufficiency is a known-limitation aside.
    """
    memory = [r for r in results if r.get("bucket", "memory") == "memory"]
    docs = [r for r in results if r.get("bucket") == "docs"]

    def _rate(items: list[dict]) -> float:
        n = len(items)
        if n == 0:
            return 0.0
        return sum(1 for r in items if r["sufficient"]) / n

    return {
        "memory_total": len(memory),
        "memory_ok": sum(1 for r in memory if r["sufficient"]),
        "memory_rate": _rate(memory),
        "docs_total": len(docs),
        "docs_ok": sum(1 for r in docs if r["sufficient"]),
        "docs_rate": _rate(docs),
    }


_JUDGE_PROMPT = """You are evaluating whether an assembled memory context is sufficient to answer a user's question.

User question: {question}

What the answer should contain: {gold_hint}

Assembled context (memory items retrieved by the system):
{context}

Verdict: based ONLY on the assembled context, could the LLM answer the question accurately? "Sufficient" means the gold-answer information is present (verbatim or paraphrased without distortion). Reply with strict JSON:
{{"sufficient": true|false, "reason": "<short>"}}"""


async def _judge(api_client, model: str, question: str, gold_hint: str,
                 context: str, rate_limiter: RateLimiter) -> dict:
    prompt = _JUDGE_PROMPT.format(
        question=question, gold_hint=gold_hint,
        context=context[:6000],
    )
    payload = {
        "model": model,
        "max_tokens": 200,
        "temperature": 0,
        "system": with_oat_preamble(),
        "messages": [{"role": "user", "content": prompt}],
    }
    resp = await call_with_retries(api_client, payload, rate_limiter=rate_limiter)
    text = ""
    for block in resp.content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text", "").strip()
            break
    if text.startswith("```"):
        text = "\n".join(text.splitlines()[1:-1])
    try:
        return json.loads(text)
    except Exception:
        return {"sufficient": False, "reason": "parse error"}


async def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--judge-model", default=_DEFAULT_MODEL)
    p.add_argument("--max-scenarios", type=int, default=5)
    p.add_argument("--top-k", type=int, default=10)
    p.add_argument("--out", type=Path, default=Path("reports/eval_context_packing.md"))
    p.add_argument("--out-json", type=Path, default=Path("reports/eval_context_packing.json"))
    p.add_argument(
        "--apply-mmr",
        choices=["none", "force_on", "force_off"],
        default="none",
        help=(
            "F030.2 per-consumer MMR override. 'none' uses settings-driven "
            "default (matches the recall_deep tool path). 'force_on' opts "
            "into MMR diversity for every recall (the diversity-hungry "
            "consumer mode that lifts context packing memory bucket "
            "0/8 → 6/8 in the EXEC-PLAN 1.5 eval)."
        ),
    )
    args = p.parse_args()
    _apply_mmr: bool | None = (
        True if args.apply_mmr == "force_on"
        else False if args.apply_mmr == "force_off"
        else None
    )

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

    logging.basicConfig(level=logging.WARNING)

    eval_settings = EvalSettings()
    main_settings = Settings()
    settings = _settings_for_eval_db(eval_settings, main_settings).model_copy(
        update={"agent_id": eval_settings.agent_id}
    )

    if not settings.openai_api_key:
        print("ERROR: OPENAI_API_KEY required.", file=sys.stderr)
        return 2
    if not (main_settings.anthropic_api_key or main_settings.anthropic_auth_token):
        print("ERROR: Anthropic creds required.", file=sys.stderr)
        return 2

    db = Database(settings)
    await db.connect()

    embedder = EmbeddingProvider(
        api_key=settings.openai_api_key,
        model=settings.embedding_model,
        dimensions=settings.embedding_dimensions,
    )

    api_client = create_client(main_settings)
    await api_client.start()
    rate_limiter = RateLimiter(min_interval_s=2.5)

    heart = Heart(database=db, settings=settings,
                  embedding_provider=embedder, owns_embeddings=False)
    brain = Brain(database=db, settings=settings, embedding_provider=embedder)

    results = []

    try:
        async with heart, brain:
            for sc in SCENARIOS[:args.max_scenarios]:
                await asyncio.sleep(1.5)
                # Run the production recall pipeline (extracted in F051)
                results_list, stats = await run_recall_pipeline(
                    query=sc.user_message,
                    heart=heart, brain=brain, settings=settings,
                    limit=args.top_k,
                    apply_mmr=_apply_mmr,  # F030.2 override
                )
                # Assemble the LLM-facing context string.
                context_lines = []
                for r in results_list[:args.top_k]:
                    label = f"[{r.type}] "
                    summary = r.description or ""
                    context_lines.append(f"{label}{summary}")
                context_text = "\n\n".join(context_lines) or "(empty)"

                verdict = await _judge(
                    api_client, args.judge_model,
                    sc.user_message, sc.gold_answer_hint,
                    context_text, rate_limiter,
                )
                results.append({
                    "name": sc.name,
                    "bucket": sc.bucket,
                    "question": sc.user_message,
                    "gold_hint": sc.gold_answer_hint,
                    "n_results": len(results_list),
                    "sufficient": bool(verdict.get("sufficient")),
                    "reason": verdict.get("reason", ""),
                    "context_preview": context_text[:600],
                })
    finally:
        await api_client.close()
        await db.disconnect()

    n = len(results)
    if not n:
        print("No results.")
        return 1
    agg = aggregate_by_bucket(results)
    headline_pct = 100 * agg["memory_rate"]
    docs_pct = 100 * agg["docs_rate"] if agg["docs_total"] else 0.0

    print()
    print("=" * 76)
    print(f"CONTEXT PACKING EVAL — {n} scenarios, top_k={args.top_k}")
    print("=" * 76)
    print(f"\n  Headline (memory bucket): "
          f"{agg['memory_ok']}/{agg['memory_total']} "
          f"({headline_pct:.0f}%)")
    if agg["docs_total"]:
        print(f"  Docs aside (known limitation): "
              f"{agg['docs_ok']}/{agg['docs_total']} "
              f"({docs_pct:.0f}%)")
    print()
    for r in results:
        marker = "OK  " if r["sufficient"] else "FAIL"
        bucket_tag = f"[{r['bucket']}]"
        print(f"  [{marker}] {bucket_tag:<8s} {r['name']:<28s} "
              f"n_results={r['n_results']:>2}  "
              f"reason: {r['reason'][:60]}")
    print("=" * 76)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    md = [
        f"# End-to-end context packing eval — {n} scenarios",
        "",
        f"- judge: `{args.judge_model}`",
        f"- top_k: {args.top_k}",
        (f"- **headline sufficiency (memory bucket): "
         f"{agg['memory_ok']}/{agg['memory_total']} ({headline_pct:.0f}%)**"),
    ]
    if agg["docs_total"]:
        md.append(
            f"- docs aside (known-limitation gold hints): "
            f"{agg['docs_ok']}/{agg['docs_total']} ({docs_pct:.0f}%)"
        )
    md += [
        "",
        "## Per-scenario",
        "",
        "| name | bucket | sufficient | n_results | reason |",
        "|---|---|---|---:|---|",
    ]
    for r in results:
        md.append(f"| {r['name']} | {r['bucket']} | "
                  f"{'OK' if r['sufficient'] else 'FAIL'} | "
                  f"{r['n_results']} | {r['reason']} |")
    args.out.write_text("\n".join(md), encoding="utf-8")
    args.out_json.write_text(json.dumps({
        "n": n,
        "headline_sufficiency": agg["memory_rate"],
        "headline_ok": agg["memory_ok"],
        "headline_total": agg["memory_total"],
        "docs_sufficiency": agg["docs_rate"],
        "docs_ok": agg["docs_ok"],
        "docs_total": agg["docs_total"],
        "judge_model": args.judge_model,
        "results": results,
    }, indent=2), encoding="utf-8")
    print(f"\nWrote: {args.out}")
    print(f"Wrote: {args.out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
