"""Agent runner -- executes conversational turns via Anthropic API.

Wires CognitiveLayer.pre_turn() and post_turn() around
Anthropic API calls (via pluggable backend: SDK or httpx).
Manages the tool use loop internally.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import OrderedDict
from collections.abc import AsyncGenerator
from typing import Any, Callable
from uuid import UUID

from nous.api.anthropic_client import (
    AnthropicClient,
    StreamEvent,
    _parse_sse_event,  # noqa: F401 — re-exported for backward compat (tests)
    create_client,
)
from nous.api.cache_optimizer import CacheBreakDetector, _hash as cache_hash
from nous.api.compaction import ConversationCompactor
from nous.api.smart_compress import smart_compress
from nous.api.models import ApiResponse, Conversation, Message  # noqa: F401 — re-exported for backward compat
from nous.brain.brain import Brain
from nous.cognitive.action_gate import ActionGate
from nous.cognitive.claim_verifier import ClaimVerifier, IntentTracker
from nous.cognitive.execution_ledger import ExecutionLedger
from nous.cognitive.layer import CognitiveLayer
from nous.cognitive.schemas import ToolResult, TurnContext, TurnResult
from nous.config import Settings
from nous.heart.heart import Heart
from nous.heart.schemas import FactInput

logger = logging.getLogger(__name__)

MAX_CONVERSATIONS = 100
MAX_HISTORY_MESSAGES = 20

# Re-export StreamEvent for backward compatibility
__all__ = ["AgentRunner", "StreamEvent", "FRAME_TOOLS"]

# Frame types where record_decision is expected
_REQUIRED_DECISION_FRAMES = frozenset({"decision"})
_OPTIONAL_DECISION_FRAMES = frozenset({"task", "debug"})

# Frame-gated tool access (D5)
FRAME_TOOLS: dict[str, list[str]] = {
    "conversation": ["record_decision", "learn_fact", "learn_skill", "recall_deep", "recall_recent", "get_procedure", "create_censor", "bash", "read_file", "write_file", "web_search", "web_fetch", "cache_retrieve", "spawn_task", "schedule_task", "list_tasks", "cancel_task", "run_python", "send_file", "heartbeat_check_create", "heartbeat_check_manage", "dag_create", "dag_manage"],
    "question": ["recall_deep", "recall_recent", "get_procedure", "bash", "read_file", "write_file", "record_decision", "learn_fact", "learn_skill", "create_censor", "web_search", "web_fetch", "cache_retrieve", "list_tasks", "cancel_task", "run_python", "dag_manage"],
    "decision": ["record_decision", "recall_deep", "recall_recent", "get_procedure", "create_censor", "bash", "read_file", "web_search", "web_fetch", "cache_retrieve", "list_tasks", "cancel_task", "dag_manage"],
    "creative": ["learn_fact", "recall_deep", "recall_recent", "get_procedure", "write_file", "web_search", "cache_retrieve"],
    "task": ["*"],  # All tools
    "debug": ["record_decision", "recall_deep", "recall_recent", "get_procedure", "bash", "read_file", "learn_fact", "web_search", "web_fetch", "cache_retrieve", "spawn_task", "schedule_task", "list_tasks", "cancel_task", "run_python", "send_file", "heartbeat_check_create", "heartbeat_check_manage", "dag_create", "dag_manage"],
    "initiation": ["store_identity", "complete_initiation"],
}


class AgentRunner:
    """Runs conversational turns with cognitive layer hooks.

    Uses direct httpx calls to the Anthropic Messages API with
    an internal tool dispatch loop.
    """

    REFLECTION_PROMPT = (
        "Review this conversation. Summarize briefly:\n"
        "1. What was the main task?\n"
        "2. What went well?\n"
        "3. What should be done differently?\n"
        '4. List any new facts learned as "learned: <fact>" lines (one per line).'
    )

    def __init__(
        self,
        cognitive: CognitiveLayer,
        brain: Brain,
        heart: Heart,
        settings: Settings,
    ) -> None:
        self._cognitive = cognitive
        self._brain = brain
        self._heart = heart
        self._settings = settings
        self._conversations: OrderedDict[str, Conversation] = OrderedDict()
        self._api: AnthropicClient | None = None
        self._api_shared: bool = False  # True if client was externally provided
        self._dispatcher: Any | None = None  # ToolDispatcher, set via set_dispatcher()
        # F064.1: DAGStore reference for fire-and-forget activity pings.
        # Late-bound via set_dag_store() because DAGStore is built later in
        # main.py wiring and not every consumer (chat session, tests) needs
        # it. None disables pings — _ping_dag_node_activity short-circuits.
        self._dag_store: Any | None = None

        # Compaction (Spec 008.1)
        self._compactor: ConversationCompactor | None = None
        if settings.tool_pruning_enabled or settings.compaction_enabled:
            self._compactor = ConversationCompactor(
                settings=settings,
                # F059 guard verdicts persist to nous_system.events so
                # log rotation doesn't lose data we need to TP/FP-audit
                # before flipping the destructive fallback flag. Mirrors
                # F026 fire-and-forget pattern.
                event_logger=self._log_compaction_guard,
            )
        self._compaction_locks: dict[str, asyncio.Lock] = {}

        # Per-session lock serializing run_turn / stream_chat against
        # end_conversation. Prevents the race where the idle-close path
        # mutates session state (cognitive.end_session pops _active_episodes
        # at layer.py:1519-1520, runner pops _conversations) while a fresh
        # run_turn coroutine has interleaved during one of cognitive's
        # internal awaits. abort_if was only a point-in-time check; the
        # lock makes the mutation block ACTUALLY exclusive.
        self._session_locks: dict[str, asyncio.Lock] = {}

        # F035.4: Context visibility
        self._context_logger: Any | None = None
        self._current_session_id: str = "unknown"
        self._current_turn_number: int = 0
        self._current_frame_id: str = "unknown"
        self._current_call_type: str = "chat"
        self._last_context_entry_id: str | None = None

        # F026: Execution Integrity
        self._ledgers: dict[str, ExecutionLedger] = {}
        self._pending_corrections: dict[str, list[str]] = {}
        self._claim_verifier: ClaimVerifier | None = (
            ClaimVerifier() if settings.claim_verification_enabled else None
        )
        self._intent_tracker: IntentTracker | None = (
            IntentTracker() if settings.claim_verification_enabled else None
        )
        self._action_gate: ActionGate | None = (
            ActionGate(settings, call_gate_model=self._call_gate_model)
            if settings.action_gating_enabled else None
        )

        # F036: Prompt cache optimization
        self._cache_break_detector: CacheBreakDetector | None = (
            CacheBreakDetector() if settings.cache_break_detection_enabled else None
        )

        # SessionTimeoutMonitor back-reference for synchronous activity
        # refresh at run_turn start. Late-bound in main.py because monitor
        # is created earlier than runner. None disables the touch (e.g.,
        # in unit tests that don't wire the monitor).
        self._session_monitor: Any | None = None

    def _log_f026_decision(
        self, event_type: str, data: dict, session_id: str
    ) -> None:
        """Fire-and-forget persistence of an F026 verdict to nous_system.events.

        Enables retrospective accuracy eval against real prod data
        (otherwise F026 verdicts are session-scoped only). asyncio.create_task
        is used so the gate hot path never blocks on DB I/O.
        """
        if not self._settings.f026_persistence_enabled:
            return
        try:
            asyncio.create_task(
                self._brain.emit_event(event_type, data, session_id=session_id)
            )
        except Exception:  # noqa: BLE001
            # Persistence is best-effort — never let it break a turn.
            logger.debug("F026 persistence failed (suppressed)", exc_info=True)

    def _log_compaction_guard(
        self, event_type: str, data: dict, session_id: str
    ) -> None:
        """Fire-and-forget persistence of an F059 guard verdict.

        Same fire-and-forget shape as `_log_f026_decision` — never
        blocks the compaction path on DB I/O.
        """
        try:
            asyncio.create_task(
                self._brain.emit_event(event_type, data, session_id=session_id)
            )
        except Exception:  # noqa: BLE001
            logger.debug("F059 guard persistence failed (suppressed)", exc_info=True)

    async def _call_gate_model(self, prompt: str) -> str:
        """F026: Call a Haiku-class model for Tier 3 action gating."""
        if not self._api:
            return '{"approved": true, "reason": "api-not-initialized"}'
        resp = await self._call_api(
            system_prompt="You are a safety gate. Respond only with JSON.",
            messages=[{"role": "user", "content": prompt}],
            tools=None,
            skip_thinking=True,
            model_override=self._settings.action_gating_model,
        )
        return self._extract_text(resp.content)

    def _get_session_lock(self, session_id: str) -> asyncio.Lock:
        """Per-session lock for serializing run_turn / stream_chat against
        end_conversation. Created on first use; popped on successful close.
        """
        lock = self._session_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[session_id] = lock
        return lock

    def set_dispatcher(self, dispatcher: Any) -> None:
        """Set the tool dispatcher for tool loop execution."""
        self._dispatcher = dispatcher

    def set_dag_store(self, dag_store: Any) -> None:
        """F064.1: late-bind the DAGStore for activity-ping writes.

        Wired in main.py after DAGStore is constructed. None-safe — the
        ping helper short-circuits when this isn't set, so chat sessions
        and tests without DAG infra continue to work unchanged.
        """
        self._dag_store = dag_store

    def set_api_client(self, client: AnthropicClient) -> None:
        """Set a pre-initialized API client (shared with handlers)."""
        self._api = client
        self._api_shared = True

    def set_context_logger(self, ctx_logger: Any) -> None:
        """F035.4: Set context logger."""
        self._context_logger = ctx_logger

    def _get_context_window_size(self) -> int:
        """Get configured or auto-detected context window size."""
        return self._settings._get_context_window(self._settings.model)

    async def start(self) -> None:
        """Initialize the API client based on configured backend."""
        if self._api is None:
            self._api = create_client(self._settings)
            await self._api.start()

    def fork(self, api_client: "AnthropicClient") -> "AgentRunner":
        """Create a sibling runner sharing cognitive layer and dispatcher.

        The forked runner uses its own API client (isolated connection pool)
        but shares the cognitive layer and tool dispatcher with the parent.
        Useful for background tasks like heartbeat that shouldn't contend
        with the main runner's httpx pool.

        The caller owns the api_client lifecycle — fork() does not close it.
        """
        forked = AgentRunner(self._cognitive, self._brain, self._heart, self._settings)
        forked._api = api_client
        forked._api_shared = True  # caller owns lifecycle
        if self._dispatcher is not None:
            forked._dispatcher = self._dispatcher
        # Share execution ledgers so heartbeat/subtask sessions appear in dashboard
        forked._ledgers = self._ledgers
        # F035.4: Context logger NOT propagated to forks — heartbeat triage
        # uses a dedicated API client on a separate connection pool, and the
        # context logger's async DB writer can contend with triage DB sessions.
        # Heartbeat calls are visible via event bus stats instead.
        return forked

    async def close(self) -> None:
        """Clean up API client (skip if shared — caller manages lifecycle)."""
        if self._api and not self._api_shared:
            await self._api.close()
        self._api = None

    async def run_turn(
        self,
        session_id: str,
        user_message: str,
        agent_id: str | None = None,
        user_id: str | None = None,
        user_display_name: str | None = None,
        platform: str | None = None,
        system_prompt_prefix: str | None = None,
        skip_episode: bool = False,
        is_subtask: bool = False,
        max_tool_calls: int | None = None,
        model_override: str | None = None,
        tool_filter: list[str] | None = None,  # F034.5: restrict available tools
        is_background: bool = False,
        # F061: per-call tool injection + forced terminal tool. Used only by
        # the hardened subtask executor; chat sessions leave both at default.
        # Routed straight through to _tool_loop without affecting the global
        # ToolDispatcher (so no leak risk on crash).
        extra_tools: dict[str, tuple[dict, Any]] | None = None,
        force_tool_on_penultimate: str | None = None,
        # F064.1: when the caller is the DAG-managed subtask worker, this
        # carries the DAGNode.id so _tool_loop can fire activity pings to
        # `dag_nodes.last_activity_at`. None on chat / non-DAG paths — ping
        # site short-circuits.
        dag_node_id: UUID | None = None,
    ) -> tuple[str, TurnContext, dict[str, int]]:
        """Execute a single conversational turn.

        Steps:
        1. Get or create Conversation for session_id
        2. Call cognitive.pre_turn() -> TurnContext
        3. Append user message to conversation history
        4. Build system prompt (cognitive context + frame instructions)
        5. Run tool loop: call API, dispatch tools, repeat until done
        6. Extract response text from final API response
        7. Append assistant message to conversation history
        8. Call cognitive.post_turn() with TurnResult
        9. Safety net: check if decision frame but no record_decision called
        10. Return (response_text, turn_context)
        """
        _agent_id = agent_id or self._settings.agent_id

        # Refresh session activity synchronously BEFORE any long-running
        # work. The event-bus path (turn_completed) only fires after the
        # entire LLM+tool loop completes — a multi-minute turn for a
        # session whose _last_activity was already past threshold would
        # otherwise be closed mid-stream by a monitor tick. The bus is
        # async-queued so emitting message_received here would leave a
        # residual race; the synchronous touch eliminates it.
        if self._session_monitor is not None:
            try:
                self._session_monitor.touch(session_id, _agent_id)
            except Exception:
                logger.debug("session_monitor.touch failed (suppressed)", exc_info=True)

        # Per-session lock: serializes the entire turn body against
        # end_conversation. Without this, end_conversation could pop
        # _conversations[sid] during one of cognitive.end_session's
        # internal awaits while a fresh run_turn coroutine has interleaved
        # and is mid-turn — the new turn would be orphaned. The lock is
        # released automatically on every exit path (return, raise).
        async with self._get_session_lock(session_id):
            conversation = await self._get_or_create_conversation(session_id)

            # 2. Pre-turn (F4: plumb conversation_messages for dedup)
            # Filter to user messages first, then take last 8 (D7: window = user turns)
            recent_messages = [
                m.content for m in conversation.messages if m.role == "user"
            ][-8:]
            turn_context = await self._cognitive.pre_turn(
                _agent_id,
                session_id,
                user_message,
                conversation_messages=recent_messages or None,
                user_id=user_id,
                user_display_name=user_display_name,
                skip_episode=skip_episode,
                is_subtask=is_subtask,
            )

            # 3. Append user message
            conversation.messages.append(Message(role="user", content=user_message))
            usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}

            # 3b. Censor block check — skip LLM if input was blocked
            if turn_context.censor_blocked:
                response_text = turn_context.censor_block_reason or "I can't process that request."
                conversation.messages.append(Message(role="assistant", content=response_text))
                turn_result = TurnResult(response_text=response_text)
                await self._cognitive.post_turn(_agent_id, session_id, turn_result, turn_context)
                return response_text, turn_context, usage

            # F026: Get/create execution ledger and set turn
            ledger = self._get_or_create_ledger(session_id)
            ledger.set_turn((len(conversation.messages) + 1) // 2)

            # F035.4: Store current context for context logger
            self._current_session_id = session_id
            self._current_turn_number = (len(conversation.messages) + 1) // 2
            self._current_frame_id = turn_context.frame.frame_id if turn_context.frame else "unknown"
            self._current_call_type = "subtask" if is_subtask else "chat"

            # 4-6. Build system prompt and run tool loop
            response_text = ""
            tool_results: list[ToolResult] = []
            error = None
            try:
                corrections = self._pending_corrections.pop(session_id, None)
                system_prompt = self._build_system_prompt(
                    turn_context, platform=platform,
                    ledger=ledger, corrections=corrections,
                )
                # F036: Handle system_prompt_prefix for both str and dict paths
                if system_prompt_prefix:
                    if isinstance(system_prompt, dict):
                        # Prefix is stable across session — prepend to static tier
                        existing = system_prompt.get("static", "")
                        system_prompt["static"] = (
                            system_prompt_prefix + "\n\n" + existing if existing
                            else system_prompt_prefix
                        )
                    else:
                        system_prompt = system_prompt_prefix + "\n\n" + system_prompt

                # Layer 2: History compaction (Spec 008.1)
                messages = self._format_messages(conversation)
                # F036: Compactor needs flat string for token estimation
                _flat_prompt = (
                    "\n\n".join(v for v in system_prompt.values() if v)
                    if isinstance(system_prompt, dict) else system_prompt
                )
                if self._compactor and self._settings.compaction_enabled:
                    system_tokens = self._compactor.estimator.estimate(_flat_prompt)
                    history_tokens = self._compactor.estimator.estimate_messages(messages)
                    if self._compactor.should_compact(system_tokens, history_tokens):
                        lock = self._compaction_locks.setdefault(session_id, asyncio.Lock())
                        async with lock:
                            messages = self._format_messages(conversation)
                            history_tokens = self._compactor.estimator.estimate_messages(messages)
                            if self._compactor.should_compact(system_tokens, history_tokens):
                                cut_point = self._compactor.find_cut_point(
                                    messages, self._settings.effective_keep_recent
                                )
                                if cut_point > 0:
                                    snapshot = messages[:cut_point]
                                    await self._cognitive.pre_compaction(
                                        agent_id=_agent_id,
                                        session_id=session_id,
                                        message_snapshot=snapshot,
                                    )
                                    await self._compactor.compact(
                                        conversation, messages,
                                        call_api=self._call_api,
                                        cut_point=cut_point,
                                    )
                                    await self._save_conversation(
                                        _agent_id, session_id, conversation
                                    )
                else:
                    system_tokens = len(_flat_prompt) // 4
                    history_tokens = sum(
                        len(m.get("content", "")) // 4 for m in messages
                    )

                logger.info(
                    "Context health: messages=%d, system_tokens~=%d, "
                    "history_tokens~=%d, frame=%s",
                    len(messages), system_tokens, history_tokens,
                    turn_context.frame.frame_id if turn_context else "unknown",
                )

                response_text, tool_results, usage, thinking_blocks = await self._tool_loop(
                    system_prompt=system_prompt,
                    conversation=conversation,
                    frame_id=turn_context.frame.frame_id,
                    session_id=session_id,
                    is_subtask=is_subtask,
                    max_tool_calls=max_tool_calls,
                    model_override=model_override,
                    user_message=user_message,
                    ledger=ledger,
                    tool_filter=tool_filter,
                    is_background=is_background,
                    extra_tools=extra_tools,
                    force_tool_on_penultimate=force_tool_on_penultimate,
                    dag_node_id=dag_node_id,
                )
                conversation.messages.append(Message(role="assistant", content=response_text))
            except Exception as e:
                logger.error("API call error: %s", e)
                error = str(e)
                thinking_blocks = []
                response_text = "I encountered an error processing your request. Please try again."
                conversation.messages.append(Message(role="assistant", content=response_text))
                _caught_exc = e
            else:
                _caught_exc = None

            # F026: Post-response claim verification + ghost planning detection
            if _caught_exc is None:
                self._verify_claims(session_id, response_text, tool_results, ledger)

            # 7. Post-turn (always called, even on error)
            turn_result = TurnResult(
                response_text=response_text,
                tool_results=tool_results,
                error=error,
                thinking_blocks=thinking_blocks,
            )
            await self._cognitive.post_turn(_agent_id, session_id, turn_result, turn_context)

            # 8. Safety net: warn if decision frame but record_decision not called
            self._check_safety_net(turn_context, tool_results)

            # Store context
            conversation.turn_contexts.append(turn_context)

            # Re-raise after cleanup so callers (e.g. subtask worker) see the real error
            if _caught_exc is not None:
                raise _caught_exc

            return response_text, turn_context, usage

    async def end_conversation(
        self,
        session_id: str,
        agent_id: str | None = None,
        *,
        abort_if: Callable[[], bool] | None = None,
    ) -> bool:
        """End a conversation with reflection.

        1. If conversation has >= 3 turns, generate reflection via LLM
        2. (Optional) Recheck ``abort_if`` — bail if activity resumed
        3. Call cognitive.end_session(agent_id, session_id, reflection=...)
        4. Remove from self._conversations

        ``abort_if`` is a no-arg callable that returns True if the close
        should be aborted (e.g., the user resumed activity while reflection
        was running). Checked AFTER reflection (read-only, safe to discard)
        but BEFORE any state mutation. Used by SessionTimeoutMonitor to
        prevent orphaning an in-flight turn that landed mid-close.

        Returns True if the conversation was closed, False if aborted.
        Manual /new path passes ``abort_if=None`` and always returns True.
        """
        _agent_id = agent_id or self._settings.agent_id
        conversation = self._conversations.get(session_id)

        reflection: str | None = None
        if conversation and len(conversation.messages) >= 6:  # 3 user + 3 assistant = 6
            try:
                # Build a reflection prompt with conversation history
                history_text = self._format_history_text(conversation)
                reflection_prompt = (
                    f"Here is a conversation to review:\n\n{history_text}\n\n"
                    f"{self.REFLECTION_PROMPT}"
                )
                # P1-9: Call _call_api directly, no tool loop needed for reflection.
                # skip_thinking=True: reflection is a simple summary, no need for
                # extended thinking budget.
                api_response = await self._call_api(
                    system_prompt="You are reviewing a conversation to extract lessons learned.",
                    messages=[{"role": "user", "content": reflection_prompt}],
                    tools=None,
                    skip_thinking=True,
                )
                # Extract text from response
                reflection = self._extract_text(api_response.content)
            except Exception as e:
                logger.warning("Failed to generate reflection: %s", e)

        # Acquire the per-session lock BEFORE the abort_if recheck and the
        # state-mutating cognitive.end_session call. Holding the lock here
        # makes the entire mutation block exclusive with run_turn/stream_chat:
        # any in-flight turn must release the lock before we can enter, and
        # any new turn arriving during our cognitive.end_session awaits will
        # block at the lock rather than racing through and orphaning state.
        # Reflection above runs WITHOUT the lock because it's read-only
        # (just generates a summary string) and slow — holding the lock
        # during a 15-30s LLM call would needlessly block users.
        async with self._get_session_lock(session_id):
            # Pre-mutation recheck: if abort_if fires here (activity resumed
            # during reflection or while we waited for the lock), bail
            # before touching any state. Reflection is best-effort and
            # discarding it is harmless.
            if abort_if is not None:
                try:
                    if abort_if():
                        logger.info(
                            "Aborting end_conversation for %s — activity resumed during close",
                            session_id,
                        )
                        return False
                except Exception:
                    logger.debug("abort_if check raised (suppressed)", exc_info=True)

            await self._cognitive.end_session(_agent_id, session_id, reflection=reflection)

            # Remove conversation + persisted state (008.1 Phase 3)
            self._conversations.pop(session_id, None)
            self._compaction_locks.pop(session_id, None)
            ledger = self._ledgers.pop(session_id, None)  # F026
            if ledger and ledger.actions:
                blocked = sum(1 for a in ledger.actions if a.status == "blocked")
                logger.info(
                    "F026: Session %s ended — %d actions recorded, %d blocked",
                    session_id, len(ledger.actions), blocked,
                )
            self._pending_corrections.pop(session_id, None)  # F026
            if self._cache_break_detector:  # F036
                self._cache_break_detector.reset()
            await self._delete_conversation_state(session_id)

        # Lock released — safe to pop the lock entry itself. Aborted closes
        # (early return False above) intentionally retain the lock entry so
        # the resumed run_turn keeps using the same mutex object.
        self._session_locks.pop(session_id, None)
        return True

    # ------------------------------------------------------------------
    # API call
    # ------------------------------------------------------------------

    def _build_api_payload(
        self,
        system_prompt: str | dict[str, str],
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        skip_thinking: bool = False,
        model_override: str | None = None,
        tool_choice: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build Anthropic Messages API request payload.

        Shared by _call_api and _call_api_stream to avoid divergence.
        skip_thinking=True omits thinking params (for utility calls like reflection).
        model_override replaces the default model (used by compaction summarization).

        F036: system_prompt may be a dict[str, str] (tier -> text) when
        cache_split_system_prompt is enabled. Builds 3 system blocks with
        optimized cache_control placement.
        """
        # Add cache_control to last user message for prompt caching
        cached_messages = list(messages)
        for i in range(len(cached_messages) - 1, -1, -1):
            if cached_messages[i].get("role") == "user":
                msg = cached_messages[i]
                content = msg.get("content")
                if isinstance(content, str):
                    cached_messages[i] = {
                        **msg,
                        "content": [
                            {
                                "type": "text",
                                "text": content,
                                "cache_control": {"type": "ephemeral"},
                            }
                        ],
                    }
                elif isinstance(content, list) and content:
                    last_block = {**content[-1], "cache_control": {"type": "ephemeral"}}
                    cached_messages[i] = {
                        **msg,
                        "content": content[:-1] + [last_block],
                    }
                break

        # F036: Build system blocks based on tier split or legacy
        if isinstance(system_prompt, dict):
            # 3-tier split: static, semi_stable, dynamic
            static_text = system_prompt.get("static", "")
            semi_stable_text = system_prompt.get("semi_stable", "")
            dynamic_text = system_prompt.get("dynamic", "")
            flat_system_prompt = "\n\n".join(
                t for t in [static_text, semi_stable_text, dynamic_text] if t
            )

            system_blocks: list[dict[str, Any]] = []

            # Block 0: Claude Code preamble — required for claude-code beta rate limits
            system_blocks.append({
                "type": "text",
                "text": "You are Claude Code, Anthropic's official CLI for Claude.",
                "cache_control": {"type": "ephemeral"},
            })

            # Block 1: Static identity — always cached
            if static_text:
                system_blocks.append({
                    "type": "text",
                    "text": static_text,
                    "cache_control": {"type": "ephemeral"},
                })

            # Block 1: Semi-stable context — cached with single breakpoint strategy
            if semi_stable_text:
                block1: dict[str, Any] = {"type": "text", "text": semi_stable_text}
                if self._settings.cache_single_breakpoint:
                    # Only add cache_control if semi-stable hasn't changed
                    # (cache break detector will report if it did)
                    prev_hash = (
                        self._cache_break_detector.last_semi_stable_hash()
                        if self._cache_break_detector else None
                    )
                    if prev_hash is not None:
                        if cache_hash(semi_stable_text) == prev_hash:
                            block1["cache_control"] = {"type": "ephemeral"}
                    else:
                        # First call or no detector — cache it
                        block1["cache_control"] = {"type": "ephemeral"}
                else:
                    block1["cache_control"] = {"type": "ephemeral"}
                system_blocks.append(block1)

            # Block 2: Dynamic context — never cached (changes every turn)
            if dynamic_text:
                system_blocks.append({"type": "text", "text": dynamic_text})

            # F036: Run cache break detection
            if self._cache_break_detector:
                tools_json = json.dumps(tools) if tools else ""
                model_str = model_override or self._settings.model
                cache_break = self._cache_break_detector.check(
                    static_text=static_text,
                    semi_stable_text=semi_stable_text,
                    dynamic_text=dynamic_text,
                    tools_json=tools_json,
                    model=model_str,
                )
            else:
                cache_break = None
        else:
            # Legacy 2-block path (no tier split)
            flat_system_prompt = system_prompt
            system_blocks = [
                {
                    "type": "text",
                    "text": "You are Claude Code, Anthropic's official CLI for Claude.",
                    "cache_control": {"type": "ephemeral"},
                },
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                },
            ]
            cache_break = None

        payload: dict[str, Any] = {
            "model": model_override or self._settings.model,
            "max_tokens": self._settings.max_tokens,
            "system": system_blocks,
            "messages": cached_messages,
        }
        if tools:
            payload["tools"] = tools
        if tool_choice is not None:
            # F061: forced tool_choice on penultimate subtask turn. Anthropic
            # docs require tool_choice in {"auto","none"} when extended thinking
            # is on; the caller (_tool_loop) is responsible for not setting
            # this when thinking is enabled for the effective model.
            payload["tool_choice"] = tool_choice
        if stream:
            payload["stream"] = True

        # Resolve effective model once for capability guards below
        effective_model = payload["model"]

        # Extended thinking (skip for utility calls like reflection)
        # Haiku 4.5 does NOT support thinking — only Sonnet 4.6+ and Opus 4.6+.
        if not skip_thinking and "haiku" not in effective_model:
            if self._settings.thinking_mode == "adaptive":
                payload["thinking"] = {"type": "adaptive"}
            elif self._settings.thinking_mode == "manual":
                payload["thinking"] = {
                    "type": "enabled",
                    "budget_tokens": self._settings.thinking_budget,
                }

        # Effort parameter ("high" is API default, so only send if different)
        # Supported by Sonnet 4.6 and Opus 4.6. Haiku 4.5 does NOT support it.
        if (
            self._settings.effort != "high"
            and "haiku" not in effective_model
        ):
            payload["output_config"] = {"effort": self._settings.effort}

        # F035.4: Log context metadata (entry_id stored locally, NOT in payload)
        if self._context_logger:
            try:
                _entry = self._context_logger.log(
                    session_id=self._current_session_id,
                    turn_number=self._current_turn_number,
                    call_type=self._current_call_type,
                    model=payload.get("model", ""),
                    system_prompt=flat_system_prompt,
                    messages=messages,
                    tools=tools,
                    frame_id=self._current_frame_id,
                    context_window=self._get_context_window_size(),
                    payload=payload if self._settings.context_log_full_payload else None,
                )
                self._last_context_entry_id = _entry.id
                # F036: Attach cache break info to context log entry
                if cache_break:
                    _entry.cache_break = True
                    _entry.cache_break_components = cache_break.components_changed
                    _entry.cache_break_tokens_lost = cache_break.estimated_tokens_lost
            except Exception:
                logger.debug("F035.4: context log failed", exc_info=True)

        return payload

    async def _call_api(
        self,
        system_prompt: str | dict[str, str],
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        skip_thinking: bool = False,
        model_override: str | None = None,
        is_background: bool = False,
        tool_choice: dict[str, Any] | None = None,
    ) -> ApiResponse:
        """Call Anthropic Messages API via configured backend.

        Delegates to self._api.call() which handles retries and error mapping.
        Returns parsed ApiResponse with content blocks and stop_reason.
        F036: system_prompt may be str or dict[str, str] (tier split).
        F048: when is_background=True and feature flag enabled, routes through
        call_streaming_aggregated() to avoid idle-connection drops on long runs.
        F061: tool_choice (None default) lets the subtask executor force a
        terminal tool call on the penultimate turn.
        """
        if not self._api:
            raise RuntimeError("API client not initialized -- call start() first")

        payload = self._build_api_payload(
            system_prompt, messages, tools,
            skip_thinking=skip_thinking, model_override=model_override,
            tool_choice=tool_choice,
        )
        if is_background:
            if self._settings.api_background_streaming_enabled:
                return await self._api.call_streaming_aggregated(payload)
            logger.warning(
                "F048: is_background=True but NOUS_API_BACKGROUND_STREAMING_ENABLED=false; "
                "falling back to non-streaming call() — idle-connection drops may recur"
            )
        return await self._api.call(payload)

    async def _call_api_stream(
        self,
        system_prompt: str | dict[str, str],
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Call Anthropic API with streaming via configured backend.

        Yields StreamEvent objects from the underlying client.
        """
        if not self._api:
            raise RuntimeError("API client not initialized -- call start() first")

        payload = self._build_api_payload(system_prompt, messages, tools, stream=True)
        async for event in self._api.stream(payload):
            yield event

    # ------------------------------------------------------------------
    # Streaming chat
    # ------------------------------------------------------------------

    async def stream_chat(
        self,
        session_id: str,
        user_message: str,
        agent_id: str | None = None,
        user_id: str | None = None,
        user_display_name: str | None = None,
        platform: str | None = None,
        system_prompt_prefix: str | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Full chat turn with streaming, including tool loops.

        Mirrors run_turn() flow but yields StreamEvents as they arrive.
        Tool calls execute between stream segments.
        """
        if not self._dispatcher:
            raise RuntimeError("No tool dispatcher set -- call set_dispatcher() first")

        _agent_id = agent_id or self._settings.agent_id

        # Sync activity refresh before any long-running work. See run_turn
        # for rationale — the bus is queued, so message_received emission
        # would leave a residual race against the monitor tick.
        if self._session_monitor is not None:
            try:
                self._session_monitor.touch(session_id, _agent_id)
            except Exception:
                logger.debug("session_monitor.touch failed (suppressed)", exc_info=True)

        # Per-session lock: serializes the entire streaming turn against
        # end_conversation. Held across all yields so end_conversation cannot
        # pop _conversations[sid] mid-stream while a tool result is being
        # awaited. The async generator keeps the lock until exhausted, so
        # the consumer driving the for-loop also extends the protection.
        async with self._get_session_lock(session_id):
            conversation = await self._get_or_create_conversation(session_id)

            # Pre-turn with conversation dedup (F4)
            recent_messages = [
                m.content for m in conversation.messages if m.role == "user"
            ][-8:]
            turn_context = await self._cognitive.pre_turn(
                _agent_id,
                session_id,
                user_message,
                conversation_messages=recent_messages or None,
                user_id=user_id,
                user_display_name=user_display_name,
            )

            conversation.messages.append(Message(role="user", content=user_message))

            # Censor block check — yield block message and return
            if turn_context.censor_blocked:
                block_msg = turn_context.censor_block_reason or "I can't process that request."
                conversation.messages.append(Message(role="assistant", content=block_msg))
                turn_result = TurnResult(response_text=block_msg)
                await self._cognitive.post_turn(_agent_id, session_id, turn_result, turn_context)
                yield StreamEvent(type="text", text=block_msg)
                yield StreamEvent(type="done")
                return

            # F026: Get/create execution ledger and set turn
            ledger = self._get_or_create_ledger(session_id)
            ledger.set_turn((len(conversation.messages) + 1) // 2)

            # F035.4: Store current context for context logger
            self._current_session_id = session_id
            self._current_turn_number = (len(conversation.messages) + 1) // 2
            self._current_frame_id = turn_context.frame.frame_id if turn_context.frame else "unknown"
            self._current_call_type = "chat"

            corrections = self._pending_corrections.pop(session_id, None)
            system_prompt = self._build_system_prompt(
                turn_context, platform=platform,
                ledger=ledger, corrections=corrections,
            )
            # F036: Handle system_prompt_prefix for both str and dict paths
            if system_prompt_prefix:
                if isinstance(system_prompt, dict):
                    existing = system_prompt.get("static", "")
                    system_prompt["static"] = (
                        system_prompt_prefix + "\n\n" + existing if existing
                        else system_prompt_prefix
                    )
                else:
                    system_prompt = system_prompt_prefix + "\n\n" + system_prompt
            tools = self._dispatcher.available_tools(turn_context.frame.frame_id)
            messages = self._format_messages(conversation)

            # F036: Compactor needs flat string for token estimation
            _flat_prompt = (
                "\n\n".join(v for v in system_prompt.values() if v)
                if isinstance(system_prompt, dict) else system_prompt
            )

            # Layer 2: History compaction (Spec 008.1)
            if self._compactor and self._settings.compaction_enabled:
                system_tokens = self._compactor.estimator.estimate(_flat_prompt)
                history_tokens = self._compactor.estimator.estimate_messages(messages)
                if self._compactor.should_compact(system_tokens, history_tokens):
                    lock = self._compaction_locks.setdefault(session_id, asyncio.Lock())
                    async with lock:
                        # Re-check under lock (double-check pattern)
                        messages = self._format_messages(conversation)
                        history_tokens = self._compactor.estimator.estimate_messages(messages)
                        if self._compactor.should_compact(system_tokens, history_tokens):
                            cut_point = self._compactor.find_cut_point(
                                messages, self._settings.effective_keep_recent
                            )
                            if cut_point > 0:
                                # 008.1 Phase 3: Snapshot for event handlers (decoupled from mutation)
                                snapshot = messages[:cut_point]
                                await self._cognitive.pre_compaction(
                                    agent_id=_agent_id,
                                    session_id=session_id,
                                    message_snapshot=snapshot,
                                )
                                await self._compactor.compact(
                                    conversation, messages,
                                    call_api=self._call_api,
                                    cut_point=cut_point,
                                )
                                # 008.1 Phase 3: Persist state after compaction
                                await self._save_conversation(
                                    _agent_id, session_id, conversation
                                )
                                messages = self._format_messages(conversation)
            else:
                system_tokens = len(_flat_prompt) // 4
                history_tokens = sum(
                    len(m.get("content", "")) // 4 for m in messages
                )

            logger.info(
                "Context health: messages=%d, system_tokens~=%d, "
                "history_tokens~=%d, frame=%s",
                len(messages), system_tokens, history_tokens,
                turn_context.frame.frame_id if turn_context else "unknown",
            )

            all_tool_results: list[ToolResult] = []
            all_thinking_blocks: list[str] = []  # Accumulated across all tool loop iterations
            total_usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}
            response_text = ""
            error = None

            try:
                for turn in range(self._settings.max_turns):
                    # Unified block accumulator: keyed by block_index, preserves
                    # original order for thinking block preservation (007 Phase D).
                    all_blocks: dict[int, dict[str, Any]] = {}
                    text_parts: list[str] = []
                    tool_calls: list[dict[str, Any]] = []
                    stop_reason = ""
                    _segment_usage: dict[str, Any] = {}  # F036.1: per-segment usage

                    # Emit keepalives while waiting for Anthropic's first byte —
                    # large contexts + thinking can cause long waits (008.1)
                    async for event in self._stream_with_keepalive(
                        system_prompt=system_prompt,
                        messages=messages,
                        tools=tools if tools else None,
                    ):
                        if event.type == "error":
                            error = event.text
                            yield event
                            return

                        elif event.type == "message_start":
                            if event.usage:
                                total_usage["input_tokens"] += event.usage.get("input_tokens", 0)
                            # F036.1: Capture per-segment usage for context logger
                            _segment_usage = dict(event.usage) if event.usage else {}

                        # -- Thinking blocks (yielded to client for thinking indicators) --
                        elif event.type == "thinking_start":
                            all_blocks[event.block_index] = {
                                "type": "thinking",
                                "thinking_parts": [],
                                "signature": "",
                            }
                            yield event

                        elif event.type == "redacted_thinking":
                            # Complete block — data arrives in start event
                            all_blocks[event.block_index] = {
                                "type": "redacted_thinking",
                                "data": event.text,
                            }
                            yield event

                        elif event.type == "thinking_delta":
                            block = all_blocks.get(event.block_index)
                            if block and block["type"] == "thinking":
                                block["thinking_parts"].append(event.text)
                            yield event

                        elif event.type == "signature_delta":
                            block = all_blocks.get(event.block_index)
                            if block and block["type"] == "thinking":
                                block["signature"] = event.text

                        # -- Text blocks --
                        elif event.type == "text_block_start":
                            all_blocks[event.block_index] = {
                                "type": "text",
                                "text_parts": [],
                            }

                        elif event.type == "text_delta":
                            text_parts.append(event.text)
                            # Also track in block for content reconstruction
                            for idx in sorted(all_blocks, reverse=True):
                                if all_blocks[idx]["type"] == "text":
                                    all_blocks[idx]["text_parts"].append(event.text)
                                    break
                            yield event

                        # -- Tool blocks --
                        elif event.type == "tool_start":
                            all_blocks[event.block_index] = {
                                "type": "tool_use",
                                "id": event.tool_id,
                                "name": event.tool_name,
                                "input_parts": [],
                            }
                            yield event

                        elif event.type == "tool_input_delta":
                            block = all_blocks.get(event.block_index)
                            if block and block["type"] == "tool_use":
                                block["input_parts"].append(event.text)

                        elif event.type == "block_stop":
                            block = all_blocks.get(event.block_index)
                            if block and block["type"] == "tool_use":
                                input_json = "".join(block["input_parts"])
                                try:
                                    block["input"] = json.loads(input_json) if input_json else {}
                                except json.JSONDecodeError:
                                    block["input"] = {}
                                tool_calls.append(block)

                        elif event.type == "done":
                            stop_reason = event.stop_reason
                            if event.usage:
                                total_usage["input_tokens"] += event.usage.get("input_tokens", 0)
                                total_usage["output_tokens"] += event.usage.get("output_tokens", 0)
                                # F036.1: Merge output_tokens into segment usage
                                _segment_usage["output_tokens"] = event.usage.get("output_tokens", 0)

                    # F036.1: Update context log with per-segment usage (not cumulative)
                    if self._context_logger and self._last_context_entry_id:
                        self._context_logger.update_response(
                            entry_id=self._last_context_entry_id,
                            input_tokens=_segment_usage.get("input_tokens"),
                            output_tokens=_segment_usage.get("output_tokens"),
                            cache_creation=_segment_usage.get("cache_creation_input_tokens"),
                            cache_read=_segment_usage.get("cache_read_input_tokens"),
                            stop_reason=stop_reason or None,
                        )
                        self._last_context_entry_id = None  # Consumed

                    # Stream segment ended -- collect thinking blocks from this iteration
                    for idx in sorted(all_blocks):
                        block = all_blocks[idx]
                        if block["type"] == "thinking":
                            thinking_text = "".join(block["thinking_parts"]).strip()
                            if thinking_text:
                                all_thinking_blocks.append(thinking_text)

                    if stop_reason == "end_turn" or not tool_calls:
                        response_text = "".join(text_parts)
                        break

                    # Build assistant message preserving ALL blocks in index order
                    # (critical for thinking block preservation with interleaved thinking)
                    content_blocks: list[dict[str, Any]] = []
                    for idx in sorted(all_blocks):
                        block = all_blocks[idx]
                        if block["type"] == "thinking":
                            content_blocks.append({
                                "type": "thinking",
                                "thinking": "".join(block["thinking_parts"]),
                                "signature": block["signature"],
                            })
                        elif block["type"] == "redacted_thinking":
                            content_blocks.append({
                                "type": "redacted_thinking",
                                "data": block["data"],
                            })
                        elif block["type"] == "text":
                            content_blocks.append({
                                "type": "text",
                                "text": "".join(block["text_parts"]),
                            })
                        elif block["type"] == "tool_use":
                            content_blocks.append({
                                "type": "tool_use",
                                "id": block["id"],
                                "name": block["name"],
                                "input": block.get("input", {}),
                            })
                    messages.append({"role": "assistant", "content": content_blocks})

                    # Execute tools (P1-2: all results in single user message)
                    tool_results_for_message: list[dict[str, Any]] = []
                    for tc in tool_calls:
                        # F022: Auto-inject source_episode_id into learn_fact.
                        # Use a local variable (not tc["input"]) to avoid mutating the
                        # shared block dict that content_blocks already references.
                        dispatch_input = self._maybe_inject_episode_id(tc["name"], tc["input"], session_id)

                        # F026: Action gating (pre-dispatch)
                        gated = False
                        if self._action_gate and ledger:
                            gate_result = await self._action_gate.check(
                                tc["name"], dispatch_input, ledger, user_message=user_message,
                            )
                            self._log_f026_decision(
                                "f026_action_gate",
                                {
                                    "tool_name": tc["name"],
                                    "approved": gate_result.approved,
                                    "reason": gate_result.reason,
                                    "mode": self._settings.action_gating_mode,
                                    "turn": ledger.current_turn,
                                },
                                session_id=session_id,
                            )
                            if gate_result.approved:
                                logger.info("F026 gate: %s approved (%s)", tc["name"], gate_result.reason)
                            else:
                                if self._settings.action_gating_mode == "enforce":
                                    result_text = f"[BLOCKED by ActionGate] {gate_result.reason}"
                                    if gate_result.suggestion:
                                        result_text += f"\n{gate_result.suggestion}"
                                    is_error = True
                                    gated = True
                                    ledger.record(tc["name"], dispatch_input, result_text, "blocked")
                                    logger.info("F026 gate: %s BLOCKED (%s)", tc["name"], gate_result.reason)
                                elif self._settings.action_gating_mode == "warn":
                                    logger.warning("F026 gate: %s would block (%s)", tc["name"], gate_result.reason)
                                else:
                                    logger.debug("F026 gate: %s would block (%s)", tc["name"], gate_result.reason)

                        if not gated:
                            start_time = time.monotonic()
                            result_text, is_error = "", False
                            async for item in self._dispatch_with_keepalive(
                                tc["name"], dispatch_input, session_id=session_id
                            ):
                                if isinstance(item, StreamEvent):
                                    yield item
                                else:
                                    result_text, is_error = item
                            duration_ms = int((time.monotonic() - start_time) * 1000)

                            # F026: Record in execution ledger (post-dispatch)
                            if ledger:
                                ledger.record(
                                    tc["name"], dispatch_input, result_text,
                                    "error" if is_error else "success",
                                )
                        else:
                            duration_ms = 0

                        tool_results_for_message.append({
                            "type": "tool_result",
                            "tool_use_id": tc["id"],
                            "content": result_text,
                            "is_error": is_error,
                        })

                        all_tool_results.append(ToolResult(
                            tool_name=tc["name"],
                            arguments=tc["input"],
                            result=result_text if not is_error else None,
                            error=result_text if is_error else None,
                            duration_ms=duration_ms,
                        ))

                        yield StreamEvent(type="tool_end", tool_name=tc["name"])

                    messages.append({"role": "user", "content": tool_results_for_message})

                    # Prune old tool results before next API call (Spec 008.1 Layer 1)
                    if self._compactor:
                        extracted_facts = self._compactor.prune_tool_results(messages)
                        if extracted_facts:
                            for fact_text in extracted_facts:
                                try:
                                    await self._heart.learn(FactInput(
                                        content=fact_text,
                                        category="technical",
                                        confidence=0.3,
                                        source="pre_prune_extraction",
                                    ))
                                except Exception:
                                    logger.debug("Failed to store pre-prune fact: %s", fact_text[:50])
                else:
                    # Max turns reached -- final call without tools
                    logger.warning("Streaming tool loop reached max_turns=%d", self._settings.max_turns)
                    try:
                        final = await self._call_api(
                            system_prompt=system_prompt,
                            messages=messages,
                            tools=None,
                        )
                        response_text = self._extract_text(final.content)
                    except Exception:
                        response_text = "I reached the maximum number of tool iterations."

                # Store assistant response
                conversation.messages.append(Message(role="assistant", content=response_text))

            except Exception as e:
                logger.error("Streaming error: %s", e)
                error = str(e)
                response_text = "I encountered an error processing your request."
                conversation.messages.append(Message(role="assistant", content=response_text))
                _caught_exc = e
            except asyncio.CancelledError:
                # Client disconnect — treat as clean exit, still run cleanup
                logger.info("Stream cancelled (client disconnect) for session %s", session_id)
                error = "cancelled"
                _caught_exc = None
            else:
                _caught_exc = None

                # F026: Post-response claim verification (streaming: warn+inject only)
                self._verify_claims(session_id, response_text, all_tool_results, ledger)
            finally:
                # ALWAYS call post_turn (review P1: guaranteed cleanup).
                # Shield from cancellation to prevent DB connection pool leaks —
                # CancelledError during post_turn's DB ops leaves sessions checked
                # out but never returned to the pool.
                turn_result = TurnResult(
                    response_text=response_text,
                    tool_results=all_tool_results,
                    error=error,
                    thinking_blocks=all_thinking_blocks,
                )
                try:
                    await asyncio.shield(
                        self._cognitive.post_turn(_agent_id, session_id, turn_result, turn_context)
                    )
                except (asyncio.CancelledError, Exception):
                    logger.warning("post_turn cleanup interrupted for session %s", session_id)
                self._check_safety_net(turn_context, all_tool_results)
                conversation.turn_contexts.append(turn_context)

            # Re-raise after cleanup so callers see the real error
            if _caught_exc is not None:
                raise _caught_exc

            # Note: streaming calibration skipped — accumulated total_usage across
            # multiple tool iterations would bias the per-char ratio. The _tool_loop
            # path calibrates per-call which is more accurate. Streaming-only turns
            # (no tools) are typically short Telegram messages where chars/4 is fine.

            yield StreamEvent(type="done", stop_reason="end_turn", usage=total_usage)

    # ------------------------------------------------------------------
    # Tool loop
    # ------------------------------------------------------------------

    async def _tool_loop(
        self,
        system_prompt: str | dict[str, str],
        conversation: Conversation,
        frame_id: str,
        session_id: str | None = None,
        is_subtask: bool = False,
        max_tool_calls: int | None = None,
        model_override: str | None = None,
        user_message: str = "",
        ledger: ExecutionLedger | None = None,
        tool_filter: list[str] | None = None,  # F034.5: restrict to named tools
        is_background: bool = False,
        # F061: per-call tool injection + forced terminal tool gating.
        extra_tools: dict[str, tuple[dict, Any]] | None = None,
        force_tool_on_penultimate: str | None = None,
        dag_node_id: UUID | None = None,  # F064.1: activity-ping target
    ) -> tuple[str, list[ToolResult], dict[str, int], list[str]]:
        """Run the tool use loop until completion or max_turns.

        Returns (response_text, tool_results, usage, thinking_blocks).

        The loop:
        1. Build messages array from conversation
        2. Call API with system prompt, messages, and available tools
        3. If stop_reason is not "tool_use", extract text and return
        4. Otherwise: append assistant response, dispatch tools, append results
        5. Repeat until done or max_turns
        """
        if not self._dispatcher:
            raise RuntimeError("No tool dispatcher set -- call set_dispatcher() first")

        # Get base tools for current frame (D5)
        base_tools = self._dispatcher.available_tools(frame_id)

        # 012.2: Remove delegation tools from subtask tool set (no-nesting rule)
        if is_subtask:
            _SUBTASK_EXCLUDED_TOOLS = {"spawn_task", "schedule_task"}
            base_tools = [t for t in base_tools if t["name"] not in _SUBTASK_EXCLUDED_TOOLS]

        # F034.5: Dynamic check tool restriction
        if tool_filter is not None:
            base_tools = [t for t in base_tools if t["name"] in tool_filter]

        # Build initial messages from conversation history
        # The latest user message is already in conversation.messages
        messages = self._format_messages(conversation)

        all_tool_results: list[ToolResult] = []
        all_thinking_blocks: list[str] = []  # Accumulated across iterations
        # F061: include tool_calls counter so the hardened executor's per-attempt
        # accumulator can populate heart.subtasks.tool_calls_made (was missing).
        total_usage: dict[str, int] = {
            "input_tokens": 0, "output_tokens": 0, "tool_calls": 0,
        }
        turns = 0
        total_tool_calls = 0
        max_turns = self._settings.max_turns

        # F061: detect whether forced tool_choice is safe for this call. Forced
        # tool_choice is incompatible with extended thinking — Anthropic only
        # allows {"type":"auto"|"none"} when thinking is enabled. Detect off
        # via global thinking_mode OR the haiku-family check the rest of the
        # runner uses.
        effective_model_for_force = (model_override or self._settings.model).lower()
        thinking_off = (
            self._settings.thinking_mode == "off"
            or "haiku" in effective_model_for_force
        )
        force_enabled = bool(force_tool_on_penultimate) and thinking_off
        terminate_after_tool_results = False  # set when submit_final_report fires
        # F061 round 4 P2-I: cap the number of force_tool_on_penultimate
        # fires within a single run_turn. Without this cap, the >= math
        # could fire force on multiple consecutive turns when the model
        # keeps producing schema-invalid payloads — 3× token cost on the
        # worst-case validator-fail path. Cap at 2 (penultimate + ultimate)
        # which preserves the recovery benefit without unbounded retries.
        _force_fires_remaining = 2

        while turns < max_turns:
            # F064.1 ping site 1 — top of every iteration. Fires for both
            # tool-using and text-only turns. asyncio.shield prevents a
            # wait_for cancellation from killing an in-flight ping write.
            if dag_node_id is not None:
                self._ping_dag_node_activity(dag_node_id)

            # F020: Rebuild tool list each iteration for dynamic cache_retrieve
            tools = list(base_tools)
            # F061: append per-call extra tool schemas (NOT registered globally).
            if extra_tools:
                for _name, (_schema, _exec) in extra_tools.items():
                    tools.append(_schema)

            # F061: force the terminal tool on the LAST TWO allowed turns
            # (penultimate + ultimate) when force_tool_on_penultimate is set
            # and thinking is off. Forcing on only the ultimate turn (the
            # earlier ``turns - 1`` math) left no recovery if the model
            # still failed to call submit_final_report — recoverable cases
            # became ``incomplete_no_terminal``. Forcing on the penultimate
            # turn AND the ultimate turn gives the model two pushed
            # opportunities to terminate before the existing tool-call-limit
            # fallback kicks in.
            #
            # Math (max_turns=4, max_tool_calls=20):
            #   turns=0: skipped by ``turns > 0`` (never force first turn)
            #   turns=1: not yet penultimate (1 < 2)
            #   turns=2: penultimate — force fires; if model terminates,
            #            short-circuit returns.
            #   turns=3: ultimate — force still fires as last push.
            #   ↳ If model still misses, the existing ``max_turns`` branch
            #     makes a final no-tools summary call.
            #
            # ``>=`` (not ``==``) handles the case where the model dispatched
            # multiple tools in one response and jumped past the boundary.
            tool_choice_arg: dict[str, Any] | None = None
            # F061 round 4 P2-G: clamp the (limit - 2) thresholds to 1.
            # Without the clamp, ``max_turns=2`` or ``max_tool_calls=2``
            # produces a threshold of 0 — the ``>=`` test then matches on
            # turn 1 / first tool call, forcing the model to terminate
            # before any work has run. Clamping to 1 means force fires no
            # earlier than the 2nd turn / 2nd tool call.
            _turn_threshold = max(1, max_turns - 2)
            _tool_threshold = (
                max(1, max_tool_calls - 2) if max_tool_calls is not None else None
            )
            is_penultimate = (
                force_enabled
                and force_tool_on_penultimate is not None
                and turns > 0
                and _force_fires_remaining > 0
                and (turns >= _turn_threshold
                     or (_tool_threshold is not None
                         and total_tool_calls >= _tool_threshold))
            )
            if is_penultimate:
                tool_choice_arg = {
                    "type": "tool", "name": force_tool_on_penultimate,
                }
                _force_fires_remaining -= 1

            # Only pass tool_choice when non-None so existing mocks of
            # _call_api (which don't accept the new kwarg) continue to work.
            _call_kwargs: dict[str, Any] = {
                "system_prompt": system_prompt,
                "messages": messages,
                "tools": tools if tools else None,
                "model_override": model_override,
                "is_background": is_background,
            }
            if tool_choice_arg is not None:
                _call_kwargs["tool_choice"] = tool_choice_arg
            api_response = await self._call_api(**_call_kwargs)

            # F035.4: Update context log with response metadata
            if self._context_logger and self._last_context_entry_id and api_response.usage:
                self._context_logger.update_response(
                    entry_id=self._last_context_entry_id,
                    input_tokens=api_response.usage.get("input_tokens"),
                    output_tokens=api_response.usage.get("output_tokens"),
                    cache_creation=api_response.usage.get("cache_creation_input_tokens"),
                    cache_read=api_response.usage.get("cache_read_input_tokens"),
                    stop_reason=api_response.stop_reason,
                )
                self._last_context_entry_id = None  # Consumed

            # Accumulate usage + calibrate token estimator
            if api_response.usage:
                total_usage["input_tokens"] += api_response.usage.get("input_tokens", 0)
                total_usage["output_tokens"] += api_response.usage.get("output_tokens", 0)
                if self._compactor:
                    input_chars = sum(len(str(m.get("content", ""))) for m in messages)
                    self._compactor.estimator.calibrate(
                        input_chars, api_response.usage.get("input_tokens", 0)
                    )

            # Extract thinking blocks from this iteration
            for block in api_response.content:
                if isinstance(block, dict) and block.get("type") == "thinking":
                    thinking_text = block.get("thinking", "").strip()
                    if thinking_text:
                        all_thinking_blocks.append(thinking_text)

            # If not a tool use, we're done
            if api_response.stop_reason != "tool_use":
                response_text = self._extract_text(api_response.content)
                return response_text, all_tool_results, total_usage, all_thinking_blocks

            # P0-3: Append FULL assistant response (all content blocks)
            messages.append({
                "role": "assistant",
                "content": api_response.content,
            })

            # P1-2: Dispatch ALL tool_use blocks, collect results in SINGLE user message
            tool_results_for_message: list[dict[str, Any]] = []
            for block in api_response.content:
                if block.get("type") == "tool_use":
                    tool_name = block["name"]
                    tool_input = block.get("input", {})
                    tool_use_id = block["id"]

                    # F022: Auto-inject source_episode_id into learn_fact.
                    tool_input = self._maybe_inject_episode_id(tool_name, tool_input, session_id)

                    # F026: Action gating (pre-dispatch)
                    gated = False
                    if self._action_gate and ledger:
                        gate_result = await self._action_gate.check(
                            tool_name, tool_input, ledger, user_message=user_message,
                        )
                        self._log_f026_decision(
                            "f026_action_gate",
                            {
                                "tool_name": tool_name,
                                "approved": gate_result.approved,
                                "reason": gate_result.reason,
                                "mode": self._settings.action_gating_mode,
                                "turn": ledger.current_turn,
                            },
                            session_id=session_id,
                        )
                        if gate_result.approved:
                            logger.info("F026 gate: %s approved (%s)", tool_name, gate_result.reason)
                        else:
                            if self._settings.action_gating_mode == "enforce":
                                result_text = f"[BLOCKED by ActionGate] {gate_result.reason}"
                                if gate_result.suggestion:
                                    result_text += f"\n{gate_result.suggestion}"
                                is_error = True
                                gated = True
                                ledger.record(tool_name, tool_input, result_text, "blocked")
                                logger.info("F026 gate: %s BLOCKED (%s)", tool_name, gate_result.reason)
                            elif self._settings.action_gating_mode == "warn":
                                logger.warning("F026 gate: %s would block (%s)", tool_name, gate_result.reason)
                            else:
                                logger.debug("F026 gate: %s would block (%s)", tool_name, gate_result.reason)

                    if not gated:
                        start_time = time.monotonic()
                        # F061: per-call dispatch override for extra_tools.
                        # Routes submit_final_report (and any other per-run
                        # tool) to the executor passed by the subtask harness
                        # WITHOUT registering globally — no leak risk on crash.
                        if extra_tools and tool_name in extra_tools:
                            _schema, executor = extra_tools[tool_name]
                            result_text, is_error = await executor(**tool_input)
                            # Short-circuit: any successful extra_tools call
                            # terminates the loop. F061's submit_final_report
                            # is currently the only extra_tool and its
                            # semantics are "I am done" — even when the
                            # model voluntarily called it (force_tool was
                            # None on this turn), we still want to exit
                            # rather than make wasted follow-up API calls.
                            if not is_error:
                                terminate_after_tool_results = True
                        else:
                            # F064.1 ping site 2 — immediately before tool
                            # dispatch. Pins last_activity_at to NOW so a long-
                            # running tool (e.g. 45-min bash build) does not
                            # trip the stall scan mid-execution.
                            if dag_node_id is not None:
                                self._ping_dag_node_activity(dag_node_id)
                            # @codex P1 on e8841b2: in-flight heartbeat
                            # for tool calls that may exceed stall_timeout.
                            # Cancels in the finally regardless of success.
                            _hb = (
                                self._start_activity_heartbeat(dag_node_id)
                                if dag_node_id is not None else None
                            )
                            try:
                                result_text, is_error = await self._dispatcher.dispatch(
                                    tool_name, tool_input, session_id=session_id
                                )
                            finally:
                                await self._stop_activity_heartbeat(_hb)
                        duration_ms = int((time.monotonic() - start_time) * 1000)

                        # F026: Record in execution ledger (post-dispatch)
                        if ledger:
                            ledger.record(
                                tool_name, tool_input, result_text,
                                "error" if is_error else "success",
                            )
                    else:
                        duration_ms = 0

                    # F020: SmartCompress — ingestion-time compression
                    compress_result = await smart_compress(
                        tool_name, tool_input, result_text, self._settings,
                        is_error=is_error,
                    )

                    # F020: Cache original if non-re-fetchable and compressed
                    if compress_result.original_text and session_id:
                        try:
                            from nous.api.tool_cache import cache_compressed_result
                            async with self._heart.db.session() as db_sess:
                                hash_key = await cache_compressed_result(
                                    db_sess,
                                    agent_id=self._settings.agent_id,
                                    session_id=session_id,
                                    tool_name=tool_name,
                                    tool_input=tool_input,
                                    original_content=compress_result.original_text,
                                    item_count=compress_result.item_count,
                                )
                        except Exception:
                            logger.warning("Failed to cache %s result", tool_name, exc_info=True)

                    tool_results_for_message.append({
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": compress_result.text,
                        "is_error": is_error,
                    })

                    # Track for post_turn
                    all_tool_results.append(ToolResult(
                        tool_name=tool_name,
                        arguments=tool_input,
                        result=result_text if not is_error else None,
                        error=result_text if is_error else None,
                        duration_ms=duration_ms,
                    ))

                    # F061 PR-3 Codex review P2: stop dispatching remaining
                    # tool_use blocks once a successful submit_final_report
                    # accepted the terminal payload. Subsequent tools in
                    # the same assistant message would otherwise run with
                    # side effects after termination has been declared.
                    if terminate_after_tool_results:
                        break

            # Append all tool results as single user message
            messages.append({
                "role": "user",
                "content": tool_results_for_message,
            })

            total_tool_calls += len(tool_results_for_message)
            total_usage["tool_calls"] += len(tool_results_for_message)

            # F061: short-circuit on successful submit_final_report. Caller
            # reads the validated payload from the collector — response_text
            # is intentionally a thin marker since the contract is the tool
            # input, not free-form prose.
            if terminate_after_tool_results:
                return (
                    "Report submitted.",
                    all_tool_results,
                    total_usage,
                    all_thinking_blocks,
                )

            # 012.2: Enforce subtask tool call limit
            if max_tool_calls and total_tool_calls >= max_tool_calls:
                logger.info("Subtask tool call limit reached (%d/%d)", total_tool_calls, max_tool_calls)
                final = await self._call_api(
                    system_prompt=system_prompt,
                    messages=messages,
                    tools=None,
                    model_override=model_override,
                    is_background=is_background,
                )
                if final.usage:
                    total_usage["input_tokens"] += final.usage.get("input_tokens", 0)
                    total_usage["output_tokens"] += final.usage.get("output_tokens", 0)
                return self._extract_text(final.content), all_tool_results, total_usage, all_thinking_blocks

            # Prune old tool results before next API call (Spec 008.1 Layer 1)
            if self._compactor:
                extracted_facts = self._compactor.prune_tool_results(messages)
                if extracted_facts:
                    for fact_text in extracted_facts:
                        try:
                            await self._heart.learn(FactInput(
                                content=fact_text,
                                category="technical",
                                confidence=0.3,
                                source="pre_prune_extraction",
                            ))
                        except Exception:
                            logger.debug("Failed to store pre-prune fact: %s", fact_text[:50])

            turns += 1

        # Max turns reached -- extract text from last response if any
        logger.warning("Tool loop reached max_turns=%d", max_turns)
        # Make one final call without tools to get a text response
        try:
            final_response = await self._call_api(
                system_prompt=system_prompt,
                messages=messages,
                tools=None,
                model_override=model_override,
                is_background=is_background,
            )
            if final_response.usage:
                total_usage["input_tokens"] += final_response.usage.get("input_tokens", 0)
                total_usage["output_tokens"] += final_response.usage.get("output_tokens", 0)
            return self._extract_text(final_response.content), all_tool_results, total_usage, all_thinking_blocks
        except Exception:
            return "I reached the maximum number of tool iterations. Please try again.", all_tool_results, total_usage, all_thinking_blocks

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    # Platform-specific formatting instructions
    _TELEGRAM_FORMAT_INSTRUCTIONS = """## Output Formatting (Telegram)
You are responding in Telegram. Format accordingly:

For structured information, use this style:

🧠 Section Name
• Item one — description
• Item two — description

📊 Another Section
• Key: value
• Key: value

Rules:
- NEVER use markdown tables (| col | col |). Use bullet lists instead.
- NEVER use ## headers in your response. Use emoji + **bold** for section headers.
- Use bullet points (• or -) for all lists.
- Use em dashes (—) to separate item from description.
- Keep formatting simple: **bold**, _italic_, `code`, code blocks only.
- No horizontal rules (---).
"""

    def _build_system_prompt(
        self,
        turn_context: TurnContext,
        platform: str | None = None,
        *,
        ledger: ExecutionLedger | None = None,
        corrections: list[str] | None = None,
    ) -> str | dict[str, str]:
        """Build the system prompt: cognitive context + frame instructions.

        P0-2 fix: Does NOT inject conversation history. History flows
        through messages[] array via _format_messages().

        F036: When cache_split_system_prompt is enabled AND sections_by_tier
        is available, returns dict[str, str] mapping tier names to prompt text.
        Runner-appended sections (frame instructions, ledger, corrections)
        go into the dynamic tier. Otherwise returns a flat string (legacy).
        """
        # F036: 3-tier split path
        if (
            self._settings.cache_split_system_prompt
            and turn_context.sections_by_tier
        ):
            tiers = dict(turn_context.sections_by_tier)  # copy to avoid mutation

            # Collect runner-appended dynamic content
            dynamic_extras: list[str] = []
            frame_instructions = self._get_frame_instructions(turn_context)
            if frame_instructions:
                dynamic_extras.append(frame_instructions)
            if turn_context.diagnostic_nudges:
                dynamic_extras.append(turn_context.diagnostic_nudges)
            if ledger and self._settings.execution_ledger_enabled:
                ledger_section = ledger.system_prompt_section(
                    self._settings.execution_ledger_max_tokens
                )
                if ledger_section:
                    dynamic_extras.append(ledger_section)
            if corrections:
                dynamic_extras.append("[Previous Turn Corrections]\n" + "\n".join(corrections))
            if platform == "telegram":
                dynamic_extras.append(self._TELEGRAM_FORMAT_INSTRUCTIONS)

            # Append runner extras to dynamic tier
            if dynamic_extras:
                existing_dynamic = tiers.get("dynamic", "")
                extras_text = "\n\n".join(dynamic_extras)
                tiers["dynamic"] = (
                    existing_dynamic + "\n\n" + extras_text
                    if existing_dynamic
                    else extras_text
                )

            return tiers

        # Legacy flat string path
        parts = [turn_context.system_prompt]

        frame_instructions = self._get_frame_instructions(turn_context)
        if frame_instructions:
            parts.append(frame_instructions)
        if turn_context.diagnostic_nudges:
            parts.append(turn_context.diagnostic_nudges)
        if ledger and self._settings.execution_ledger_enabled:
            ledger_section = ledger.system_prompt_section(
                self._settings.execution_ledger_max_tokens
            )
            if ledger_section:
                parts.append(ledger_section)
        if corrections:
            parts.append("[Previous Turn Corrections]\n" + "\n".join(corrections))
        if platform == "telegram":
            parts.append(self._TELEGRAM_FORMAT_INSTRUCTIONS)

        return "\n\n".join(parts)

    def _get_frame_instructions(self, turn_context: TurnContext) -> str:
        """Return frame-specific tool use instructions.

        Nudges Claude to use appropriate tools based on the active cognitive frame.
        Only mentions tools available for the frame (synced with FRAME_TOOLS).
        """
        frame_id = turn_context.frame.frame_id

        if frame_id == "decision":
            return (
                "## Tool Instructions\n\n"
                "You are in a DECISION frame. You MUST call `record_decision` "
                "to record your decision before responding. Include your reasoning, "
                "confidence level, and category.\n\n"
                "**What IS a decision:** A choice between alternatives — architecture "
                "choices, tool selections, process changes, trade-offs with pros/cons.\n\n"
                "**What is NOT a decision:** Status reports, routine completions, "
                "simple observations, task acknowledgments, greetings. "
                "Do NOT record these.\n\n"
                "Use `recall_deep` to search for relevant past decisions. "
                "Use `web_search` and `web_fetch` to research options before deciding."
            )
        elif frame_id == "task":
            return (
                "## Tool Instructions\n\n"
                "You are in a TASK frame. If you make a meaningful choice between "
                "alternatives during this task, call `record_decision` to record it. "
                "Do NOT record routine task completions, status updates, or simple "
                "observations as decisions — a decision requires choosing between "
                "alternatives with trade-offs.\n\n"
                "Use `recall_deep` to search for relevant past decisions and knowledge. "
                "Use `learn_fact` to store any new facts discovered. You can also use "
                "`bash`, `read_file`, and `write_file` for system operations. "
                "Use `web_search` and `web_fetch` for research.\n\n"
                "**Efficiency:** For multi-file investigation or batch operations, "
                "prefer `run_python` over sequential `bash` calls. Combine related "
                "commands into a single `bash` call using `&&` when possible."
            )
        elif frame_id == "debug":
            return (
                "## Tool Instructions\n\n"
                "You are in a DEBUG frame. Use `recall_deep` to search for relevant "
                "past decisions and procedures. Record meaningful debugging decisions "
                "(e.g., root cause identified, fix approach chosen) with "
                "`record_decision`. Do NOT record routine debug steps or status "
                "observations. Store root cause findings with `learn_fact`. "
                "Use `bash` and `read_file` for investigation. Use `web_search` and "
                "`web_fetch` to look up documentation or error messages.\n\n"
                "**Efficiency:** For multi-file investigation, prefer `run_python` "
                "with file reads over sequential `bash` calls. Combine related "
                "commands into a single `bash` call using `&&`."
            )
        elif frame_id == "question":
            return (
                "## Tool Instructions\n\n"
                "You are in a QUESTION frame. Use `recall_deep` to search memory "
                "for relevant knowledge before answering. Use `web_search` and "
                "`web_fetch` for questions about current events or topics not in memory."
            )
        elif frame_id == "creative":
            return (
                "## Tool Instructions\n\n"
                "You are in a CREATIVE frame. Use `recall_deep` to find relevant "
                "knowledge. Use `learn_fact` to store creative insights. "
                "Use `write_file` to save creative output. Use `web_search` "
                "for inspiration and reference material."
            )
        elif frame_id == "conversation":
            return (
                "## Tool Instructions\n\n"
                "You are in a CONVERSATION frame. Use `web_search` and `web_fetch` "
                "to find current information when needed."
            )

        return ""

    def _extract_text(self, content_blocks: list[dict[str, Any]]) -> str:
        """Extract text from API response content blocks."""
        text_parts = []
        for block in content_blocks:
            if block.get("type") == "text":
                text_parts.append(block["text"])
        return "\n".join(text_parts) if text_parts else ""

    async def _activity_heartbeat_loop(
        self, dag_node_id: UUID, interval_seconds: float
    ) -> None:
        """F064.1: background heartbeat ping while a tool dispatch is in flight.

        @codex P1 on e8841b2: the single pre-dispatch ping isn't enough for
        tool calls that exceed stall_timeout — a 30-min bash with a 10-min
        stall_timeout would still trip stall detection mid-execution because
        no ping fires for 30 minutes. This loop emits a ping every
        `interval_seconds` until cancelled by the caller's try/finally.

        Failure mode handling:
        - asyncio.CancelledError on cancel → re-raise (caller awaits to drain)
        - other exceptions → DEBUG log + continue loop (best-effort telemetry)
        """
        store = getattr(self, "_dag_store", None)
        if store is None:
            return
        try:
            while True:
                await asyncio.sleep(interval_seconds)
                try:
                    await asyncio.shield(store.touch_node_activity(dag_node_id))
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "F064.1 heartbeat ping failed (suppressed)", exc_info=True
                    )
        except asyncio.CancelledError:
            raise

    def _start_activity_heartbeat(
        self, dag_node_id: UUID
    ) -> "asyncio.Task | None":
        """Start the background heartbeat for a tool dispatch. Returns the
        task so the caller's try/finally can cancel it. Returns None when
        stall detection is disabled or no store wired."""
        if not self._settings.dag_stall_detection_enabled:
            return None
        store = getattr(self, "_dag_store", None)
        if store is None:
            return None
        # Heartbeat at 1/3 of the default stall_timeout, min 30s. This keeps
        # write amplification bounded (≤3 pings per stall window) while
        # ensuring at least one ping lands within the window for any tool
        # call that runs longer than stall_timeout.
        base = self._settings.dag_node_default_stall_timeout
        if base <= 0:
            return None
        interval = max(base / 3.0, 30.0)
        try:
            return asyncio.create_task(
                self._activity_heartbeat_loop(dag_node_id, interval)
            )
        except RuntimeError:
            return None

    @staticmethod
    async def _stop_activity_heartbeat(task: "asyncio.Task | None") -> None:
        """Cancel + drain a heartbeat task. Idempotent on None."""
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    def _ping_dag_node_activity(self, dag_node_id: UUID) -> None:
        """F064.1: fire-and-forget activity ping for stall detection.

        Wrapped in an async helper that internally awaits asyncio.shield so a
        wait_for cancellation in the outer subtask path can't cancel the
        in-flight UPDATE. A write failure is absorbed by
        DAGStore.touch_node_activity (DEBUG log, swallowed) so the tool loop
        never blocks or raises because of telemetry.

        No-op when _dag_store hasn't been wired (chat path, tests without
        DAG infra) OR when NOUS_DAG_STALL_DETECTION_ENABLED=false (the
        write would be wasted — the stall scan never reads it). Called
        from _tool_loop's two ping sites; the third baseline ping fires
        from orchestrator._launch_subtask_node.

        Fix per @codex review: asyncio.create_task requires a coroutine, but
        asyncio.shield returns a Future — passing shield() directly to
        create_task() raises TypeError. The async wrapper here is a
        coroutine factory, satisfying create_task while still letting the
        body await shield(...) so the write survives wait_for cancellation.

        @codex P2 on d281ac6: gate behind the master flag so the per-tool
        boundary doesn't generate unread DB writes when stall detection
        is disabled (default state).
        """
        if not self._settings.dag_stall_detection_enabled:
            return
        store = getattr(self, "_dag_store", None)
        if store is None:
            return

        async def _ping() -> None:
            try:
                await asyncio.shield(store.touch_node_activity(dag_node_id))
            except asyncio.CancelledError:
                # Cancellation is expected during shutdown / wait_for timeout.
                # Re-raising would bubble into the task and log; we want silent.
                raise
            except Exception:  # noqa: BLE001 — best-effort telemetry
                logger.debug("F064.1 ping failed (suppressed)", exc_info=True)

        try:
            asyncio.create_task(_ping())
        except RuntimeError:
            # No running event loop — happens in some test paths. Silent
            # by design; the stall scan's NULL-fallback policy covers us.
            pass

    def _check_safety_net(
        self,
        turn_context: TurnContext,
        tool_results: list[ToolResult],
    ) -> None:
        """Log if a decision frame was active but record_decision wasn't called.

        WARNING for decision frame (mandatory), DEBUG for task/debug (optional).
        """
        frame_id = turn_context.frame.frame_id
        tool_names = {tr.tool_name for tr in tool_results}

        if "record_decision" in tool_names:
            return

        if frame_id in _REQUIRED_DECISION_FRAMES:
            logger.warning(
                "Safety net: frame=%s but record_decision was not called during turn "
                "(session decision_id=%s). Consider recording this decision.",
                frame_id,
                turn_context.decision_id,
            )
        elif frame_id in _OPTIONAL_DECISION_FRAMES:
            logger.debug(
                "Safety net: frame=%s, record_decision not called during turn "
                "(session decision_id=%s).",
                frame_id,
                turn_context.decision_id,
            )

    def _get_or_create_ledger(self, session_id: str) -> ExecutionLedger:
        """Get or create a session-scoped execution ledger."""
        if session_id not in self._ledgers:
            self._ledgers[session_id] = ExecutionLedger(session_id=session_id)
            logger.info("F026: Ledger created for session %s", session_id)
        return self._ledgers[session_id]

    def _verify_claims(
        self,
        session_id: str,
        response_text: str,
        tool_results: list[ToolResult],
        ledger: ExecutionLedger,
    ) -> None:
        """F026: Post-response claim verification and ghost planning detection.

        Note: Spec says block-and-rerun for external claims, but streaming
        can't unsend yielded text. Both paths use inject-correction-for-next-turn.
        Accepted limitation from plan review.
        """
        turn_tool_names = [tr.tool_name for tr in tool_results]

        # Claim verification
        if self._claim_verifier:
            verification = self._claim_verifier.verify(response_text, turn_tool_names, ledger)
            self._log_f026_decision(
                "f026_claim_verification",
                {
                    "verified": verification.verified,
                    "violation_count": len(verification.violations),
                    "violations": [
                        {
                            "claimed_text": v.claimed_text[:200],
                            "expected_tool": v.expected_tool,
                            "found_in_turn": v.found_in_turn,
                            "found_in_ledger": v.found_in_ledger,
                        }
                        for v in verification.violations
                    ],
                    "tool_names_this_turn": turn_tool_names,
                    "mode": self._settings.claim_verification_mode,
                },
                session_id=session_id,
            )
            if verification.verified:
                logger.info("F026 claims: verified (%d tools this turn)", len(turn_tool_names))
            else:
                mode = self._settings.claim_verification_mode
                if mode == "enforce":
                    logger.warning("Claim verification failed: %s", verification.correction)
                    self._pending_corrections.setdefault(session_id, []).append(
                        verification.correction or ""
                    )
                elif mode == "warn":
                    logger.warning("Claim verification: %s", verification.correction)
                else:
                    logger.debug("Claim verification (shadow): %s", verification.correction)

        # Ghost planning detection (only on zero-tool turns, respects mode)
        if self._intent_tracker and not turn_tool_names:
            if self._intent_tracker.check_ghost_planning(response_text, turn_tool_names, ledger):
                mode = self._settings.claim_verification_mode
                if mode == "enforce":
                    logger.info("Ghost planning detected in session %s", session_id)
                    nudge = self._intent_tracker.build_nudge()
                    self._pending_corrections.setdefault(session_id, []).append(nudge)
                elif mode == "warn":
                    logger.warning("Ghost planning detected (warn) in session %s", session_id)
                else:
                    logger.debug("Ghost planning detected (shadow) in session %s", session_id)

    async def _get_or_create_conversation(self, session_id: str) -> Conversation:
        """Get existing or create new conversation with LRU eviction.

        008.1 Phase 3: If conversation not in memory, check Heart for
        persisted state (survives container restarts). Restore if found.
        """
        if session_id in self._conversations:
            # Move to end (most recently used)
            self._conversations.move_to_end(session_id)
            return self._conversations[session_id]

        # Evict oldest if at capacity
        while len(self._conversations) >= MAX_CONVERSATIONS:
            evicted_id, _ = self._conversations.popitem(last=False)
            self._compaction_locks.pop(evicted_id, None)
            self._ledgers.pop(evicted_id, None)  # F026
            self._pending_corrections.pop(evicted_id, None)  # F026

        # 008.1 Phase 3: Try to restore from Heart persistence
        conversation = await self._restore_conversation(session_id)
        if conversation is None:
            conversation = Conversation(session_id=session_id)

        self._conversations[session_id] = conversation
        return conversation

    async def _stream_with_keepalive(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Wrap _call_api_stream with keepalive events during initial wait.

        Emits keepalives every keepalive_interval seconds until the first
        real event arrives from Anthropic. After that, events flow directly.
        """
        interval = self._settings.keepalive_interval
        stream = self._call_api_stream(system_prompt, messages, tools)

        # Wait for first event with keepalives — use a persistent task
        # to avoid cancelling the generator's __anext__ on timeout
        next_task: asyncio.Task[StreamEvent] | None = None
        try:
            while True:
                if next_task is None:
                    next_task = asyncio.create_task(stream.__anext__())
                try:
                    event = await asyncio.wait_for(
                        asyncio.shield(next_task), timeout=interval
                    )
                    next_task = None  # consumed
                    yield event
                    break  # got first event, switch to direct passthrough
                except StopAsyncIteration:
                    return
                except TimeoutError:
                    if next_task.done():
                        # Task finished during our timeout handling
                        try:
                            yield next_task.result()
                            next_task = None
                            break
                        except StopAsyncIteration:
                            return
                    yield StreamEvent(type="keepalive")

            # After first event, pass through directly
            async for event in stream:
                yield event
        finally:
            if next_task is not None and not next_task.done():
                next_task.cancel()
                try:
                    await next_task
                except (asyncio.CancelledError, StopAsyncIteration):
                    pass

    def _maybe_inject_episode_id(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        session_id: str | None,
    ) -> dict[str, Any]:
        """Return tool_input with source_episode_id injected for learn_fact calls.

        F022: Facts learned during an active episode should be linked to that
        episode via source_episode_id so link_episode_deterministic() can create
        fact→episode graph edges.  The model never needs to know the UUID;
        injection happens transparently server-side.

        Returns the original dict unchanged for all other tools, or if no active
        episode exists for the session.

        NOTE (P1-1 limitation): falls back to None after process restart because
        _active_episodes is in-memory only.  A proper fix requires adding
        session_id to the Episode DB schema so we can query the active episode
        from DB when the in-memory dict misses.  Tracked as follow-up migration.
        """
        if tool_name != "learn_fact" or "source_episode_id" in tool_input:
            return tool_input
        if not session_id:
            return tool_input
        active_ep = self._cognitive.get_active_episode_id(session_id)
        if not active_ep:
            return tool_input
        return {**tool_input, "source_episode_id": active_ep}

    async def _dispatch_with_keepalive(
        self, name: str, args: dict[str, Any], session_id: str | None = None,
    ) -> AsyncGenerator[StreamEvent | tuple[str, bool], None]:
        """Execute a tool, yielding keepalive events during long execution.

        Yields StreamEvent(type="keepalive") every `keepalive_interval` seconds
        while the tool is running. The final yield is a tuple (result_text, is_error).
        If the tool exceeds `tool_timeout`, it is cancelled and an error is returned.
        """
        interval = self._settings.keepalive_interval
        timeout = self._settings.tool_timeout

        task = asyncio.create_task(
            asyncio.wait_for(
                self._dispatcher.dispatch(name, args, session_id=session_id),
                timeout=timeout,
            )
        )

        try:
            while not task.done():
                try:
                    await asyncio.wait_for(
                        asyncio.shield(task), timeout=interval
                    )
                except TimeoutError:
                    if task.done():
                        break
                    yield StreamEvent(type="keepalive")
                except Exception:
                    # Task raised — will be handled below via task.result()
                    break

            try:
                result_text, is_error = task.result()
            except TimeoutError:
                logger.warning(
                    "Tool '%s' timed out after %ds", name, timeout
                )
                result_text = f"Tool '{name}' timed out after {timeout}s"
                is_error = True
            except Exception as e:
                result_text = str(e)
                is_error = True

            yield (result_text, is_error)
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

    def _format_messages(self, conversation: Conversation) -> list[dict[str, Any]]:
        """Format conversation history for API calls. Pure, sync, no side effects.

        When compaction is enabled, returns ALL messages (compaction manages size).
        Otherwise, limits to last MAX_HISTORY_MESSAGES messages (legacy behavior).
        """
        if self._settings.compaction_enabled:
            return [{"role": m.role, "content": m.content} for m in conversation.messages]
        recent = conversation.messages[-MAX_HISTORY_MESSAGES:]
        return [{"role": m.role, "content": m.content} for m in recent]

    def _format_history_text(self, conversation: Conversation) -> str:
        """Format conversation history as readable text for reflection."""
        lines = []
        if conversation.summary:
            lines.append(f"[Previous context summary]\n{conversation.summary}\n")
        recent = conversation.messages[-MAX_HISTORY_MESSAGES:]
        for msg in recent:
            role_label = "User" if msg.role == "user" else "Assistant"
            lines.append(f"{role_label}: {msg.content}")
        return "\n\n".join(lines)

    # ------------------------------------------------------------------
    # Conversation persistence (008.1 Phase 3)
    # ------------------------------------------------------------------

    async def _save_conversation(
        self, agent_id: str, session_id: str, conversation: Conversation
    ) -> None:
        """Persist conversation state to Heart after compaction."""
        try:
            messages_json = [
                {"role": m.role, "content": m.content}
                for m in conversation.messages
            ]
            await self._heart.save_conversation_state(
                agent_id=agent_id,
                session_id=session_id,
                summary=conversation.summary,
                messages=messages_json,
                turn_count=len(conversation.turn_contexts),
                compaction_count=conversation.compaction_count,
            )
            logger.debug("Saved conversation state for session %s", session_id)
        except Exception:
            logger.warning("Failed to save conversation state for session %s", session_id)

    async def _restore_conversation(self, session_id: str) -> Conversation | None:
        """Restore conversation from Heart persistence if available."""
        try:
            state = await self._heart.load_conversation_state(
                agent_id=self._settings.agent_id,
                session_id=session_id,
            )
            if state is None:
                return None

            messages_data = state.get("messages") or []
            messages = [
                Message(role=m["role"], content=m["content"])
                for m in messages_data
                if isinstance(m, dict) and "role" in m and "content" in m
            ]

            conversation = Conversation(
                session_id=session_id,
                messages=messages,
                summary=state.get("summary"),
                compaction_count=state.get("compaction_count", 0),
            )
            logger.info(
                "Restored conversation for session %s (%d messages, %d compactions)",
                session_id,
                len(messages),
                conversation.compaction_count,
            )
            return conversation
        except Exception:
            logger.warning("Failed to restore conversation for session %s", session_id)
            return None

    async def _delete_conversation_state(self, session_id: str) -> None:
        """Remove persisted conversation state on session end.

        Logged at WARNING with stack trace on failure: a silently-leaked
        conversation_state row will re-hydrate stale messages on the next
        session use ("user comes back tomorrow and gets ghost messages"),
        which is hard to diagnose after the fact. The idle-timeout path
        newly exercises this code, so visibility matters more now.
        """
        try:
            await self._heart.delete_conversation_state(
                agent_id=self._settings.agent_id,
                session_id=session_id,
            )
        except Exception:
            logger.warning(
                "Failed to delete conversation state for session %s",
                session_id,
                exc_info=True,
            )
