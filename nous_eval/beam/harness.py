"""Wiring, ingest and the two answer modes.

Ingest follows the production write path (nous/main.py, nous/cognitive/layer.py):
for each BEAM session, ``start_episode`` as pre_turn does, ``end_episode`` with the
``User:``/``Assistant:`` transcript as end_session does, then a ``session_ended``
event on an event bus carrying the production handlers: EpisodeSummarizer.handle
(summary, F067 chunks, F022/F040 links) emits ``episode_summarized`` for
FactExtractor.handle (candidate facts, admission, F027 supersession), whose
``fact_learned`` events reach FactGraphLinker. The bus is not started; it is
drained after each session, in the order the production loop would dispatch.

OutcomeDetector and CorrectionExtractor (rule facts, censors) run on
``session_ended`` as in prod, each when its flag is on; so do the summarizer and
the extractor. A single-turn session shorter than ``episode_min_content_length``
is soft-deleted as trivial instead of ended, as end_session does.

Clock: each session runs at its BEAM date. Its episode is dated before summarizing
(the summarizer resolves relative dates against ``started_at``), and every fact
written while the session (or the sleep cycle after its batch) is processed gets
that date as ``created_at``/``learned_at`` (and ``mentioned_at`` where the column
exists). arms.EVAL_OVERRIDES turns the sleep stale scan off for every arm; what
still reads the wall clock is listed in arms.CLOCK_NOTES and recorded with the run.
With ``sleep=True`` a full sleep cycle runs after each batch (BEAM batches are days
apart). Sleep reflection reads only episodes started within
``sleep_reflection_hours`` of wall-clock now, so on dated episodes it finds none
unless the arm widens that window. With every memory BEAM-dated, the staleness
penalty and episode recency decay sit at their floors for all of them alike, so
absolute scores compare arm to arm only, not with a run on a prod timeline.

Answer modes, both through BEAM's own answer prompt (scoring.ANSWER_PROMPT):
  preturn      the context is the system prompt ``ContextEngine.build`` returns,
               planned as pre_turn plans it (frame, intent, deictic/recap rescue,
               epistemic class): what the model sees at the start of a prod turn.
  recall_deep  the context is the text the ``recall_deep`` tool returns for the
               question: what the model sees after one memory search.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text as sa_text

from nous.events import Event, EventBus
from nous_eval.beam.corpus import Conversation, Session
from nous_eval.beam.scoring import ANSWER_PROMPT, fill

logger = logging.getLogger(__name__)

# Frames are agent config, not memory: the per-conversation agent selects from the
# seeded agent's frame set, as prod does from its own.
FRAME_AGENT = "nous-default"
SESSION_LENGTH = timedelta(minutes=5)
SLEEP_GAP = timedelta(minutes=1)  # a batch's sleep cycle runs this long after its last session ends
RECALL_DEEP_LIMIT = 10  # the recall_deep tool's default limit


class DrainedBus(EventBus):
    """An EventBus whose queue the harness drains itself instead of a background loop."""

    async def drain(self, *pending: set[asyncio.Task]) -> int:
        """Dispatch queued events (and those they cause) until none are left."""
        n = 0
        while True:
            for tasks in pending:
                if tasks:
                    await asyncio.gather(*list(tasks), return_exceptions=True)
            if self._queue.empty():
                return n
            await self._dispatch(self._queue.get_nowait())
            n += 1


@dataclass
class Stack:
    settings: Any
    db: Any
    heart: Any
    brain: Any
    bus: DrainedBus
    llm: Any
    frames: Any
    classifier: Any
    context: Any
    recall_deep: Any
    epistemic: Any
    sleep_handler: Any
    fact_time_columns: tuple[str, ...] = ("created_at", "learned_at")


async def prepare_db(settings: Any) -> None:
    """Apply pending migrations and check the schema, as the other eval ingests do."""
    from nous.storage.database import Database
    from nous.storage.migrator import run_migrations
    from nous_eval.schema_preflight import assert_eval_db_schema_matches_orm, seed_baked_migration_history

    db = Database(settings)
    await db.connect()
    try:
        await seed_baked_migration_history(db.engine)
        await run_migrations(db.engine)
        await assert_eval_db_schema_matches_orm(db)
    finally:
        await db.disconnect()


@asynccontextmanager
async def open_stack(
    settings: Any, *, llm: Any = None, embeddings: Any = None, sleep: bool = False
) -> AsyncIterator[Stack]:
    """Components wired as nous/main.py wires them; ``llm``/``embeddings`` injectable for tests."""
    from nous.api.anthropic_client import create_client
    from nous.api.tools import create_nous_tools
    from nous.brain.brain import Brain
    from nous.brain.graph_linker import GraphLinker
    from nous.cognitive.context import ContextEngine
    from nous.cognitive.frames import FrameEngine
    from nous.cognitive.intent import IntentClassifier
    from nous.handlers.correction_extractor import CorrectionExtractor
    from nous.handlers.episode_summarizer import EpisodeSummarizer
    from nous.handlers.fact_extractor import FactExtractor
    from nous.handlers.fact_graph_linker import FactGraphLinker
    from nous.handlers.outcome_detector import OutcomeDetector
    from nous.heart.date_window import DateWindowParser
    from nous.heart.heart import Heart
    from nous.storage.database import Database
    from nous_eval.retrieval_runner import make_eval_embedding_provider

    owns_llm = llm is None
    if embeddings is None:
        embeddings = make_eval_embedding_provider(settings)
        if embeddings is None:
            raise SystemExit("OPENAI_API_KEY is required (embeddings)")
    db = Database(settings)
    await db.connect()
    if owns_llm:
        llm = create_client(settings)
        await llm.start()
    try:
        brain = Brain(db, settings, embeddings)
        heart = Heart(db, settings, embeddings, owns_embeddings=False)
        bus = DrainedBus()
        # main.py: F050 query expansion, F055 residual activation, F023 admission
        # LLM, F027 supersession classifier, F075 date-window parser.
        if settings.query_expansion_enabled:
            from nous.heart.query_expansion import QueryExpander

            heart.set_query_expander(
                QueryExpander(llm=llm, settings=settings, db=db, model=settings.query_expansion_model)
            )
        if settings.residual_activation_enabled:
            from nous.heart.residual_activation import ResidualActivator

            heart.set_residual_activator(ResidualActivator(settings=settings, wm=heart.working_memory, db=db))
        if heart.facts._admission_controller is not None:
            from nous.heart.admission import AdmissionLLMClient

            heart.facts._admission_controller.llm_client = AdmissionLLMClient(api_client=llm)
        heart.facts.set_llm_client(llm, model=settings.contradiction_model)
        heart.date_window_parser = DateWindowParser(llm, settings)

        # main.py handler block: the handlers that write memory, each behind its flag as there.
        graph_linker = GraphLinker(db=db, embedder=embeddings, settings=settings, agent_id=settings.agent_id)
        summarizer = None
        if settings.episode_summary_enabled:
            summarizer = EpisodeSummarizer(heart, brain, settings, bus, llm, graph_linker=graph_linker)
            summarizer._embedder = embeddings
        if settings.fact_extraction_enabled:
            FactExtractor(heart, settings, bus, llm)
        if settings.rubric_outcome_detection_enabled:
            OutcomeDetector(db=db, settings=settings, bus=bus, llm_client=llm, agent_id=settings.agent_id)
        if settings.correction_extraction_enabled:
            CorrectionExtractor(
                db=db, settings=settings, bus=bus, llm_client=llm, heart=heart, agent_id=settings.agent_id
            )
        brain._bus = bus
        if settings.cross_type_linking_enabled:
            heart._bus = bus
            FactGraphLinker(graph_linker, settings, bus)
        sleep_handler = None
        if sleep:
            from nous.handlers.sleep_handler import SleepHandler

            sleep_handler = SleepHandler(brain, heart, settings, bus, llm)
            if settings.graph_backfill_enabled:
                from nous.brain.graph_densifier import GraphDensifier

                sleep_handler._graph_densifier = GraphDensifier(
                    db=db, graph_linker=graph_linker, embedder=embeddings, settings=settings, agent_id=settings.agent_id
                )
            if summarizer is not None:
                sleep_handler._episode_summarizer = summarizer

        async with db.session() as s:
            columns = set(
                (
                    await s.execute(
                        sa_text(
                            "SELECT column_name FROM information_schema.columns"
                            " WHERE table_schema = 'heart' AND table_name = 'facts'"
                        )
                    )
                ).scalars()
            )
        fact_time_columns = tuple(c for c in ("created_at", "learned_at", "mentioned_at") if c in columns)

        epistemic = None
        if settings.epistemic_gate_enabled:
            from nous.cognitive.epistemic import EpistemicClassifier

            epistemic = EpistemicClassifier(llm=llm, settings=settings, model=settings.epistemic_gate_model)
        yield Stack(
            settings=settings,
            db=db,
            heart=heart,
            brain=brain,
            bus=bus,
            llm=llm,
            frames=FrameEngine(db, settings),
            classifier=IntentClassifier(settings=settings),
            context=ContextEngine(brain, heart, settings, settings.identity_prompt),
            recall_deep=create_nous_tools(brain, heart, settings)["recall_deep"],
            epistemic=epistemic,
            sleep_handler=sleep_handler,
            fact_time_columns=fact_time_columns,
        )
    finally:
        if owns_llm:
            await llm.close()
        await embeddings.close()
        await db.disconnect()


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------


def render_transcript(session: Session, max_chars: int) -> str:
    """layer.py's transcript: one ``User:``/``Assistant:`` line per message, capped, blank-line joined."""
    label = {"user": "User", "assistant": "Assistant"}
    return "\n\n".join(f"{label.get(m.role, m.role.title())}: {m.content[:max_chars]}" for m in session.messages)


