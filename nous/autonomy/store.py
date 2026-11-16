"""F115: the owner's autonomy settings, stored with a record of each change and applied in place.

``AutonomyStore`` is built once, at start, from the shared ``Settings`` before anything reads a limit: it keeps the
host's value of each limit (``host``), lowered to the host's ceiling where it is above it (``baseline``, one warning
per key). ``load`` then applies the owner's stored values in one pass and installs the tool guard's snapshot. A
change (``set_limit``, ``clear_limit``, ``set_tool``, ``clear_tool``) is checked, written with its record in one
transaction, and only then applied: the limit set in place on the shared ``Settings`` (as ``PUT /heartbeat/config``
does), the tool guard's snapshot replaced. Changes run one at a time (an ``asyncio.Lock``); a reader never takes the
lock, and reads the old value until the change has been committed. The write and the apply are one unit: a request
cancelled meanwhile does not stop it (``asyncio.shield``), and once the commit has returned the apply runs whatever
is raised after it. A change to the value already saved writes nothing.

A stored value is checked again when it is loaded (the registry's range, the host's ceiling and the cross rule): one
that fails is skipped with a warning naming its key, and the limit stays the host's. Only scope ``background`` with
the settings ``allowed`` and ``denied`` is read or written; an ``allowed`` row changes nothing (the guard only takes
tools away). Nothing else in the code writes these tables.

When the saved settings cannot be read at start, the store is not loaded (``loaded`` is False): every limit is the
host's (lowered to its ceiling), the tool guard holds every tool for background work (``ToolGuard.hold_all``), a
change is refused, and the store reads again in the background, from ``RETRY_FIRST_SECONDS`` doubling to
``RETRY_MAX_SECONDS``, until it can; then it applies what it read as ``load`` does.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from nous.autonomy import tool_guard
from nous.autonomy.registry import LIMITS, by_key, ceiling_problem, cross_problem, parse_ceilings, range_problem
from nous.autonomy.tool_guard import HARNESS_TOOLS, ToolGuard, parse_locked_tools
from nous.storage.models import AutonomySetting, AutonomySettingLog, ToolGuardRow

logger = logging.getLogger(__name__)

__all__ = [
    "ASK_NOT_AVAILABLE",
    "BACKGROUND",
    "GUARD_NOT_AVAILABLE",
    "LOG_KINDS",
    "LOG_MAX",
    "NOT_LOADED_WRITE",
    "SCOPE_NOT_AVAILABLE",
    "SCOPE_UNKNOWN",
    "SETTING_NOT_AVAILABLE",
    "TOOL_SETTINGS",
    "AutonomyRefused",
    "AutonomyStore",
    "Change",
    "tool_key",
]

BACKGROUND = "background"
# The settings of a tool the owner may choose now.
TOOL_SETTINGS: tuple[str, ...] = ("allowed", "denied")
LOG_SHOWN = 20
NOT_AVAILABLE = "not available yet"
# The fixed answers to what the table can hold and this version does not offer yet.
ASK_NOT_AVAILABLE = f"Ask each time is {NOT_AVAILABLE}. A tool is Allowed or Not allowed."
SETTING_NOT_AVAILABLE = f"That setting is {NOT_AVAILABLE}. A tool is Allowed or Not allowed."
SCOPE_NOT_AVAILABLE = f"Settings for goal sessions are {NOT_AVAILABLE}. A tool's setting applies to background work."
SCOPE_UNKNOWN = "That scope is not known. A tool's setting applies to background work."
GOAL_SESSION = "goal_session"
GUARD_NOT_AVAILABLE = f"The destructive-command guard is {NOT_AVAILABLE}."
# While the saved settings could not be read.
NOT_LOADED_TOOLS = "The saved settings could not be read; background work is held from tools until they can."
NOT_LOADED_LIMITS = "Every limit is the server's own value until then."
NOT_LOADED_LIMITS_ONLY = "The saved settings could not be read; every limit is the server's own value until they can."
NOT_LOADED_WRITE = "The saved settings could not be read, so nothing can be changed until they can."
RETRY_FIRST_SECONDS = 5.0
RETRY_MAX_SECONDS = 300.0
LOG_MAX = 100
# What GET /autonomy/log may list: every change, the limits' or the tools'.
LOG_KINDS: tuple[str, ...] = ("all", "limits", "tools")
TOOL_KEY_PREFIX = "tool_guard:"
# The wait between two reads of the saved settings (its own name, so a test can replace it alone).
_sleep = asyncio.sleep


class AutonomyRefused(Exception):
    """A change the store refuses: ``status`` is the HTTP status the route answers, the text the sentence it says."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Change:
    """One record of a change: the key, the value before and after (None: the host's value or the default), who made
    it and when."""

    key: str
    old: Any
    new: Any
    actor: str
    at: datetime | None


