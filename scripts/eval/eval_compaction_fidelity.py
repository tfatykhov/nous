"""Compaction fidelity eval (cognitive-layer plan item 3).

Tests `nous/api/compaction.py::ConversationCompactor.compact` —
specifically: when the LLM compaction summarizer runs, are the
load-bearing facts in the conversation preserved in the summary?

Failure mode: silent fact loss. Easy to ship a regression here.

Method:
  1. Synthesize 15 long conversations, each with 1-3 "load-bearing
     facts" embedded across the message turns (e.g. "the API key is
     X-1234", "we decided to use Postgres").
  2. Run ConversationCompactor.compact with a real Anthropic call
     (default Sonnet via background_model).
  3. Sonnet judge: for each fact, is it preserved in the resulting
     summary (verbatim or paraphrased without distortion)?
  4. Score: per-fact preservation rate, per-conversation rate.

Cost: ~$2 (15 compactions × ~2k tokens each + judge calls).

Usage:
    uv run python scripts/eval/eval_compaction_fidelity.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from nous.api.anthropic_client import create_client
from nous.api.compaction import (
    ConversationCompactor,
    detect_hallucinated_entities,
)
from nous.api.models import Conversation, Message
from nous.config import Settings
from nous_eval._oat_preamble import (
    RateLimiter, call_with_retries, with_oat_preamble,
)


_DEFAULT_MODEL = "claude-sonnet-4-6"


@dataclass
class CompactScenario:
    name: str
    messages: list[tuple[str, str]]  # (role, content) pairs
    load_bearing_facts: list[str]  # facts that MUST survive compaction
    notes: str = ""


# Hand-crafted scenarios. Each has 6-10 turns of dialog with 1-3 facts
# embedded that the compactor must preserve. Length is enough that
# real compaction is needed.
SCENARIOS: list[CompactScenario] = [
    CompactScenario(
        "config_value",
        [("user", "I'm setting up the new staging environment."),
         ("assistant", "Sure, what services do you need configured?"),
         ("user", "API gateway, the orders service, and a Redis cache. The Redis port is 6379 by default but I'm using port 6380 for staging to avoid conflict."),
         ("assistant", "Got it — Redis on 6380, API gateway, and orders service. Want me to start with the orders config?"),
         ("user", "Yes, start there. Use the staging-orders database name."),
         ("assistant", "Working on the orders service config now. I'll wire it to the staging-orders database."),
         ("user", "And remember the orders service needs to listen on 0.0.0.0:8080, not just localhost."),
         ("assistant", "Acknowledged: bind to 0.0.0.0:8080.")],
        ["Redis on staging uses port 6380 (not the default 6379)",
         "Orders service uses database 'staging-orders'",
         "Orders service binds to 0.0.0.0:8080"],
    ),
    CompactScenario(
        "decision_with_rationale",
        [("user", "Should I use Postgres or MongoDB for the new feature?"),
         ("assistant", "Depends on your access patterns. Document-shaped or relational?"),
         ("user", "Mostly relational, with a few JSON fields for metadata."),
         ("assistant", "Postgres handles JSON well — JSONB columns. Recommend Postgres."),
         ("user", "OK let's go with Postgres. I'll use JSONB for the metadata column."),
         ("assistant", "Good. Want me to draft the schema?"),
         ("user", "Yes please.")],
        ["Decided to use Postgres (not MongoDB) for the new feature",
         "Metadata field will use JSONB column type"],
    ),
    CompactScenario(
        "person_attributes",
        [("user", "Tell me what we know about the new client, Acme Corp."),
         ("assistant", "I don't have prior context. What should I record?"),
         ("user", "Acme Corp is a manufacturing company based in Cleveland. CEO is Sarah Chen. Annual revenue is $250M. They use SAP for ERP."),
         ("assistant", "Recorded: Acme Corp, manufacturing, Cleveland, CEO Sarah Chen, $250M revenue, SAP ERP."),
         ("user", "Right. Their primary contact is Marcus Webb at marcus.webb@acme.com."),
         ("assistant", "Got it.")],
        ["Acme Corp is based in Cleveland",
         "Sarah Chen is the CEO of Acme Corp",
         "Acme Corp uses SAP for ERP",
         "Marcus Webb is the primary contact at marcus.webb@acme.com"],
    ),
    CompactScenario(
        "schedule_change",
        [("user", "What's my schedule today?"),
         ("assistant", "Standup at 9am, then pair-programming with Tim 10-12, lunch with Sarah 12:30, design review at 3pm."),
         ("user", "The standup got moved to 9:30. And lunch with Sarah is rescheduled to tomorrow."),
         ("assistant", "Updated: standup 9:30, no lunch with Sarah today (rescheduled to tomorrow)."),
         ("user", "Also add a 4pm call with the client. Acme Corp."),
         ("assistant", "Added: 4pm Acme Corp call."),
         ("user", "Thanks.")],
        ["Standup is now at 9:30 (moved from 9am)",
         "Lunch with Sarah is rescheduled to tomorrow (not today)",
         "4pm call with Acme Corp added today"],
    ),
    CompactScenario(
        "credential_redacted",
        [("user", "I'm setting up CI for the new repo."),
         ("assistant", "Got it. What integrations do you need?"),
         ("user", "GitHub, Slack notifications, and AWS deploys. The AWS account is 123456789012 and the IAM role is github-actions-deployer."),
         ("assistant", "Recorded — AWS account 123456789012, IAM role github-actions-deployer."),
         ("user", "Slack webhook is in the secrets manager under ci-slack-webhook."),
         ("assistant", "Acknowledged."),
         ("user", "Default deploy target is the us-east-2 region.")],
        ["AWS account ID is 123456789012",
         "IAM role for deploys is github-actions-deployer",
         "Slack webhook secret name is ci-slack-webhook",
         "Default deploy region is us-east-2"],
    ),
    CompactScenario(
        "long_chat_one_fact",
        [("user", "How are you doing?"),
         ("assistant", "Fine, ready to help. What's up?"),
         ("user", "Just chatting. Did you watch the game last night?"),
         ("assistant", "I don't watch games — I'm an agent. But I can pull stats."),
         ("user", "Ha. By the way, my new phone number is 555-0123, the old one is being deprecated."),
         ("assistant", "Recorded — new number 555-0123."),
         ("user", "Cool. Anyway, back to work later.")],
        ["User's new phone number is 555-0123 (old one deprecated)"],
    ),
    CompactScenario(
        "version_pin",
        [("user", "What Python version are we on?"),
         ("assistant", "Looking at pyproject.toml — 3.12 minimum, 3.14 in the container."),
         ("user", "OK we need to pin to 3.12.7 specifically — there's a regression in 3.12.8."),
         ("assistant", "Got it: pin to 3.12.7."),
         ("user", "And we should add a CI check to enforce this.")],
        ["Pinned to Python 3.12.7 specifically (because 3.12.8 has a regression)"],
    ),
    CompactScenario(
        "negative_decision",
        [("user", "I'm thinking of adding Kafka for event streaming."),
         ("assistant", "Heavy infrastructure. What's the use case?"),
         ("user", "User events for analytics. Maybe 100 events/sec at peak."),
         ("assistant", "100 events/sec is well within Postgres LISTEN/NOTIFY range. Skip Kafka."),
         ("user", "Hmm yeah, let's skip Kafka for now. Postgres LISTEN/NOTIFY it is.")],
        ["Decided NOT to add Kafka",
         "Will use Postgres LISTEN/NOTIFY for event streaming instead"],
    ),
    CompactScenario(
        "fact_in_assistant_turn",
        [("user", "What's the largest table in our DB?"),
         ("assistant", "Running a quick count... the biggest is heart.facts with 1,629 rows. Next is heart.episodes with 492."),
         ("user", "Surprising. I'd have thought episodes were higher."),
         ("assistant", "Episodes get summarized and compressed, so the active count stays lower."),
         ("user", "OK. What about graph_edges?"),
         ("assistant", "graph_edges has 2,589 rows, of which 1,604 are 'related_to'.")],
        ["heart.facts has 1,629 rows (largest table)",
         "heart.episodes has 492 rows",
         "graph_edges has 2,589 total rows; 1,604 are 'related_to'"],
    ),
    CompactScenario(
        "user_preference",
        [("user", "How should you respond to me?"),
         ("assistant", "Concise, no filler, with code examples when relevant. Want to set anything else?"),
         ("user", "Yes — never use emojis. Bullet points only when listing >3 items. And quote file:line for code references."),
         ("assistant", "Recorded: no emojis, bullets only for >3 items, quote file:line for code refs."),
         ("user", "Also stop saying 'great question' and 'absolutely' — they're filler."),
         ("assistant", "Got it.")],
        ["User wants no emojis in responses",
         "User wants bullet points only when listing more than 3 items",
         "User wants file:line citations for code references",
         "User dislikes filler phrases like 'great question' and 'absolutely'"],
    ),
    CompactScenario(
        "deadline_change",
        [("user", "Status of the Q2 launch?"),
         ("assistant", "On track per last update. Three features remaining."),
         ("user", "Original deadline was June 1, but it's been pushed to July 15 because of the security review."),
         ("assistant", "Updated: launch deadline now July 15 (moved from June 1)."),
         ("user", "And the security review is being led by Priya Patel from infosec.")],
        ["Q2 launch deadline moved from June 1 to July 15",
         "Security review delay was the reason for the deadline change",
         "Priya Patel from infosec leads the security review"],
    ),
    CompactScenario(
        "multiple_subjects",
        [("user", "Quick check-in: any issues with deploys this week?"),
         ("assistant", "One issue: Tuesday's deploy of orders-service failed at 2:14pm due to migration timeout."),
         ("user", "Migration timeout? On what table?"),
         ("assistant", "It was on user_sessions — added a non-null column without batching."),
         ("user", "Right. We fixed it by chunking to batches of 5000 rows. It took 3 retries to land."),
         ("assistant", "Good — chunking on user_sessions worked after 3 retries.")],
        ["Tuesday's orders-service deploy failed due to migration timeout",
         "Migration was on user_sessions table (non-null column added without batching)",
         "Fix was to chunk to 5000 rows per batch",
         "Took 3 retries to land the fix"],
    ),
    CompactScenario(
        "nested_priorities",
        [("user", "What should I focus on this week?"),
         ("assistant", "Mostly the Q2 launch features. Anything else come up?"),
         ("user", "Yes — security audit findings need to be triaged by Friday. Three are P1, one is P0."),
         ("assistant", "Acknowledged: 3 P1 + 1 P0 audit findings, triage by Friday."),
         ("user", "The P0 is a SQL injection in the search endpoint. Block on that."),
         ("assistant", "Confirmed: P0 = SQL injection in search endpoint, blocking. Triage P1s after.")],
        ["3 P1 + 1 P0 security audit findings to triage by Friday",
         "The P0 is a SQL injection in the search endpoint",
         "P0 is blocking; P1s are after"],
    ),
    CompactScenario(
        "model_change",
        [("user", "Are we still using Sonnet for everything?"),
         ("assistant", "Mostly. Sonnet for chat, Haiku for some classifiers."),
         ("user", "Switch the chat model to Opus. The new context-engineering work needs the bigger model."),
         ("assistant", "Updated: chat model is now Opus (was Sonnet). Haiku stays for classifiers."),
         ("user", "Also bump the thinking budget to 18000 tokens.")],
        ["Chat model changed from Sonnet to Opus",
         "Reason: context-engineering work needs the bigger model",
         "Thinking budget set to 18000 tokens",
         "Haiku is still the classifier model"],
    ),
    CompactScenario(
        "incident_postmortem",
        [("user", "Walk me through Friday's incident."),
         ("assistant", "Sure — outage started 3:47 PM EDT and lasted 23 minutes."),
         ("user", "What was the root cause?"),
         ("assistant", "A Redis eviction bug — keys with TTL=0 were being deleted prematurely. Affected the session store."),
         ("user", "Fix?"),
         ("assistant", "Patched in PR #482, set TTL minimum to 60 seconds."),
         ("user", "Who was on-call?"),
         ("assistant", "Sarah Chen, with Emerson on backup.")],
        ["Friday's outage was 23 minutes (started 3:47 PM EDT)",
         "Root cause: Redis eviction bug with TTL=0 keys",
         "Fix: PR #482 set TTL minimum to 60 seconds",
         "Sarah Chen was on-call, Emerson on backup"],
    ),
]


_JUDGE_PROMPT = """You are evaluating whether a conversation summary preserves specific load-bearing facts.