async def count_rows(stack: Stack) -> dict[str, int]:
    async with stack.db.session() as s:
        row = (
            await s.execute(
                sa_text(
                    "SELECT (SELECT count(*) FROM heart.episodes WHERE agent_id = :a) AS episodes,"
                    " (SELECT count(*) FROM heart.facts WHERE agent_id = :a AND active) AS active_facts,"
                    " (SELECT count(*) FROM heart.facts WHERE agent_id = :a) AS facts,"
                    " (SELECT count(*) FROM heart.episode_chunks WHERE agent_id = :a) AS chunks,"
                    " (SELECT count(*) FROM brain.graph_edges WHERE agent_id = :a) AS edges"
                ),
                {"a": stack.settings.agent_id},
            )
        ).one()
    return dict(row._mapping)


async def _set_episode_times(stack: Stack, episode_id: Any, started: Any, ended: Any = None) -> None:
    async with stack.db.session() as s:
        await s.execute(
            sa_text(
                "UPDATE heart.episodes SET started_at = :s, created_at = :s,"
                " ended_at = COALESCE(:e, ended_at) WHERE id = :i"
            ),
            {"s": started, "e": ended, "i": episode_id},
        )
        await s.commit()


async def ensure_agent(stack: Stack) -> None:
    """The agent row (outcome signals reference it), configured like the seeded agent."""
    async with stack.db.session() as s:
        await s.execute(
            sa_text(
                "INSERT INTO nous_system.agents (id, name, description, config)"
                " SELECT :a, :a, 'BEAM eval agent',"
                " COALESCE((SELECT config FROM nous_system.agents WHERE id = :seed), '{}'::jsonb)"
                " ON CONFLICT (id) DO NOTHING"
            ),
            {"a": stack.settings.agent_id, "seed": FRAME_AGENT},
        )
        await s.commit()


