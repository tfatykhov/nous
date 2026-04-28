"""F056 PR #3: graph backfill eval CLI — edge precision via LLM-judge.

Exercises `nous/brain/graph_densifier.py::GraphDensifier.run_backfill_cycle`
(line 534) — called by the sleep handler at `sleep_handler.py:892` and
the entry point for F040 backfill + F043 cross-encoder gate + F045
CE-aware thresholds + F054 selective relaxation.

Per F056 spec §C:
- Per-handler agent_id `nous-eval-handler-backfill`
- `graph_backfill_enabled=True` (gated at 4 sites in graph_densifier.py)
- GraphDensifier construction mirrors `_build_densifier_for_eval`
  (`nous_eval/retrieval_runner.py:407-449`) — db, graph_linker, embedder,
  settings, agent_id (NOT the v2-spec's wrong arg order)
- F053 `density_eval._snapshot` reused for before/after diffs
- 20-edge sample via seeded `random.Random(42).sample(sorted(...))`
- Haiku LLM-judge with `payload["temperature"] = 0`
- Primary metric `edge_precision` (gated 10pp drop)

PR #3 v1 simplifies the spec's "100 mixed entities" to facts-only — see
`BackfillEntity` model docstring for rationale + F056.2 follow-up scope.
"""
from __future__ import annotations

import argparse
import json
import logging
import random
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import text

from nous.api.anthropic_client import AnthropicClient, create_client
from nous.config import Settings
from nous.heart.schemas import FactInput, FactRejected
from nous.storage.database import Database
from nous_eval.config import EvalSettings
from nous_eval.density_eval import DensitySnapshot, _snapshot
from nous_eval.handlers._cli_base import (
    HandlerResult,
    _DeleteSpec,
    clear_handler_state,
    run_handler_eval,
)
from nous_eval.handlers._jsonl import load_jsonl
from nous_eval.handlers._models import BackfillEntity
from nous_eval.retrieval_runner import _build_heart_for_eval, _settings_for_eval_db

if TYPE_CHECKING:
    from nous.heart.heart import Heart

logger = logging.getLogger(__name__)


_AGENT_ID = "nous-eval-handler-backfill"
_HANDLER_NAME = "backfill"
_DEFAULT_FIXTURE = Path("tests/fixtures/handlers/backfill_corpus.jsonl")
_LLM_JUDGE_SAMPLE_N = 20
_LLM_JUDGE_SEED = 42  # Deterministic per F056 spec §"Determinism"


def _settings_with_backfill_overrides(base: Settings) -> Settings:
    """Apply F056 §C required overrides.

    `graph_backfill_enabled=True` is the load-bearing override —
    `graph_densifier.py` gates at 4 sites (lines 429, 456, 483, 510).
    Without explicit True, an env-var-driven False would silently
    short-circuit `run_backfill_cycle` and the eval would report 0 edges
    every run. Defaults True in prod, but explicit removes ambiguity.
    """
    update: dict[str, Any] = {
        "graph_backfill_enabled": True,
        "agent_id": _AGENT_ID,
    }
    return base.model_copy(update=update)


def filter_entities(
    entities: list[BackfillEntity], *, include_unreviewed: bool,
) -> list[BackfillEntity]:
    """Apply the reviewed_by gate. Mirrors qrels_loader.py:80-85 pattern."""
    if include_unreviewed:
        return entities
    return [e for e in entities if e.reviewed_by]


async def _seed_facts(
    heart: "Heart", entities: list[BackfillEntity],
) -> list[UUID]:
    """Insert each fact-typed entity via Heart.learn; return seeded UUIDs.

    PR #3 v1 simplification: only entity_type=='fact' rows are seeded.
    Decisions/episodes/procedures are skipped with a WARNING. Extending
    to those types is F056.2.
    """
    seeded: list[UUID] = []
    skipped_types: dict[str, int] = {}
    for ent in entities:
        if ent.entity_type != "fact":
            skipped_types[ent.entity_type] = skipped_types.get(ent.entity_type, 0) + 1
            continue
        result = await heart.learn(
            FactInput(content=ent.content, source="backfill_eval"),
            check_contradictions=False,
        )
        if isinstance(result, FactRejected):
            logger.warning(
                "backfill eval: fact rejected during seed (row %s): %s",
                ent.row_id, result.explanation,
            )
            continue
        seeded.append(result.id)
    if skipped_types:
        logger.warning(
            "backfill eval: skipped non-fact entities (PR #3 v1 limitation): %s",
            skipped_types,
        )
    logger.info("backfill eval: seeded %d facts", len(seeded))
    return seeded


