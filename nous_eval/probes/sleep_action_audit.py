"""Sleep-phase action-quality audit probe.

Sleep cycle health monitor (PR #404) detects "phase produced 0
output." Filter dry-run (PR #406) detects "phase filter would
select 0." This probe catches the third failure mode neither
covers: **phase fired AND produced output, but the output was
wrong.**

For two LLM-driven sleep phases (F031 contradiction resolution,
F027 cluster consolidation) we now persist a per-action event
to ``nous_system.events`` (sibling-PR instrumentation in
``sleep_handler.py``). This probe samples N events of each type
from the past N days, fetches the source-fact content by ID, and
asks Sonnet to judge each action against a focused rubric.

Output: per-phase quality score + sample of disputed actions for
human review.

Cost: ~$0.05 per judged action (Sonnet, ~600 tokens in / ~150 out).
Default --max-actions 50 → ~$2.50 per run.

Connects to live PROD READ-ONLY for events + fact content.

Run:
    set -a; source .env; set +a
    uv run python -m nous_eval.probes.sleep_action_audit

Exit code (with --strict):
    0 — both phases >= --quality-floor (default 0.70 = 70% correct)
    1 — at least one phase below floor
    2 — env / connection error
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

import asyncpg

from nous.api.anthropic_client import create_client
from nous.config import Settings
from nous_eval._oat_preamble import (
    RateLimiter, call_with_retries, with_oat_preamble,
)


_DEFAULT_AGENT_ID = "nous-default"
_DEFAULT_JUDGE_MODEL = "claude-sonnet-4-6"
_DEFAULT_MAX_ACTIONS = 50  # split across both phases
_DEFAULT_LOOKBACK_DAYS = 14
_DEFAULT_QUALITY_FLOOR = 0.70


@dataclass
class JudgedAction:
    event_type: str
    action_summary: str
    verdict: str   # "correct" | "wrong" | "ambiguous"
    reason: str
    raw_event: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Fetch-and-enrich
# ---------------------------------------------------------------------------


async def fetch_recent_actions(
    conn: asyncpg.Connection, agent_id: str,
    event_type: str, lookback_days: int, limit: int,
) -> list[dict]:
    rows = await conn.fetch(
        """
        SELECT data, created_at
        FROM nous_system.events
        WHERE agent_id = $1
          AND event_type = $2
          AND created_at > now() - ($3::int || ' days')::interval
        ORDER BY created_at DESC
        LIMIT $4
        """,
        agent_id, event_type, lookback_days, limit,
    )
    out: list[dict] = []
    for r in rows:
        d = r["data"]
        if isinstance(d, str):
            try:
                d = json.loads(d)
            except json.JSONDecodeError:
                continue
        out.append({"created_at": r["created_at"], "data": d})
    return out


async def fetch_fact_content(
    conn: asyncpg.Connection, fact_id: UUID,
) -> str | None:
    row = await conn.fetchrow(
        """
        SELECT content, active, superseded_by IS NOT NULL AS was_superseded
        FROM heart.facts WHERE id = $1
        """,
        fact_id,
    )
    if not row:
        return None
    suffix = ""
    if not row["active"]:
        suffix = " [INACTIVE]"
    return f"{row['content']}{suffix}"


# ---------------------------------------------------------------------------
# Judge prompts
# ---------------------------------------------------------------------------


_F031_RUBRIC = """You are auditing a contradiction-resolution decision the agent made during sleep.

Two facts were detected as contradictory. The agent chose an action:
  SUPERSEDE_A — fact A is the older stale version; deactivate A
  SUPERSEDE_B — fact B is the older stale version; deactivate B
  MERGE       — combine both into one richer fact (must include merged_content)
  KEEP_BOTH   — facts are not actually contradictory; keep both
  REMOVE_A    — fact A is provably wrong (factual error); deactivate A
  REMOVE_B    — fact B is provably wrong (factual error); deactivate B

The agent has a safety floor: any action with confidence below 0.7 OR a
MERGE without merged_content is automatically downgraded to KEEP_BOTH
before being applied. This is INTENTIONAL safety behavior, not a bug —
KEEP_BOTH preserves both facts and is reversible. When you see
"applied=KEEP_BOTH (downgraded from MERGE)", evaluate the APPLIED
action (KEEP_BOTH) — i.e., would keeping both facts be acceptable
here? — not the raw model output.

Fact A: {fact_a}

Fact B: {fact_b}

Agent's applied action: {applied_action}
Raw model output (pre-downgrade): {raw_action}
Agent's reason: {reason}
Confidence: {confidence}