async def db_now(stack: Stack) -> datetime:
    async with stack.db.session() as s:
        return (await s.execute(sa_text("SELECT clock_timestamp()"))).scalar_one()


async def date_new_facts(stack: Stack, since: datetime, when: datetime) -> int:
    """Give every fact this agent wrote since ``since`` (wall clock) the simulated time ``when``."""
    sets = ", ".join(f"{c} = :t" for c in stack.fact_time_columns)
    async with stack.db.session() as s:
        result = await s.execute(
            sa_text(f"UPDATE heart.facts SET {sets} WHERE agent_id = :a AND created_at >= :since"),
            {"t": when, "a": stack.settings.agent_id, "since": since},
        )
        await s.commit()
    return result.rowcount or 0


def is_trivial(session: Session, min_chars: int) -> bool:
    """end_session's test: one turn, no tools, combined user+assistant text under the minimum."""
    turns = sum(1 for m in session.messages if m.role == "user")
    return turns <= 1 and sum(len(m.content) for m in session.messages) < min_chars


async def ingest_session(stack: Stack, conv: Conversation, session: Session, since: datetime) -> None:
    from nous.heart.schemas import EpisodeInput

    settings, heart = stack.settings, stack.heart
    session_id = f"beam-{conv.bucket}-{conv.conv_id}-b{session.batch}-s{session.index}"
    first_user = next((m.content for m in session.messages if m.role == "user"), session.messages[0].content)
    frame = await stack.frames.select(FRAME_AGENT, first_user)
    episode = await heart.start_episode(
        EpisodeInput(
            summary=first_user[: settings.episode_seed_summary_chars],
            frame_used=frame.frame_id,
            trigger="user_message",
            session_id=session_id,
        )
    )
    await _set_episode_times(stack, episode.id, session.started_at)
    transcript = render_transcript(session, settings.transcript_message_max_chars)
    ended = session.started_at + SESSION_LENGTH
    if is_trivial(session, settings.episode_min_content_length):
        await heart.deactivate_episode(episode.id)
    else:
        await heart.end_episode(episode.id, outcome="success", transcript=transcript)
        await _set_episode_times(stack, episode.id, session.started_at, ended)
    event = Event(
        type="session_ended",
        agent_id=settings.agent_id,
        session_id=session_id,
        data={
            "session_id": session_id,
            "episode_id": str(episode.id),
            "transcript": transcript,
            "reflection": None,
            "had_reflection": False,
            "facts_extracted": 0,
            "summary": {
                "transcript_length": len(transcript),
                "had_reflection": False,
                "reflection": None,
                "facts_extracted": 0,
            },
        },
    )
    event.trace_id = event.event_id
    await stack.bus.emit(event)
    await stack.bus.drain(heart._pending_emits, stack.brain._pending_emits)
    await date_new_facts(stack, since, ended)


