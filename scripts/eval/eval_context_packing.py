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
    name: str
    user_message: str
    gold_answer_hint: str  # what info needs to be in context
    notes: str = ""


# Hand-curated scenarios. Drawn from the prod-snapshot domain so the
# pipeline has real memory to retrieve.
SCENARIOS: list[PackingScenario] = [
    PackingScenario(
        "f042_finding",
        "Tell me what we found about cross-encoder reranking.",
        "F042 cross-encoder is corpus-dependent — helps on Nous-shape data, regresses on LongMemEval.",
    ),
    PackingScenario(
        "f058_reason",
        "Why did we calibrate confidence?",
        "Brier 0.252 at random baseline; ~20% systemic overconfidence on prod decisions.",
    ),
    PackingScenario(
        "ce_recommendation",
        "Should we enable the cross-encoder in production?",
        "Yes — measured +4% MRR on Nous-shape data; corpus-dependent finding.",
    ),
    PackingScenario(
        "calibration_factor",
        "What's the calibration factor we're using?",
        "0.7627, derived from 401 reviewed prod decisions.",
    ),
    PackingScenario(
        "edge_audit",
        "Summarize what the F022 edge audit found.",
        "0.70 precision on informed_by/related_to; empty content the dominant cause.",
    ),
    PackingScenario(
        "supersession_change",
        "What changed in the F027 prompt?",
        "Decision tree, mutability framing, examples; UPDATE accuracy 53→90%.",
    ),
    PackingScenario(
        "calibration_metric",
        "What's the Brier score on prod decisions?",
        "0.252 — at random-guess baseline.",
    ),
    PackingScenario(
        "tooling_calibration",
        "How well-calibrated are tooling decisions?",
        "Worst category: 0.745 mean confidence vs 36% success — gap +38%.",
    ),
]


_JUDGE_PROMPT = """You are evaluating whether an assembled memory context is sufficient to answer a user's question.

User question: {question}

What the answer should contain: {gold_hint}

Assembled context (memory items retrieved by the system):
{context}

Verdict: based ONLY on the assembled context, could the LLM answer the question accurately? "Sufficient" means the gold-answer information is present (verbatim or paraphrased without distortion). Reply with strict JSON:
{{"sufficient": true|false, "reason": "<short>"}}"""


async def _judge(api_client, model: str, question: str, gold_hint: str,
                 context: str) -> dict:
    prompt = _JUDGE_PROMPT.format(
        question=question, gold_hint=gold_hint,
        context=context[:6000],
    )
    payload = {
        "model": model,
        "max_tokens": 200,
        "temperature": 0,
        "system": "",
        "messages": [{"role": "user", "content": prompt}],
    }
    resp = await api_client.call(payload)
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
    args = p.parse_args()

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

    heart = Heart(database=db, settings=settings,
                  embedding_provider=embedder, owns_embeddings=False)
    brain = Brain(database=db, settings=settings, embedding_provider=embedder)

    results = []

    try:
        async with heart, brain:
            for sc in SCENARIOS[:args.max_scenarios]:
                await asyncio.sleep(1.5)
                # Run the production recall pipeline (extracted in F051)
                pipeline = await run_recall_pipeline(
                    query=sc.user_message,
                    heart=heart, brain=brain, settings=settings,
                    top_k=args.top_k,
                )
                # Assemble the LLM-facing context string the same way
                # `recall_deep` does for the agent.
                context_lines = []
                for r in pipeline.results[:args.top_k]:
                    label = f"[{r.memory_type}] " if r.memory_type else ""
                    summary = r.summary or ""
                    context_lines.append(f"{label}{summary}")
                context_text = "\n\n".join(context_lines) or "(empty)"

                await asyncio.sleep(1.5)
                verdict = await _judge(
                    api_client, args.judge_model,
                    sc.user_message, sc.gold_answer_hint,
                    context_text,
                )
                results.append({
                    "name": sc.name,
                    "question": sc.user_message,
                    "gold_hint": sc.gold_answer_hint,
                    "n_results": len(pipeline.results),
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
    sufficient = sum(1 for r in results if r["sufficient"])
    rate = sufficient / n

    print()
    print("=" * 76)
    print(f"CONTEXT PACKING EVAL — {n} scenarios, top_k={args.top_k}")
    print("=" * 76)
    print(f"\n  Sufficiency: {sufficient}/{n} ({100*rate:.0f}%)\n")
    for r in results:
        marker = "OK  " if r["sufficient"] else "FAIL"
        print(f"  [{marker}] {r['name']:<28s} "
              f"n_results={r['n_results']:>2}  "
              f"reason: {r['reason'][:60]}")
    print("=" * 76)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    md = [
        f"# End-to-end context packing eval — {n} scenarios",
        "",
        f"- judge: `{args.judge_model}`",
        f"- top_k: {args.top_k}",
        f"- sufficiency: **{sufficient}/{n} ({100*rate:.0f}%)**",
        "",
        "## Per-scenario",
        "",
        "| name | sufficient | n_results | reason |",
        "|---|---|---:|---|",
    ]
    for r in results:
        md.append(f"| {r['name']} | {'OK' if r['sufficient'] else 'FAIL'} | "
                  f"{r['n_results']} | {r['reason']} |")
    args.out.write_text("\n".join(md), encoding="utf-8")
    args.out_json.write_text(json.dumps({
        "n": n, "sufficient": sufficient, "rate": rate,
        "judge_model": args.judge_model,
        "results": results,
    }, indent=2), encoding="utf-8")
    print(f"\nWrote: {args.out}")
    print(f"Wrote: {args.out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