ORIGINAL CONVERSATION (compacted from):
{conversation}

SUMMARY (output of compaction):
{summary}

For each fact below, determine whether the summary preserves it accurately. The fact may be paraphrased — it does NOT need to be verbatim. The fact must be derivable from the summary without distortion.

FACTS TO CHECK:
{facts}

Respond with strict JSON:
{{
  "verdicts": [
    {{"fact": "<fact 1 text>", "preserved": true|false, "reason": "<short>"}},
    ...
  ]
}}

Mark `preserved: false` if the fact is missing, distorted, contradicted, or so vague it can't be reconstructed."""


async def _judge(api_client, model: str, conversation: str,
                 summary: str, facts: list[str],
                 rate_limiter: RateLimiter) -> list[dict]:
    """Have Sonnet judge fact preservation. Returns list of {fact, preserved, reason}."""
    facts_block = "\n".join(f"{i+1}. {f}" for i, f in enumerate(facts))
    prompt = _JUDGE_PROMPT.format(
        conversation=conversation[:6000],
        summary=summary[:3000],
        facts=facts_block,
    )
    payload = {
        "model": model,
        "max_tokens": 1500,
        "temperature": 0,
        "system": with_oat_preamble(),
        "messages": [{"role": "user", "content": prompt}],
    }
    response = await call_with_retries(api_client, payload, rate_limiter=rate_limiter)
    text = ""
    for block in response.content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text", "").strip()
            break
    # Strip code fences
    if text.startswith("```"):
        text = "\n".join(text.splitlines()[1:-1])
    try:
        data = json.loads(text)
        return data.get("verdicts", [])
    except Exception:
        return [{"fact": f, "preserved": False, "reason": "judge parse error"}
                for f in facts]