async def run_sleep(stack: Stack, since: datetime, when: datetime) -> None:
    """One full sleep cycle, then whatever it emitted; facts it wrote get the time ``when``."""
    handler = stack.sleep_handler
    event = Event(type="sleep_started", agent_id=stack.settings.agent_id, data={"source": "beam_harness"})
    event.trace_id = event.event_id
    handler._sleeping = True
    handler._interrupted = False
    await handler._run_sleep(event)
    await stack.bus.drain(stack.heart._pending_emits, stack.brain._pending_emits, handler._pending_emits)
    await date_new_facts(stack, since, when)


async def ingest_conversation(stack: Stack, conv: Conversation, *, limit_sessions: int | None = None) -> dict[str, Any]:
    """Every session in order; with a sleep handler, a sleep cycle after each batch."""
    before = await count_rows(stack)
    if before["episodes"]:
        raise SystemExit(
            f"agent {stack.settings.agent_id} already has {before['episodes']} episodes; use a new --agent-tag"
        )
    sessions = conv.sessions[:limit_sessions] if limit_sessions else conv.sessions
    batch_ends = set(conv.batch_ends)
    hours = stack.settings.sleep_reflection_hours
    if (
        stack.sleep_handler is not None
        and sessions
        and datetime.now(UTC) - sessions[0].started_at > timedelta(hours=hours)
    ):
        logger.warning(
            "sleep reflection reads episodes started in the last %dh of wall-clock time; the backdated "
            "BEAM episodes are older, so reflection finds none unless the arm widens NOUS_SLEEP_REFLECTION_HOURS",
            hours,
        )
    await ensure_agent(stack)
    # Every fact written from here on is this ingest's. Facts' created_at is their
    # transaction's start, so ``since`` must be read before the first session opens one.
    since = await db_now(stack)
    sleeps = 0
    t0 = time.monotonic()
    for i, session in enumerate(sessions):
        await ingest_session(stack, conv, session, since)
        if stack.sleep_handler is not None and (i in batch_ends or i == len(sessions) - 1):
            await run_sleep(stack, since, session.started_at + SESSION_LENGTH + SLEEP_GAP)
            sleeps += 1
        if (i + 1) % 10 == 0:
            logger.info("%s: %d/%d sessions ingested", conv.key, i + 1, len(sessions))
    return {
        "sessions": len(sessions),
        "sleep_cycles": sleeps,
        "seconds": round(time.monotonic() - t0, 1),
        "rows": await count_rows(stack),
        "bus": stack.bus.stats.to_dict(),
    }


# ---------------------------------------------------------------------------
# Answer modes
# ---------------------------------------------------------------------------


