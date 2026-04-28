"""F056 PR #4: episode summary eval CLI — LLM-judged quality.

Exercises `nous/handlers/episode_summarizer.py::EpisodeSummarizer.
summarize_episode` (line 104). Real signature:
`summarize_episode(self, episode_id: UUID, transcript: str,
agent_id: str | None = None) -> dict | None`. The eval seeds an
`Episode` row per fixture entry (required NOT-NULL columns: agent_id,
summary placeholder), calls summarize_episode, then Haiku-judges the
produced structured_summary on two dimensions:

- `key_point_coverage`: of N gold key-points, how many appear in the
  produced `key_points` list (sub-string OR semantic match)? 0..1.
- `summary_faithfulness`: does the produced `summary` text contain any
  claim NOT supported by the transcript? 0..1.

`summary_quality = mean(key_point_coverage) * mean(summary_faithfulness)`
is the gated primary metric (5pp drop threshold per spec §D).

Per F056 spec §D:
- Per-handler agent_id `nous-eval-handler-summary`
- Episode required NOT-NULL columns: `agent_id`, `summary` (the latter
  confusingly shares its name with the structured_summary column the
  eval is testing — set placeholder "<eval-stub>" to bypass NOT-NULL).
- Haiku LLM-judge with `payload["temperature"] = 0` (Haiku no seed yet)
- Ownership-aware llm_client lifecycle
- N=80 fixture (raised from spec §D v1's N=20 per devil's review math)
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import text

from nous.api.anthropic_client import AnthropicClient, create_client
from nous.config import Settings
from nous.handlers.episode_summarizer import EpisodeSummarizer
from nous.storage.database import Database
from nous_eval.config import EvalSettings
from nous_eval.handlers._cli_base import (
    HandlerResult,
    _DeleteSpec,
    clear_handler_state,
    run_handler_eval,
)
from nous_eval.handlers._jsonl import load_jsonl
from nous_eval.handlers._models import SummaryRow
from nous_eval.retrieval_runner import _build_heart_for_eval, _settings_for_eval_db

if TYPE_CHECKING:
    from nous.heart.heart import Heart

logger = logging.getLogger(__name__)


_AGENT_ID = "nous-eval-handler-summary"
_HANDLER_NAME = "summary"
_DEFAULT_FIXTURE = Path("tests/fixtures/handlers/summary_transcripts.jsonl")
# F056 spec §D estimates ~$0.36/run with Haiku. Using Sonnet (the default
# main_settings.background_model) is ~5x slower per call for 240 calls
# total — empirically observed to push the run past 60 min vs ~15 min
# with Haiku, with minimal accuracy gain on simple "yes/no/borderline"
# verdict tasks. Haiku 4.5 is sufficient.
_DEFAULT_JUDGE_MODEL = "claude-haiku-4-5-20251001"


def _add_judge_model_arg(parser) -> None:
    """Extra CLI flag: --judge-model overrides the LLM-judge model."""
    parser.add_argument(
        "--judge-model", default=_DEFAULT_JUDGE_MODEL,
        help=f"Anthropic model for LLM-judge calls (default {_DEFAULT_JUDGE_MODEL}). "
             f"Override to claude-sonnet-4-6 for higher-fidelity judging at ~5x cost+latency.",
    )


def _settings_with_summary_overrides(base: Settings) -> Settings:
    """Apply F056 §D required overrides.

    No special production-flag toggles needed — summarize_episode runs
    purely against the LLM client we inject. Just scope agent_id.
    """
    update: dict[str, Any] = {"agent_id": _AGENT_ID}
    return base.model_copy(update=update)


def filter_rows(rows: list[SummaryRow], *, include_unreviewed: bool) -> list[SummaryRow]:
    """Apply the reviewed_by gate. Mirrors qrels_loader.py:80-85 pattern."""
    if include_unreviewed:
        return rows
    return [r for r in rows if r.reviewed_by]


async def _seed_episode_stub(
    db: Database, agent_id: str, transcript: str,
) -> UUID:
    """Insert a stub Episode row; return its id.

    Required NOT-NULL columns per `nous/storage/models.py:310-345`:
    - `agent_id` (Text NOT NULL, no default)
    - `summary` (Text NOT NULL, no default — confusingly named: this is
      the existing short-form summary column, NOT the structured_summary
      JSONB column the eval is testing summarize_episode populates).
    All other columns either have server_default or are nullable.
    `structured_summary` is left NULL so summarize_episode doesn't early-
    return at episode_summarizer.py:126.
    """
    async with db.session() as session:
        result = await session.execute(
            text(
                """
                INSERT INTO heart.episodes (agent_id, summary, transcript)
                VALUES (:aid, :sum_stub, :transcript)
                RETURNING id
                """
            ),
            {
                "aid": agent_id,
                "sum_stub": "<eval-stub>",
                "transcript": transcript,
            },
        )
        episode_id = result.scalar_one()
        await session.commit()
        return episode_id


async def _judge_key_point_coverage(
    llm: AnthropicClient,
    model: str,
    gold_key_points: list[str],
    produced_key_points: list[str],
) -> float:
    """Ask Haiku: of N gold key-points, how many appear in produced list?

    Returns 0..1 (count_covered / count_gold). Sub-string OR semantic
    match — instruction left to the model.
    """
    if not gold_key_points:
        return 0.0
    gold_lines = "\n".join(f"- {p}" for p in gold_key_points)
    produced_lines = "\n".join(f"- {p}" for p in produced_key_points) or "(empty)"
    prompt = (
        f"Gold key points the summary MUST cover:\n{gold_lines}\n\n"
        f"Produced key points from the summarizer:\n{produced_lines}\n\n"
        f"Of the {len(gold_key_points)} gold key points, how many are "
        f"covered by the produced list (sub-string match OR semantic match)? "
        f"Reply with exactly one integer between 0 and {len(gold_key_points)}."
    )
    payload = {
        "model": model,
        "max_tokens": 8,
        "temperature": 0,
        # SdkAnthropicClient at anthropic_client.py:1014 does `payload["system"]`
        # (not .get) — KeyError if absent. Empty string satisfies both clients.
        "system": "",
        "messages": [{"role": "user", "content": prompt}],
    }
    try:
        response = await llm.call(payload)
    except Exception:
        logger.exception("summary eval: key_point_coverage judge call failed")
        return 0.0
    text_out = ""
    for block in response.content:
        if isinstance(block, dict) and block.get("type") == "text":
            text_out = block.get("text", "").strip()
            break
    # Extract the first integer from the response
    digits = "".join(ch for ch in text_out.split()[0] if ch.isdigit()) if text_out else ""
    if not digits:
        logger.warning("summary eval: kpc judge returned unparseable %r; counting 0", text_out)
        return 0.0
    covered = int(digits)
    return min(covered, len(gold_key_points)) / len(gold_key_points)


async def _judge_summary_faithfulness(
    llm: AnthropicClient,
    model: str,
    transcript: str,
    produced_summary: str,
) -> float:
    """Ask Haiku: does the produced summary contain any unsupported claims?

    Returns 1.0 if fully faithful, 0.0 if any unsupported claim, 0.5 if
    borderline. Trims transcript to first 4000 chars to bound prompt cost.
    """
    transcript_excerpt = transcript[:4000]
    prompt = (
        f"Transcript:\n{transcript_excerpt}\n\n"
        f"Produced summary:\n{produced_summary}\n\n"
        f"Does the produced summary contain any factual claim NOT supported "
        f"by the transcript? Reply with EXACTLY one of these three words and "
        f"NOTHING ELSE: yes, no, or borderline. "
        f"(yes = unsupported claim found, no = fully faithful, borderline = ambiguous)"
    )
    payload = {
        "model": model,
        "max_tokens": 16,
        "temperature": 0,
        "system": "",  # SdkAnthropicClient requires this key (anthropic_client.py:1014)
        "messages": [{"role": "user", "content": prompt}],
    }
    try:
        response = await llm.call(payload)
    except Exception:
        logger.exception("summary eval: faithfulness judge call failed")
        return 0.0
    text_out = ""
    for block in response.content:
        if isinstance(block, dict) and block.get("type") == "text":
            text_out = block.get("text", "").strip().lower()
            break
    # Scan all tokens for first verdict word — defensive against verbose
    # Haiku replies like "I think no, the summary is faithful". Devil's PR
    # #4 review #5: temp=0 + tightened prompt should make this rare, but
    # the all-tokens scan eliminates the failure mode entirely.
    tokens = [t.rstrip(".,!?") for t in text_out.split()]
    for tok in tokens:
        if tok == "no":
            return 1.0  # No unsupported claim → fully faithful
        if tok == "yes":
            return 0.0  # Unsupported claim found
        if tok == "borderline":
            return 0.5
    logger.warning("summary eval: faithfulness judge returned unparseable %r", text_out)
    return 0.0


def compute_summary_quality(
    coverages: list[float], faithfulness_scores: list[float],
) -> tuple[float, float, float]:
    """Return (summary_quality, mean_kpc, mean_sf) per spec §D formula.

    summary_quality = mean(coverages) * mean(faithfulness_scores).
    Empty lists → all zeros (avoid 0/0).
    """
    if not coverages or not faithfulness_scores:
        return 0.0, 0.0, 0.0
    mean_kpc = sum(coverages) / len(coverages)
    mean_sf = sum(faithfulness_scores) / len(faithfulness_scores)
    return mean_kpc * mean_sf, mean_kpc, mean_sf


async def _run_summary_eval(
    args: argparse.Namespace,
    eval_settings: EvalSettings,
    main_settings: Settings,
    *,
    llm_client: AnthropicClient | None = None,
    summarizer_llm: AnthropicClient | None = None,
) -> HandlerResult:
    fixture_path = args.fixture_path or _DEFAULT_FIXTURE
    rows = load_jsonl(fixture_path, SummaryRow)
    rows = filter_rows(rows, include_unreviewed=args.include_unreviewed)
    rows.sort(key=lambda r: r.row_id)

    if not rows:
        logger.error("summary eval: zero rows after reviewed_by filter")
        return HandlerResult(
            metrics={"summary_quality": 0.0, "mean_key_point_coverage": 0.0,
                     "mean_summary_faithfulness": 0.0, "null_returns": 0.0},
            extras={},
            report_lines=["No rows passed the reviewed_by filter."],
            primary_metric="summary_quality",
            fixture_size=0,
        )

    overridden = _settings_with_summary_overrides(main_settings)
    eval_scoped = _settings_for_eval_db(eval_settings, overridden)
    # Same agent_id-clobber-restore dance documented in admission.py /
    # dedup.py / backfill.py — _settings_for_eval_db sets agent_id from
    # eval_settings.agent_id (default "nous-eval-corpus") which would
    # route writes away from our handler scope.
    eval_scoped = eval_scoped.model_copy(update={"agent_id": _AGENT_ID})

    # Ownership-aware client lifecycle. Two clients here:
    # 1. `summarizer_llm` — used by EpisodeSummarizer to generate the
    #    structured summary (production code path).
    # 2. `llm_client` — used by our judges (key_point_coverage,
    #    summary_faithfulness).
    # In production both would point at the same shared client; tests
    # inject FakeJudge for either independently.
    owns_judge = llm_client is None
    owns_summarizer = summarizer_llm is None
    if owns_judge:
        llm_client = create_client(main_settings)
        await llm_client.start()
    if owns_summarizer:
        summarizer_llm = create_client(main_settings)
        await summarizer_llm.start()

    eval_db = Database(eval_scoped)
    null_returns = 0
    coverages: list[float] = []
    faithfulness_scores: list[float] = []
    judged_rows: list[dict[str, Any]] = []
    try:
        await eval_db.connect()

        # Lifecycle step 6: clean slate before seed under advisory lock.
        await clear_handler_state(
            eval_db, name=_HANDLER_NAME, agent_id=_AGENT_ID,
            deletes=[_DeleteSpec(schema_table="heart.episodes", agent_id=_AGENT_ID)],
        )

        async with _build_heart_for_eval(eval_db, eval_scoped) as heart:
            # F056 PR #4 v2 fix: brain=None is REQUIRED by the constructor
            # (episode_summarizer.py:88 declares `brain: Brain | None` with
            # no default). Omitting it raises TypeError on first instantiation
            # — a runtime crash undetectable by unit tests since none
            # instantiate the real EpisodeSummarizer. The brain attr is only
            # used in handle()'s graph-linking side-effect path, which the
            # eval bypasses by calling summarize_episode directly.
            summarizer = EpisodeSummarizer(
                heart=heart, brain=None, settings=eval_scoped, bus=None,
                llm_client=summarizer_llm,
            )
            for row in rows:
                # Step 1: seed Episode row
                try:
                    episode_id = await _seed_episode_stub(
                        eval_db, _AGENT_ID, row.transcript,
                    )
                except Exception:
                    logger.exception(
                        "summary eval: episode seed failed for row %s", row.row_id,
                    )
                    continue

                # Step 2: invoke production summarize_episode
                produced = await summarizer.summarize_episode(
                    episode_id=episode_id,
                    transcript=row.transcript,
                    agent_id=_AGENT_ID,
                )
                if produced is None:
                    null_returns += 1
                    judged_rows.append({
                        "row_id": row.row_id,
                        "null_return": True,
                    })
                    continue

                # Step 3: judge twice
                produced_kp = produced.get("key_points", []) or []
                produced_summary_text = produced.get("summary", "") or ""
                judge_model = getattr(args, "judge_model", _DEFAULT_JUDGE_MODEL)
                kpc = await _judge_key_point_coverage(
                    llm_client, judge_model,
                    row.gold_key_points, produced_kp,
                )
                sf = await _judge_summary_faithfulness(
                    llm_client, judge_model,
                    row.transcript, produced_summary_text,
                )
                coverages.append(kpc)
                faithfulness_scores.append(sf)
                judged_rows.append({
                    "row_id": row.row_id,
                    "key_point_coverage": kpc,
                    "summary_faithfulness": sf,
                    "question_type": row.question_type,
                })
    finally:
        if owns_judge and llm_client is not None:
            await llm_client.close()
        if owns_summarizer and summarizer_llm is not None:
            await summarizer_llm.close()
        await eval_db.disconnect()

    quality, mean_kpc, mean_sf = compute_summary_quality(coverages, faithfulness_scores)

    report_lines = [
        f"- fixture rows: {len(rows)} ({sum(1 for r in rows if not r.reviewed_by)} unreviewed)",
        f"- null_returns (summarize_episode skipped): {null_returns}",
        f"- judged rows: {len(coverages)}",
        f"- mean_key_point_coverage: {mean_kpc:.3f}",
        f"- mean_summary_faithfulness: {mean_sf:.3f}",
        f"- summary_quality (kpc * sf): {quality:.3f}",
    ]
    # Per-question_type breakdown (informational)
    by_qtype: dict[str, list[tuple[float, float]]] = {}
    for jr in judged_rows:
        if jr.get("null_return"):
            continue
        qt = jr.get("question_type") or "(unknown)"
        by_qtype.setdefault(qt, []).append(
            (jr["key_point_coverage"], jr["summary_faithfulness"])
        )
    if by_qtype:
        report_lines.append("")
        report_lines.append("### Per question_type")
        report_lines.append("| question_type | n | mean_kpc | mean_sf | quality |")
        report_lines.append("|---|---:|---:|---:|---:|")
        for qt in sorted(by_qtype):
            pairs = by_qtype[qt]
            kpc_q = sum(k for k, _ in pairs) / len(pairs)
            sf_q = sum(s for _, s in pairs) / len(pairs)
            report_lines.append(
                f"| {qt} | {len(pairs)} | {kpc_q:.3f} | {sf_q:.3f} | {kpc_q * sf_q:.3f} |"
            )

    return HandlerResult(
        metrics={
            "summary_quality": quality,
            "mean_key_point_coverage": mean_kpc,
            "mean_summary_faithfulness": mean_sf,
            "null_returns": float(null_returns),
        },
        extras={
            "judged_rows": judged_rows,
            "n_evaluated": len(coverages),
            "per_question_type": {
                qt: {"n": len(pairs),
                     "mean_kpc": sum(k for k, _ in pairs) / len(pairs),
                     "mean_sf": sum(s for _, s in pairs) / len(pairs)}
                for qt, pairs in by_qtype.items()
            },
        },
        report_lines=report_lines,
        primary_metric="summary_quality",
        fixture_size=len(rows),
        handler_specific_notes=(
            f"judge_model={getattr(args, 'judge_model', _DEFAULT_JUDGE_MODEL)}, "
            f"include_unreviewed={args.include_unreviewed}, "
            f"null_return_rate={null_returns / len(rows):.3f}"
        ),
    )


def main(argv: list[str] | None = None) -> int:
    return run_handler_eval(
        _HANDLER_NAME,
        _run_summary_eval,
        default_threshold=0.05,  # 5pp per F056 spec §D
        extra_args_fn=_add_judge_model_arg,
        argv=argv,
    )


if __name__ == "__main__":
    raise SystemExit(main())