Was the APPLIED action correct? Reply with strict JSON:
{{"verdict": "correct" | "wrong" | "ambiguous", "reason": "<brief>"}}

Use "ambiguous" only when both A and B contain so little context that no
verdict can be reached without external knowledge."""


_F027_RUBRIC = """You are auditing a cluster-consolidation decision the agent made during sleep.

Multiple facts about the same subject were grouped and either merged
into one (MERGED), refused by the LLM (LLM_REFUSED), or rejected by
the admission gate (REJECTED_BY_ADMISSION).

Subject: {subject}

Source facts ({source_count}):
{source_facts}

Outcome: {outcome}
Merged content (if any): {merged_content}

Was this action correct? Specifically:
  - If MERGED: does merged_content faithfully capture all distinct
    information from the source facts without dropping or distorting?
  - If LLM_REFUSED: were the source facts genuinely too disparate to
    merge into one fact (correct refusal), or did they share a clear
    common claim the LLM should have caught (wrong refusal)?
  - If REJECTED_BY_ADMISSION: was the merged_content clearly redundant
    with existing facts (correct rejection) or substantively new
    (wrong rejection)?

Reply with strict JSON:
{{"verdict": "correct" | "wrong" | "ambiguous", "reason": "<brief>"}}"""


# ---------------------------------------------------------------------------
# Per-phase judge runners
# ---------------------------------------------------------------------------


async def _judge_one(
    api_client, model: str, prompt: str,
    rate_limiter: RateLimiter,
) -> dict:
    payload = {
        "model": model,
        "max_tokens": 200,
        "temperature": 0,
        "system": with_oat_preamble(),
        "messages": [{"role": "user", "content": prompt}],
    }
    try:
        resp = await call_with_retries(api_client, payload,
                                       rate_limiter=rate_limiter)
    except Exception as exc:
        return {"verdict": "ambiguous", "reason": f"judge error: {exc}"}
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
        return {"verdict": "ambiguous", "reason": "judge parse error"}


async def audit_f031(
    events_conn: asyncpg.Connection,
    facts_conn: asyncpg.Connection,
    api_client,
    judge_model: str,
    rate_limiter: RateLimiter,
    agent_id: str,
    lookback_days: int,
    max_actions: int,
) -> list[JudgedAction]:
    actions = await fetch_recent_actions(
        events_conn, agent_id, "f031_contradiction_resolution",
        lookback_days, max_actions,
    )
    judged: list[JudgedAction] = []
    for a in actions:
        d = a["data"]
        # Fetch source-fact content for the rubric.
        try:
            fact1_id = UUID(str(d.get("fact1_id", "")))
            fact2_id = UUID(str(d.get("fact2_id", "")))
        except (TypeError, ValueError):
            continue
        fact_a = await fetch_fact_content(facts_conn, fact1_id) or "(unknown)"
        fact_b = await fetch_fact_content(facts_conn, fact2_id) or "(unknown)"
        prompt = _F031_RUBRIC.format(
            fact_a=fact_a[:600],
            fact_b=fact_b[:600],
            applied_action=d.get("applied_action", "?"),
            raw_action=d.get("raw_action", "?"),
            reason=str(d.get("reason", ""))[:300],
            confidence=d.get("confidence", "?"),
        )
        verdict = await _judge_one(api_client, judge_model, prompt,
                                   rate_limiter)
        judged.append(JudgedAction(
            event_type="f031_contradiction_resolution",
            action_summary=(
                f"{d.get('applied_action', '?')}"
                f"{'(downgraded from ' + d.get('raw_action') + ')' if d.get('raw_action') and d.get('raw_action') != d.get('applied_action') else ''}"
            ),
            verdict=verdict.get("verdict", "ambiguous"),
            reason=verdict.get("reason", ""),
            raw_event=d,
        ))
    return judged


async def audit_f027(
    events_conn: asyncpg.Connection,
    facts_conn: asyncpg.Connection,
    api_client,
    judge_model: str,
    rate_limiter: RateLimiter,
    agent_id: str,
    lookback_days: int,
    max_actions: int,
) -> list[JudgedAction]:
    actions = await fetch_recent_actions(
        events_conn, agent_id, "f027_cluster_merge",
        lookback_days, max_actions,
    )
    judged: list[JudgedAction] = []
    for a in actions:
        d = a["data"]
        ids = d.get("source_fact_ids", []) or []
        source_facts = []
        for sid in ids[:6]:  # cap; clusters can be large
            try:
                content = await fetch_fact_content(
                    facts_conn, UUID(str(sid))
                ) or "(unknown)"
            except (TypeError, ValueError):
                content = "(invalid id)"
            source_facts.append(f"- {content[:300]}")
        prompt = _F027_RUBRIC.format(
            subject=d.get("subject", "?"),
            source_count=d.get("source_count", len(ids)),
            source_facts="\n".join(source_facts) or "(none)",
            outcome=d.get("outcome", "?"),
            merged_content=str(d.get("merged_content") or "(n/a)")[:500],
        )
        verdict = await _judge_one(api_client, judge_model, prompt,
                                   rate_limiter)
        judged.append(JudgedAction(
            event_type="f027_cluster_merge",
            action_summary=(
                f"{d.get('outcome', '?')} on {d.get('subject', '?')[:40]} "
                f"({d.get('source_count', len(ids))} facts)"
            ),
            verdict=verdict.get("verdict", "ambiguous"),
            reason=verdict.get("reason", ""),
            raw_event=d,
        ))
    return judged


# ---------------------------------------------------------------------------
# Aggregation + reporting
# ---------------------------------------------------------------------------


def aggregate_quality(
    judged: list[JudgedAction],
) -> dict[str, dict]:
    """Per-event-type aggregate: total / correct / wrong / ambiguous /
    quality_score (correct / (correct + wrong); excludes ambiguous)."""
    by_type: dict[str, list[JudgedAction]] = {}
    for j in judged:
        by_type.setdefault(j.event_type, []).append(j)
    out: dict[str, dict] = {}
    for et, items in by_type.items():
        n = len(items)
        correct = sum(1 for j in items if j.verdict == "correct")
        wrong = sum(1 for j in items if j.verdict == "wrong")
        ambiguous = n - correct - wrong
        decisive = correct + wrong
        score = (correct / decisive) if decisive > 0 else float("nan")
        out[et] = {
            "n": n, "correct": correct, "wrong": wrong,
            "ambiguous": ambiguous, "quality_score": score,
        }
    return out


def overall_exit_code(
    aggregate: dict[str, dict], floor: float,
) -> int:
    """1 if any phase below floor, else 0. NaN scores (no decisive
    judgments) don't trip the gate — caller should investigate."""
    for et, stats in aggregate.items():
        s = stats["quality_score"]
        if s == s and s < floor:  # NaN guard
            return 1
    return 0


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