async def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--judge-model", default=_DEFAULT_MODEL)
    p.add_argument("--max-scenarios", type=int, default=15,
                   help="Limit how many scenarios to run (cost control).")
    p.add_argument("--out", type=Path, default=Path("reports/eval_compaction_fidelity.md"))
    p.add_argument("--out-json", type=Path, default=Path("reports/eval_compaction_fidelity.json"))
    args = p.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

    logging.basicConfig(level=logging.WARNING)

    settings = Settings()
    if not (settings.anthropic_api_key or settings.anthropic_auth_token):
        print("ERROR: Anthropic creds required.", file=sys.stderr)
        return 2

    api_client = create_client(settings)
    await api_client.start()

    # Shared rate limiter for both compactor + judge calls.
    rate_limiter = RateLimiter(min_interval_s=2.5)

    # ApiCaller protocol: (system_prompt, messages, tools=None,
    # skip_thinking=False, model_override=None) -> ApiResponse
    async def _api_call(
        system_prompt: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        skip_thinking: bool = False,
        model_override: str | None = None,
    ):
        from nous.api.models import ApiResponse
        payload = {
            "model": model_override or settings.background_model,
            "max_tokens": 4000,
            "temperature": 0,
            "system": with_oat_preamble(system_prompt),
            "messages": messages,
        }
        if tools:
            payload["tools"] = tools
        response = await call_with_retries(api_client, payload, rate_limiter=rate_limiter)
        # Wrap in ApiResponse-shaped object that compactor expects
        return ApiResponse(
            content=response.content,
            stop_reason=getattr(response, "stop_reason", "end_turn"),
            usage=getattr(response, "usage", {}),
        )

    compactor = ConversationCompactor(settings)

    results = []
    overall_facts = 0
    overall_preserved = 0

    try:
        for sc in SCENARIOS[:args.max_scenarios]:
            messages = [Message(role=r, content=c) for r, c in sc.messages]
            conv = Conversation(session_id=f"eval-{sc.name}", messages=messages)
            # cut at the second-to-last turn so we always have something to compact
            cut_point = max(2, len(messages) - 2)

            # Snapshot for judge prompt
            convo_text = "\n\n".join(
                f"[{m.role.upper()}]: {m.content}" for m in messages[:cut_point]
            )

            try:
                await compactor.compact(conv, [
                    {"role": m.role, "content": m.content} for m in messages
                ], _api_call, cut_point)
            except Exception as exc:
                results.append({
                    "name": sc.name, "compact_error": str(exc),
                    "facts": [{"fact": f, "preserved": False,
                               "reason": "compaction error"}
                              for f in sc.load_bearing_facts],
                })
                continue

            # Extract the summary text from the rebuilt messages
            summary_text = ""
            for m in conv.messages[:3]:
                if "[Previous conversation summary]" in (m.content or ""):
                    summary_text = m.content
                    break

            # Judge fact preservation
            verdicts = await _judge(
                api_client, args.judge_model, convo_text,
                summary_text, sc.load_bearing_facts,
                rate_limiter,
            )

            preserved = sum(1 for v in verdicts if v.get("preserved"))
            # F059 hallucination guard verdict, recorded alongside the
            # judge so we can compare guard fire rate against ground-truth
            # silent fact loss.
            guard_suspects = detect_hallucinated_entities(
                convo_text, summary_text
            )
            results.append({
                "name": sc.name,
                "n_facts": len(sc.load_bearing_facts),
                "preserved": preserved,
                "rate": preserved / len(sc.load_bearing_facts),
                "verdicts": verdicts,
                "summary": summary_text[:600],
                "guard_suspect_count": len(guard_suspects),
                "guard_suspects": guard_suspects,
            })
            overall_facts += len(sc.load_bearing_facts)
            overall_preserved += preserved
    finally:
        await api_client.close()

    overall_rate = overall_preserved / overall_facts if overall_facts else 0

    print()
    print("=" * 76)
    print(f"COMPACTION FIDELITY EVAL — {len(results)} scenarios")
    print("=" * 76)
    print(f"\n  Overall: {overall_preserved}/{overall_facts} facts preserved "
          f"({100*overall_rate:.1f}%)\n")
    for r in results:
        if "compact_error" in r:
            print(f"  [ERROR ] {r['name']:<28s} {r['compact_error'][:60]}")
            continue
        marker = "OK" if r["rate"] == 1.0 else ("PART" if r["rate"] > 0 else "FAIL")
        print(f"  [{marker:<6s}] {r['name']:<28s} "
              f"{r['preserved']}/{r['n_facts']} preserved")
        for v in r["verdicts"]:
            if not v.get("preserved"):
                print(f"          - DROPPED: {v.get('fact', '')[:70]}")
                if v.get("reason"):
                    print(f"            reason: {v['reason'][:70]}")
    print("=" * 76)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    md = [
        f"# Compaction fidelity eval — {len(results)} scenarios",
        "",
        f"- judge: `{args.judge_model}`",
        f"- overall fact preservation: **{overall_preserved}/{overall_facts} "
        f"({100*overall_rate:.1f}%)**",
        "- SUT: `nous.api.compaction.ConversationCompactor.compact`",
        "",
        "## Per-scenario",
        "",
        "| name | facts | preserved | rate |",
        "|---|---:|---:|---:|",
    ]
    for r in results:
        if "compact_error" in r:
            md.append(f"| {r['name']} | — | — | compact error |")
            continue
        md.append(f"| {r['name']} | {r['n_facts']} | {r['preserved']} | "
                  f"{r['rate']:.0%} |")
    # F059 guard summary — fire rate vs. judge "dropped fact" rate.
    fired = sum(
        1 for r in results
        if r.get("guard_suspect_count", 0) > 0
    )
    md.extend([
        "",
        "## F059 hallucination guard",
        "",
        f"- scenarios with guard suspects: **{fired}/{len(results)}**",
        "",
        "| name | dropped facts | guard suspects |",
        "|---|---:|---|",
    ])
    for r in results:
        if "compact_error" in r:
            continue
        n_dropped = sum(
            1 for v in r.get("verdicts", []) if not v.get("preserved")
        )
        suspects = r.get("guard_suspects", []) or []
        sample = ", ".join(suspects[:3]) + ("…" if len(suspects) > 3 else "")
        md.append(
            f"| {r['name']} | {n_dropped} | "
            f"{len(suspects)} ({sample if sample else '—'}) |"
        )

    md.extend(["", "## Dropped facts (samples)", ""])
    for r in results:
        if "compact_error" in r:
            continue
        for v in r.get("verdicts", []):
            if not v.get("preserved"):
                md.append(f"- **{r['name']}**: {v.get('fact', '')} — "
                          f"_{v.get('reason', '')}_")
    args.out.write_text("\n".join(md), encoding="utf-8")
    args.out_json.write_text(json.dumps({
        "n_scenarios": len(results),
        "n_facts": overall_facts,
        "preserved": overall_preserved,
        "overall_rate": overall_rate,
        "judge_model": args.judge_model,
        "results": results,
    }, indent=2), encoding="utf-8")
    print(f"\nWrote: {args.out}")
    print(f"Wrote: {args.out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
