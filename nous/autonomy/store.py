"""F115: the owner's autonomy settings, stored with a record of each change and applied in place.

``AutonomyStore`` is built once, at start, from the shared ``Settings`` before anything reads a limit: it keeps the
host's value of each limit (``host``), lowered to the host's ceiling where it is above it (``baseline``, one warning
per key). ``load`` then applies the owner's stored values in one pass and installs the tool guard's snapshot. A
change (``set_limit``, ``clear_limit``, ``set_tool``, ``clear_tool``) is checked, written with its record in one
transaction, and only then applied: the limit set in place on the shared ``Settings`` (as ``PUT /heartbeat/config``
does), the tool guard's snapshot replaced. Changes run one at a time (an ``asyncio.Lock``); a reader never takes the
lock, and reads the old value until the change has been committed.

A stored value is checked again when it is loaded (the registry's range, the host's ceiling and the cross rule): one
that fails is skipped with a warning naming its key, and the limit stays the host's. Only scope ``background`` with
the settings ``allowed`` and ``denied`` is read or written; an ``allowed`` row changes nothing (the guard only takes
tools away). Nothing else in the code writes these tables.
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
    "BACKGROUND",
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
            tool_guard.install(ToolGuard(denied=denied, locked=self.locked))

    async def load(self) -> None:
        """At start: apply the clamped host values and the owner's stored limits in one pass, and install the tool
        guard's snapshot. Off (``NOUS_AUTONOMY_SETTINGS_ENABLED``): nothing is read or changed. A stored value that
        fails a check is skipped with a warning; when the rows cannot be read, the limits stay the host's and the
        guard holds the host's locked tools only."""
        if not self._settings.autonomy_settings_enabled:
            return
        rows: list[tuple[str, Any]] = []
        tool_rows: list[tuple[str, str]] = []
        try:
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
        except Exception as exc:
            # The class only: an error's text can repeat what it was given.
            logger.warning(
                "F115: the stored autonomy settings could not be read (%s); host values kept", type(exc).__name__
            )
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
        problem = cross_problem({**self.baseline, **owner})
        if problem is not None:
            logger.warning("F115: the stored subtask time limits break the cross rule; both skipped")
            owner.pop("subtask_default_timeout", None)
            owner.pop("subtask_max_timeout", None)
        for key, value in {**{key: self.baseline[key] for key in self.clamped}, **owner}.items():
            self._set(key, value)
        self.owner = owner
        self.tools = {tool: setting for tool, setting in tool_rows if setting in TOOL_SETTINGS}
        self._install_guard()

    # ---- limits ------------------------------------------------------------------------------------------------

    def _limit(self, key: str) -> Any:
        limit = by_key(key)
        if limit is None:
            raise AutonomyRefused(404, f"There is no limit named {key[:40]!r}.")
        return limit

    async def set_limit(self, key: str, value: Any, actor: str) -> None:
        """The owner's value of ``key``: refused (400) outside the range, (409) above the host's ceiling or when the
        cross rule fails; else written with its record in one transaction, then applied."""
        limit = self._limit(key)
        problem = range_problem(limit, value)
        if problem is not None:
            raise AutonomyRefused(400, problem)
        problem = ceiling_problem(limit, value, self.ceilings.get(key))
        if problem is not None:
            raise AutonomyRefused(409, problem)
        async with self._lock:
            problem = cross_problem({**self.effective(), key: value})
            if problem is not None:
                raise AutonomyRefused(409, problem)
            async with self._db.session() as session:
                old = await self._locked_row(session, key)
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
            self._set(key, value)
            self.owner[key] = value

    async def clear_limit(self, key: str, actor: str) -> bool:
        """Back to the host's value: the row deleted with its record in one transaction, then the baseline applied.
        False (and no record) when the owner had set nothing; 409 when the host's value would break the cross rule."""
        self._limit(key)
        async with self._lock:
            problem = cross_problem({**self.effective(), key: self.baseline[key]})
            if problem is not None:
                raise AutonomyRefused(409, problem)
            async with self._db.session() as session:
                old = await self._locked_row(session, key)
                if old is None:
                    await session.rollback()
                    self._set(key, self.baseline[key])
                    self.owner.pop(key, None)
                    return False
                await session.execute(
                    delete(AutonomySetting).where(
                        AutonomySetting.agent_id == self._agent_id, AutonomySetting.key == key
                    )
                )
                session.add(self._record(key, old, None, actor))
                await session.commit()
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

    async def set_tool(self, tool: str, setting: str, actor: str) -> None:
        """The owner's setting of ``tool`` for background work: ``allowed`` or ``denied`` (400 for another word, 409
        for a tool the host locked), written with its record in one transaction, then the guard's snapshot
        replaced."""
        if setting not in TOOL_SETTINGS:
            word = "Ask each time" if setting == "ask" else "That setting"
            raise AutonomyRefused(400, f"{word} is {NOT_AVAILABLE}. A tool is Allowed or Not allowed.")
        self._tool_refusal(tool)
        async with self._lock:
            async with self._db.session() as session:
                old = await self._locked_tool(session, tool)
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
            self.tools[tool] = setting
            self._install_guard()

    async def clear_tool(self, tool: str, actor: str) -> bool:
        """Back to the default (allowed): the row deleted with its record, then the snapshot replaced. False (and no
        record) when the owner had set nothing."""
        self._tool_refusal(tool)
        async with self._lock:
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

    async def changes(self, limit: int = LOG_SHOWN) -> list[Change]:
        """The newest ``limit`` changes, newest first."""
        async with self._db.session() as session:
            rows = (
                await session.execute(
                    select(AutonomySettingLog)
                    .where(AutonomySettingLog.agent_id == self._agent_id)
                    .order_by(AutonomySettingLog.id.desc())
                    .limit(limit)
                )
            ).scalars()
            return [Change(row.key, row.old_value, row.new_value, row.actor, row.created_at) for row in rows]

    async def last_changes(self, keys: Iterable[str]) -> dict[str, Change]:
        """The newest change of each of ``keys`` that has one."""
        wanted = list(keys)
        if not wanted:
            return {}
        async with self._db.session() as session:
            rows = (
                await session.execute(
                    select(AutonomySettingLog)
                    .where(AutonomySettingLog.agent_id == self._agent_id, AutonomySettingLog.key.in_(wanted))
                    .order_by(AutonomySettingLog.key, AutonomySettingLog.id.desc())
                    .distinct(AutonomySettingLog.key)
                )
            ).scalars()
            return {row.key: Change(row.key, row.old_value, row.new_value, row.actor, row.created_at) for row in rows}