async def run(
    events_conn: asyncpg.Connection,
    facts_conn: asyncpg.Connection,
    api_client,
    *,
    agent_id: str,
    judge_model: str,
    lookback_days: int,
    max_actions_per_phase: int,
) -> list[JudgedAction]:
    rate_limiter = RateLimiter(min_interval_s=2.5)
    f031 = await audit_f031(
        events_conn, facts_conn, api_client, judge_model,
        rate_limiter, agent_id, lookback_days, max_actions_per_phase,
    )
    f027 = await audit_f027(
        events_conn, facts_conn, api_client, judge_model,
        rate_limiter, agent_id, lookback_days, max_actions_per_phase,
    )
    return f031 + f027


def _print_report(
    judged: list[JudgedAction], aggregate: dict[str, dict],
    quality_floor: float, agent_id: str,
) -> None:
    print()
    print("=" * 84)
    print(f"SLEEP ACTION-QUALITY AUDIT - agent={agent_id}")
    print(f"  judged: {len(judged)} actions, floor: {quality_floor:.0%}")
    print("=" * 84)
    if not aggregate:
        print("\n  NO actions found in window.")
        return
    print()
    for et, stats in aggregate.items():
        s = stats["quality_score"]
        score_str = f"{s:.0%}" if s == s else "N/A"
        verdict = ("[OK]  " if s == s and s >= quality_floor
                   else "[FAIL]" if s == s else "[----]")
        print(f"  {verdict} {et:<32}  "
              f"n={stats['n']:>3}  "
              f"correct={stats['correct']:>3}  "
              f"wrong={stats['wrong']:>3}  "
              f"ambiguous={stats['ambiguous']:>3}  "
              f"score={score_str}")
    # Surface up to 5 wrong-judged samples for triage
    wrong = [j for j in judged if j.verdict == "wrong"]
    if wrong:
        print(f"\n  Wrong-judged samples (showing {min(5, len(wrong))} of "
              f"{len(wrong)}):")
        for j in wrong[:5]:
            print(f"    [{j.event_type}] {j.action_summary}")
            print(f"      reason: {j.reason[:120]}")
    print()


