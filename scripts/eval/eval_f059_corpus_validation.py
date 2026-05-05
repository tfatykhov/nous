"""F059 hallucination guard — corpus validation.

Runs `ConversationCompactor.compact` against real conversations from
the eval corpora and records the guard's suspect entities so we can
measure fire rate + spot false positives BEFORE turning fallback on
in prod.

Two corpora:
  - `prod-flavored` — the 15 hand-built scenarios from
    eval_compaction_fidelity.py (modeled on nous-prod conversation
    shapes: config values, decisions, version pins, deploys, schedules,
    incidents).
  - `longmemeval` — multi-turn haystacks from the LongMemEval_S cache
    at ~/.cache/nous-eval/longmemeval/longmemeval_s_cleaned.json.

Output is a markdown + JSON report so we can hand-audit suspect lists.

Usage:
    uv run python scripts/eval/eval_f059_corpus_validation.py \
        --corpus prod-flavored --n 15
    uv run python scripts/eval/eval_f059_corpus_validation.py \
        --corpus longmemeval --n 20

Cost: ~$0.30 per scenario (one Sonnet compaction call), no judge.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

from nous.api.anthropic_client import create_client
from nous.api.compaction import (
    ConversationCompactor,
    detect_hallucinated_entities,
)
from nous.api.models import ApiResponse, Conversation, Message
from nous.config import Settings
from nous_eval._oat_preamble import RateLimiter, call_with_retries, with_oat_preamble


_LME_CACHE = (
    Path.home() / ".cache" / "nous-eval" / "longmemeval"
    / "longmemeval_s_cleaned.json"
)


# Re-imported so we can run the prod-flavored corpus without adding a
# circular dependency on the judge eval.
def _load_prod_flavored() -> list[dict[str, Any]]:
    """Pull the 15 hand-built CompactScenarios out of the judge eval."""
    sys.path.insert(0, str(Path(__file__).parent))
    from eval_compaction_fidelity import SCENARIOS  # type: ignore
    return [
        {
            "name": sc.name,
            "messages": list(sc.messages),
            "expected_facts": sc.load_bearing_facts,
        }
        for sc in SCENARIOS
    ]


def _load_longmemeval(n: int) -> list[dict[str, Any]]:
    if not _LME_CACHE.exists():
        raise SystemExit(
            f"LongMemEval cache missing at {_LME_CACHE}. "
            f"Run `python -m nous_eval.ingest_longmemeval --n 20` first."
        )
    data = json.loads(_LME_CACHE.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise SystemExit(f"Unexpected LME JSON shape at {_LME_CACHE}")

    out: list[dict[str, Any]] = []
    for entry in data[:n]:
        qid = entry.get("question_id", "?")
        messages: list[tuple[str, str]] = []
        for session in entry.get("haystack_sessions", []) or []:
            for turn in session if isinstance(session, list) else []:
                role = turn.get("role") if isinstance(turn, dict) else None
                content = turn.get("content") if isinstance(turn, dict) else None
                if role and content and isinstance(content, str):
                    messages.append((role, content))
        # Cap at ~40 turns so the compactor has something to chew on
        # without blowing the context window of a single API call.
        if len(messages) < 4:
            continue
        messages = messages[:40]
        out.append(
            {
                "name": qid,
                "messages": messages,
                "expected_facts": [entry.get("answer", "")],
            }
        )
    return out


async def _run_corpus(
    corpus: list[dict[str, Any]], settings: Settings, api_client: Any
) -> list[dict[str, Any]]:
    rate_limiter = RateLimiter(min_interval_s=2.5)

    async def _api_call(
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        skip_thinking: bool = False,
        model_override: str | None = None,
    ) -> ApiResponse:
        payload: dict[str, Any] = {
            "model": model_override or settings.background_model,
            "max_tokens": 4000,
            "temperature": 0,
            "system": with_oat_preamble(system_prompt),
            "messages": messages,
        }
        if tools:
            payload["tools"] = tools
        response = await call_with_retries(
            api_client, payload, rate_limiter=rate_limiter
        )
        return ApiResponse(
            content=response.content,
            stop_reason=getattr(response, "stop_reason", "end_turn"),
            usage=getattr(response, "usage", {}),
        )

    compactor = ConversationCompactor(settings)
    results: list[dict[str, Any]] = []

    for entry in corpus:
        msgs = [Message(role=r, content=c) for r, c in entry["messages"]]
        conv = Conversation(session_id=f"f059-{entry['name']}", messages=msgs)
        cut_point = max(2, len(msgs) - 2)
        convo_text = "\n\n".join(
            f"[{m.role.upper()}]: {m.content}" for m in msgs[:cut_point]
        )

        try:
            await compactor.compact(
                conv,
                [{"role": m.role, "content": m.content} for m in msgs],
                _api_call,
                cut_point,
            )
        except Exception as exc:
            results.append(
                {
                    "name": entry["name"],
                    "error": str(exc),
                    "n_turns": len(entry["messages"]),
                }
            )
            continue

        summary_text = ""
        for m in conv.messages[:3]:
            if "[Previous conversation summary]" in (m.content or ""):
                summary_text = m.content
                break

        suspects = detect_hallucinated_entities(convo_text, summary_text)
        results.append(
            {
                "name": entry["name"],
                "n_turns": len(entry["messages"]),
                "summary_chars": len(summary_text),
                "guard_suspect_count": len(suspects),
                "guard_suspects": suspects,
            }
        )
        print(
            f"  [{entry['name'][:30]:<30s}] turns={len(entry['messages'])} "
            f"suspects={len(suspects)} "
            f"{(suspects[:3] if suspects else [])}"
        )

    return results


def _write_report(
    corpus_label: str, results: list[dict[str, Any]], out_md: Path, out_json: Path
) -> None:
    out_md.parent.mkdir(parents=True, exist_ok=True)
    fired = sum(1 for r in results if r.get("guard_suspect_count", 0) > 0)
    errored = sum(1 for r in results if "error" in r)
    total_susp = sum(r.get("guard_suspect_count", 0) for r in results)

    md = [
        f"# F059 hallucination guard — `{corpus_label}` corpus",
        "",
        f"- scenarios: **{len(results)}**, errored: **{errored}**",
        f"- guard fired (>=1 suspect): **{fired}/{len(results)}** "
        f"({100 * fired / max(1, len(results)):.1f}%)",
        f"- total suspect entities: **{total_susp}**",
        "",
        "| name | turns | summary chars | suspects |",
        "|---|---:|---:|---|",
    ]
    for r in results:
        if "error" in r:
            md.append(
                f"| {r['name']} | {r['n_turns']} | — | error: {r['error'][:50]} |"
            )
            continue
        suspects = r.get("guard_suspects", [])
        sample = ", ".join(suspects[:5]) + ("…" if len(suspects) > 5 else "")
        md.append(
            f"| {r['name']} | {r['n_turns']} | {r['summary_chars']} | "
            f"{r['guard_suspect_count']}: {sample if sample else '—'} |"
        )

    if fired:
        md.extend(["", "## Suspect samples (full lists)", ""])
        for r in results:
            if r.get("guard_suspect_count", 0) > 0:
                md.append(f"### {r['name']}")
                md.append("")
                md.append("```")
                for s in r["guard_suspects"]:
                    md.append(f"  {s}")
                md.append("```")
                md.append("")

    out_md.write_text("\n".join(md), encoding="utf-8")
    out_json.write_text(
        json.dumps(
            {
                "corpus": corpus_label,
                "n_scenarios": len(results),
                "n_errored": errored,
                "n_fired": fired,
                "fire_rate": fired / max(1, len(results)),
                "total_suspects": total_susp,
                "results": results,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nWrote: {out_md}")
    print(f"Wrote: {out_json}")


async def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--corpus", choices=["prod-flavored", "longmemeval"], required=True)
    p.add_argument("--n", type=int, default=15, help="Max scenarios.")
    p.add_argument(
        "--out-md",
        type=Path,
        default=None,
        help="Markdown report path (auto-named if omitted).",
    )
    p.add_argument(
        "--out-json",
        type=Path,
        default=None,
        help="JSON report path (auto-named if omitted).",
    )
    args = p.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

    logging.basicConfig(level=logging.INFO)

    settings = Settings()
    if not (settings.anthropic_api_key or settings.anthropic_auth_token):
        print("ERROR: Anthropic creds required.", file=sys.stderr)
        return 2

    if args.corpus == "prod-flavored":
        corpus = _load_prod_flavored()[: args.n]
    else:
        corpus = _load_longmemeval(args.n)

    print(f"Loaded {len(corpus)} scenarios from `{args.corpus}` corpus.\n")

    api_client = create_client(settings)
    await api_client.start()
    try:
        results = await _run_corpus(corpus, settings, api_client)
    finally:
        await api_client.close()

    out_md = args.out_md or Path(
        f"reports/eval_f059_guard_{args.corpus.replace('-', '_')}.md"
    )
    out_json = args.out_json or Path(
        f"reports/eval_f059_guard_{args.corpus.replace('-', '_')}.json"
    )
    _write_report(args.corpus, results, out_md, out_json)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