async def _list_new_edges(
    db: Database, agent_id: str,
) -> list[dict[str, Any]]:
    """Read all edges currently in brain.graph_edges for `agent_id`.

    Returns one dict per edge with source/target/relation + the
    source/target content fields needed for LLM-judge prompts.
    """
    async with db.session() as session:
        result = await session.execute(
            text(
                """
                SELECT
                    e.source_id::text AS source_id,
                    e.target_id::text AS target_id,
                    e.relation,
                    e.source_type,
                    e.target_type
                FROM brain.graph_edges e
                WHERE e.agent_id = :aid
                ORDER BY e.source_id::text, e.target_id::text, e.relation
                """
            ),
            {"aid": agent_id},
        )
        return [dict(row._mapping) for row in result]


async def _content_lookup(
    db: Database, agent_id: str, fact_ids: set[str],
) -> dict[str, str]:
    """Fetch heart.facts.content for the given UUIDs.

    Edges may reference non-fact entities too, but PR #3 v1 only seeds
    facts so this lookup covers all expected source/target IDs. Returns
    {} for any UUID not found in heart.facts (edge sample skips those).
    """
    if not fact_ids:
        return {}
    async with db.session() as session:
        result = await session.execute(
            text(
                "SELECT id::text AS id, content FROM heart.facts "
                "WHERE agent_id = :aid AND id = ANY(:ids)"
            ),
            {"aid": agent_id, "ids": list(fact_ids)},
        )
        return {row.id: row.content for row in result}


async def _judge_edge(
    llm: AnthropicClient,
    model: str,
    source_content: str,
    target_content: str,
    relation: str,
) -> str:
    """Ask Haiku to judge one edge. Returns 'true', 'false', or 'borderline'.

    `payload["temperature"] = 0` per F056 spec §"Determinism". Haiku
    doesn't yet expose `seed`; temp=0 + identical prompt is the determinism
    floor.
    """
    prompt = (
        f"Two facts are related via the edge type '{relation}'. Is this edge "
        f"semantically defensible? Reply with exactly one word: true, false, "
        f"or borderline.\n\n"
        f"Source: {source_content}\n"
        f"Target: {target_content}"
    )
    payload = {
        "model": model,
        "max_tokens": 16,
        "temperature": 0,
        "messages": [{"role": "user", "content": prompt}],
    }
    try:
        response = await llm.call(payload)
    except Exception:
        logger.exception("backfill eval: LLM judge call failed")
        return "borderline"
    text_out = ""
    for block in response.content:
        if isinstance(block, dict) and block.get("type") == "text":
            text_out = block.get("text", "").strip().lower()
            break
    # Strip punctuation and take the first token
    first_token = text_out.split()[0].rstrip(".,!?") if text_out else ""
    if first_token in ("true", "false", "borderline"):
        return first_token
    logger.warning("backfill eval: judge returned unparseable %r; counting borderline", text_out)
    return "borderline"


def _sample_edges(
    edges: list[dict[str, Any]], *, n: int = _LLM_JUDGE_SAMPLE_N, seed: int = _LLM_JUDGE_SEED,
) -> list[dict[str, Any]]:
    """Deterministic 20-edge sample per F056 spec §"Determinism".

    Sort by `(source_id, target_id, relation)` first — asyncpg row order is
    undefined; without sort the seeded `Random(seed)` is deterministic but
    the input is not. F056 spec §"Determinism" mandates this exact ordering.
    """
    sorted_edges = sorted(
        edges,
        key=lambda e: (e["source_id"], e["target_id"], e["relation"]),
    )
    rng = random.Random(seed)
    return rng.sample(sorted_edges, k=min(n, len(sorted_edges)))


