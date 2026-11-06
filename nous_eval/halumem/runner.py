"""One HaluMem user through Nous, in the order of HaluMem's reference adapters.

For each session, in file order:

1. With sleep on, a sleep cycle runs first when the session starts on a later day
   than the previous one ended: prod sleeps between days, not between turns. Sleep
   reflection reads the episodes started in the last ``sleep_reflection_hours`` of
   wall-clock time; before each cycle that window is widened to reach back exactly
   that many hours before the simulated sleep time (later sessions are not stored
   yet), so reflection sees the episodes prod's would.
2. The dialogue (both roles) is stored as one dated episode through the write path
   (``beam.harness.ingest_transcript``).
3. ``extracted_memories`` lists the active facts this session left, read right after
   it: new rows, and existing rows whose ``confirmation_count`` it raised (a dedup
   hit is the store keeping the point). Facts only: the episode summary goes in the
   session's ``nous`` record, never into the scored list.
4. Each update point (``is_update`` with original memories) gets the top 10 of a
   fact search on its text as ``memories_from_system``, with access tracking off:
   the search is the evaluator's probe, not a read the agent made.

In both scored lists "the user"/"User" becomes the persona's name (``name_owner``):
the store holds one person's memories and Nous writes its owner as "the user",
while HaluMem's judge requires the gold point's name and scores "User works at X"
0 against "Martin Mark works at X" (the reference adapters told Mem0 to write names).
Nous's own text is kept in the session's ``nous`` record. The question context is
left as recalled: its header names the user.
5. Each question gets the ``recall_deep`` text as context and is answered by the
   answer model with HaluMem's answer prompt.

The output dict has the reference adapters' shape, so HaluMem's ``evaluation.py``
scores it unchanged.
"""

from __future__ import annotations

import copy
import logging
import math
import re
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text as sa_text

from nous_eval.beam import harness
from nous_eval.halumem import corpus

logger = logging.getLogger(__name__)

UPDATE_TOP_K = 10  # the reference adapters' update search
_OWNER_RE = re.compile(r"\b(?:the user|user)('s)?\b", re.IGNORECASE)

Answer = Callable[[str], Awaitable[str]]


def name_owner(text: str, name: str) -> str:
    """``"The user's colleague"`` -> ``"<name>'s colleague"`` (see the module docstring)."""
    return _OWNER_RE.sub(lambda m: name + (m.group(1) or ""), text)


def _ms(t0: float) -> float:
    return (time.monotonic() - t0) * 1000


def reflection_hours(wall_now: datetime, sleep_at: datetime, window_hours: int) -> int:
    """The wall-clock window that reaches back ``window_hours`` before the simulated ``sleep_at``."""
    return max(window_hours, math.ceil((wall_now - sleep_at).total_seconds() / 3600) + window_hours)


async def sleep_at(stack: harness.Stack, since: datetime, when: datetime, window_hours: int) -> None:
    """One sleep cycle at the simulated time ``when``, reflection window moved to that clock."""
    stack.settings.sleep_reflection_hours = reflection_hours(datetime.now(UTC), when, window_hours)
    try:
        await harness.run_sleep(stack, since, when)
    finally:
        stack.settings.sleep_reflection_hours = window_hours


async def fact_counts(stack: harness.Stack) -> dict[str, int]:
    """Every fact of the agent (active or not) -> its confirmation count."""
    async with stack.db.session() as s:
        rows = await s.execute(
            sa_text("SELECT id::text, COALESCE(confirmation_count, 0) FROM heart.facts WHERE agent_id = :a"),
            {"a": stack.settings.agent_id},
        )
        return {fid: n for fid, n in rows}


async def stored_since(stack: harness.Stack, before: dict[str, int]) -> dict[str, Any]:
    """What one session left in the fact store, against the counts read before it."""
    async with stack.db.session() as s:
        rows = (
            await s.execute(
                sa_text(
                    "SELECT id::text AS id, content, active, COALESCE(confirmation_count, 0) AS n, source"
                    " FROM heart.facts WHERE agent_id = :a ORDER BY learned_at, id"
                ),
                {"a": stack.settings.agent_id},
            )
        ).all()
    new = [r for r in rows if r.id not in before]
    confirmed = [r for r in rows if r.id in before and r.active and r.n > before[r.id]]
    kept = [r for r in new if r.active] + confirmed
    return {
        "memories": [r.content for r in kept],
        "fact_ids": [r.id for r in kept],
        "new_facts": sum(1 for r in new if r.active),
        "new_then_inactive": sum(1 for r in new if not r.active),
        "confirmed_facts": len(confirmed),
        "new_sources": dict(Counter(str(r.source).split(":")[0] for r in new if r.active)),
    }