def _plan_like_pre_turn(stack: Stack, text: str, frame: Any) -> tuple[Any, bool]:
    """layer.py pre_turn step 2b for a first turn: classify, plan, deictic and recap rescues."""
    from nous.cognitive.intent import IntentSignals
    from nous.cognitive.layer import _DEICTIC_RECENCY_FLOOR, _is_recap_query, _should_boost_deictic

    clf, s = stack.classifier, stack.settings
    signals = clf.classify(text, frame)
    plan = clf.plan_retrieval(signals, input_text=text)
    if _should_boost_deictic(True, s.followup_deictic_detection_enabled, text):
        signals.temporal_recency = max(signals.temporal_recency, _DEICTIC_RECENCY_FLOOR)
        signals.is_greeting = False
        plan = clf.plan_retrieval(signals, input_text=text)
    is_recap = _is_recap_query(text)
    temporal_boost = is_recap or signals.temporal_recency > 0.5
    if is_recap and signals.temporal_recency <= 0.5:
        signals = IntentSignals(
            frame_type=signals.frame_type,
            entity_mentions=signals.entity_mentions,
            temporal_recency=0.8,
            memory_type_hints=signals.memory_type_hints,
            is_question=signals.is_question,
            is_greeting=signals.is_greeting and not s.followup_greeting_request_detection_enabled,
            topic_keywords=signals.topic_keywords,
            text=signals.text,
        )
        plan = clf.plan_retrieval(signals, input_text=text)
    return plan, temporal_boost


async def context_preturn(stack: Stack, question: str) -> tuple[str, dict[str, Any]]:
    session_id = f"beam-ask-{uuid.uuid4().hex[:12]}"
    frame = await stack.frames.select(FRAME_AGENT, question)
    plan, temporal_boost = _plan_like_pre_turn(stack, question, frame)
    epistemic_class = await stack.epistemic.classify(question) if stack.epistemic is not None else None
    built = await stack.context.build(
        stack.settings.agent_id,
        session_id,
        question,
        frame,
        conversation_messages=[],
        retrieval_plan=plan,
        temporal_boost=temporal_boost,
        epistemic_class=epistemic_class,
        is_first_turn=True,
        turn_number=1,
    )
    sections: dict[str, int] = {}
    for sec in built.sections:
        sections[sec.label] = sections.get(sec.label, 0) + sec.token_estimate
    return built.system_prompt, {
        "session_id": session_id,
        "frame_id": frame.frame_id,
        "plan_queries": {q.memory_type: q.query_text for q in plan.queries},
        "plan_skip_types": sorted(plan.skip_types),
        "temporal_boost": temporal_boost,
        "epistemic_class": epistemic_class,
        "section_tokens": sections,
        "recalled_ids": {k: [str(i) for i in v] for k, v in built.recalled_ids.items()},
    }


async def context_recall_deep(stack: Stack, question: str) -> tuple[str, dict[str, Any]]:
    session_id = f"beam-ask-{uuid.uuid4().hex[:12]}"
    out = await stack.recall_deep(query=question, limit=RECALL_DEEP_LIMIT, _session_id=session_id, _turn_number=1)
    return out["content"][0]["text"], {"session_id": session_id, "tool_is_error": bool(out.get("is_error"))}


MODES = {"preturn": context_preturn, "recall_deep": context_recall_deep}


async def generate_answer(
    llm: Any, model: str, context: str, question: str, *, max_tokens: int = 16000
) -> tuple[str, str | None]:
    """BEAM's answer prompt for retrieval methods, sent as one user message; returns (text, stop_reason).

    No sampling parameters (current models reject them); thinking runs at the model's default.
    A refused or cut-off answer is kept as given and scored as such, with its stop_reason recorded.
    The text is not stripped: event ordering splits the raw answer on newlines, as upstream does.
    """
    from nous_eval._oat_preamble import call_with_retries, with_oat_preamble

    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "system": with_oat_preamble(""),
        "messages": [
            {
                "role": "user",
                "content": [{"type": "text", "text": fill(ANSWER_PROMPT, {"context": context, "question": question})}],
            }
        ],
    }
    response = await call_with_retries(llm, payload)
    text = "".join(b.get("text", "") for b in response.content if isinstance(b, dict) and b.get("type") == "text")
    return text, getattr(response, "stop_reason", None)
