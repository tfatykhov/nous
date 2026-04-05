"""Nous agent entry point.

Initializes all components and starts the server:
  Settings -> Database -> Brain -> Heart -> CognitiveLayer -> Runner -> App -> Uvicorn

Uses Starlette lifespan to manage component lifecycle on the same
event loop as uvicorn (F2/F3 fix from 3-agent review).
"""

from __future__ import annotations

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

    # F023: Wire admission LLM client using shared api_client
    if heart.facts._admission_controller is not None:
        from nous.heart.admission import AdmissionLLMClient
        heart.facts._admission_controller.llm_client = AdmissionLLMClient(
            api_client=api_client,
        )

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

        try:
            from nous.handlers.episode_summarizer import EpisodeSummarizer

            if settings.episode_summary_enabled:
                EpisodeSummarizer(heart, brain, settings, bus, api_client, graph_linker=graph_linker)
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
                FactGraphLinker(graph_linker, settings, bus)
                logger.debug("F022: FactGraphLinker wired — fact->decision linking enabled")
        except ImportError:
            logger.debug("FactGraphLinker not available yet")

        try:
            from nous.handlers.knowledge_extractor import KnowledgeExtractor

            if settings.compaction_enabled:
                KnowledgeExtractor(heart, settings, bus, api_client)
        except ImportError:
            logger.debug("KnowledgeExtractor not available yet")

        try:
            from nous.handlers.session_monitor import SessionTimeoutMonitor

            session_monitor = SessionTimeoutMonitor(bus, settings, cognitive=cognitive)
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

    # F011: Bootstrap local skills (one-time, only if DB has no skills)
    try:
        from nous.skills.bootstrap import bootstrap_local_skills, reactivate_skills
        await bootstrap_local_skills(settings.workspace_dir, heart)
        await reactivate_skills(heart)
    except Exception:
        logger.debug("Skill bootstrap skipped or failed (non-fatal)")

    # Create tool dispatcher and register all tools
    dispatcher = ToolDispatcher(tool_schema_cache_enabled=settings.tool_schema_cache_enabled)
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

    # F035.4: Context Logger
    context_logger = None
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

        context_logger = ContextLogger(
            db_writer=_write_context_log,
            full_payload_enabled=settings.context_log_full_payload,
            ring_size=settings.context_log_ring_size,
            max_total=settings.context_log_max_total,
        )
        runner.set_context_logger(context_logger)
        logger.info("F035.4: ContextLogger wired (full_payload=%s)", settings.context_log_full_payload)

    # 011.1 + 012.2: Register subtask/schedule tools (after runner for inline execution)
    if settings.subtask_enabled:
        from nous.api.tools import register_subtask_tools
        register_subtask_tools(dispatcher, heart, settings, runner=runner)

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

            if settings.heartbeat_email_enabled and settings.email_user:
                registry.register(EmailCheck(settings))

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
            )

            # Create dedicated API client for heartbeat (isolated connection pool)
            heartbeat_api_client = create_client(settings)
            await heartbeat_api_client.start()
            logger.info("F034: Heartbeat API client created (isolated from main runner)")

            heartbeat_runner = HeartbeatRunner(
                settings=settings, registry=registry, runner=runner,
                brain=brain, heart=heart, bus=bus, http_client=handler_http,
                finding_store=finding_store,
                api_client=heartbeat_api_client,
                dynamic_loader=dynamic_loader,
            )
            await heartbeat_runner.start()
        except ImportError:
            logger.debug("Heartbeat not available yet")

    # F034.5: Register heartbeat check management tools
    if heartbeat_runner and heartbeat_runner.dynamic_loader:
        from nous.api.tools import register_heartbeat_tools
        register_heartbeat_tools(dispatcher, heartbeat_runner.dynamic_loader)

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
        "context_logger": context_logger,
    }


async def shutdown_components(components: dict) -> None:
    """Graceful shutdown in reverse order."""
    logger.info("Shutting down Nous...")

    # F034: Stop heartbeat before other components
    heartbeat_runner = components.get("heartbeat_runner")
    if heartbeat_runner:
        await heartbeat_runner.stop()

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
