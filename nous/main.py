"""Nous agent entry point.

Initializes all components and starts the server:
  Settings -> Database -> Brain -> Heart -> CognitiveLayer -> Runner -> App -> Uvicorn

Uses Starlette lifespan to manage component lifecycle on the same
event loop as uvicorn (F2/F3 fix from 3-agent review).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.routing import Mount

from nous.api.builtin_tools import register_builtin_tools
from nous.api.runner import AgentRunner
from nous.api.tools import ToolDispatcher, register_nous_tools
from nous.api.web_tools import register_web_tools
from nous.brain import Brain
from nous.brain.embeddings import EmbeddingProvider
from nous.cognitive import CognitiveLayer
from nous.config import Settings
from nous.events import Event, EventBus
from nous.heart import Heart
from nous.storage.database import Database
from nous.storage.migrator import run_migrations

logger = logging.getLogger(__name__)


async def create_components(settings: Settings) -> dict:
    """Initialize all components in dependency order.

    Returns dict with all components for lifespan storage.

    1. Database - connection pool
    2. EmbeddingProvider - optional (None if no API key)
    3. Brain - decision intelligence
    4. Heart - memory system (owns_embeddings=False per F4)
    5. CognitiveLayer - orchestrator
    6. AgentRunner - LLM integration
    """
    database = Database(settings)
    await database.connect()  # F1: connect() not initialize()
    await run_migrations(database.engine)  # Apply pending SQL migrations

    # Load runtime config overrides from DB (must be after migrations)
    from nous.runtime_config import RuntimeConfig
    runtime_cfg = RuntimeConfig.get()
    async with database.session() as cfg_session:
        await runtime_cfg.load_from_db(cfg_session)

    embedding_provider = None
    if settings.openai_api_key:
        embedding_provider = EmbeddingProvider(
            api_key=settings.openai_api_key,
            model=settings.embedding_model,
            dimensions=settings.embedding_dimensions,
            cache_size=settings.embedding_cache_size,
        )

    brain = Brain(database, settings, embedding_provider)
    heart = Heart(database, settings, embedding_provider, owns_embeddings=False)  # F4

    # F023: Admission LLM client — wired after api_client is created (below)

    # 006: Create EventBus (only if enabled)
    bus = None
    handler_http = None
    session_monitor = None
    if settings.event_bus_enabled:
        bus = EventBus()

        # DB persistence adapter (P0-1 fix: correct signature — no agent_id/session_id kwargs)
        # 007.4: Pass event.session_id to populate ORM column
        async def persist_to_db(event: Event) -> None:
            data = {**event.data}
            await brain.emit_event(
                event.type, data, session_id=event.session_id,
                event_id=event.event_id, trace_id=event.trace_id,
                caused_by=event.caused_by,
            )

        bus.set_db_persister(persist_to_db)

    # P0-2/P0-3 fix: preserve identity_prompt, pass bus as keyword arg
    # 008: Initialize IdentityManager
    from nous.identity.manager import IdentityManager
    identity_manager = IdentityManager(database, settings.agent_id)

    # 008: Auto-seed from existing facts on upgrade (review fix P2-2)
    try:
        async with database.session() as _seed_session:
            seeded = await identity_manager.auto_seed_from_facts(heart, _seed_session)
            if seeded:
                await _seed_session.commit()
                logger.info("Auto-seeded identity from existing facts")
    except Exception:
        logger.warning("Identity auto-seed check failed (non-fatal)")

    # Create shared API client for all LLM calls (handlers + runner + admission + critic)
    from nous.api.anthropic_client import create_client
    api_client = create_client(settings)
    await api_client.start()

    # F050: wire QueryExpander into Heart (only if flag enabled).
    # Without this wiring NOUS_QUERY_EXPANSION_ENABLED=true is a no-op
    # because heart._query_expander stays None. Construct here so the
    # OAT-capable shared api_client is reused (single auth path).
    if settings.query_expansion_enabled:
        from nous.heart.query_expansion import QueryExpander
        query_expander = QueryExpander(
            llm=api_client,
            settings=settings,
            db=database,
            model=settings.query_expansion_model,
        )
        heart.set_query_expander(query_expander)
        logger.info(
            "F050: QueryExpander wired (model=%s, max_variants=%d, timeout=%.1fs)",
            settings.query_expansion_model,
            settings.query_expansion_max_variants,
            settings.query_expansion_timeout_seconds,
        )

    # F055: Cross-Turn Residual Activation. Default-off; flag-gated.
    if settings.residual_activation_enabled:
        from nous.heart.residual_activation import ResidualActivator
        residual_activator = ResidualActivator(
            settings=settings,
            wm=heart.working_memory,
            db=database,
        )
        heart.set_residual_activator(residual_activator)
        logger.info(
            "F055: ResidualActivator wired (decay_mode=%s, decay=%.2f, top_k=%d, "
            "seed_weight=%.2f, boost_weight=%.2f)",
            settings.residual_decay_mode,
            settings.residual_decay_per_turn,
            settings.residual_top_k_carried,
            settings.residual_seed_weight,
            settings.residual_boost_weight,
        )

    # F024: Critic Agent (uses shared api_client)
    critic = None
    if settings.critic_enabled:
        from nous.cognitive.critic import CriticAgent
        critic = CriticAgent(settings, procedure_manager=heart.procedures)
        critic.set_api_client(api_client)
        logger.info("F024: CriticAgent wired (mode=%s, model=%s)",
                     settings.critic_mode, settings.critic_model)

    # F024 Phase 3b: Rubric manager
    rubric_manager = None
    if settings.rubric_enabled:
        from nous.cognitive.rubric import RubricManager
        rubric_manager = RubricManager(db=database, agent_id=settings.agent_id)
        # Seed v1.0.0 if no active rubric exists
        existing = await rubric_manager.get_active()
        if not existing:
            try:
                await rubric_manager.seed_v1()
                logger.info("F024-3b: Seeded initial rubric v1.0.0")
            except Exception as e:
                # Race condition: another process seeded first — that's fine
                if "unique" in str(e).lower() or "integrity" in str(e).lower():
                    logger.debug("F024-3b: Rubric v1 already seeded by another process")
                else:
                    raise

    cognitive = CognitiveLayer(
        brain, heart, settings, settings.identity_prompt,
        bus=bus, identity_manager=identity_manager,
        critic=critic,
    )

    # §2: wire EpistemicClassifier into the cognitive layer (flag-gated).
    # Reuses the OAT-capable shared api_client (single auth path — same
    # rationale as F050's QueryExpander wiring above).
    if settings.epistemic_gate_enabled:
        from nous.cognitive.epistemic import EpistemicClassifier
        epistemic_classifier = EpistemicClassifier(
            llm=api_client,
            settings=settings,
            model=settings.epistemic_gate_model,
        )
        cognitive.set_epistemic_classifier(epistemic_classifier)
        logger.info(
            "§2: EpistemicClassifier wired (model=%s, timeout=%.1fs, budget=%d/hr)",
            settings.epistemic_gate_model,
            settings.epistemic_gate_timeout_seconds,
            settings.epistemic_gate_max_per_hour,
        )

    # F023: Wire admission LLM client using shared api_client
    if heart.facts._admission_controller is not None:
        from nous.heart.admission import AdmissionLLMClient
        heart.facts._admission_controller.llm_client = AdmissionLLMClient(
            api_client=api_client,
        )

    # F027: Wire supersession classifier LLM client
    heart.facts.set_llm_client(api_client, model=settings.contradiction_model)

    # F075 L3: Wire the date-window parser (reuses the shared api_client)
    from nous.heart.date_window import DateWindowParser
    heart.date_window_parser = DateWindowParser(api_client, settings)

    # F047: Wire actionability classifier + schedule backfill for NULL rows
    if settings.actionability_enabled:
        from nous.heart.actionability import ActionabilityClassifier

        actionability_classifier = ActionabilityClassifier(
            llm=api_client if settings.actionability_llm_enabled else None,
            model=settings.actionability_model,
            default_when_unknown=settings.actionability_default,
        )
        heart.facts._actionability_classifier = actionability_classifier

        if settings.actionability_backfill_on_startup:
            import asyncio as _asyncio
            from nous.handlers.actionability_backfill import (
                ActionabilityBackfillHandler,
                run_backfill_with_supervision,
            )

            backfill_handler = ActionabilityBackfillHandler(
                db=database,
                classifier=actionability_classifier,
                agent_id=settings.agent_id,
                token_budget=settings.actionability_backfill_token_budget,
            )
            # Fire-and-forget; wrapper logs and re-raises CancelledError so
            # exceptions don't vanish into asyncio's void.
            _asyncio.create_task(run_backfill_with_supervision(backfill_handler))

    rubric_evolver = None

    # 006: Register handlers on bus (after cognitive exists for monitor)
    if bus is not None:
        # httpx client for non-LLM HTTP calls (GitHub API, Telegram notifications)
        handler_http = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10, read=60, write=10, pool=10),
        )

        # F022: Create GraphLinker (shared between episode and fact linking)
        graph_linker = None
        try:
            from nous.brain.graph_linker import GraphLinker

            if settings.cross_type_linking_enabled or settings.episode_summary_enabled:
                graph_linker = GraphLinker(
                    db=database, embedder=embedding_provider,
                    settings=settings, agent_id=settings.agent_id,
                )
        except ImportError:
            logger.debug("GraphLinker not available yet")

        # Initialize before the conditional so downstream wiring (F060
        # sleep handler, etc.) can read it unconditionally even when the
        # summarizer is disabled or the import fails.
        episode_summarizer = None
        try:
            from nous.handlers.episode_summarizer import EpisodeSummarizer

            if settings.episode_summary_enabled:
                episode_summarizer = EpisodeSummarizer(heart, brain, settings, bus, api_client, graph_linker=graph_linker)
                # F040: Inject embedder for episode↔episode semantic linking
                if embedding_provider is not None:
                    episode_summarizer._embedder = embedding_provider
        except ImportError:
            logger.debug("EpisodeSummarizer not available yet")

        try:
            from nous.handlers.fact_extractor import FactExtractor

            if settings.fact_extraction_enabled:
                FactExtractor(heart, settings, bus, api_client)
        except ImportError:
            logger.debug("FactExtractor not available yet")

        # F024 Phase 3b: Outcome signal detection
        try:
            from nous.handlers.outcome_detector import OutcomeDetector

            if settings.rubric_outcome_detection_enabled:
                OutcomeDetector(
                    db=database, settings=settings, bus=bus,
                    llm_client=api_client, agent_id=settings.agent_id,
                )
        except ImportError:
            logger.debug("OutcomeDetector not available yet")

        # F039: Correction learning pipeline
        try:
            from nous.handlers.correction_extractor import CorrectionExtractor

            if settings.correction_extraction_enabled:
                CorrectionExtractor(
                    db=database, settings=settings, bus=bus,
                    llm_client=api_client, heart=heart,
                    agent_id=settings.agent_id,
                )
        except ImportError:
            logger.debug("CorrectionExtractor not available yet")

        # F024 Phase 3b: Rubric evolver (triggered via REST or sleep handler)
        rubric_evolver = None
        try:
            if rubric_manager:
                from nous.handlers.rubric_evolver import RubricEvolver
                rubric_evolver = RubricEvolver(
                    rubric_manager=rubric_manager,
                    db=database,
                    settings=settings,
                    agent_id=settings.agent_id,
                )
        except ImportError:
            logger.debug("RubricEvolver not available yet")

        # F022 Phase 2: Wire fact->decision graph linking
        try:
            from nous.handlers.fact_graph_linker import FactGraphLinker

            if graph_linker is not None and settings.cross_type_linking_enabled:
                heart._bus = bus  # Inject bus for fact_learned emission
                brain._bus = bus  # F040: Inject bus for decision_recorded emission
                FactGraphLinker(graph_linker, settings, bus)
                logger.debug("F022: FactGraphLinker wired — fact->decision linking enabled")
        except ImportError:
            logger.debug("FactGraphLinker not available yet")

        # F040: Wire graph densifier
        graph_densifier = None
        try:
            from nous.brain.graph_densifier import GraphDensifier
            if graph_linker is not None and settings.graph_backfill_enabled:
                graph_densifier = GraphDensifier(
                    db=database, graph_linker=graph_linker,
                    embedder=embedding_provider, settings=settings,
                    agent_id=settings.agent_id,
                )
                logger.debug("F040: GraphDensifier created")
        except ImportError:
            logger.debug("GraphDensifier not available yet")

        # F040: Wire decision reverse-linking
        try:
            from nous.handlers.decision_graph_linker import DecisionGraphLinker
            if graph_linker is not None and settings.cross_type_linking_enabled:
                DecisionGraphLinker(brain, graph_linker, embedding_provider, settings, bus)
                logger.debug("F040: DecisionGraphLinker wired")
        except ImportError:
            logger.debug("DecisionGraphLinker not available yet")

        # F040: Wire procedure graph linking
        try:
            from nous.handlers.procedure_graph_linker import ProcedureGraphLinker
            if graph_linker is not None and settings.cross_type_linking_enabled:
                ProcedureGraphLinker(graph_linker, embedding_provider, settings, bus)
                logger.debug("F040: ProcedureGraphLinker wired")
        except ImportError:
            logger.debug("ProcedureGraphLinker not available yet")

        try:
            from nous.handlers.knowledge_extractor import KnowledgeExtractor

            if settings.compaction_enabled:
                KnowledgeExtractor(heart, settings, bus, api_client)
        except ImportError:
            logger.debug("KnowledgeExtractor not available yet")

        try:
            from nous.handlers.session_monitor import SessionTimeoutMonitor

            session_monitor = SessionTimeoutMonitor(
                bus, settings, cognitive=cognitive, heart=heart
            )
        except ImportError:
            logger.debug("SessionTimeoutMonitor not available yet")

        sleep_handler = None
        try:
            from nous.handlers.sleep_handler import SleepHandler

            if settings.sleep_enabled:
                sleep_handler = SleepHandler(brain, heart, settings, bus, api_client)
        except ImportError:
            logger.debug("SleepHandler not available yet")

        # F024-3b: Wire rubric evolver into sleep handler
        if sleep_handler is not None and rubric_evolver is not None:
            sleep_handler._rubric_evolver = rubric_evolver

        # F040: Wire graph densifier into sleep handler
        if sleep_handler is not None and graph_densifier is not None:
            sleep_handler._graph_densifier = graph_densifier

        # F060: Wire episode summarizer into sleep handler so the abandoned-
        # episode recovery phase can re-summarize stuck-open sessions.
        if sleep_handler is not None and episode_summarizer is not None:
            sleep_handler._episode_summarizer = episode_summarizer

        # F012: Wire procedure learner into sleep handler + monitor
        procedure_learner = None
        if settings.procedure_learning_enabled:
            try:
                from nous.handlers.procedure_learner import ProcedureLearner

                procedure_learner = ProcedureLearner(
                    brain=brain, heart=heart, embeddings=embedding_provider,
                    settings=settings, llm_client=api_client,
                )
                if sleep_handler is not None:
                    sleep_handler._procedure_learner = procedure_learner
                # Wire into monitor for pathway 3 (real-time recovery)
                if cognitive._monitor is not None:
                    cognitive._monitor._procedure_learner = procedure_learner
                logger.info("F012: ProcedureLearner wired into sleep handler + monitor")
            except ImportError:
                logger.debug("ProcedureLearner not available yet")

        try:
            from nous.handlers.decision_reviewer import DecisionReviewer

            if settings.decision_review_enabled:
                decision_reviewer = DecisionReviewer(brain, settings, bus, handler_http)
            else:
                decision_reviewer = None
        except ImportError:
            decision_reviewer = None
            logger.debug("DecisionReviewer not available yet")

        # F020: Clean up tool cache on session end
        from nous.api.tool_cache import cleanup_session_cache

        async def _on_session_ended_cleanup_cache(event):
            sid = getattr(event, "session_id", None) or (event.get("session_id") if isinstance(event, dict) else None)
            if sid:
                try:
                    async with database.session() as db_sess:
                        count = await cleanup_session_cache(db_sess, sid)
                        if count:
                            logger.debug("Cleaned %d cache entries for session %s", count, sid)
                except Exception:
                    logger.warning("Failed to cleanup tool cache", exc_info=True)

        bus.on("session_ended", _on_session_ended_cleanup_cache)

        # Start bus + monitor
        await bus.start()
        if session_monitor:
            await session_monitor.start()
        if decision_reviewer:
            await decision_reviewer.start()

    # F039: Wire LLM client into monitor for inline correction detection
    # (outside bus guard — inline corrections work independently of event bus)
    if settings.correction_extraction_enabled and cognitive._monitor is not None:
        cognitive._monitor._llm_client = api_client

    # F011: Bootstrap local skills (one-time, only if DB has no skills)
    try:
        from nous.skills.bootstrap import bootstrap_local_skills, reactivate_skills
        await bootstrap_local_skills(settings.workspace_dir, heart)
        await reactivate_skills(heart)
    except Exception:
        logger.debug("Skill bootstrap skipped or failed (non-fatal)")

    # Create tool dispatcher and register all tools
    dispatcher = ToolDispatcher(
        tool_schema_cache_enabled=settings.tool_schema_cache_enabled,
        stable_tool_set_enabled=settings.stable_tool_set_enabled,
        arg_salvage_enabled=settings.tool_arg_salvage_enabled,
    )
    register_nous_tools(dispatcher, brain, heart, settings=settings)
    register_builtin_tools(dispatcher, settings)

    # Web tools httpx client (separate from runner — no API auth headers)
    web_http = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10, read=30, write=10, pool=10),
        limits=httpx.Limits(max_connections=5, max_keepalive_connections=2),
    )
    # F033: Multi-tier search router
    from nous.api.search_providers import TavilyProvider, ExaProvider, BraveProvider
    from nous.api.search_router import SearchRouter

    search_router = SearchRouter(
        tavily=TavilyProvider(api_key=settings.tavily_api_key),
        exa=ExaProvider(api_key=settings.exa_api_key),
        brave=BraveProvider(api_key=settings.brave_search_api_key),
        mode=settings.search_provider,
    )
    register_web_tools(dispatcher, settings, web_http, router=search_router)

    # Issue #220: Register Telegram file delivery tool (gated on bot token)
    if settings.telegram_bot_token:
        from nous.api.telegram_tools import register_telegram_tools
        register_telegram_tools(dispatcher, settings, web_http)
        logger.info("Telegram file delivery tool registered (send_file)")

    # F078.1: Register guarded send_email tool (gated on SMTP login user).
    # Additive — the agent-authored bash+smtplib path stays available (BC).
    if settings.email_user:
        from nous.api.email_tools import register_email_tools
        register_email_tools(dispatcher, settings)
        logger.info("Guarded email tool registered (send_email)")

    # F020: Register cache_retrieve tool
    from nous.api.tools import register_cache_retrieve_tool
    register_cache_retrieve_tool(dispatcher, database.session_factory)

    # 008: Register identity tools (gated by "initiation" frame)
    from nous.identity.tools import register_identity_tools
    register_identity_tools(dispatcher, identity_manager)

    runner = AgentRunner(cognitive, brain, heart, settings)
    runner.set_dispatcher(dispatcher)
    runner.set_api_client(api_client)
    await runner.start()

    # Late-bind runner into SessionTimeoutMonitor so idle-timeout closures
    # take the canonical runner.end_conversation path (full cleanup + reflection)
    # rather than only cognitive.end_session. Mirrors the procedure_learner
    # late-binding above. Safe no-op if monitor is unavailable.
    if session_monitor is not None:
        session_monitor._runner = runner
        # Back-reference so runner.run_turn / stream_chat can synchronously
        # touch _last_activity at request-receipt time, closing the race
        # where a long in-flight turn would otherwise be closed mid-stream
        # by a monitor tick (the bus path is queued and turn_completed
        # only fires after the entire turn finishes).
        runner._session_monitor = session_monitor

    # F091: Retrieval Telemetry — what recall retrieved, and what it dropped.
    # Registered process-wide because the two retrieval paths are reached from
    # very different places (a tool closure and a cognitive-layer component).
    retrieval_log_retention_task = None
    retrieval_logger = None
    if settings.retrieval_telemetry_enabled:
        from nous.observability.retrieval_logger import RetrievalLogger, set_active

        # Latch so only the FIRST write failure logs at ERROR (list, not bool,
        # so the closure can mutate it without `nonlocal`).
        _retrieval_write_failed: list[bool] = []

        async def _write_retrieval_log(payload: dict):
            try:
                async with database.session() as s:
                    from sqlalchemy import text
                    await s.execute(text(
                        "INSERT INTO nous_system.retrieval_log "
                        "(id, agent_id, session_id, turn_number, trace_id, path, query, "
                        "duration_ms, legs, excluded_types, n_candidates, n_rendered, "
                        "n_expansions, disposition_counts, candidates, expansions, truncated) "
                        "VALUES (:id, :agent_id, :sid, :turn, :trace, :path, :query, "
                        ":dur, :legs, :excl, :n_cand, :n_rend, :n_exp, :disp, "
                        ":cands, :exps, :trunc)"
                    ), {
                        "id": payload["id"],
                        "agent_id": payload.get("agent_id") or settings.agent_id,
                        "sid": payload.get("session_id"),
                        "turn": payload.get("turn_number"),
                        "trace": payload.get("trace_id"),
                        "path": payload.get("path", "pipeline"),
                        "query": payload.get("query"),
                        "dur": payload.get("duration_ms"),
                        "legs": json.dumps(payload.get("legs", [])),
                        "excl": json.dumps(payload.get("excluded_types", [])),
                        "n_cand": payload.get("n_candidates", 0),
                        "n_rend": payload.get("n_rendered", 0),
                        "n_exp": payload.get("n_expansions", 0),
                        "disp": json.dumps(payload.get("disposition_counts", {})),
                        # NULL (not '[]') when unsampled, so "not captured" is
                        # distinguishable from "captured, found nothing".
                        "cands": (
                            json.dumps(payload["candidates"])
                            if payload.get("candidates") is not None else None
                        ),
                        "exps": json.dumps(payload.get("expansions", [])),
                        "trunc": payload.get("truncated", False),
                    })
                    await s.commit()
            except Exception:
                # First failure at ERROR, the rest at DEBUG. Swallowing every
                # one at DEBUG meant an unapplied migration 070 (or a renamed
                # column) showed up only as "No retrievals recorded yet" on the
                # dashboard, with nothing above info to explain the silence.
                if not _retrieval_write_failed:
                    _retrieval_write_failed.append(True)
                    logger.error(
                        "F091: retrieval log write failed — telemetry will not "
                        "persist. Is migration 070 applied? Further failures "
                        "log at DEBUG.", exc_info=True,
                    )
                else:
                    logger.debug("F091: retrieval log write failed", exc_info=True)

        retrieval_logger = RetrievalLogger(
            db_writer=_write_retrieval_log,
            enabled=True,
            candidate_sample_rate=settings.retrieval_telemetry_candidate_sample_rate,
            snippet_chars=settings.retrieval_telemetry_snippet_chars,
            max_candidates=settings.retrieval_telemetry_max_candidates,
            ring_size=settings.retrieval_telemetry_ring_size,
            agent_id=settings.agent_id,
            query_chars=settings.retrieval_telemetry_query_chars,
        )
        set_active(retrieval_logger)
        logger.info(
            "F091: RetrievalLogger wired (candidate_sample_rate=%.2f)",
            settings.retrieval_telemetry_candidate_sample_rate,
        )

        if getattr(settings, "retrieval_telemetry_retention_days", 0) > 0:
            async def _retrieval_log_retention_loop():
                # Sweep once at startup, THEN daily. These rows are 10-100x
                # larger than context_log's, so a process restarted daily would
                # never prune under a sleep-first loop.
                first = True
                while True:
                    try:
                        if first:
                            first = False
                        else:
                            await asyncio.sleep(86400)
                        days = settings.retrieval_telemetry_retention_days
                        async with database.session() as s:
                            from sqlalchemy import text
                            # agent-scoped: the table is agent-scoped and the
                            # retention setting is per-process, so an unscoped
                            # DELETE lets a default-configured agent destroy
                            # the rows of one configured to keep them longer.
                            await s.execute(text(
                                "DELETE FROM nous_system.retrieval_log "
                                "WHERE agent_id = :agent_id "
                                "AND timestamp < now() - make_interval(days => :d)"
                            ), {"d": days, "agent_id": settings.agent_id})
                            await s.commit()
                        logger.info(
                            "F091: retrieval_log retention sweep (>%dd, agent=%s) done",
                            days, settings.agent_id,
                        )
                    except asyncio.CancelledError:
                        break
                    except Exception:
                        logger.debug("F091: retrieval retention sweep failed", exc_info=True)

            retrieval_log_retention_task = asyncio.create_task(_retrieval_log_retention_loop())

    # F035.4: Context Logger
    context_logger = None
    context_log_retention_task = None
    if settings.context_log_enabled:
        from nous.observability.context_logger import ContextLogger

        async def _write_context_log(entry):
            try:
                async with database.session() as s:
                    from sqlalchemy import text
                    await s.execute(text(
                        "INSERT INTO nous_system.context_log "
                        "(id, agent_id, session_id, turn_number, call_type, model, frame_id, trace_id, "
                        "token_breakdown, total_tokens_est, context_window_size, utilization_pct, "
                        "sections_present, tools_count, tool_names, messages_count, message_roles, "
                        "loaded_facts, loaded_decisions, loaded_procedures, loaded_episodes, recent_conversations) "
                        "VALUES (:id, :agent_id, :sid, :turn, :ctype, :model, :frame, :trace, "
                        ":breakdown, :total, :window, :util, :sections, :tools_c, :tool_names, "
                        ":msg_c, :msg_roles, :facts, :decisions, :procedures, :episodes, :conversations)"
                    ), {
                        "id": entry.id, "agent_id": settings.agent_id,
                        "sid": entry.session_id, "turn": entry.turn_number,
                        "ctype": entry.call_type, "model": entry.model,
                        "frame": entry.frame_id, "trace": entry.trace_id,
                        "breakdown": json.dumps(entry.token_breakdown),
                        "total": entry.total_tokens_est, "window": entry.context_window_size,
                        "util": entry.utilization_pct, "sections": entry.sections_present,
                        "tools_c": entry.tools_count, "tool_names": entry.tool_names,
                        "msg_c": entry.messages_count, "msg_roles": json.dumps(entry.message_roles),
                        "facts": entry.loaded_facts, "decisions": entry.loaded_decisions,
                        "procedures": entry.loaded_procedures, "episodes": entry.loaded_episodes,
                        "conversations": entry.recent_conversations,
                    })
                    await s.commit()
            except Exception:
                logger.debug("F035.4: context log write failed", exc_info=True)

        async def _update_context_log(entry):
            # Audit OB-3: persist the response-side columns set by
            # update_response (the INSERT at log() time predates the response).
            try:
                async with database.session() as s:
                    from sqlalchemy import text
                    await s.execute(text(
                        "UPDATE nous_system.context_log SET "
                        "input_tokens_actual = :in_tok, output_tokens = :out_tok, "
                        "cache_creation = :cc, cache_read = :cr, "
                        "duration_ms = :dur, stop_reason = :stop "
                        "WHERE id = :id"
                    ), {
                        "in_tok": entry.input_tokens_actual,
                        "out_tok": entry.output_tokens,
                        "cc": entry.cache_creation_tokens,
                        "cr": entry.cache_read_tokens,
                        "dur": entry.duration_ms,
                        "stop": entry.stop_reason,
                        "id": entry.id,
                    })
                    await s.commit()
            except Exception:
                logger.debug("OB-3: context log response update failed", exc_info=True)

        context_logger = ContextLogger(
            db_writer=_write_context_log,
            full_payload_enabled=settings.context_log_full_payload,
            ring_size=settings.context_log_ring_size,
            max_total=settings.context_log_max_total,
            db_updater=_update_context_log,
        )
        runner.set_context_logger(context_logger)
        logger.info("F035.4: ContextLogger wired (full_payload=%s)", settings.context_log_full_payload)

        # Audit OB-1: periodic retention sweep so nous_system.context_log and
        # behavior_snapshots don't grow unbounded (one row per API call). The
        # documented context_log_retention_days had zero consumers before this.
        # (context_log_retention_task initialized to None above the conditional.)
        if getattr(settings, "context_log_retention_days", 0) > 0:
            async def _context_log_retention_loop():
                while True:
                    try:
                        await asyncio.sleep(86400)  # daily
                        days = settings.context_log_retention_days
                        async with database.session() as s:
                            from sqlalchemy import text
                            await s.execute(text(
                                "DELETE FROM nous_system.context_log "
                                "WHERE timestamp < now() - make_interval(days => :d)"
                            ), {"d": days})
                            await s.execute(text(
                                "DELETE FROM nous_system.behavior_snapshots "
                                "WHERE timestamp < now() - make_interval(days => :d)"
                            ), {"d": days})
                            await s.commit()
                        logger.info("OB-1: context_log/behavior_snapshots retention sweep (>%dd) done", days)
                    except asyncio.CancelledError:
                        break
                    except Exception:
                        logger.debug("OB-1: retention sweep failed", exc_info=True)

            context_log_retention_task = asyncio.create_task(
                _context_log_retention_loop(), name="context-log-retention"
            )

    # 011.1 + 012.2: Register subtask/schedule tools (after runner for inline execution)
    # F061 PR-3: pass bus so inline hardened subtasks emit subtask_outcome telemetry.
    if settings.subtask_enabled:
        from nous.api.tools import register_subtask_tools
        register_subtask_tools(dispatcher, heart, settings, runner=runner, bus=bus)

    # 012.3: Register programmatic tool calling (run_python)
    if settings.programmatic_tools_enabled:
        from nous.api.tools import register_programmatic_tools
        register_programmatic_tools(dispatcher, brain, heart, settings, cognitive=cognitive)

    # 011.1: Start SubtaskWorkerPool (needs runner + bus)
    subtask_pool = None
    if settings.subtask_enabled and bus is not None:
        try:
            from nous.handlers.subtask_worker import SubtaskWorkerPool
            subtask_pool = SubtaskWorkerPool(
                runner=runner, heart=heart, settings=settings,
                bus=bus, http_client=handler_http,
            )
            await subtask_pool.start()
        except ImportError:
            logger.debug("SubtaskWorkerPool not available yet")

    # 011.1: Start TaskScheduler
    task_scheduler = None
    if settings.schedule_enabled:
        try:
            from nous.handlers.task_scheduler import TaskScheduler
            task_scheduler = TaskScheduler(heart, settings)
            await task_scheduler.start()
        except ImportError:
            logger.debug("TaskScheduler not available yet")

    # F034: Heartbeat proactive monitoring
    dynamic_loader = None
    heartbeat_runner = None
    if settings.heartbeat_enabled:
        try:
            from nous.heartbeat.finding_store import FindingStore
            from nous.heartbeat.runner import HeartbeatRunner
            from nous.heartbeat.registry import CheckRegistry
            from nous.heartbeat.schemas import EscalationConfig
            from nous.heartbeat.checks import (
                HealthCheck, SelfInitiatedCheck, EmailCheck, DriveCheck,
            )

            escalation_config = EscalationConfig(
                low_to_normal_hours=settings.heartbeat_escalation_low_to_normal_hours,
                normal_to_high_hours=settings.heartbeat_escalation_normal_to_high_hours,
                high_realert_hours=settings.heartbeat_escalation_high_realert_hours,
                accumulation_threshold=settings.heartbeat_escalation_accumulation_threshold,
            )
            finding_store = FindingStore(escalation_config=escalation_config)

            registry = CheckRegistry()
            registry.register(HealthCheck(heart, brain, settings), permanent=True)
            registry.register(
                SelfInitiatedCheck(heart, brain, settings, embeddings=embedding_provider),
                permanent=True,
            )

            # Audit HB-4: EmailCheck registration moved below, after the
            # heartbeat API client exists, so the F034.2 LLM classification tier
            # is actually wired (previously EmailCheck(settings) was built with
            # no llm_callable → the tier was dead and every email fell back to
            # 4-keyword matching).

            if settings.heartbeat_drive_enabled and settings.google_service_account_json:
                registry.register(DriveCheck(settings, heart=heart))

            # F035.3: Behavioral drift detection
            if settings.drift_detection_enabled and bus is not None:
                from nous.heartbeat.checks import BehaviorDriftCheck
                drift_check = BehaviorDriftCheck(
                    heart=heart, brain=brain, settings=settings,
                    bus_stats=bus.stats, db=database,
                )
                registry.register(drift_check)
                logger.info("F035.3: BehaviorDriftCheck registered (interval=%ds)", drift_check.interval)


            # F034.5: Create dynamic check loader
            from nous.heartbeat.dynamic import DynamicCheckLoader
            dynamic_loader = DynamicCheckLoader(
                db=database, registry=registry,
                agent_id=settings.agent_id,
                max_checks=settings.heartbeat_max_dynamic_checks,
                model_override=settings.heartbeat_model or settings.background_model,
                default_timeout=settings.heartbeat_default_check_timeout,
            )

            # Create dedicated API client for heartbeat (isolated connection pool)
            heartbeat_api_client = create_client(settings)
            await heartbeat_api_client.start()
            logger.info("F034: Heartbeat API client created (isolated from main runner)")

            # Audit HB-4: register EmailCheck WITH an llm_callable so the F034.2
            # LLM classification tier is reachable (uses the isolated heartbeat
            # client + the configured background model). Falls back to keyword
            # classification automatically if the call returns empty/raises.
            if settings.heartbeat_email_enabled and settings.email_user:
                from nous.handlers import call_background_llm

                _email_model = settings.heartbeat_model or settings.background_model

                async def _email_llm_classify(prompt: str) -> str:
                    resp = await call_background_llm(
                        heartbeat_api_client,
                        _email_model,
                        "You are an email triage classifier.",
                        prompt,
                        max_tokens=16,
                    )
                    return resp or ""

            heartbeat_runner = HeartbeatRunner(
                settings=settings, registry=registry, runner=runner,
                brain=brain, heart=heart, bus=bus, http_client=handler_http,
                finding_store=finding_store,
                api_client=heartbeat_api_client,
                dynamic_loader=dynamic_loader,
            )

            # Audit HB-4 (review P2): register EmailCheck AFTER the runner exists
            # so the LLM classification tier is gated by the same heartbeat daily
            # token budget as every other LLM call (budget_check). Without this
            # the email LLM ran unbudgeted — a first-deploy backlog of unseen
            # mail could burst Haiku calls with no accounting. The registry is
            # read live by the tick loop, so registering before start() is fine.
            if settings.heartbeat_email_enabled and settings.email_user:
                registry.register(EmailCheck(
                    settings,
                    llm_callable=_email_llm_classify,
                    budget_check=heartbeat_runner._has_budget,
                ))
                logger.info(
                    "F034.2: EmailCheck registered with LLM tier (budget-gated)"
                )

            await heartbeat_runner.start()
        except ImportError:
            logger.debug("Heartbeat not available yet")

    # F034.5: Register heartbeat check management tools
    if heartbeat_runner and heartbeat_runner.dynamic_loader:
        from nous.api.tools import register_heartbeat_tools
        register_heartbeat_tools(dispatcher, heartbeat_runner.dynamic_loader)

    # F038: DAG Orchestration
    dag_orchestrator = None
    dag_store = None
    if settings.dag_enabled:
        try:
            from nous.dag.store import DAGStore
            from nous.dag.orchestrator import DAGOrchestrator
            from nous.dag.delivery import DAGResultDelivery

            dag_store = DAGStore(database, agent_id=settings.agent_id, settings=settings)
            # F087: carries a finished DAG's outcome to the bus, an optional
            # agent-authored summary, and Telegram. Without it a DAG that
            # completes after hours of work is never announced at all.
            dag_delivery = DAGResultDelivery(
                settings,
                agent_id=settings.agent_id,
                bus=bus,
                runner=runner,
            )
            dag_orchestrator = DAGOrchestrator(
                store=dag_store,
                subtask_mgr=heart.subtasks,
                dynamic_loader=dynamic_loader,
                bus=bus,
                settings=settings,
                delivery=dag_delivery,
                # Audit DG-5: pass the LLM client so F066.1 free-form fix
                # dispatch actually runs when dag_fix_llm_dispatch_enabled=true
                # (prod). Previously llm_client defaulted to None → the flag was
                # inert and every fix node used rule-based dispatch only.
                llm_client=api_client,
            )

            if heartbeat_runner is not None:
                heartbeat_runner.dag_orchestrator = dag_orchestrator
                # F087: the heartbeat loop is the orchestrator's only clock.
                # dag_create checks this flag and refuses when it is False.
                dag_orchestrator.clock_wired = True
                logger.info("F038: DAG orchestrator wired to heartbeat runner")
            else:
                # F087: previously silent. Tools registered regardless of the
                # tick being wired, so with the heartbeat disabled the agent
                # could create DAGs that launch wave-0 and then never advance
                # — no error anywhere. Fail loud instead.
                logger.error(
                    "F038/F087: DAG orchestration is enabled but no heartbeat "
                    "runner exists, so nothing will ever advance a DAG. "
                    "dag_create will refuse until NOUS_HEARTBEAT_ENABLED=true "
                    "(or set NOUS_DAG_ENABLED=false to hide the tools)."
                )

            from nous.api.tools import register_dag_tools
            register_dag_tools(dispatcher, dag_store, dag_orchestrator)

            # F064.1: late-bind DAGStore so runner._tool_loop can fire
            # activity pings to dag_nodes.last_activity_at for subtasks
            # running under a DAG node.
            runner.set_dag_store(dag_store)

            logger.info("F038: DAG orchestration enabled")

            # F064.6: work-queue ingress check. Lives inside the DAG-init
            # block because the check needs dag_store + dag_orchestrator
            # to dispatch DAGs for work items. Master flag default off.
            if settings.work_queue_enabled and heartbeat_runner is not None:
                try:
                    from nous.heart.work_queue import WorkQueueItemManager
                    from nous.heartbeat.work_queue import (
                        WorkQueueCheck, build_adapter,
                    )
                    wq_items_mgr = WorkQueueItemManager(
                        database, settings.agent_id,
                    )
                    wq_check = WorkQueueCheck(
                        adapter=build_adapter(settings),
                        items_mgr=wq_items_mgr,
                        dag_store=dag_store,
                        orchestrator=dag_orchestrator,
                        settings=settings,
                    )
                    heartbeat_runner.registry.register(wq_check)
                    logger.info(
                        "F064.6: WorkQueueCheck registered (source=%s, interval=%ds)",
                        settings.work_queue_source,
                        settings.work_queue_interval_seconds,
                    )
                except Exception:
                    logger.warning(
                        "F064.6: WorkQueueCheck registration failed",
                        exc_info=True,
                    )
        except ImportError:
            logger.debug("F038: DAG module not available yet")

    # F092: A2UI companion surfaces
    surface_service = None
    action_router = None
    a2ui_sweep_task = None
    if settings.a2ui_enabled:
        from nous.a2ui.actions import ActionRouter
        from nous.a2ui.compose import SurfaceComposer
        from nous.a2ui.service import SurfaceService
        from nous.a2ui.sources import build_default_registry
        from nous.a2ui.tools import register_a2ui_tools

        surface_service = SurfaceService(database, settings, heart=heart)
        # F092.1: ephemeral micro-apps. The composer needs the same client
        # the background handlers use; the source registry gives every
        # micro-app its server-resolved data (self-sourcing, extended from
        # the Phase 2 template guard).
        composer = None
        if settings.a2ui_compose_enabled:
            composer = SurfaceComposer(
                api_client,
                settings,
                build_default_registry(
                    heart=heart,
                    brain=brain,
                    dag_store=dag_store,
                    heartbeat_runner=heartbeat_runner,
                ),
            )
        action_router = ActionRouter(
            database,
            settings,
            surface_service,
            heart=heart,
            brain=brain,
            heartbeat_runner=heartbeat_runner,
            dag_orchestrator=dag_orchestrator,
            composer=composer,
        )
        register_a2ui_tools(
            dispatcher, surface_service, brain=brain, dag_store=dag_store, composer=composer
        )

        async def _a2ui_sweep_loop():
            # Sweep once at startup, then periodically. The sweep must run
            # unobserved: expiry writes no_objection evidence ("silence
            # counts", spec 6.2) even if no client ever connects, so it
            # cannot be piggybacked on client activity.
            first = True
            while True:
                try:
                    if first:
                        first = False
                        # Restart invalidation: live heartbeat surfaces
                        # reference an in-memory finding store that no longer
                        # exists — every button on them is dead. Expire them
                        # up front instead of serving 72h of "not found".
                        stale = await surface_service.invalidate_heartbeat_surfaces()
                        if stale:
                            logger.info("F092: invalidated %d stale heartbeat surface(s)", stale)
                    else:
                        await asyncio.sleep(settings.a2ui_sweep_interval_seconds)
                    expired = await surface_service.expire_sweep()
                    if expired:
                        logger.info("F092: expiry sweep expired %d surface(s)", expired)
                except asyncio.CancelledError:
                    break
                except Exception:
                    logger.warning("F092: expiry sweep failed", exc_info=True)

        a2ui_sweep_task = asyncio.create_task(_a2ui_sweep_loop())
        logger.info("F092: A2UI companion enabled (push_surface + /a2ui routes + sweep)")

    return {
        "database": database,
        "brain": brain,
        "heart": heart,
        "cognitive": cognitive,
        "runner": runner,
        "dispatcher": dispatcher,
        "embedding_provider": embedding_provider,
        "web_http": web_http,
        "bus": bus,
        "session_monitor": session_monitor,
        "handler_http": handler_http,
        "identity_manager": identity_manager,
        "subtask_pool": subtask_pool,
        "task_scheduler": task_scheduler,
        "decision_reviewer": decision_reviewer,
        "api_client": api_client,
        "sleep_handler": sleep_handler,
        "rubric_manager": rubric_manager,
        "rubric_evolver": rubric_evolver if bus else None,
        "heartbeat_runner": heartbeat_runner,
        "dag_orchestrator": dag_orchestrator,
        "context_logger": context_logger,
        "context_log_retention_task": context_log_retention_task,
        "retrieval_log_retention_task": retrieval_log_retention_task,
        "retrieval_logger": retrieval_logger,
        "surface_service": surface_service,
        "action_router": action_router,
        "a2ui_sweep_task": a2ui_sweep_task,
    }


async def shutdown_components(components: dict) -> None:
    """Graceful shutdown in reverse order."""
    logger.info("Shutting down Nous...")

    # F034: Stop heartbeat before other components
    heartbeat_runner = components.get("heartbeat_runner")
    if heartbeat_runner:
        await heartbeat_runner.stop()

    # F087: the delivery sweep is detached from tick() so it cannot stall the
    # heartbeat loop, which means it can still be in flight here — holding a
    # DB session and a half-finished notification. Drain it after the
    # heartbeat has stopped launching new ones but BEFORE the pool closes,
    # bounded so a hung Telegram call cannot block shutdown.
    dag_orchestrator = components.get("dag_orchestrator")
    if dag_orchestrator is not None:
        try:
            await asyncio.wait_for(dag_orchestrator.wait_for_delivery(), timeout=30)
        except asyncio.TimeoutError:
            logger.warning(
                "F087: in-flight DAG delivery did not finish within 30s — "
                "abandoning it; the sweep will re-deliver after restart"
            )
        except Exception:
            logger.warning("F087: error draining DAG delivery", exc_info=True)

    # F092: stop the surface expiry sweep
    a2ui_sweep = components.get("a2ui_sweep_task")
    if a2ui_sweep:
        a2ui_sweep.cancel()
        try:
            await a2ui_sweep
        except (asyncio.CancelledError, Exception):
            pass

    # OB-1: stop the context-log retention sweep
    retention_task = components.get("context_log_retention_task")
    if retention_task:
        retention_task.cancel()
        try:
            await retention_task
        except (asyncio.CancelledError, Exception):
            pass

    # F091: stop the retrieval-log retention sweep and unregister the sink so a
    # restarted process never writes through a logger bound to a closed pool.
    retrieval_retention_task = components.get("retrieval_log_retention_task")
    if retrieval_retention_task:
        retrieval_retention_task.cancel()
        try:
            await retrieval_retention_task
        except (asyncio.CancelledError, Exception):
            pass
    # Drain in-flight writes BEFORE unregistering and before the pool closes —
    # a fire-and-forget write caught by loop teardown is lost silently, since
    # the writer swallows its own errors.
    retrieval_logger = components.get("retrieval_logger")
    if retrieval_logger is not None:
        try:
            await retrieval_logger.drain()
        except Exception:
            logger.debug("F091: retrieval drain failed", exc_info=True)
    try:
        from nous.observability.retrieval_logger import set_active
        set_active(None)
    except Exception:
        pass

    # 011.1: Stop subtask pool and task scheduler first
    subtask_pool = components.get("subtask_pool")
    if subtask_pool:
        await subtask_pool.stop()
    task_scheduler = components.get("task_scheduler")
    if task_scheduler:
        await task_scheduler.stop()

    # 009.5: Stop decision reviewer
    decision_reviewer = components.get("decision_reviewer")
    if decision_reviewer:
        await decision_reviewer.stop()

    # 006: Stop session monitor and event bus first
    session_monitor = components.get("session_monitor")
    if session_monitor:
        await session_monitor.stop()

    bus = components.get("bus")
    if bus:
        await bus.stop()

    handler_http = components.get("handler_http")
    if handler_http:
        await handler_http.aclose()

    web_http = components.get("web_http")
    if web_http:
        await web_http.aclose()

    runner = components.get("runner")
    if runner:
        await runner.close()

    api_client = components.get("api_client")
    if api_client:
        await api_client.close()

    heart = components.get("heart")
    if heart:
        await heart.close()

    brain = components.get("brain")
    if brain:
        await brain.close()

    database = components.get("database")
    if database:
        await database.disconnect()  # F1: disconnect() not close()

    logger.info("Nous shutdown complete.")


def build_app(settings: Settings) -> Starlette:
    """Build the combined Starlette app with REST + MCP.

    Uses Starlette lifespan for component lifecycle management (F2/F3).
    """
    # Closure to share components between lifespan and app
    components: dict = {}

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        # Startup
        nonlocal components
        components.update(await create_components(settings))

        # Store on app.state for access in tests
        app.state.components = components

        logger.info("Nous started: %s (%s)", settings.agent_name, settings.agent_id)
        logger.info(
            "API: max_turns=%d, workspace=%s",
            settings.max_turns,
            settings.workspace_dir,
        )
        yield

        # Shutdown (reverse order)
        # MCP session manager cleanup (F25)
        mcp_manager = getattr(app.state, "mcp_manager", None)
        if mcp_manager:
            try:
                await mcp_manager.close()
            except Exception:
                logger.warning("MCP session manager cleanup failed")

        await shutdown_components(components)

    # Import here to avoid circular imports at module level
    from nous.api.rest import create_app

    app = create_app(
        runner=_lazy_component(components, "runner"),
        brain=_lazy_component(components, "brain"),
        heart=_lazy_component(components, "heart"),
        cognitive=_lazy_component(components, "cognitive"),
        database=_lazy_component(components, "database"),
        settings=settings,
        lifespan=lifespan,
        identity_manager=_lazy_component(components, "identity_manager"),
        bus=_lazy_component(components, "bus"),
        sleep_handler=_lazy_component(components, "sleep_handler"),
        rubric_manager=_lazy_component(components, "rubric_manager"),
        rubric_evolver=_lazy_component(components, "rubric_evolver"),
        heartbeat_runner=_lazy_component(components, "heartbeat_runner"),
        session_monitor=_lazy_component(components, "session_monitor"),
        context_logger=_lazy_component(components, "context_logger"),
        surface_service=_lazy_component(components, "surface_service"),
        action_router=_lazy_component(components, "action_router"),
    )

    if settings.mcp_enabled:
        try:
            from nous.api.mcp import create_mcp_server

            # MCP server needs real components, which are only available after lifespan starts.
            # We mount a lazy ASGI app that creates the MCP server on first request.
            _mcp_manager = None

            async def mcp_asgi(scope, receive, send):
                nonlocal _mcp_manager
                if _mcp_manager is None:
                    _mcp_manager = create_mcp_server(
                        runner=components["runner"],
                        brain=components["brain"],
                        heart=components["heart"],
                        settings=settings,
                    )
                    app.state.mcp_manager = _mcp_manager
                await _mcp_manager.handle_request(scope, receive, send)

            app.routes.append(Mount("/mcp", app=mcp_asgi))
            logger.info("MCP server mounted at /mcp")
        except ImportError:
            logger.warning("MCP dependencies not installed, skipping MCP server")

    return app


class _LazyProxy:
    """Proxy that defers attribute access to a dict-backed component.

    Allows create_app() to receive component references before lifespan
    has initialized them. All attribute access is forwarded to the actual
    component once it's available.
    """

    def __init__(self, components: dict, key: str) -> None:
        object.__setattr__(self, "_components", components)
        object.__setattr__(self, "_key", key)

    def _resolve(self):
        components = object.__getattribute__(self, "_components")
        key = object.__getattribute__(self, "_key")
        obj = components.get(key)
        if obj is None:
            raise RuntimeError(f"Component '{key}' not yet initialized — lifespan hasn't started")
        return obj

    def __getattr__(self, name):
        return getattr(self._resolve(), name)

    def __bool__(self):
        # Truthiness reflects whether the component is initialized. Without
        # this, Python falls back to __len__, which raises on components that
        # are not sized (e.g. SessionTimeoutMonitor) — breaking `if proxy`
        # guards such as the one in /events/stats.
        components = object.__getattribute__(self, "_components")
        key = object.__getattribute__(self, "_key")
        return components.get(key) is not None

    def __len__(self):
        return len(self._resolve())


def _lazy_component(components: dict, key: str) -> _LazyProxy:
    """Create a lazy proxy for a component that will be initialized in lifespan."""
    return _LazyProxy(components, key)


def main() -> None:
    """Entry point — parse settings, build app, run server."""
    settings = Settings()

    # Configure logging
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    logger.info("Starting Nous agent: %s (%s)", settings.agent_name, settings.agent_id)
    logger.info("Model: %s", settings.model)
    logger.info("Database: %s:%s/%s", settings.db_host, settings.db_port, settings.db_name)
    logger.info("MCP: %s", "enabled" if settings.mcp_enabled else "disabled")

    # F15: Warn if no Anthropic credentials set
    if not settings.anthropic_api_key and not settings.anthropic_auth_token:
        logger.warning(
            "Neither ANTHROPIC_API_KEY nor ANTHROPIC_AUTH_TOKEN is set — "
            "/chat endpoints will fail"
        )

    if not settings.brave_search_api_key:
        logger.warning("BRAVE_SEARCH_API_KEY not set — web_search will be unavailable")

    app = build_app(settings)

    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
    )


if __name__ == "__main__":
    main()
