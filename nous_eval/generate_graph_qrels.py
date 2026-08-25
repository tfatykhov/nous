"""F051 graph-targeted qrels miner.

Generates a qrels JSONL whose gold IDs are reachable only via graph
expansion — they're NOT in the vector+keyword top-K, but ARE in the
graph-expanded top-K. Without this, eval configs that touch graph
behavior (F065 penalty multiplier, F022 spreading activation,
F030 MMR-after-graph) produce zero deltas against probes because
probes' gold IDs are all directly findable.

Pipeline:

  1. Sample trusted edges (deterministic OR high-weight heuristic).
  2. For each (S, T) edge, ask Haiku to write a question whose answer
     is in T but whose vocabulary comes from S (without naming T).
  3. Validate via run_recall_pipeline:
       a. graph_recall_enabled=False → assert T is NOT in top-K.
       b. graph_recall_enabled=True  → assert T IS in top-K.
  4. Emit JSONL rows compatible with QrelSource.graph_targeted.

Run:
    python -m nous_eval.generate_graph_qrels \\
        --sample-size 50 \\
        --out E:/Projects/nous-eval-fixtures/v2026-Q2/qrels_graph_targeted.jsonl

Both validation passes are mandatory. A query that survives only the
first check (graph-off miss) is still useless if graph expansion
ALSO can't reach the gold — that's a malformed query, not a graph
positive. The yield is typically 10-30% of candidates; cost a couple
hundred Haiku tokens per kept qrel.

The miner reads from the eval DB (NOUS_EVAL_* env vars), not from
prod, so generation is reproducible against a pinned fixture.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import text

from nous.api.anthropic_client import create_client
from nous.api.retrieval_pipeline import PipelineResult, run_recall_pipeline
from nous.brain.brain import Brain
from nous.config import Settings
from nous.heart.heart import Heart
from nous_eval.config import EvalSettings
from nous_eval.retrieval_runner import _build_heart_for_eval, _settings_for_eval_db
from nous.storage.database import Database

logger = logging.getLogger(__name__)


_QUERY_GEN_TOOL_NAME = "emit_graph_query"

_QUERY_GEN_TOOL = {
    "name": _QUERY_GEN_TOOL_NAME,
    "description": (
        "Emit a question whose factual answer is in TARGET but whose "
        "vocabulary leans toward SOURCE. The query MUST NOT name TARGET "
        "directly — the test is whether graph expansion (not vector "
        "search alone) is needed to reach TARGET from a SOURCE-flavored "
        "query."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "The generated question. Phrase it naturally; do not "
                    "include any phrasing from TARGET that would let a "
                    "lexical or embedding match find TARGET directly."
                ),
            },
            "answerable": {
                "type": "boolean",
                "description": (
                    "True if TARGET genuinely answers the question. "
                    "False if you couldn't write a faithful question "
                    "linking SOURCE-vocabulary to TARGET-fact (in which "
                    "case the row will be dropped)."
                ),
            },
            "rationale": {
                "type": "string",
                "description": "One short sentence on why this bridges S→T.",
            },
        },
        "required": ["query", "answerable", "rationale"],
        "additionalProperties": False,
    },
}


@dataclass(frozen=True, slots=True)
class EdgeCandidate:
    source_id: UUID
    target_id: UUID
    source_type: str
    target_type: str
    relation: str
    weight: float
    source_content: str
    target_content: str


@dataclass(frozen=True, slots=True)
class GeneratedQrel:
    query: str
    gold_id: UUID
    gold_type: str
    source_id: UUID
    source_type: str
    relation: str
    rationale: str


async def fetch_edge_candidates(
    db: Database,
    agent_id: str,
    *,
    sample_size: int,
    min_weight: float = 0.7,
    allow_inferred: bool = False,
) -> list[EdgeCandidate]:
    """Pull trusted edges from the eval DB.

    `extraction_method='inferred'` rows are excluded — those are the
    edges the penalty is designed to weigh down, so using them as the
    qrel bridge would be circular. `deterministic` (supersession) and
    high-weight `heuristic` rows are trusted ground truth.

    Selection is randomized via TABLESAMPLE-equivalent (ORDER BY
    random() LIMIT N) so repeated runs explore different bridges.
    Fact↔fact and fact→decision relations dominate the prod snapshot
    and are the most retrieval-relevant — no per-type quota for now.
    """
    # We need to join graph_edges to the source/target content tables
    # to feed the LLM. Three node types matter here: fact, decision,
    # episode. Procedures rarely participate in retrieval-shaped queries.
    # The retrieval_pipeline graph-expansion paths ONLY surface DECISION
    # neighbors (heart→graph→decisions at line 268-292; brain
    # decision-expansion at line 305+). So for a graph-targeted qrel to
    # be reachable via the graph path at all, target_type MUST equal
    # 'decision'. Fact↔fact bridges in the prod corpus also tend to be
    # near-duplicates of the auto-linker, which don't differentiate
    # configs even if reachable.
    #
    # Source side is open — fact/decision/episode/procedure all work as
    # the seed via Heart's vector+keyword retrieval. We rank cross-type
    # candidates first (fact→decision evidence_for, procedure→decision
    # informed_by) because the source-target embedding gap is larger,
    # giving the validator a better chance of finding a query where
    # graph helps the rank.
    # `allow_inferred` relaxes the anti-circularity guard. That guard exists
    # for the F065 PENALTY eval (which down-weights inferred edges, so using
    # them as bridges would be circular). It does NOT apply to F044 α-downscale,
    # which keys on consolidation_state, not extraction_method — so F044 qrel
    # generation may include inferred bridges (the only decision bridges that
    # exist on prod-shape corpora). Default off preserves the F065 contract.
    inferred_clause = "" if allow_inferred else "AND e.extraction_method <> 'inferred'"
    sql = text(f"""
        WITH candidates AS (
            SELECT
                e.source_id, e.target_id, e.source_type, e.target_type,
                e.relation, e.weight,
                CASE
                    WHEN e.source_type <> e.target_type THEN 0
                    ELSE 1
                END AS same_type_penalty
            FROM brain.graph_edges e
            WHERE e.agent_id = :agent_id
              {inferred_clause}
              AND e.weight >= :min_weight
              AND e.source_type IN ('fact','decision','episode','procedure')
              AND e.target_type = 'decision'
            ORDER BY same_type_penalty ASC, random()
            LIMIT :sample_size
        )
        SELECT
            c.source_id, c.target_id, c.source_type, c.target_type,
            c.relation, c.weight,
            COALESCE(
                fs.content,
                ds.description,
                es.summary,
                ps.description
            ) AS source_content,
            COALESCE(
                ft.content,
                dt.description,
                et.summary,
                pt.description
            ) AS target_content
        FROM candidates c
        LEFT JOIN heart.facts      fs ON c.source_type='fact'      AND fs.id=c.source_id
        LEFT JOIN brain.decisions  ds ON c.source_type='decision'  AND ds.id=c.source_id
        LEFT JOIN heart.episodes   es ON c.source_type='episode'   AND es.id=c.source_id
        LEFT JOIN heart.procedures ps ON c.source_type='procedure' AND ps.id=c.source_id
        LEFT JOIN heart.facts      ft ON c.target_type='fact'      AND ft.id=c.target_id
        LEFT JOIN brain.decisions  dt ON c.target_type='decision'  AND dt.id=c.target_id
        LEFT JOIN heart.episodes   et ON c.target_type='episode'   AND et.id=c.target_id
        LEFT JOIN heart.procedures pt ON c.target_type='procedure' AND pt.id=c.target_id
    """)
    async with db.session() as s:
        rows = (await s.execute(
            sql,
            {"agent_id": agent_id, "min_weight": min_weight, "sample_size": sample_size},
        )).all()
    out: list[EdgeCandidate] = []
    for r in rows:
        if not r.source_content or not r.target_content:
            continue
        # Trim very long contents — Haiku prompt budget.
        out.append(EdgeCandidate(
            source_id=r.source_id,
            target_id=r.target_id,
            source_type=r.source_type,
            target_type=r.target_type,
            relation=r.relation,
            weight=float(r.weight),
            source_content=(r.source_content or "")[:600],
            target_content=(r.target_content or "")[:600],
        ))
    return out


def _build_query_gen_prompt(c: EdgeCandidate) -> str:
    return (
        "You will be shown two memory items that are connected by a "
        "graph edge in our knowledge base. Your task is to generate a "
        "natural-language question that someone might realistically ask, "
        "such that:\n"
        " - The factual answer is in TARGET.\n"
        " - The question's wording leans on SOURCE's vocabulary, not TARGET's.\n"
        " - The question does NOT mention TARGET's distinctive terms directly.\n\n"
        f"SOURCE ({c.source_type}):\n{c.source_content}\n\n"
        f"TARGET ({c.target_type}):\n{c.target_content}\n\n"
        f"Edge relation: {c.relation} (from SOURCE to TARGET).\n\n"
        "Call emit_graph_query. If you cannot write a faithful question "
        "where TARGET genuinely answers it, set answerable=false."
    )


async def generate_query(
    candidate: EdgeCandidate,
    llm_client: Any,
    model: str,
) -> tuple[str, str] | None:
    """Returns (query, rationale) or None if the model declines."""
    payload = {
        "model": model,
        "max_tokens": 512,
        "system": "",
        "tools": [_QUERY_GEN_TOOL],
        "tool_choice": {"type": "tool", "name": _QUERY_GEN_TOOL_NAME},
        "messages": [
            {"role": "user", "content": _build_query_gen_prompt(candidate)},
        ],
    }
    response = await llm_client.call(payload)
    for block in (response.content or []):
        if block.get("type") == "tool_use" and block.get("name") == _QUERY_GEN_TOOL_NAME:
            data = block.get("input") or {}
            if not data.get("answerable", False):
                return None
            query = (data.get("query") or "").strip()
            rationale = (data.get("rationale") or "").strip()
            if not query:
                return None
            return query, rationale
    return None


def _rank_of(results: list[PipelineResult], target_id: UUID, limit: int) -> int | None:
    """1-based rank within top-K, or None if not present in top-K."""
    for i, r in enumerate(results[:limit], 1):
        if r.id == target_id:
            return i
    return None


def _rank_of_full(results: list[PipelineResult], target_id: UUID) -> int | None:
    """1-based rank across the full result list (no K limit)."""
    for i, r in enumerate(results, 1):
        if r.id == target_id:
            return i
    return None


async def _validate_query(
    query: str,
    target_id: UUID,
    heart: Heart,
    brain: Brain,
    settings: Settings,
    limit: int,
) -> bool:
    """Keep iff graph_off MISSES top-K AND graph_on HITS top-K.

    Codex P2 (2026-05-23): this source is named `graph_targeted` and
    documented as "graph-only-reachable". Keeping rows where graph_off
    already finds the gold in top-K (just at a worse rank than
    graph_on) violates that contract: P@K and R@K count both configs
    as hits, so per-config recall comparisons can't distinguish
    graph-only signal from rank-shuffle signal. MRR can still differ
    on rank-shifts, but mixing rank-shift rows into a source marketed
    as "graph-only" makes the source's purpose ambiguous and risks
    distorting gate decisions.

    The strict criterion below — graph-off MUST miss top-K and
    graph-on MUST hit top-K — gives the source a single, audit-clear
    contract.
    """
    settings_off = settings.model_copy(update={"graph_recall_enabled": False})
    settings_on = settings.model_copy(update={"graph_recall_enabled": True})

    # `rerank_by_score=True` is REQUIRED, not a tuning choice, and its absence
    # made this whole function return False unconditionally.
    #
    # Under the default (False) `run_recall_pipeline` assembles in STAGE ORDER:
    # heart (:463) -> heart-graph (:466,:472) -> decisions (:477) ->
    # graph_expanded (:502). A graph-reached target is therefore appended AFTER
    # the heart results, so with `limit` heart rows in hand it sits at index
    # >= limit and `_rank_of`, which slices `results[:limit]`, can never see it.
    # `on_rank` was always None, so `on_rank is not None and off_rank is None`
    # was UNSATISFIABLE — the mine yielded 0 qrels by construction, on any
    # corpus, which is the "0 qrels = harness bug" written off on 2026-07-01.
    #
    # It is also what prod runs: tools.py derives `rerank_by_score` from
    # `NOUS_EPISODE_CHUNKS_ENABLED=true` for all/fact queries, so validating
    # under False measured a configuration production does not use.
    off_results, _ = await run_recall_pipeline(
        query, heart, brain, settings_off, limit=limit, rerank_by_score=True,
    )
    on_results, _ = await run_recall_pipeline(
        query, heart, brain, settings_on, limit=limit, rerank_by_score=True,
    )

    off_rank = _rank_of(off_results, target_id, limit)
    on_rank = _rank_of(on_results, target_id, limit)
    logger.debug(
        "validate query=%r off_topk_rank=%s on_topk_rank=%s",
        query[:60], off_rank, on_rank,
    )

    return on_rank is not None and off_rank is None


def _qrel_to_jsonl(qrel: GeneratedQrel, *, gated: bool = True) -> str:
    # Codex P1 (2026-05-23): memory_types MUST include the full pipeline
    # surface, not just the gold's type. The retrieval harness routes
    # `qrel.memory_types` into `run_recall_pipeline(memory_types=...)`,
    # which gates which stages fire. Restricting to `[gold_type]` would
    # disable the Heart stage when gold_type='decision' (the 100% case
    # under our target_type filter) — that's the very stage whose graph
    # expansion produces the qrel's signal. Include `censor` so the full
    # production candidate composition runs (Codex P2 follow-up: omitting
    # censor changes stage-1 candidates vs the production `all` default).
    # Codex P2/P3 follow-up: `bridge_source_type` must carry the SOURCE
    # node's type, not the gold's type — the previous emit hardcoded all
    # rows to `gold_type` (always 'decision' under our target_type filter)
    # which made downstream provenance analysis useless.
    return json.dumps({
        "query": qrel.query,
        "gold_ids": [str(qrel.gold_id)],
        "memory_types": ["fact", "decision", "episode", "procedure", "censor"],
        # codex P1: ungated rows get their OWN source. `graph_targeted` means
        # graph-off-miss + graph-on-hit was VERIFIED; a row that skipped that
        # check must not inherit the claim.
        "source": "graph_targeted" if gated else "graph_bridge_ungated",
        "notes": {
            "bridge_via": str(qrel.source_id),
            "bridge_source_type": qrel.source_type,
            "edge_relation": qrel.relation,
            "rationale": qrel.rationale,
        },
        "reviewed_by": "auto",
    })


async def _run_async(args: argparse.Namespace) -> int:
    eval_settings = EvalSettings()
    eval_settings.warn_if_default_password()
    base_settings = Settings()
    main_settings = _settings_for_eval_db(eval_settings, base_settings)

    db = Database(settings=main_settings)
    await db.connect()
    try:
        candidates = await fetch_edge_candidates(
            db,
            agent_id=main_settings.agent_id,
            sample_size=args.sample_size,
            min_weight=args.min_weight,
            allow_inferred=args.allow_inferred,
        )
        logger.info("Sampled %d edge candidates", len(candidates))
        if not candidates:
            print("No candidates available — confirm the eval DB has heuristic/deterministic edges.", file=sys.stderr)
            return 1

        api_client = create_client(main_settings)
        await api_client.start()
        try:
            kept: list[GeneratedQrel] = []
            async with _build_heart_for_eval(db, main_settings) as heart:
                brain = Brain(database=db, settings=main_settings)
                for i, cand in enumerate(candidates, 1):
                    try:
                        gen = await generate_query(cand, api_client, args.model)
                    except Exception as exc:
                        logger.warning("Haiku call failed for edge %s→%s: %s",
                                       cand.source_id, cand.target_id, exc)
                        continue
                    if gen is None:
                        continue
                    query, rationale = gen
                    if args.no_reachability_gate:
                        # The gate keeps a qrel only when graph-off MISSES and
                        # graph-on HITS. Measured on this corpus (2026-08-24,
                        # 58 generated queries): the target is already in the
                        # query's VECTOR top-50 for 55 of them (94.8%), at
                        # median rank 2, 52/58 in the top 10. The generator
                        # writes questions semantic search answers, so the
                        # graph-ONLY criterion is capped near 5% for ANY edge
                        # family and no edge-selection tuning moves it.
                        #
                        # A paired A/B does not need graph-only qrels — it needs
                        # qrels on which arms CAN differ. Ties cost n, not
                        # correctness. Skipping the gate trades a contract the
                        # measurement never required for a set that actually
                        # yields, and the resulting set DID discriminate: a
                        # positive control (graph_recall_enabled=False) moved
                        # 23/57 queries at dMRR +0.0640.
                        #
                        # The cost is real and must travel with the file: rows
                        # kept here are NOT graph-only, so this output must not
                        # be loaded as `QrelSource.graph_targeted` without
                        # saying so.
                        ok = True
                    else:
                        try:
                            ok = await _validate_query(
                                query, cand.target_id, heart, brain, main_settings, args.top_k,
                            )
                        except Exception as exc:
                            logger.warning("Validation failed for %s: %s", query[:60], exc)
                            continue
                    if not ok:
                        continue
                    kept.append(GeneratedQrel(
                        query=query,
                        gold_id=cand.target_id,
                        gold_type=cand.target_type,
                        source_id=cand.source_id,
                        source_type=cand.source_type,
                        relation=cand.relation,
                        rationale=rationale,
                    ))
                    logger.info("KEEP [%d/%d]: %s", i, len(candidates), query[:80])
                    if len(kept) >= args.target_size:
                        break
        finally:
            await api_client.close()
    finally:
        await db.disconnect()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        for q in kept:
            f.write(_qrel_to_jsonl(q, gated=not args.no_reachability_gate) + "\n")
    print(f"Wrote {len(kept)} qrels to {out_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m nous_eval.generate_graph_qrels")
    parser.add_argument("--sample-size", type=int, default=60,
                        help="Edge candidates to sample (yield is typically 10-30%%).")
    parser.add_argument("--target-size", type=int, default=20,
                        help="Stop early once this many qrels are kept.")
    parser.add_argument("--min-weight", type=float, default=0.7,
                        help="Minimum edge weight for trusted bridges.")
    parser.add_argument("--allow-inferred", action="store_true",
                        help="Include extraction_method='inferred' bridges. Default OFF "
                             "preserves the F065 anti-circularity contract; safe to enable "
                             "for F044 (keys on consolidation_state, not extraction_method).")
    parser.add_argument("--top-k", type=int, default=10,
                        help="Top-K depth used by validation passes.")
    parser.add_argument("--model", default="claude-haiku-4-5-20251001",
                        help="LLM for query generation.")
    parser.add_argument("--out", required=True,
                        help="Output JSONL path (typically inside the fixtures dir).")
    parser.add_argument(
        "--no-reachability-gate", action="store_true",
        help="Keep every generated query instead of requiring graph-off MISS + "
             "graph-on HIT. The gate is capped near 5%% yield on prod-shaped "
             "corpora because the generator writes vector-findable questions "
             "(measured 94.8%% in vector top-50, median rank 2). A paired A/B "
             "does not need graph-only qrels — ties cost n, not correctness. "
             "Output is NOT graph-only; do not label it as such.")
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    return asyncio.run(_run_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
