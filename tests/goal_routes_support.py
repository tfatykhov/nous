"""Helpers of the goal route tests: an app over the goal routes alone, with a store of its own agent, and goals put
in a given condition through the store and, where the store has no method for it, a direct UPDATE that keeps the
table's CHECKs."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy import func, select, update
from starlette.applications import Starlette

from nous.api.goal_routes import build_goal_routes
from nous.api.owner_guard import OwnerGuard
from nous.config import Settings
from nous.goals.store import GoalStore
from nous.storage.models import Goal, GoalJournal, GoalSession

OWNER_VALUE = "goal-owner-test-value"
PROXY_VALUE = "goal-proxy-test-value"
OWNER = {"X-Nous-Owner-Secret": OWNER_VALUE}
PROXY = {"X-Nous-Proxy-Secret": PROXY_VALUE}
FORBIDDEN = {"error": "forbidden"}
NOT_JSON = {"error": "Content-Type must be application/json"}
CHAN = "telegram:4242"
# Goal pacing on, in New York, with the goal settings it needs.
PACING_ON = {
    "goals_enabled": True,
    "continuation_enabled": True,
    "intentions_enabled": True,
    "result_inbox_enabled": True,
    "subtask_enabled": True,
    "goals_pacing_enabled": True,
    "goals_timezone": "America/New_York",
}


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def settings_for(agent: str | None = None, **over: Any) -> Settings:
    """Settings of a fresh agent, both secrets' digests set, and no default chat unless ``over`` gives one."""
    values = {
        "owner_route_secret_sha256": digest(OWNER_VALUE),
        "mcp_client_ui_secret_sha256": digest(PROXY_VALUE),
        "telegram_chat_id": "",
        **over,
    }
    return Settings(_env_file=None, agent_id=agent or f"goal-routes-{uuid.uuid4().hex[:8]}", **values)


@dataclass
class Env:
    """The goal routes of one agent, and that agent's store (None with the flag off)."""

    app: Starlette
    store: GoalStore | None
    settings: Settings

    async def call(self, method: str, path: str, *, headers: dict[str, str] | None = None, **kw: Any) -> httpx.Response:
        """A request with the owner's secret (unless ``headers`` replace it)."""
        return await send(self.app, method, path, headers=OWNER if headers is None else headers, **kw)


def make_env(database: Any, *, enabled: bool = True, **over: Any) -> Env:
    settings = settings_for(**over)
    store = GoalStore(database, settings.agent_id, settings) if enabled else None
    routes = build_goal_routes(database=database, settings=settings, goals=store, guard=OwnerGuard(settings))
    return Env(Starlette(routes=routes), store, settings)


async def send(app: Starlette, method: str, path: str, **kw: Any) -> httpx.Response:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://nous") as client:
        return await client.request(method, path, **kw)


async def draft(store: GoalStore, **over: Any) -> Goal:
    values = {
        "title": "Tidy the notes",
        "objective": "Merge the duplicate notes in the notes folder",
        "done_when": "No two notes say the same thing",
        "proposed_by": "owner",
        "origin_channel": CHAN,
        **over,
    }
    return await store.create(**values)


async def active(store: GoalStore, **over: Any) -> Goal:
    goal = await draft(store, **over)
    return await store.approve(goal.id, actor="owner")


async def set_values(store: GoalStore, goal: Goal, **values: Any) -> None:
    async with store._db.session() as s:
        await s.execute(update(Goal).where(Goal.id == goal.id).values(**values))
        await s.commit()


async def db_now(store: GoalStore) -> Any:
    async with store._db.session() as s:
        return (await s.execute(select(func.clock_timestamp()))).scalar_one()


async def add_session(
    store: GoalStore, goal: Goal, *, n: int, spent: int = 0, open_: bool = False, started_at: Any = None
) -> GoalSession:
    """A session row: closed (``no_step``) unless ``open_``."""
    async with store._db.session() as s:
        row = GoalSession(
            agent_id=store.agent_id,
            goal_id=goal.id,
            n=n,
            trigger="timer",
            first_subtask_id=uuid.uuid4(),
            spent=spent,
            **({} if open_ else {"ended_at": func.clock_timestamp(), "outcome": "no_step"}),
            **({"started_at": started_at} if started_at is not None else {}),
        )
        s.add(row)
        await s.commit()
        return row


async def harness_row(
    store: GoalStore, goal: Goal, text: str, *, session_n: int | None = None, evidence: tuple[str, ...] = ()
) -> None:
    """A ``state`` row under the ``harness`` actor, written as the goal driver writes it (``add_journal_row`` in a
    transaction of its own): the store's ``append_journal`` refuses that actor."""
    async with store._db.session() as s:
        store.add_journal_row(s, goal.id, "state", text, actor="harness", session_n=session_n, evidence=evidence)
        await s.commit()


async def journal_rows(store: GoalStore, goal: Goal) -> list[GoalJournal]:
    async with store._db.session() as s:
        rows = await s.execute(select(GoalJournal).where(GoalJournal.goal_id == goal.id).order_by(GoalJournal.seq))
        return list(rows.scalars())


async def reread(store: GoalStore, goal: Goal) -> Goal:
    found = await store.get(goal.id)
    assert found is not None
    return found


async def paused(store: GoalStore, reason: str, **over: Any) -> Goal:
    """An approved goal, paused for ``reason`` (the driver's pauses have no store method)."""
    goal = await active(store, **over)
    await set_values(store, goal, state="paused", pause_reason=reason, next_wake_at=None)
    return await reread(store, goal)


async def deadline_in(store: GoalStore, goal: Goal, days: float) -> None:
    """Move an approved goal's deadline to ``days`` from the database's now (negative: passed)."""
    await set_values(store, goal, deadline_at=func.clock_timestamp() + timedelta(days=days))
