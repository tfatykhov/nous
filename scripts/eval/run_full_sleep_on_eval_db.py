"""F056 follow-up: trigger full Nous sleep cycle on the eval DB.

Runs all 10 sleep phases against the LongMemEval-ingested corpus
(`agent_id="nous-lme-corpus"` on `nous_eval_scratch` DB), captures graph
density before + after, prints the delta. Required before re-running F042
cross-encoder evals on a realistic memory state — sleep is what creates
graph edges, learns procedures, resolves contradictions, etc.

Usage (from repo root):

    NOUS_EVAL_DB_NAME=nous_eval_scratch \
      uv run python scripts/eval/run_full_sleep_on_eval_db.py

Cost estimate on 200-episode LongMemEval ingest: ~$2.65 (mostly Sonnet
reflect calls). Runtime: ~45-60 min.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time
from datetime import UTC, datetime

from nous.api.anthropic_client import create_client
from nous.brain.brain import Brain
from nous.brain.embeddings import EmbeddingProvider
from nous.brain.graph_densifier import GraphDensifier
from nous.brain.graph_linker import GraphLinker
from nous.config import Settings
from nous.events import Event, EventBus
from nous.handlers.sleep_handler import SleepHandler
from nous.heart.heart import Heart
from nous.storage.database import Database
from nous_eval.config import EvalSettings
from nous_eval.density_eval import _snapshot
from nous_eval.retrieval_runner import _settings_for_eval_db


_AGENT_ID = "nous-lme-corpus"


def _format_snapshot(snap, label: str) -> list[str]:
    lines = [f"{label}:"]
    lines.append(f"  edges (total): {snap.edge_count_total}")
    if snap.edge_count_per_relation:
        lines.append("  edges by relation:")
        for k, v in sorted(snap.edge_count_per_relation.items()):
            lines.append(f"    {k}: {v}")
    if snap.orphan_count_per_type:
        lines.append("  orphans by type:")
        for k, v in sorted(snap.orphan_count_per_type.items()):
            lines.append(f"    {k}: {v}")
    return lines


async def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger = logging.getLogger(__name__)

    eval_settings = EvalSettings()
    main_settings = Settings()

    # Override agent_id for the LongMemEval corpus + re-enable phases that
    # _settings_for_eval_db's _EVAL_DISABLE_FIELDS would clobber. The default
    # eval-scoped Settings disables event_bus, fact_extraction, episode_summary,
    # graph_backfill, etc. — but for sleep-cycle execution we need them ON
    # (sleep IS the consolidation phase that exercises all those features).
    main_settings = main_settings.model_copy(update={"agent_id": _AGENT_ID})
    eval_scoped = _settings_for_eval_db(eval_settings, main_settings)
    eval_scoped = eval_scoped.model_copy(update={
        "agent_id": _AGENT_ID,
        # Re-enable everything the disable list nuked:
        "event_bus_enabled": True,
        "fact_extraction_enabled": True,
        "episode_summary_enabled": True,
        "graph_backfill_enabled": True,
        "cross_type_linking_enabled": True,
        "contradiction_detection": True,
        "spreading_activation_enabled": True,
        "rubric_enabled": True,
        "rubric_outcome_detection_enabled": True,
        "correction_extraction_enabled": True,
    })

    if not eval_scoped.openai_api_key:
        print("ERROR: OPENAI_API_KEY required for sleep cycle (embeddings).", file=sys.stderr)
        return 2
    if not (eval_scoped.anthropic_api_key or eval_scoped.anthropic_auth_token):
        print("ERROR: ANTHROPIC_API_KEY (or AUTH_TOKEN) required for sleep cycle.", file=sys.stderr)
        return 2

    db = Database(eval_scoped)
    await db.connect()

    embedder = EmbeddingProvider(
        api_key=eval_scoped.openai_api_key,
        model=eval_scoped.embedding_model,
        dimensions=eval_scoped.embedding_dimensions,
    )

    api_client = create_client(main_settings)
    await api_client.start()

    bus = EventBus()

    try:
        # ---------- BEFORE snapshot ----------
        before = await _snapshot(db, _AGENT_ID)
        for line in _format_snapshot(before, "BEFORE sleep"):
            logger.info(line)

        # ---------- Build full stack ----------
        heart = Heart(
            database=db, settings=eval_scoped,
            embedding_provider=embedder, owns_embeddings=False,
        )
        brain = Brain(
            database=db, settings=eval_scoped,
            embedding_provider=embedder,
        )

        async with heart, brain:
            # F022: GraphLinker — needed by GraphDensifier
            graph_linker = GraphLinker(
                db=db, embedder=embedder,
                settings=eval_scoped, agent_id=_AGENT_ID,
            )

            # F040: GraphDensifier — wired into sleep handler
            graph_densifier = GraphDensifier(
                db=db, graph_linker=graph_linker, embedder=embedder,
                settings=eval_scoped, agent_id=_AGENT_ID,
            )

            # F023: Wire admission LLM client (sleep's resolve_contradictions
            # phase invokes Heart.learn → admission)
            try:
                from nous.heart.admission import AdmissionLLMClient
                if heart.facts._admission_controller is not None:
                    heart.facts._admission_controller.llm_client = AdmissionLLMClient(
                        api_client=api_client,
                    )
            except Exception:
                logger.warning("admission LLM wiring failed (non-fatal)", exc_info=True)

            # F027: supersession classifier
            heart.facts.set_llm_client(api_client, model=eval_scoped.contradiction_model)

            # Build sleep handler
            sleep = SleepHandler(
                brain=brain, heart=heart, settings=eval_scoped,
                bus=bus, llm_client=api_client,
            )
            sleep._graph_densifier = graph_densifier

            # F012 procedure learner — best-effort wiring
            try:
                from nous.brain.procedure_learner import ProcedureLearner
                sleep._procedure_learner = ProcedureLearner(
                    brain=brain, heart=heart, settings=eval_scoped,
                    llm_client=api_client,
                )
                logger.info("F012 ProcedureLearner wired")
            except Exception:
                logger.warning("F012 ProcedureLearner not wired (phase will skip)", exc_info=True)

            # F024-3b rubric evolver — best-effort wiring
            try:
                from nous.cognitive.rubric import RubricEvolver, RubricManager
                rubric_manager = RubricManager(db=db, agent_id=_AGENT_ID)
                if not await rubric_manager.get_active():
                    try:
                        await rubric_manager.seed_v1()
                    except Exception:
                        pass  # Already seeded by another process
                sleep._rubric_evolver = RubricEvolver(
                    db=db, settings=eval_scoped, llm_client=api_client,
                    agent_id=_AGENT_ID,
                )
                logger.info("F024-3b RubricEvolver wired")
            except Exception:
                logger.warning("F024-3b RubricEvolver not wired (phase will skip)", exc_info=True)

            # ---------- Run sleep ----------
            t0 = time.monotonic()
            sleep_event = Event(
                type="sleep_started",
                agent_id=_AGENT_ID,
                data={"trigger": "eval_script"},
            )
            logger.info("===== STARTING FULL SLEEP CYCLE =====")
            await sleep._run_sleep(sleep_event)
            wall = time.monotonic() - t0
            logger.info("===== SLEEP CYCLE COMPLETE in %.1fs =====", wall)
            logger.info("Phases completed: %s", sleep._last_phases)

            # ---------- AFTER snapshot ----------
            after = await _snapshot(db, _AGENT_ID)
            for line in _format_snapshot(after, "AFTER sleep"):
                logger.info(line)

            # ---------- Delta report ----------
            edge_delta = after.edge_count_total - before.edge_count_total
            # NOTE: ASCII-only output. Windows cp1252 console can't render
            # the Greek delta char without setting PYTHONIOENCODING=utf-8;
            # 'd=' is the safe portable form.
            print()
            print("=" * 70)
            print(f"SLEEP CYCLE DELTA REPORT (agent_id={_AGENT_ID})")
            print("=" * 70)
            print(f"Wall time: {wall:.1f}s ({wall/60:.1f} min)")
            print(f"Phases completed: {len(sleep._last_phases)}/10")
            print(f"  {', '.join(sleep._last_phases)}")
            print()
            print(f"Edges total:  {before.edge_count_total} -> {after.edge_count_total} (d={edge_delta:+d})")
            for relation in sorted(set(before.edge_count_per_relation) | set(after.edge_count_per_relation)):
                b = before.edge_count_per_relation.get(relation, 0)
                a = after.edge_count_per_relation.get(relation, 0)
                print(f"  {relation:30s}: {b} -> {a} (d={a-b:+d})")
            print()
            print("Orphans by type (lower = better):")
            for t in sorted(set(before.orphan_count_per_type) | set(after.orphan_count_per_type)):
                b = before.orphan_count_per_type.get(t, 0)
                a = after.orphan_count_per_type.get(t, 0)
                print(f"  {t:20s}: {b} -> {a} (d={a-b:+d})")
            print("=" * 70)

    finally:
        await api_client.close()
        await db.disconnect()

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