async def _async_main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

    p = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    p.add_argument("--prod-host",
                   default=os.environ.get("PROD_DB_HOST", "192.168.1.141"))
    p.add_argument("--prod-port", type=int,
                   default=int(os.environ.get("DB_PORT", "5432")))
    p.add_argument("--prod-user", default=os.environ.get("DB_USER", "nous"))
    p.add_argument("--prod-password", default=os.environ.get("DB_PASSWORD"))
    p.add_argument("--prod-db", default=os.environ.get("DB_NAME", "nous"))
    p.add_argument("--agent-id", default=_DEFAULT_AGENT_ID)
    p.add_argument("--judge-model", default=_DEFAULT_JUDGE_MODEL)
    p.add_argument("--lookback-days", type=int,
                   default=_DEFAULT_LOOKBACK_DAYS)
    p.add_argument("--max-actions-per-phase", type=int,
                   default=_DEFAULT_MAX_ACTIONS // 2,
                   help=("Sample cap per phase. Total cost ~$0.05 per "
                         "action so 25 per phase = ~$2.50 per run."))
    p.add_argument("--quality-floor", type=float,
                   default=_DEFAULT_QUALITY_FLOOR)
    p.add_argument("--strict", action="store_true",
                   help="Exit 1 if any phase quality_score < floor.")
    p.add_argument("--out", type=Path,
                   default=Path("reports/eval_sleep_action_audit.md"))
    p.add_argument("--out-json", type=Path,
                   default=Path("reports/eval_sleep_action_audit.json"))
    args = p.parse_args(argv)

    if not args.prod_password:
        print(
            "ERROR: prod database password not set. Set DB_PASSWORD in env "
            "or pass --prod-password=<value>.",
            file=sys.stderr,
        )
        return 2

    logging.basicConfig(level=logging.WARNING)

    main_settings = Settings()
    if not (main_settings.anthropic_api_key
            or main_settings.anthropic_auth_token):
        print("ERROR: Anthropic creds required.", file=sys.stderr)
        return 2

    events_conn = await asyncpg.connect(
        host=args.prod_host, port=args.prod_port,
        user=args.prod_user, password=args.prod_password,
        database=args.prod_db,
    )
    facts_conn = await asyncpg.connect(
        host=args.prod_host, port=args.prod_port,
        user=args.prod_user, password=args.prod_password,
        database=args.prod_db,
    )
    api_client = create_client(main_settings)
    await api_client.start()

    try:
        # Defense-in-depth: read-only at the Postgres level.
        await events_conn.execute("SET default_transaction_read_only = on")
        await facts_conn.execute("SET default_transaction_read_only = on")
        judged = await run(
            events_conn, facts_conn, api_client,
            agent_id=args.agent_id,
            judge_model=args.judge_model,
            lookback_days=args.lookback_days,
            max_actions_per_phase=args.max_actions_per_phase,
        )
    finally:
        await api_client.close()
        await events_conn.close()
        await facts_conn.close()

    aggregate = aggregate_quality(judged)
    _print_report(judged, aggregate, args.quality_floor, args.agent_id)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out_json.write_text(
        json.dumps(
            {
                "agent_id": args.agent_id,
                "judge_model": args.judge_model,
                "lookback_days": args.lookback_days,
                "quality_floor": args.quality_floor,
                "aggregate": aggregate,
                "actions": [
                    {
                        "event_type": j.event_type,
                        "action_summary": j.action_summary,
                        "verdict": j.verdict,
                        "reason": j.reason,
                    }
                    for j in judged
                ],
            },
            indent=2, default=str,
        ),
        encoding="utf-8",
    )
    md_lines = [
        f"# Sleep action-quality audit - agent_id=`{args.agent_id}`",
        f"- judge: `{args.judge_model}`",
        f"- lookback: {args.lookback_days} days",
        f"- quality floor: {args.quality_floor:.0%}",
        "",
        "## Per-phase",
        "",
        "| event type | n | correct | wrong | ambiguous | quality |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for et, s in aggregate.items():
        score_str = (f"{s['quality_score']:.0%}"
                     if s['quality_score'] == s['quality_score']
                     else "N/A")
        md_lines.append(
            f"| {et} | {s['n']} | {s['correct']} | {s['wrong']} | "
            f"{s['ambiguous']} | **{score_str}** |"
        )
    args.out.write_text("\n".join(md_lines), encoding="utf-8")
    print(f"Wrote: {args.out}")
    print(f"Wrote: {args.out_json}")

    if args.strict:
        return overall_exit_code(aggregate, args.quality_floor)
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_async_main(argv))


if __name__ == "__main__":
    raise SystemExit(main())
