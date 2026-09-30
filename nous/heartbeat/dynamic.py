"""Dynamic heartbeat checks (F034.5).

Prompt-driven checks loaded from DB, running alongside permanent checks.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from croniter import croniter
from sqlalchemy import func, select, update

from nous.api.execution_context import ExecutionContext
from nous.heartbeat.registry import BaseCheck
from nous.heartbeat.schemas import CheckResult, Finding

if TYPE_CHECKING:
    from nous.api.runner import AgentRunner
    from nous.heartbeat.registry import CheckRegistry
    from nous.storage.database import Database

logger = logging.getLogger(__name__)


class DynamicCheckLimitReached(ValueError):
    """Raised when the max-concurrent dynamic-check count is reached.

    Audit DG-4 (review follow-up): a dedicated type (ValueError subclass for
    backward-compat) so the DAG orchestrator can DEFER a check node on this
    transient condition instead of failing it permanently — mirrors
    heart.subtasks.SubtaskQueueFull on the subtask path.
    """


class DynamicCheckCancelled(RuntimeError):
    """A check's in-flight run was cancelled because the check was disabled.

    ``by_sibling_run`` is True when the disable came from ANOTHER run of the
    same check disabling itself: that run is the check's final run and owns
    the outcome, so this cancelled sibling must not be reported as a failed
    final run (codex P2, PR #656).
    """

    def __init__(self, message: str, *, by_sibling_run: bool = False) -> None:
        super().__init__(message)
        self.by_sibling_run = by_sibling_run


# The check-run task that owns the current task (and every task it spawns,
# since create_task copies the context). Lets a check disable ITSELF from a
# tool call without cancelling the very run that is making the call.
_CURRENT_CHECK_RUN: contextvars.ContextVar[asyncio.Task | None] = contextvars.ContextVar(
    "nous_current_check_run", default=None
)

# Per-run outcome slot set by the heartbeat runner around ``check.run()``.
# run() records ``final_run`` (did THIS run disable its own check) before it
# gives up ownership of its task, so the runner never infers it from the live
# ``_self_disabled`` flag after an await.
RUN_OUTCOME: contextvars.ContextVar[dict[str, bool] | None] = contextvars.ContextVar(
    "nous_check_run_outcome", default=None
)

# Tools allowed for dynamic checks.
# Note: bash is included per spec but could execute arbitrary commands;
# check creation is restricted to admin/conversation so risk is accepted.
# heartbeat_check_create/manage enable autonomous sequential pipelines:
# a check can spawn follow-up checks and disable itself when done.
ALLOWED_TOOLS = frozenset({
    "web_search", "web_fetch", "recall_deep", "recall_recent", "bash", "read_file",
    "heartbeat_check_create", "heartbeat_check_manage",
})

MIN_INTERVAL_SECONDS = 300  # 5 minutes minimum

# Metadata key stamped fresh by every enable/disable (Phase 2.8 revert guard).
_STATE_TOKEN_KEY = "enabled_state_token"


def _meta(model: Any) -> dict:
    return model.metadata_ if isinstance(model.metadata_, dict) else {}
CALLBACK_RETRY_DELAY_SECONDS = 30


class DynamicCheck(BaseCheck):
    """A prompt-driven heartbeat check loaded from DB."""

    def __init__(
        self,
        check_id: str,
        name: str,
        prompt: str,
        tools: list[str],
        interval: int = 3600,
        timeout: int = 30,
        urgent: bool = False,
        runner: AgentRunner | None = None,
        model_override: str | None = None,
        on_complete_prompt: str | None = None,
        on_complete_tools: list[str] | None = None,
        active_runs: dict[str, set[DynamicCheck]] | None = None,
    ) -> None:
        super().__init__()
        self.check_id = check_id
        self.name = name
        self._prompt = prompt
        self._tools = [t for t in tools if t in ALLOWED_TOOLS] if tools else []
        self.interval = interval
        self.timeout = timeout
        self.urgent_override = urgent
        self._runner = runner
        self._model_override = model_override
        self._cron_expr: str | None = None
        self.on_complete_prompt = on_complete_prompt
        self.on_complete_tools = [t for t in on_complete_tools if t in ALLOWED_TOOLS] if on_complete_tools else []
        self._self_disabled = False
        # In-flight run tracking (codex P1, PR #656): disabling a check must
        # stop a run that is already executing, not only prevent the next one.
        # ``active_runs`` is owned by the loader so it can reach a running
        # instance even after the registry dropped or replaced it.
        self._active_runs = active_runs
        # One entry per in-flight run (a REST trigger can overlap a tick), and
        # the subset whose cancellation came from a disable.
        self._run_tasks: set[asyncio.Task] = set()
        self._disable_cancelled: set[asyncio.Task] = set()
        # The subset cancelled because a sibling run disabled the check.
        self._sibling_cancelled: set[asyncio.Task] = set()

    def cancel_run(self, *, by_sibling_run: bool = False) -> bool:
        """Cancel this check's in-flight runs, if any. Returns True if any cancelled.

        A run never cancels itself: a check that disables itself from one of
        its own tool calls is finishing its final run, which must complete.
        ``by_sibling_run`` marks the cancelled runs as siblings of that final
        run, so they do not report the final run's outcome.
        """
        cancelled = False
        for task in list(self._run_tasks):
            if _CURRENT_CHECK_RUN.get() is task:
                # The initiating run may live on an instance an ``update``
                # already replaced (codex P1, PR #656 round 9): record the
                # self-disable here, or its final_run reads False.
                self._self_disabled = True
                continue
            # A done turn whose run() has not resumed yet is still an active
            # run (codex P1, PR #656 round 7): mark it too, or it would return
            # self_disabled=True and fire on_complete for a disable it did
            # not make. run() removes a task from _run_tasks once consumed.
            self._disable_cancelled.add(task)
            if by_sibling_run:
                self._sibling_cancelled.add(task)
            if not task.done():
                task.cancel()
            cancelled = True
        if cancelled:
            self._self_disabled = True
        return cancelled

    def set_cron(self, cron_expr: str | None) -> None:
        """Set cron expression for scheduling."""
        self._cron_expr = cron_expr

    def is_due(self, now: datetime | None = None) -> bool:
        """Check if due, supporting cron expressions."""
        if not self.active:
            return False
        if self.consecutive_failures >= self.max_failures:
            return False
        now = now or datetime.now(UTC)
        if self._cron_expr:
            anchor = self.last_run or datetime(2000, 1, 1, tzinfo=UTC)
            cron = croniter(self._cron_expr, anchor)
            next_fire = cron.get_next(datetime)
            return now >= next_fire
        return super().is_due(now)

    async def run(self) -> CheckResult:
        """Execute the check by running the prompt through the agent."""
        if self._runner is None:
            return CheckResult()
        # Re-verify state at the actual execution boundary. _tick has a
        # synchronous pre-check, but a concurrent DAG task can disable this
        # check between that check and the first await inside this coroutine.
        # Rechecking here is race-free within the coroutine (no awaits yet).
        # Return skipped=True so callers can distinguish a no-op from a real
        # successful run and avoid recording spurious success stats.
        if not self.active or self._self_disabled:
            return CheckResult(skipped=True)

        session_id = f"dynamic-check-{self.name}-{uuid4().hex[:8]}"
        has_pipeline_tools = bool(
            {"heartbeat_check_create", "heartbeat_check_manage"} & set(self._tools)
        )
        pipeline_section = ""
        if has_pipeline_tools:
            pipeline_section = (
                "\n\nYou can create follow-up checks with heartbeat_check_create "
                "and manage existing checks (enable/disable/delete/update) with "
                "heartbeat_check_manage. This lets you build sequential pipelines: "
                "spawn the next step as a new check, then disable yourself with "
                "heartbeat_check_manage(action='disable', name='" + self.name + "') "
                "when your task is complete."
            )
        instruction = (
            f"[Dynamic Heartbeat Check: {self.name}]\n"
            f"You are running a heartbeat check. Your job is to evaluate "
            f"whether there is anything worth reporting.\n\n"
            f"Instructions: {self._prompt}\n"
            f"{pipeline_section}\n\n"
            f"Respond with a JSON object:\n"
            f'{{"has_findings": bool, "findings": [{{"summary": "...", '
            f'"urgency": "high|normal|low", "needs_action": bool}}]}}\n\n'
            f"If nothing noteworthy, return: {{\"has_findings\": false, \"findings\": []}}"
        )

        run_task = asyncio.create_task(
            self._run_turn(session_id, instruction),
            name=f"dynamic-check-run-{self.name}",
        )
        self._run_tasks.add(run_task)
        if self._active_runs is not None:
            self._active_runs.setdefault(self.name, set()).add(self)
        try:
            try:
                response_text, _ctx, usage = await run_task
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if run_task in self._disable_cancelled and not (current is not None and current.cancelling()):
                    # Cancelled by a disable, not by our caller: surface it as
                    # a failed run so the runner records it and never reads it
                    # as success (the runner's own cancel still propagates).
                    logger.info("DynamicCheck '%s' cancelled mid-run: check disabled", self.name)
                    raise DynamicCheckCancelled(
                        f"check '{self.name}' was disabled while running",
                        by_sibling_run=run_task in self._sibling_cancelled,
                    ) from None
                raise
            except Exception:
                logger.exception("DynamicCheck '%s' failed", self.name)
                raise
            if run_task in self._disable_cancelled:
                # The turn swallowed the cancel and returned anyway: a run the
                # DAG terminated is still a failed run, never a completion.
                raise DynamicCheckCancelled(
                    f"check '{self.name}' was disabled while running",
                    by_sibling_run=run_task in self._sibling_cancelled,
                )

            findings = self._parse_findings(response_text or "")
            tokens = (usage or {}).get("input_tokens", 0) + (usage or {}).get("output_tokens", 0)
            return CheckResult(
                has_updates=bool(findings),
                findings=findings,
                tokens_used=tokens,
                self_disabled=self._self_disabled,
            )
        finally:
            # Decide here, while this run still owns run_task, whether it is
            # the check's self-disabling final run (codex P1, PR #656 round
            # 7). The caller must not re-read the live _self_disabled flag
            # later: a disable landing after this run ended (during the
            # awaits below or in the caller's stats write) is not this run's.
            outcome = RUN_OUTCOME.get()
            if outcome is not None:
                outcome["final_run"] = self._self_disabled and run_task not in self._sibling_cancelled
            if not run_task.done():
                run_task.cancel()
            self._run_tasks.discard(run_task)
            self._disable_cancelled.discard(run_task)
            self._sibling_cancelled.discard(run_task)
            if self._active_runs is not None and not self._run_tasks:
                runs = self._active_runs.get(self.name)
                if runs is not None:
                    runs.discard(self)
                    if not runs:
                        self._active_runs.pop(self.name, None)
            try:
                await self._runner.end_conversation(session_id)
            except Exception:
                pass

    async def _run_turn(self, session_id: str, instruction: str) -> tuple:
        """Run the agent turn inside this check's own run task."""
        assert self._runner is not None
        _CURRENT_CHECK_RUN.set(asyncio.current_task())
        return await self._runner.run_turn(
            session_id,
            instruction,
            platform="heartbeat",
            skip_episode=True,
            is_subtask=True,
            tool_filter=self._tools if self._tools else None,
            model_override=self._model_override,
            is_background=True,
            context=ExecutionContext(
                kind="heartbeat_check",
                session_id=session_id,
                declared_tools=tuple(self._tools) or None,
                check_name=self.name,
            ),
        )

    def _parse_findings(self, response: str) -> list[Finding]:
        """Extract findings from LLM JSON response."""
        from nous.handlers import parse_llm_json

        try:
            data = parse_llm_json(response)
        except (json.JSONDecodeError, ValueError):
            return []

        if not isinstance(data, dict) or not data.get("has_findings"):
            return []

        findings = []
        for item in data.get("findings", []):
            if not isinstance(item, dict):
                continue
            summary = item.get("summary", "").strip()
            if not summary:
                continue
            urgency = item.get("urgency", "normal")
            if urgency not in ("high", "normal", "low"):
                urgency = "normal"
            findings.append(Finding(
                source=f"dynamic:{self.name}",
                summary=summary[:200],
                urgency=urgency,
                needs_action=item.get("needs_action", False),
                raw_data={"check_id": self.check_id, "dynamic": True},
            ))

        return findings

    def signature(self) -> str:
        """Return a signature string for change detection."""
        return f"{self.name}|{self._prompt}|{self._tools}|{self.interval}|{self.timeout}|{self.urgent_override}|{self._cron_expr}|{self.on_complete_prompt}|{self.on_complete_tools}"


class DynamicCheckLoader:
    """Loads dynamic checks from DB and registers them in CheckRegistry."""

    def __init__(
        self,
        db: Database,
        registry: CheckRegistry,
        runner: AgentRunner | None = None,
        agent_id: str = "nous",
        max_checks: int = 10,
        model_override: str | None = None,
        default_timeout: int = 30,
    ) -> None:
        self._db = db
        self._registry = registry
        self._runner = runner
        self._agent_id = agent_id
        self._max_checks = max_checks
        self._model_override = model_override
        self._default_timeout = default_timeout
        self._loaded_ids: set[str] = set()
        self._id_to_name: dict[str, str] = {}
        self._signatures: dict[str, str] = {}  # name -> signature for change detection
        # name -> instances with a run in flight (maintained by DynamicCheck.run)
        self._active_runs: dict[str, set[DynamicCheck]] = {}
        # codex P1 (PR #656 round 8): sync() reconciles a DB snapshot against
        # _loaded_ids/_signatures, which create_check and manage_check mutate
        # from other tasks (the DAG loop, REST). Interleaved, a stale snapshot
        # unregisters a just-created check or resurrects a just-disabled one,
        # so sync, create and manage run one at a time.
        self._mutation_lock = asyncio.Lock()

    def _cancel_active_runs(self, name: str) -> None:
        """Cancel every in-flight run of ``name`` (a disabled check stops now)."""
        checks = list(self._active_runs.get(name, ()))
        # A run of ``name`` disabling its own check is that check's final run;
        # the runs cancelled here are its siblings, not the final run.
        initiator = _CURRENT_CHECK_RUN.get()
        by_sibling_run = initiator is not None and any(initiator in c._run_tasks for c in checks)
        for check in checks:
            if check.cancel_run(by_sibling_run=by_sibling_run):
                logger.info("F034.5: Cancelled in-flight run of disabled check '%s'", name)

    def set_runner(self, runner: AgentRunner) -> None:
        """Set the runner after construction (needed when runner is created in start())."""
        self._runner = runner
        # Update all existing checks with the new runner
        for name in list(self._id_to_name.values()):
            check = self._registry.get_check(name)
            if check and isinstance(check, DynamicCheck):
                check._runner = runner

    async def sync(self) -> int:
        """Load/reload dynamic checks from DB. Returns count of active checks."""
        async with self._mutation_lock:
            return await self._sync_locked()

    async def _sync_locked(self) -> int:
        """sync() body; the caller holds ``_mutation_lock``."""
        rows = await self._fetch_enabled()

        current_ids = {str(r.id) for r in rows}

        # Unregister removed/disabled checks
        for check_id in self._loaded_ids - current_ids:
            name = self._id_to_name.get(check_id)
            if name:
                self._registry.unregister(name)
                self._signatures.pop(name, None)
                self._cancel_active_runs(name)
                logger.info("F034.5: Unregistered dynamic check '%s'", name)

        # Register new/updated checks
        registered = 0
        for row in rows:
            check_id = str(row.id)
            name = row.name

            # Reject names that collide with permanent checks
            existing = self._registry.get_check(name)
            if existing and name in self._registry._permanent:
                logger.warning(
                    "F034.5: Skipping dynamic check '%s' — collides with permanent check", name,
                )
                continue

            check = DynamicCheck(
                check_id=check_id,
                name=name,
                prompt=row.prompt,
                tools=row.tools or [],
                interval=row.interval_seconds,
                timeout=row.timeout_seconds,
                urgent=row.urgent,
                runner=self._runner,
                model_override=self._model_override,
                on_complete_prompt=row.on_complete_prompt,
                on_complete_tools=row.on_complete_tools or [],
                active_runs=self._active_runs,
            )
            check.set_cron(row.cron_expr)

            # Skip re-registration if unchanged
            sig = check.signature()
            if name in self._signatures and self._signatures[name] == sig:
                registered += 1
                continue

            # Preserve runtime state from old check (P2-2 review fix)
            old_check = self._registry.get_check(name)
            if old_check and isinstance(old_check, DynamicCheck):
                check.last_run = old_check.last_run
                check.consecutive_failures = old_check.consecutive_failures

            self._registry.register(check, permanent=False)
            self._id_to_name[check_id] = name
            self._signatures[name] = sig
            registered += 1
            logger.info("F034.5: Registered dynamic check '%s' (interval=%ds)", name, check.interval)

        # Clean up id_to_name for removed checks
        removed_ids = self._loaded_ids - current_ids
        for check_id in removed_ids:
            self._id_to_name.pop(check_id, None)

        self._loaded_ids = current_ids
        return registered

    async def _fetch_enabled(self) -> list:
        """Fetch all enabled dynamic checks for this agent."""
        from nous.storage.models import DynamicCheckModel

        async with self._db.session() as session:
            result = await session.execute(
                select(DynamicCheckModel)
                .where(DynamicCheckModel.agent_id == self._agent_id)
                .where(DynamicCheckModel.enabled == True)  # noqa: E712
            )
            return list(result.scalars().all())

    async def update_run_stats(
        self, check_id: str, success: bool, error_msg: str | None = None,
    ) -> None:
        """Update run statistics in DB after a check execution."""
        from nous.storage.models import DynamicCheckModel

        async with self._db.session() as session:
            if success:
                await session.execute(
                    update(DynamicCheckModel)
                    .where(DynamicCheckModel.id == check_id)
                    .values(
                        run_count=DynamicCheckModel.run_count + 1,
                        last_run_at=datetime.now(UTC),
                        updated_at=datetime.now(UTC),
                    )
                )
            else:
                await session.execute(
                    update(DynamicCheckModel)
                    .where(DynamicCheckModel.id == check_id)
                    .values(
                        run_count=DynamicCheckModel.run_count + 1,
                        error_count=DynamicCheckModel.error_count + 1,
                        last_error=error_msg,
                        last_run_at=datetime.now(UTC),
                        updated_at=datetime.now(UTC),
                    )
                )
            await session.commit()

    async def get_successful_run_count(self, name: str) -> int | None:
        """Return a dynamic check's SUCCESSFUL run count, or None if no row exists.

        codex P1 (quiet-hours check-node fix, round 3): the DAG
        orchestrator needs evidence a check's heartbeat worker has
        actually COMPLETED a run before trusting a shell
        completion_check's "success" — raw run_count is not that
        signal, because update_run_stats() increments it on every
        execution attempt, success OR failure (error_count is bumped
        alongside it only on the failure branch — confirmed the sole
        writer of both columns anywhere in the codebase). A check
        whose first LLM turn errors would satisfy a bare run_count>0
        test despite never doing real work.

        run_count - error_count is exactly the count of successful
        completions, computed here rather than in the caller so a
        stale intermediate read can't desync the two.
        """
        from nous.storage.models import DynamicCheckModel

        async with self._db.session() as session:
            result = await session.execute(
                select(DynamicCheckModel.run_count - DynamicCheckModel.error_count)
                .where(DynamicCheckModel.agent_id == self._agent_id)
                .where(DynamicCheckModel.name == name)
            )
            return result.scalar_one_or_none()

    async def is_check_disabled(self, name: str) -> bool | None:
        """Return whether a dynamic check's row exists and is disabled
        (enabled=False), or None if no row exists at all.

        codex P2 round 4 (quiet-hours check-node fix): a SECOND,
        independent evidence source for _heartbeat_worker_has_run when
        the successful-run counter reads zero. manage_check(action=
        "disable") commits model.enabled = False BEFORE unregistering
        the check — a durable DB fact that survives independently of
        whether the immediately-following update_run_stats(success=
        True) write in HeartbeatRunner._tick/.trigger_check succeeds.
        A worker that completed, self-disabled, and then hit a
        transient stats-write failure (that write is wrapped in a
        bare try/except that logs and swallows) would otherwise be
        indistinguishable from one that never ran — and with no
        worker left registered to ever retry the stats write, the
        node would wedge until wall-clock timeout: the ORIGINAL bug
        this whole fix wave exists to close, arriving behind a rarer
        trigger.

        This is NOT the registry-absence signal Finding C removed.
        That was an in-memory proxy any cold restart or failed
        DynamicCheckLoader.sync() satisfies trivially — indistinguishable
        from a genuine disable with no way to tell them apart.
        enabled=False is a committed row that only manage_check(
        action="disable") writes anywhere in this codebase (confirmed:
        enable/disable are the sole writers of this column; delete
        removes the row instead of flipping it; no migration or sweep
        touches it) — narrower and durable in a way the in-memory
        signal never was.

        Returns None, not False, when the row is entirely absent
        (deleted) — that is NOT evidence of anything and must not be
        conflated with a deliberate disable.
        """
        from nous.storage.models import DynamicCheckModel

        async with self._db.session() as session:
            result = await session.execute(
                select(DynamicCheckModel.enabled)
                .where(DynamicCheckModel.agent_id == self._agent_id)
                .where(DynamicCheckModel.name == name)
            )
            enabled = result.scalar_one_or_none()
            if enabled is None:
                return None
            return not enabled

    async def create_check(
        self,
        name: str,
        description: str,
        prompt: str,
        tools: list[str] | None = None,
        interval_seconds: int = 3600,
        cron_expr: str | None = None,
        timeout_seconds: int | None = None,
        urgent: bool = False,
        on_complete_prompt: str | None = None,
        on_complete_tools: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a new dynamic check. Returns the check dict."""
        async with self._mutation_lock:
            return await self._create_check_locked(
                name,
                description,
                prompt,
                tools,
                interval_seconds,
                cron_expr,
                timeout_seconds,
                urgent,
                on_complete_prompt,
                on_complete_tools,
            )

    async def _create_check_locked(
        self,
        name: str,
        description: str,
        prompt: str,
        tools: list[str] | None = None,
        interval_seconds: int = 3600,
        cron_expr: str | None = None,
        timeout_seconds: int | None = None,
        urgent: bool = False,
        on_complete_prompt: str | None = None,
        on_complete_tools: list[str] | None = None,
    ) -> dict[str, Any]:
        """create_check() body; the caller holds ``_mutation_lock``."""
        if timeout_seconds is None:
            timeout_seconds = self._default_timeout
        from nous.storage.models import DynamicCheckModel

        # Validate required fields
        if not name or not name.strip():
            raise ValueError("Check name is required")
        if not prompt or not prompt.strip():
            raise ValueError("Check prompt is required")

        # Validate interval
        if interval_seconds < MIN_INTERVAL_SECONDS and not cron_expr:
            raise ValueError(f"Minimum interval is {MIN_INTERVAL_SECONDS} seconds")

        # Check max count
        current_count = len(self._loaded_ids)
        if current_count >= self._max_checks:
            raise DynamicCheckLimitReached(
                f"Maximum of {self._max_checks} dynamic checks reached"
            )

        # Validate cron expression
        if cron_expr:
            try:
                croniter(cron_expr)
            except (ValueError, KeyError) as e:
                raise ValueError(f"Invalid cron expression: {e}")

        # Filter tools to allowed set
        validated_tools = [t for t in (tools or []) if t in ALLOWED_TOOLS]

        # Validate on_complete_tools
        validated_on_complete_tools = [t for t in (on_complete_tools or []) if t in ALLOWED_TOOLS]
        if validated_on_complete_tools and not set(validated_on_complete_tools).issubset(set(validated_tools)):
            raise ValueError("on_complete_tools must be a subset of check tools")

        # Reject permanent name collisions
        existing = self._registry.get_check(name)
        if existing and name in self._registry._permanent:
            raise ValueError(f"Name '{name}' conflicts with a permanent check")

        async with self._db.session() as session:
            model = DynamicCheckModel(
                agent_id=self._agent_id,
                name=name,
                description=description,
                prompt=prompt,
                tools=validated_tools,
                cron_expr=cron_expr,
                interval_seconds=interval_seconds,
                timeout_seconds=timeout_seconds,
                urgent=urgent,
                on_complete_prompt=on_complete_prompt,
                on_complete_tools=validated_on_complete_tools,
            )
            session.add(model)
            await session.commit()
            await session.refresh(model)

            check_id = str(model.id)

        # Immediately register in registry (don't wait for next sync)
        check = DynamicCheck(
            check_id=check_id,
            name=name,
            prompt=prompt,
            tools=validated_tools,
            interval=interval_seconds,
            timeout=timeout_seconds,
            urgent=urgent,
            runner=self._runner,
            model_override=self._model_override,
            on_complete_prompt=on_complete_prompt,
            on_complete_tools=validated_on_complete_tools,
            active_runs=self._active_runs,
        )
        check.set_cron(cron_expr)
        self._registry.register(check, permanent=False)
        self._loaded_ids.add(check_id)
        self._id_to_name[check_id] = name
        self._signatures[name] = check.signature()

        return {
            "id": check_id,
            "name": name,
            "description": description,
            "interval_seconds": interval_seconds,
            "cron_expr": cron_expr,
            "tools": validated_tools,
            "urgent": urgent,
            "on_complete_prompt": on_complete_prompt,
            "on_complete_tools": validated_on_complete_tools,
        }

    async def manage_check(
        self, action: str, name: str | None = None, updates: dict | None = None,
        *, capture: dict | None = None,
    ) -> dict[str, Any]:
        """List, enable, disable, delete, or update a dynamic check.

        Every enable/disable stamps a fresh ``enabled_state_token`` in the
        row's metadata. ``capture``, when given to a disable, receives the
        check's prior ``enabled`` and the token this disable wrote, so a
        compensation revert can refuse once anyone has toggled it since
        (``enable_if_unchanged``).
        """
        async with self._mutation_lock:
            return await self._manage_check_locked(
                action, name, updates, capture=capture,
            )

    async def _manage_check_locked(
        self, action: str, name: str | None, updates: dict | None,
        *, capture: dict | None = None,
    ) -> dict[str, Any]:
        """manage_check() body; the caller holds ``_mutation_lock``."""
        from nous.storage.models import DynamicCheckModel

        if action == "list":
            return await self._list_checks()

        if not name:
            raise ValueError("Name required for action: " + action)

        async with self._db.session() as session:
            result = await session.execute(
                select(DynamicCheckModel)
                .where(DynamicCheckModel.agent_id == self._agent_id)
                .where(DynamicCheckModel.name == name)
            )
            model = result.scalar_one_or_none()
            if model is None:
                raise ValueError(f"Dynamic check '{name}' not found")

            if action == "enable":
                model.enabled = True
                model.metadata_ = {**_meta(model), _STATE_TOKEN_KEY: uuid4().hex}
                model.updated_at = datetime.now(UTC)
                await session.commit()
                await self._sync_locked()
                return {"status": "enabled", "name": name}

            elif action == "disable":
                prior_enabled = model.enabled
                token = uuid4().hex
                model.enabled = False
                model.metadata_ = {**_meta(model), _STATE_TOKEN_KEY: token}
                model.updated_at = datetime.now(UTC)
                await session.commit()
                if capture is not None:
                    capture["prior_enabled"] = prior_enabled
                    capture["written"] = {"check_id": str(model.id), _STATE_TOKEN_KEY: token}
                # codex P2 (PR #656): flag the in-memory check only once the
                # disable is durable. Flagging before the commit left a failed
                # commit with an enabled, registered check whose run() skips
                # forever (sync() keeps the instance: same signature).
                existing = self._registry.get_check(name)
                if existing and isinstance(existing, DynamicCheck):
                    existing._self_disabled = True
                self._registry.unregister(name)
                self._signatures.pop(name, None)
                # codex P1 (PR #656): a DAG reap/cancel lands here while the
                # check may already be mid-run; stop that run instead of
                # letting it keep calling tools until its own timeout.
                self._cancel_active_runs(name)
                check_id = str(model.id)
                self._loaded_ids.discard(check_id)
                self._id_to_name.pop(check_id, None)
                return {"status": "disabled", "name": name}

            elif action == "delete":
                check_id = str(model.id)
                await session.delete(model)
                await session.commit()
                self._registry.unregister(name)
                self._signatures.pop(name, None)
                self._cancel_active_runs(name)
                self._loaded_ids.discard(check_id)
                self._id_to_name.pop(check_id, None)
                return {"status": "deleted", "name": name}

            elif action == "update":
                if not updates:
                    raise ValueError("No updates provided")
                allowed_fields = {
                    "description", "prompt", "tools", "interval_seconds",
                    "cron_expr", "timeout_seconds", "urgent",
                    "on_complete_prompt", "on_complete_tools",
                }
                for key, value in updates.items():
                    if key not in allowed_fields:
                        continue
                    # Type validation
                    if key in ("interval_seconds", "timeout_seconds") and not isinstance(value, int):
                        raise ValueError(f"{key} must be an integer")
                    if key == "tools" and not isinstance(value, list):
                        raise ValueError("tools must be a list")
                    if key == "urgent" and not isinstance(value, bool):
                        raise ValueError("urgent must be a boolean")
                    if key == "on_complete_tools" and not isinstance(value, list):
                        raise ValueError("on_complete_tools must be a list")
                    if key == "tools":
                        value = [t for t in value if t in ALLOWED_TOOLS]
                    if key == "on_complete_tools":
                        value = [t for t in value if t in ALLOWED_TOOLS]
                    if key == "interval_seconds" and value < MIN_INTERVAL_SECONDS:
                        raise ValueError(f"Minimum interval is {MIN_INTERVAL_SECONDS} seconds")
                    setattr(model, key, value)
                # If cron_expr was removed, validate interval is still >= minimum
                if updates.get("cron_expr") is None and model.cron_expr is None:
                    if model.interval_seconds < MIN_INTERVAL_SECONDS:
                        raise ValueError(
                            f"Minimum interval is {MIN_INTERVAL_SECONDS} seconds "
                            f"(current: {model.interval_seconds}s) — set a valid interval or cron expression"
                        )
                # Re-validate on_complete_tools subset after all updates
                current_tools = set(model.tools or [])
                current_on_complete = set(model.on_complete_tools or [])
                if current_on_complete and not current_on_complete.issubset(current_tools):
                    raise ValueError("on_complete_tools must be a subset of check tools")
                model.updated_at = datetime.now(UTC)
                await session.commit()
                await self._sync_locked()
                return {"status": "updated", "name": name}

            else:
                raise ValueError(f"Unknown action: {action}")

    async def enable_if_unchanged(self, name: str, check_id: str, token: str) -> bool:
        """Re-enable check ``name`` only while it is still the disabled row
        ``check_id`` carrying ``token`` -- the state one disable left. Any
        later enable/disable (or delete and re-create) changes that, and the
        conditional update then touches nothing. Returns whether it enabled."""
        from uuid import UUID

        from nous.storage.models import DynamicCheckModel

        async with self._db.session() as session:
            result = await session.execute(
                update(DynamicCheckModel)
                .where(DynamicCheckModel.agent_id == self._agent_id)
                .where(DynamicCheckModel.name == name)
                .where(DynamicCheckModel.id == UUID(check_id))
                .where(DynamicCheckModel.enabled == False)  # noqa: E712
                .where(DynamicCheckModel.metadata_[_STATE_TOKEN_KEY].astext == token)
                .values(
                    enabled=True,
                    metadata_=DynamicCheckModel.metadata_.op("||")(
                        func.jsonb_build_object(_STATE_TOKEN_KEY, uuid4().hex)
                    ),
                    updated_at=datetime.now(UTC),
                )
            )
            await session.commit()
        if (result.rowcount or 0) != 1:
            return False
        await self.sync()
        return True

    async def _list_checks(self) -> dict[str, Any]:
        """List all dynamic checks with status."""
        from nous.storage.models import DynamicCheckModel

        async with self._db.session() as session:
            result = await session.execute(
                select(DynamicCheckModel)
                .where(DynamicCheckModel.agent_id == self._agent_id)
                .order_by(DynamicCheckModel.created_at)
            )
            rows = list(result.scalars().all())

        checks = []
        for row in rows:
            registry_check = self._registry.get_check(row.name)
            checks.append({
                "name": row.name,
                "description": row.description,
                "enabled": row.enabled,
                "interval_seconds": row.interval_seconds,
                "cron_expr": row.cron_expr,
                "urgent": row.urgent,
                "tools": row.tools or [],
                "run_count": row.run_count,
                "error_count": row.error_count,
                "last_error": row.last_error,
                "last_run_at": row.last_run_at.isoformat() if row.last_run_at else None,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "created_by": row.created_by,
                "on_complete_prompt": (row.on_complete_prompt or "")[:200] if row.on_complete_prompt else None,
                "on_complete_tools": row.on_complete_tools or [],
                "circuit_breaker_open": (
                    registry_check.consecutive_failures >= registry_check.max_failures
                    if registry_check else False
                ),
            })

        return {"checks": checks, "count": len(checks), "max": self._max_checks}