def tool_key(tool: str, scope: str = BACKGROUND) -> str:
    """The record key of a tool's setting: ``tool_guard:<scope>:<tool>``."""
    return f"tool_guard:{scope}:{tool}"


class AutonomyStore:
    """One agent's autonomy settings over its ``Settings`` object (see the module docstring)."""

    def __init__(self, database: Any, settings: Any) -> None:
        self._db = database
        self._settings = settings
        self._agent_id: str = settings.agent_id
        self._lock = asyncio.Lock()
        # The host's values, read before anything is applied.
        self.host: dict[str, Any] = {limit.key: getattr(settings, limit.key) for limit in LIMITS}
        self.ceilings: dict[str, int] = parse_ceilings(settings.autonomy_ceilings)
        self.baseline: dict[str, Any] = dict(self.host)
        self.clamped: frozenset[str] = frozenset(
            key for key, ceiling in self.ceilings.items() if self.host[key] > ceiling
        )
        for key in sorted(self.clamped):
            self.baseline[key] = self.ceilings[key]
            logger.warning(
                "F115: %s from the environment (%s) is above its ceiling in NOUS_AUTONOMY_CEILINGS; %s is used",
                key,
                self.host[key],
                self.ceilings[key],
            )
        # The owner's values now applied, by key.
        self.owner: dict[str, int] = {}
        self.locked: frozenset[str] = (
            parse_locked_tools(settings.autonomy_locked_tools) if settings.tool_guard_enabled else frozenset()
        )
        # The owner's settings of tools for background work, by tool.
        self.tools: dict[str, str] = {}
        # Whether the saved settings were read (False until load reads them, and while they cannot be).
        self.loaded = False
        self._retry: asyncio.Task[None] | None = None

    # ---- applying ------------------------------------------------------------------------------------------------

    def _coerce(self, key: str, value: Any) -> Any:
        """``value`` with the type of the host's value (the two hour limits are floats)."""
        return float(value) if isinstance(self.host[key], float) else int(value)

    def _set(self, key: str, value: Any) -> None:
        object.__setattr__(self._settings, key, self._coerce(key, value))

    def effective(self) -> dict[str, Any]:
        """Each limit's value now, as the readers resolve it."""
        return {limit.key: getattr(self._settings, limit.key) for limit in LIMITS}

    def source(self, key: str) -> str:
        """``owner`` for the owner's value, ``clamped`` for a host value lowered to its ceiling, else ``host``."""
        if key in self.owner:
            return "owner"
        return "clamped" if key in self.clamped else "host"

    def _install_guard(self) -> None:
        if self._settings.tool_guard_enabled:
            denied = frozenset(tool for tool, setting in self.tools.items() if setting == "denied")
            tool_guard.install(ToolGuard(denied=denied, locked=self.locked, hold_all=not self.loaded))

    def _require_loaded(self) -> None:
        if not self.loaded:
            raise AutonomyRefused(503, NOT_LOADED_WRITE)

    def not_loaded_message(self) -> str:
        """What the screen says while the saved settings could not be read."""
        if self._settings.tool_guard_enabled:
            return f"{NOT_LOADED_TOOLS} {NOT_LOADED_LIMITS}"
        return NOT_LOADED_LIMITS_ONLY

    async def load(self) -> None:
        """At start: apply the clamped host values and the owner's stored limits in one pass, and install the tool
        guard's snapshot. Off (``NOUS_AUTONOMY_SETTINGS_ENABLED``): nothing is read or changed. A stored value that
        fails a check is skipped with a warning. When the rows cannot be read the store is not loaded: the limits are
        the host's (lowered to its ceilings), the guard holds every tool for background work, and the rows are read
        again in the background until they can be."""
        if not self._settings.autonomy_settings_enabled:
            return
        try:
            rows, tool_rows = await self._read()
        except Exception as exc:
            self._apply([], [], loaded=False)
            # The class only: an error's text can repeat what it was given.
            logger.error(
                "F115: the saved autonomy settings could not be read (%s): %s; reading them again in the background",
                type(exc).__name__,
                "background work is held from every tool and every limit is the host's"
                if self._settings.tool_guard_enabled
                else "every limit is the host's",
            )
            self._retry = asyncio.create_task(self._read_again(), name="autonomy-settings-read")
            return
        self._apply(rows, tool_rows, loaded=True)

    async def close(self) -> None:
        """Stop reading again in the background (at shutdown)."""
        task, self._retry = self._retry, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _read_again(self) -> None:
        delay = RETRY_FIRST_SECONDS
        while True:
            await _sleep(delay)
            try:
                rows, tool_rows = await self._read()
            except Exception as exc:
                delay = min(delay * 2, RETRY_MAX_SECONDS)
                logger.warning(
                    "F115: the saved autonomy settings could still not be read (%s); trying again in %d s",
                    type(exc).__name__,
                    int(delay),
                )
                continue
            async with self._lock:
                self._apply(rows, tool_rows, loaded=True)
            logger.info("F115: the saved autonomy settings were read; the owner's limits and tool settings apply")
            return

    async def _read(self) -> tuple[list[tuple[str, Any]], list[tuple[str, str]]]:
        """The stored limits and, with the tool guard on, the stored tool settings of scope ``background``."""
        tool_rows: list[tuple[str, str]] = []
        async with self._db.session() as session:
            rows = [
                (row.key, row.value)
                for row in (
                    await session.execute(
                        select(AutonomySetting.key, AutonomySetting.value).where(
                            AutonomySetting.agent_id == self._agent_id
                        )
                    )
                ).all()
            ]
            if self._settings.tool_guard_enabled:
                tool_rows = [
                    (row.tool, row.setting)
                    for row in (
                        await session.execute(
                            select(ToolGuardRow.tool, ToolGuardRow.setting).where(
                                ToolGuardRow.agent_id == self._agent_id, ToolGuardRow.scope == BACKGROUND
                            )
                        )
                    ).all()
                ]
        return rows, tool_rows

    def _apply(self, rows: list[tuple[str, Any]], tool_rows: list[tuple[str, str]], *, loaded: bool) -> None:
        """Check and apply what was read in one pass (no await): the clamped host values and each stored limit that
        passes, then the tools; ``loaded`` False installs a guard that holds every tool."""
        owner: dict[str, int] = {}
        for key, value in rows:
            limit = by_key(key)
            if limit is None:
                logger.warning("F115: a stored autonomy setting has no limit of that name (%.40s); skipped", key)
                continue
            if range_problem(limit, value) is not None or ceiling_problem(limit, value, self.ceilings.get(key)):
                logger.warning("F115: the stored value of %s is outside its range or ceiling; skipped", key)
                continue
            owner[key] = value
        problem = cross_problem({**self.baseline, **owner}, changed=owner)
        if problem is not None:
            logger.warning(
                "F115: the stored subtask_default_timeout and subtask_max_timeout break the cross rule; both skipped"
            )
            owner.pop("subtask_default_timeout", None)
            owner.pop("subtask_max_timeout", None)
        values = {key: self.baseline[key] for key in self.clamped}
        values.update({key: self.baseline[key] for key in self.owner if key not in owner})
        values.update(owner)
        for key, value in values.items():
            self._set(key, value)
        self.owner = owner
        self.tools = {}
        for tool, setting in tool_rows:
            if setting not in TOOL_SETTINGS or tool in HARNESS_TOOLS:
                logger.warning(
                    "F115: the stored setting of the tool %.200s is not one this version reads; skipped", tool
                )
                continue
            self.tools[tool] = setting
        self.loaded = loaded
        self._install_guard()

    # ---- limits ------------------------------------------------------------------------------------------------

    def _limit(self, key: str) -> Any:
        limit = by_key(key)
        if limit is None:
            raise AutonomyRefused(404, f"There is no limit named {key[:40]!r}.")
        return limit

    async def set_limit(self, key: str, value: Any, actor: str) -> bool:
        """The owner's value of ``key``: refused (400) outside the range, (409) above the host's ceiling or when the
        cross rule fails, (503) while the saved settings could not be read; else written with its record in one
        transaction, then applied. False, and nothing written, when it is the value already saved."""
        limit = self._limit(key)
        self._require_loaded()
        problem = range_problem(limit, value)
        if problem is not None:
            raise AutonomyRefused(400, problem)
        problem = ceiling_problem(limit, value, self.ceilings.get(key))
        if problem is not None:
            raise AutonomyRefused(409, problem)
        return await asyncio.shield(self._set_limit(key, value, actor))

    async def _set_limit(self, key: str, value: Any, actor: str) -> bool:
        async with self._lock:
            self._require_loaded()
            problem = cross_problem({**self.effective(), key: value}, changed=[key])
            if problem is not None:
                raise AutonomyRefused(409, problem)
            committed = False
            try:
                async with self._db.session() as session:
                    old = await self._locked_row(session, key)
                    if old is not None and old == value and self.owner.get(key) == value:
                        await session.rollback()
                        return False
                    await session.execute(
                        pg_insert(AutonomySetting)
                        .values(agent_id=self._agent_id, key=key, value=value, updated_by=actor)
                        .on_conflict_do_update(
                            index_elements=[AutonomySetting.agent_id, AutonomySetting.key],
                            set_={"value": value, "updated_by": actor, "updated_at": func.now()},
                        )
                    )
                    session.add(self._record(key, old, value, actor))
                    await session.commit()
                    committed = True
            finally:
                if committed:
                    self._set(key, value)
                    self.owner[key] = value
            return True

    async def clear_limit(self, key: str, actor: str) -> bool:
        """Back to the host's value: the row deleted with its record in one transaction, then the baseline applied.
        False (and no record) when the owner had set nothing; 409 when the host's value would break the cross rule;
        503 while the saved settings could not be read."""
        self._limit(key)
        self._require_loaded()
        return await asyncio.shield(self._clear_limit(key, actor))

    async def _clear_limit(self, key: str, actor: str) -> bool:
        async with self._lock:
            self._require_loaded()
            problem = cross_problem({**self.effective(), key: self.baseline[key]}, changed=[key])
            if problem is not None:
                raise AutonomyRefused(409, problem)
            committed = False
            try:
                async with self._db.session() as session:
                    old = await self._locked_row(session, key)
                    if old is None:
                        await session.rollback()
                        return False
                    await session.execute(
                        delete(AutonomySetting).where(
                            AutonomySetting.agent_id == self._agent_id, AutonomySetting.key == key
                        )
                    )
                    session.add(self._record(key, old, None, actor))
                    await session.commit()
                    committed = True
            finally:
                if committed:
                    self._set(key, self.baseline[key])
                    self.owner.pop(key, None)
            return True

    async def _locked_row(self, session: Any, key: str) -> Any:
        """The stored value of ``key``, its row locked for this transaction; None when there is none."""
        return (
            await session.execute(
                select(AutonomySetting.value)
                .where(AutonomySetting.agent_id == self._agent_id, AutonomySetting.key == key)
                .with_for_update()
            )
        ).scalar_one_or_none()

    def _record(self, key: str, old: Any, new: Any, actor: str) -> AutonomySettingLog:
        return AutonomySettingLog(agent_id=self._agent_id, key=key, old_value=old, new_value=new, actor=actor)

    # ---- tools ------------------------------------------------------------------------------------------------

    def _tool_refusal(self, tool: str) -> None:
        if tool in HARNESS_TOOLS:
            raise AutonomyRefused(400, f"{tool} is part of how a turn ends and cannot be switched off.")
        if tool in self.locked:
            raise AutonomyRefused(409, f"{tool} is locked by the server and cannot be changed here.")

    @staticmethod
    def _scope_refusal(scope: str, destructive_guard: Any = None) -> None:
        if scope == GOAL_SESSION:
            raise AutonomyRefused(400, SCOPE_NOT_AVAILABLE)
        if scope != BACKGROUND:
            raise AutonomyRefused(400, SCOPE_UNKNOWN)
        if destructive_guard is not None:
            raise AutonomyRefused(400, GUARD_NOT_AVAILABLE)

    async def set_tool(
        self, tool: str, setting: str, actor: str, *, scope: str = BACKGROUND, destructive_guard: Any = None
    ) -> bool:
        """The owner's setting of ``tool`` for background work: ``allowed`` or ``denied``, written with its record in
        one transaction, then the guard's snapshot replaced. 400 with a fixed sentence for another scope, a destructive
        guard, ``ask`` or another word (not available yet); 409 for a tool the host locked; 503 while the saved
        settings could not be read. False, and nothing written, when it is the setting already saved."""
        self._scope_refusal(scope, destructive_guard)
        if setting not in TOOL_SETTINGS:
            raise AutonomyRefused(400, ASK_NOT_AVAILABLE if setting == "ask" else SETTING_NOT_AVAILABLE)
        self._tool_refusal(tool)
        self._require_loaded()
        return await asyncio.shield(self._set_tool(tool, setting, actor))

    async def _set_tool(self, tool: str, setting: str, actor: str) -> bool:
        async with self._lock:
            self._require_loaded()
            committed = False
            try:
                async with self._db.session() as session:
                    old = await self._locked_tool(session, tool)
                    if old == setting and self.tools.get(tool) == setting:
                        await session.rollback()
                        return False
                    await session.execute(
                        pg_insert(ToolGuardRow)
                        .values(agent_id=self._agent_id, scope=BACKGROUND, tool=tool, setting=setting, updated_by=actor)
                        .on_conflict_do_update(
                            index_elements=[ToolGuardRow.agent_id, ToolGuardRow.scope, ToolGuardRow.tool],
                            set_={"setting": setting, "updated_by": actor, "updated_at": func.now()},
                        )
                    )
                    session.add(self._record(tool_key(tool), old, setting, actor))
                    await session.commit()
                    committed = True
            finally:
                if committed:
                    self.tools[tool] = setting
                    self._install_guard()
            return True

    async def clear_tool(
        self, tool: str, actor: str, *, scope: str = BACKGROUND, destructive_guard: Any = None
    ) -> bool:
        """Back to the default (allowed): the row deleted with its record, then the snapshot replaced. False (and no
        record) when the owner had set nothing. 400 with a fixed sentence for another scope or a destructive guard
        (not available yet); 503 while the saved settings could not be read."""
        self._scope_refusal(scope, destructive_guard)
        self._tool_refusal(tool)
        self._require_loaded()
        return await asyncio.shield(self._clear_tool(tool, actor))

    async def _clear_tool(self, tool: str, actor: str) -> bool:
        async with self._lock:
            self._require_loaded()
            committed = False
            try:
                async with self._db.session() as session:
                    old = await self._locked_tool(session, tool)
                    if old is None:
                        await session.rollback()
                        return False
                    await session.execute(
                        delete(ToolGuardRow).where(
                            ToolGuardRow.agent_id == self._agent_id,
                            ToolGuardRow.scope == BACKGROUND,
                            ToolGuardRow.tool == tool,
                        )
                    )
                    session.add(self._record(tool_key(tool), old, None, actor))
                    await session.commit()
                    committed = True
            finally:
                if committed:
                    self.tools.pop(tool, None)
                    self._install_guard()
            return True

    async def _locked_tool(self, session: Any, tool: str) -> str | None:
        return (
            await session.execute(
                select(ToolGuardRow.setting)
                .where(
                    ToolGuardRow.agent_id == self._agent_id,
                    ToolGuardRow.scope == BACKGROUND,
                    ToolGuardRow.tool == tool,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()

    # ---- the record --------------------------------------------------------------------------------------------

    async def changes(self, limit: int = LOG_SHOWN, kind: str = "all") -> list[Change]:
        """The newest ``limit`` changes of this agent (at most ``LOG_MAX``), newest first: every change, the limits'
        (``kind="limits"``) or the tools' (``kind="tools"``)."""
        where = [AutonomySettingLog.agent_id == self._agent_id]
        if kind == "limits":
            where.append(AutonomySettingLog.key.not_like(f"{TOOL_KEY_PREFIX}%"))
        elif kind == "tools":
            where.append(AutonomySettingLog.key.like(f"{TOOL_KEY_PREFIX}%"))
        async with self._db.session() as session:
            rows = (
                await session.execute(
                    select(AutonomySettingLog)
                    .where(*where)
                    .order_by(AutonomySettingLog.id.desc())
                    .limit(max(1, min(limit, LOG_MAX)))
                )
            ).scalars()
            return [Change(row.key, row.old_value, row.new_value, row.actor, row.created_at) for row in rows]

    async def last_changes(self, keys: Iterable[str]) -> dict[str, Change]:
        """The newest change of each of ``keys`` that has one."""
        wanted = list(keys)
        if not wanted:
            return {}
        newest = (
            select(func.max(AutonomySettingLog.id))
            .where(AutonomySettingLog.agent_id == self._agent_id, AutonomySettingLog.key.in_(wanted))
            .group_by(AutonomySettingLog.key)
        )
        async with self._db.session() as session:
            rows = (
                await session.execute(select(AutonomySettingLog).where(AutonomySettingLog.id.in_(newest)))
            ).scalars()
            return {row.key: Change(row.key, row.old_value, row.new_value, row.actor, row.created_at) for row in rows}