def compute_edge_precision(
    judgments: list[str],
) -> tuple[float, dict[str, int]]:
    """Compute precision excluding 'borderline' verdicts.

    Returns (precision, counts) where counts is {true, false, borderline}.
    Precision = true / (true + false); 0.0 when denominator is 0.
    """
    counts = {"true": 0, "false": 0, "borderline": 0}
    for j in judgments:
        if j in counts:
            counts[j] += 1
    decisive = counts["true"] + counts["false"]
    if decisive == 0:
        return 0.0, counts
    return counts["true"] / decisive, counts


def compute_orphan_resolution_rate(
    pre: DensitySnapshot, post: DensitySnapshot,
) -> float:
    """Fraction of pre-cycle orphans now linked. 0.0 when pre had no orphans."""
    pre_total = sum(pre.orphan_count_per_type.values())
    post_total = sum(post.orphan_count_per_type.values())
    if pre_total == 0:
        return 0.0
    resolved = max(pre_total - post_total, 0)
    return resolved / pre_total


async def _run_backfill_eval(
    args: argparse.Namespace,
    eval_settings: EvalSettings,
    main_settings: Settings,
    *,
    llm_client: AnthropicClient | None = None,
) -> HandlerResult:
    fixture_path = args.fixture_path or _DEFAULT_FIXTURE
    entities = load_jsonl(fixture_path, BackfillEntity)
    entities = filter_entities(entities, include_unreviewed=args.include_unreviewed)
    entities.sort(key=lambda e: e.row_id)

    if not entities:
        logger.error("backfill eval: zero entities after reviewed_by filter")
        return HandlerResult(
            metrics={"edge_precision": 0.0, "orphan_resolution_rate": 0.0, "density_delta": 0.0},
            extras={"judge_counts": {"true": 0, "false": 0, "borderline": 0}},
            report_lines=["No entities passed the reviewed_by filter."],
            primary_metric="edge_precision",
            fixture_size=0,
        )

    overridden = _settings_with_backfill_overrides(main_settings)
    eval_scoped = _settings_for_eval_db(eval_settings, overridden)
    eval_scoped = eval_scoped.model_copy(update={"agent_id": _AGENT_ID})

    # Ownership-aware LLM client lifecycle (per F056 spec §"LLM client
    # injection"). Tests inject a FakeJudge; eval owns close() only when
    # client is None.
    owns_client = llm_client is None
    if owns_client:
        llm_client = create_client(main_settings)
        await llm_client.start()

    eval_db = Database(eval_scoped)
    try:
        await eval_db.connect()

        # Lifecycle step 6: clean slate before seed under advisory lock.
        # Wipes both heart.facts (the entities) AND brain.graph_edges
        # (any leftover edges from prior runs).
        await clear_handler_state(
            eval_db, name=_HANDLER_NAME, agent_id=_AGENT_ID,
            deletes=[
                _DeleteSpec(schema_table="heart.facts", agent_id=_AGENT_ID),
                _DeleteSpec(schema_table="brain.graph_edges", agent_id=_AGENT_ID),
            ],
        )

        async with _build_heart_for_eval(eval_db, eval_scoped) as heart:
            # Seed facts (PR #3 v1 limitation: facts-only)
            seeded = await _seed_facts(heart, entities)

            # Pre-snapshot
            pre = await _snapshot(eval_db, _AGENT_ID)

            # Construct GraphDensifier (mirrors retrieval_runner.py:407-449)
            from nous.brain.embeddings import EmbeddingProvider
            from nous.brain.graph_densifier import GraphDensifier
            from nous.brain.graph_linker import GraphLinker
            if not eval_scoped.openai_api_key:
                raise RuntimeError(
                    "F056 backfill eval requires OPENAI_API_KEY for embeddings"
                )
            embedder = EmbeddingProvider(
                api_key=eval_scoped.openai_api_key,
                model=eval_scoped.embedding_model,
                dimensions=eval_scoped.embedding_dimensions,
            )
            linker = GraphLinker(
                db=eval_db, embedder=embedder,
                settings=eval_scoped, agent_id=_AGENT_ID,
            )
            densifier = GraphDensifier(
                db=eval_db, graph_linker=linker, embedder=embedder,
                settings=eval_scoped, agent_id=_AGENT_ID,
            )

            # Run the densification cycle
            await densifier.run_backfill_cycle()

            # Post-snapshot
            post = await _snapshot(eval_db, _AGENT_ID)
            density_delta = post.edge_count_total - pre.edge_count_total
            orphan_rate = compute_orphan_resolution_rate(pre, post)

            # LLM-judge sample
            edges = await _list_new_edges(eval_db, _AGENT_ID)
            sample = _sample_edges(edges)
            content_lookup = await _content_lookup(
                eval_db, _AGENT_ID,
                {e["source_id"] for e in sample} | {e["target_id"] for e in sample},
            )
            judgments: list[str] = []
            judged_edges: list[dict[str, Any]] = []
            for edge in sample:
                src = content_lookup.get(edge["source_id"])
                tgt = content_lookup.get(edge["target_id"])
                if src is None or tgt is None:
                    judgments.append("borderline")
                    continue
                verdict = await _judge_edge(
                    llm_client, main_settings.background_model,
                    src, tgt, edge["relation"],
                )
                judgments.append(verdict)
                judged_edges.append({
                    "source_id": edge["source_id"],
                    "target_id": edge["target_id"],
                    "relation": edge["relation"],
                    "verdict": verdict,
                })

            precision, counts = compute_edge_precision(judgments)
    finally:
        if owns_client and llm_client is not None:
            await llm_client.close()
        await eval_db.disconnect()

    report_lines = [
        f"- entities seeded: {len(seeded)} / fixture rows: {len(entities)}",
        f"- pre-snapshot: {pre.edge_count_total} edges, {sum(pre.orphan_count_per_type.values())} orphans",
        f"- post-snapshot: {post.edge_count_total} edges, {sum(post.orphan_count_per_type.values())} orphans",
        f"- density_delta: {density_delta} edges",
        f"- orphan_resolution_rate: {orphan_rate:.3f}",
        f"- LLM-judge: {counts['true']} true, {counts['false']} false, {counts['borderline']} borderline",
        f"- edge_precision (true / (true + false)): {precision:.3f}",
        "",
        "### Sampled edges + verdicts (deterministic Random(42))",
        "",
    ]
    for je in judged_edges:
        report_lines.append(
            f"- `{je['source_id'][:8]}` -> `{je['target_id'][:8]}` "
            f"[{je['relation']}] **{je['verdict']}**"
        )

    return HandlerResult(
        metrics={
            "edge_precision": precision,
            "orphan_resolution_rate": orphan_rate,
            "density_delta": float(density_delta),
        },
        extras={
            "judge_counts": counts,
            "pre_edge_count": pre.edge_count_total,
            "post_edge_count": post.edge_count_total,
            "pre_orphan_total": sum(pre.orphan_count_per_type.values()),
            "post_orphan_total": sum(post.orphan_count_per_type.values()),
            "sample_size": len(sample),
            "judged_edges": json.dumps(judged_edges),  # JSON string for JSONB sub-payload
        },
        report_lines=report_lines,
        primary_metric="edge_precision",
        fixture_size=len(entities),
        handler_specific_notes=(
            f"sample_n={_LLM_JUDGE_SAMPLE_N}, sample_seed={_LLM_JUDGE_SEED}, "
            f"judge_model={main_settings.background_model}"
        ),
    )


def main(argv: list[str] | None = None) -> int:
    return run_handler_eval(
        _HANDLER_NAME,
        _run_backfill_eval,
        default_threshold=0.10,  # 10pp per F056 spec §C
        argv=argv,
    )


if __name__ == "__main__":
    raise SystemExit(main())