async def episode_summary(stack: harness.Stack, episode_id: Any) -> str | None:
    async with stack.db.session() as s:
        return (
            await s.execute(sa_text("SELECT summary FROM heart.episodes WHERE id = :i"), {"i": episode_id})
        ).scalar_one_or_none()


async def search_memories(stack: harness.Stack, query: str, k: int) -> list[str]:
    """Top-k fact search, formatted as the reference adapters format memories: ``"<time>: <text>"``."""
    facts = await stack.heart.search_facts(query, limit=k, track_access=False)
    return [f"{f.learned_at.isoformat() if f.learned_at else ''}: {f.content}" for f in facts]


async def run_user(
    stack: harness.Stack,
    user: dict[str, Any],
    *,
    answer: Answer,
    answer_prompt: str,
    memory_template: str,
    sleep: bool,
    limit_sessions: int | None = None,
) -> dict[str, Any]:
    """The adapter output for one user (see the module docstring)."""
    if (await harness.count_rows(stack))["episodes"]:
        raise SystemExit(f"agent {stack.settings.agent_id} already has episodes; use a new --agent-tag")
    await harness.ensure_agent(stack)
    since = await harness.db_now(stack)  # facts written from here on are this run's (see ingest_conversation)
    name = corpus.user_name(user["persona_info"])
    out: dict[str, Any] = {"uuid": user["uuid"], "user_name": name, "sessions": []}
    sessions = user["sessions"][:limit_sessions] if limit_sessions else user["sessions"]
    prev_end = None
    sleeps = 0
    window_hours = stack.settings.sleep_reflection_hours
    for i, raw in enumerate(sessions):
        session = corpus.as_session(raw, i)
        ended = corpus.session_end(raw)
        slept = False
        if sleep and prev_end is not None and session.started_at.date() > prev_end.date():
            await sleep_at(stack, since, prev_end + harness.SLEEP_GAP, window_hours)
            slept, sleeps = True, sleeps + 1
        before = await fact_counts(stack)
        t0 = time.monotonic()
        episode_id = await harness.ingest_transcript(stack, f"halumem-{user['uuid'][:8]}-s{i}", session, ended, since)
        add_ms = _ms(t0)
        stored = await stored_since(stack, before)
        new_session: dict[str, Any] = {
            "memory_points": copy.deepcopy(raw["memory_points"]),
            "dialogue": raw["dialogue"],
            "extracted_memories": [name_owner(m, name) for m in stored["memories"]],
            "add_dialogue_duration_ms": add_ms,
            "nous": {
                "episode_id": str(episode_id),
                "summary": await episode_summary(stack, episode_id),
                "slept_before": slept,
                **stored,
            },
        }
        for point in new_session["memory_points"]:
            if corpus.is_update_point(point):
                found = await search_memories(stack, point["memory_content"], UPDATE_TOP_K)
                point["memories_from_system"] = [name_owner(m, name) for m in found]
                new_session["nous"].setdefault("update_search_raw", []).append(found)
        if "questions" in raw:
            new_session["questions"] = []
            for qa in raw["questions"]:
                new_qa = copy.deepcopy(qa)
                t0 = time.monotonic()
                recalled, _ = await harness.context_recall_deep(stack, qa["question"])
                new_qa["context"] = memory_template.format(user_id=name, memories=recalled)
                new_qa["search_duration_ms"] = _ms(t0)
                t0 = time.monotonic()
                new_qa["system_response"] = await answer(
                    answer_prompt.format(context=new_qa["context"], question=qa["question"])
                )
                new_qa["response_duration_ms"] = _ms(t0)
                new_session["questions"].append(new_qa)
        out["sessions"].append(new_session)
        prev_end = ended if prev_end is None else max(prev_end, ended)
        if (i + 1) % 10 == 0:
            logger.info("%s: %d/%d sessions", stack.settings.agent_id, i + 1, len(sessions))
    out["nous_run"] = {"agent_id": stack.settings.agent_id, "sessions": len(sessions), "sleep_cycles": sleeps}
    return out
