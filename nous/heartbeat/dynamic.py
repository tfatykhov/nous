"""Dynamic heartbeat checks (F034.5).

Prompt-driven checks loaded from DB, running alongside permanent checks.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from croniter import croniter
from sqlalchemy import select, update

from nous.heartbeat.registry import BaseCheck
from nous.heartbeat.schemas import CheckResult, Finding

if TYPE_CHECKING:
    from nous.api.runner import AgentRunner
    from nous.heartbeat.registry import CheckRegistry
    from nous.storage.database import Database

logger = logging.getLogger(__name__)

# Tools allowed for dynamic checks (sensors only, no state mutation)
# Note: bash is included per spec but could execute arbitrary commands;
# check creation is restricted to admin/conversation so risk is accepted.
ALLOWED_TOOLS = frozenset({
    "web_search", "web_fetch", "recall_deep", "recall_recent", "bash", "read_file",
})

MIN_INTERVAL_SECONDS = 300  # 5 minutes minimum


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

        session_id = f"dynamic-check-{self.name}-{uuid4().hex[:8]}"
        instruction = (
            f"[Dynamic Heartbeat Check: {self.name}]\n"
            f"You are running a heartbeat check. Your job is to evaluate "
            f"whether there is anything worth reporting.\n\n"
            f"Instructions: {self._prompt}\n\n"
            f"Respond with a JSON object:\n"
            f'{{"has_findings": bool, "findings": [{{"summary": "...", '
            f'"urgency": "high|normal|low", "needs_action": bool}}]}}\n\n'
            f"If nothing noteworthy, return: {{\"has_findings\": false, \"findings\": []}}"
        )

        try:
            response_text, _ctx, usage = await self._runner.run_turn(
                session_id, instruction,
                platform="heartbeat",
                skip_episode=True,
                is_subtask=True,
                tool_filter=self._tools if self._tools else None,
                model_override=self._model_override,
            )

            findings = self._parse_findings(response_text or "")
            tokens = (usage or {}).get("input_tokens", 0) + (usage or {}).get("output_tokens", 0)
            return CheckResult(
                has_updates=bool(findings),
                findings=findings,
                tokens_used=tokens,
            )
        except Exception:
            logger.exception("DynamicCheck '%s' failed", self.name)
            raise
        finally:
            try:
                await self._runner.end_conversation(session_id)
            except Exception:
                pass

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
        return f"{self.name}|{self._prompt}|{self._tools}|{self.interval}|{self.timeout}|{self.urgent_override}|{self._cron_expr}"


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
        rows = await self._fetch_enabled()

        current_ids = {str(r.id) for r in rows}

        # Unregister removed/disabled checks
        for check_id in self._loaded_ids - current_ids:
            name = self._id_to_name.get(check_id)
            if name:
                self._registry.unregister(name)
                self._signatures.pop(name, None)
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
    ) -> dict[str, Any]:
        """Create a new dynamic check. Returns the check dict."""
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
            raise ValueError(f"Maximum of {self._max_checks} dynamic checks reached")

        # Validate cron expression
        if cron_expr:
            try:
                croniter(cron_expr)
            except (ValueError, KeyError) as e:
                raise ValueError(f"Invalid cron expression: {e}")

        # Filter tools to allowed set
        validated_tools = [t for t in (tools or []) if t in ALLOWED_TOOLS]

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
        }

    async def manage_check(
        self, action: str, name: str | None = None, updates: dict | None = None,
    ) -> dict[str, Any]:
        """List, enable, disable, delete, or update a dynamic check."""
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
                model.updated_at = datetime.now(UTC)
                await session.commit()
                await self.sync()
                return {"status": "enabled", "name": name}

            elif action == "disable":
                model.enabled = False
                model.updated_at = datetime.now(UTC)
                await session.commit()
                self._registry.unregister(name)
                self._signatures.pop(name, None)
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
                self._loaded_ids.discard(check_id)
                self._id_to_name.pop(check_id, None)
                return {"status": "deleted", "name": name}

            elif action == "update":
                if not updates:
                    raise ValueError("No updates provided")
                allowed_fields = {
                    "description", "prompt", "tools", "interval_seconds",
                    "cron_expr", "timeout_seconds", "urgent",
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
                    if key == "tools":
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
                model.updated_at = datetime.now(UTC)
                await session.commit()
                await self.sync()
                return {"status": "updated", "name": name}

            else:
                raise ValueError(f"Unknown action: {action}")

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
                "circuit_breaker_open": (
                    registry_check.consecutive_failures >= registry_check.max_failures
                    if registry_check else False
                ),
            })

        return {"checks": checks, "count": len(checks), "max": self._max_checks}
