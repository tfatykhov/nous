# F031: Censor Middleware with Action Payloads — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Transform censors from passive text injectors into active behavioral guardrails that execute prescribed read-only tool actions (e.g., `recall_deep`) and inject results into turn context. For warn censors, results are injected into the system prompt before the LLM sees the turn. For block censors, results enrich the block reason with evidence and guidance.

**Architecture:** Add two nullable fields (`trigger_action` JSONB, `action_instruction` TEXT) to the censors table. When a censor with `trigger_action` fires during `pre_turn`, execute the prescribed action via Heart methods directly (not through ToolDispatcher). For warn censors, inject results into a new `censor_injected_context` field on `TurnContext` and append to system prompt. For block censors, enrich `censor_block_reason` with action results and `action_instruction` so the user sees evidence and guidance. Post-turn verifies the agent referenced injected context (warn only). Existing censors are unaffected — both fields are nullable.

**Tech Stack:** Python 3.12+, PostgreSQL 17, SQLAlchemy 2.0 (async), pydantic v2, pytest

---

## File Structure

| Action | File | Responsibility |
|--------|------|---------------|
| Modify | `sql/migrations/024_censor_action_payloads.sql` | DB migration: add `trigger_action` JSONB + `action_instruction` TEXT |
| Modify | `sql/init.sql` | Add columns to base schema definition |
| Modify | `nous/storage/models.py:520-558` | ORM: add `trigger_action` + `action_instruction` columns |
| Modify | `nous/heart/schemas.py:211-253` | Pydantic: add fields to `CensorInput`, `CensorDetail`, `CensorMatch` |
| Modify | `nous/heart/censors.py:84-118,142-212,379-390,421-430` | Pass new fields through `_add()`, `_check()`, `_semantic_search()`, `_keyword_search()` |
| Create | `nous/heart/censor_actions.py` | Action executor: validate + run prescribed actions via Heart |
| Modify | `nous/cognitive/schemas.py:166-183` | Add `censor_injected_context` to `TurnContext` |
| Modify | `nous/cognitive/layer.py:516-561,711-731` | Pre-turn: execute actions, inject results. Post-turn: verify compliance |
| Modify | `nous/cognitive/context.py:800-818` | Format `action_instruction` for warn censors in system prompt |
| Modify | `nous/heart/heart.py` | Heart wrapper for `update_censor()` |
| Modify | `nous/api/rest.py` | PUT `/censors/{id}` endpoint for updating existing censors |
| Modify | `tests/test_censors.py` | Tests for new fields, action execution, pipeline integration, update API |

---

## Task 1: Database Migration + ORM

**Files:**
- Create: `sql/migrations/024_censor_action_payloads.sql`
- Modify: `sql/init.sql:384-403`
- Modify: `nous/storage/models.py:520-558`

- [ ] **Step 1: Write the migration SQL**

Create `sql/migrations/024_censor_action_payloads.sql`:

```sql
-- F031: Censor middleware with action payloads
-- Adds trigger_action (JSONB) and action_instruction (TEXT) to censors

ALTER TABLE heart.censors ADD COLUMN IF NOT EXISTS trigger_action JSONB;
ALTER TABLE heart.censors ADD COLUMN IF NOT EXISTS action_instruction TEXT;
ALTER TABLE heart.censors ADD COLUMN IF NOT EXISTS unblock_pattern TEXT;

-- Record migration
INSERT INTO nous_system.schema_migrations (version, description)
VALUES (24, 'F031: censor action payloads')
ON CONFLICT (version) DO NOTHING;
```

- [ ] **Step 2: Update init.sql base schema**

In `sql/init.sql`, inside the `CREATE TABLE heart.censors` block (after line 399, the `embedding vector(1536)` line), add:

```sql
    trigger_action JSONB,
    action_instruction TEXT,
    unblock_pattern TEXT,
```

- [ ] **Step 3: Add ORM columns**

In `nous/storage/models.py`, inside the `Censor` class (after the `updated_at` column at line 553, before the relationships), add:

```python
    trigger_action: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    action_instruction: Mapped[str | None] = mapped_column(Text, nullable=True)
    unblock_pattern: Mapped[str | None] = mapped_column(Text, nullable=True)
```

Import `JSONB` from `sqlalchemy.dialects.postgresql` if not already imported.

- [ ] **Step 4: Run migration against local DB**

```bash
docker compose up -d postgres
uv run python -c "
import asyncio
from nous.storage.database import Database
from nous.config import Settings
async def main():
    s = Settings()
    db = Database(s)
    await db.connect()
    await db.run_migrations()
    await db.disconnect()
asyncio.run(main())
"
```

Verify columns exist:
```bash
docker compose exec postgres psql -U nous -d nous -c "\d heart.censors" | grep -E "trigger_action|action_instruction"
```

Expected: both columns shown as `jsonb` and `text`.

- [ ] **Step 5: Commit**

```bash
git add sql/migrations/024_censor_action_payloads.sql sql/init.sql nous/storage/models.py
git commit -m "feat(f031): add trigger_action and action_instruction columns to censors"
```

---

## Task 2: Pydantic Schema Updates

**Files:**
- Modify: `nous/heart/schemas.py:211-253`

- [ ] **Step 1: Write failing test for CensorInput with trigger_action**

Add to `tests/test_censors.py`:

```python
from nous.heart.schemas import CensorInput, CensorMatch, CensorDetail


async def test_censor_input_with_trigger_action(heart, session):
    """CensorInput accepts trigger_action and action_instruction."""
    inp = CensorInput(
        trigger_pattern="citing.*source",
        reason="Verify citations",
        action="warn",
        trigger_action={"tool": "recall", "args": {"query": "citations", "limit": 5}},
        action_instruction="Verify all citations against recalled sources.",
    )
    detail = await heart.add_censor(inp, session=session)
    assert detail.trigger_action == {"tool": "recall", "args": {"query": "citations", "limit": 5}}
    assert detail.action_instruction == "Verify all citations against recalled sources."


async def test_censor_input_without_trigger_action(heart, session):
    """Existing censors without trigger_action still work (backward compat)."""
    inp = CensorInput(
        trigger_pattern="never deploy on Friday",
        reason="Weekend risk",
    )
    detail = await heart.add_censor(inp, session=session)
    assert detail.trigger_action is None
    assert detail.action_instruction is None


async def test_censor_match_includes_action_fields():
    """CensorMatch carries trigger_action and action_instruction."""
    from uuid import uuid4
    match = CensorMatch(
        id=uuid4(),
        trigger_pattern="test",
        action="warn",
        reason="test reason",
        domain=None,
        trigger_action={"tool": "recall", "args": {"query": "test"}},
        action_instruction="Check memory first.",
    )
    assert match.trigger_action["tool"] == "recall"
    assert match.action_instruction == "Check memory first."
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_censors.py::test_censor_input_with_trigger_action tests/test_censors.py::test_censor_input_without_trigger_action tests/test_censors.py::test_censor_match_includes_action_fields -v
```

Expected: FAIL — fields not yet on schemas.

- [ ] **Step 3: Update CensorInput schema**

In `nous/heart/schemas.py`, add to `CensorInput` (after `learned_from_episode` at line 222):

```python
    trigger_action: dict | None = None  # F031: e.g. {"tool": "recall", "args": {...}}
    action_instruction: str | None = None  # F031: human-readable instruction
    unblock_pattern: str | None = None  # F031: regex — if action results match, downgrade block→warn
```

- [ ] **Step 4: Update CensorDetail schema**

In `nous/heart/schemas.py`, add to `CensorDetail` (after `created_at` at line 242):

```python
    trigger_action: dict | None = None  # F031
    action_instruction: str | None = None  # F031
    unblock_pattern: str | None = None  # F031
```

- [ ] **Step 5: Update CensorMatch schema**

In `nous/heart/schemas.py`, add to `CensorMatch` (after `score` at line 253):

```python
    trigger_action: dict | None = None  # F031
    action_instruction: str | None = None  # F031
    unblock_pattern: str | None = None  # F031
```

- [ ] **Step 6: Update _add() to pass new fields**

In `nous/heart/censors.py`, in `_add()` (lines 94-104), add to the `Censor()` constructor:

```python
            trigger_action=input.trigger_action,
            action_instruction=input.action_instruction,
            unblock_pattern=input.unblock_pattern,
```

- [ ] **Step 7: Update _to_detail() to include new fields**

In `nous/heart/censors.py`, in `_to_detail()` (line 606-624), add after `created_at`:

```python
            trigger_action=censor.trigger_action,
            action_instruction=censor.action_instruction,
            unblock_pattern=censor.unblock_pattern,
```

- [ ] **Step 8: Update _check() CensorMatch construction**

In `nous/heart/censors.py`, in `_check()` (lines 201-209), update the `CensorMatch()` constructor to include:

```python
                    trigger_action=censor.trigger_action,
                    action_instruction=censor.action_instruction,
                    unblock_pattern=censor.unblock_pattern,
```

- [ ] **Step 9: Update _semantic_search() CensorMatch construction**

In `nous/heart/censors.py`, in `_semantic_search()` (lines 379-390), update the `CensorMatch()` constructor to include:

```python
                trigger_action=c.trigger_action,
                action_instruction=c.action_instruction,
                unblock_pattern=c.unblock_pattern,
```

- [ ] **Step 10: Update _keyword_search() CensorMatch construction**

In `nous/heart/censors.py`, in `_keyword_search()` (lines 421-430), update the `CensorMatch()` constructor to include:

```python
                            trigger_action=censor.trigger_action,
                            action_instruction=censor.action_instruction,
                            unblock_pattern=censor.unblock_pattern,
```

- [ ] **Step 11: Run tests to verify they pass**

```bash
uv run pytest tests/test_censors.py::test_censor_input_with_trigger_action tests/test_censors.py::test_censor_input_without_trigger_action tests/test_censors.py::test_censor_match_includes_action_fields -v
```

Expected: PASS

- [ ] **Step 12: Run full censor test suite for regression**

```bash
uv run pytest tests/test_censors.py -v
```

Expected: all existing tests PASS.

- [ ] **Step 13: Commit**

```bash
git add nous/heart/schemas.py nous/heart/censors.py tests/test_censors.py
git commit -m "feat(f031): add trigger_action and action_instruction to censor schemas"
```

---

## Task 3: Censor Action Executor

**Files:**
- Create: `nous/heart/censor_actions.py`
- Test: `tests/test_censors.py` (append)

This module validates and executes the prescribed action from `trigger_action`. It calls Heart methods directly — no ToolDispatcher dependency. Only read-only operations are allowed.

- [ ] **Step 1: Write failing tests for action executor**

Add to `tests/test_censors.py`:

```python
from nous.heart.censor_actions import CensorActionExecutor, ALLOWED_TOOLS


async def test_allowed_tools_are_read_only():
    """Only read-only tools are in the allow list."""
    assert "recall" in ALLOWED_TOOLS
    assert "recall_recent" in ALLOWED_TOOLS
    assert "search_facts" in ALLOWED_TOOLS
    assert "search_episodes" in ALLOWED_TOOLS
    assert "search_procedures" in ALLOWED_TOOLS
    assert "list_tasks" in ALLOWED_TOOLS
    # Write tools must NOT be in the list
    assert "learn_fact" not in ALLOWED_TOOLS
    assert "add_censor" not in ALLOWED_TOOLS


async def test_execute_recall_action(heart, session):
    """Execute a recall action and get formatted results."""
    # Seed a fact so recall has something to find
    from nous.heart.schemas import FactInput
    await heart.learn_fact(
        FactInput(content="The sky is blue", category="science", subject="sky"),
        session=session,
    )

    executor = CensorActionExecutor(heart)
    result = await executor.execute(
        trigger_action={"tool": "recall", "args": {"query": "sky color", "limit": 3}},
        session=session,
    )
    assert result is not None
    assert isinstance(result, str)
    assert len(result) > 0


async def test_execute_rejects_unknown_tool(heart, session):
    """Unknown tools are rejected, returning None."""
    executor = CensorActionExecutor(heart)
    result = await executor.execute(
        trigger_action={"tool": "write_file", "args": {"path": "/etc/passwd"}},
        session=session,
    )
    assert result is None


async def test_execute_rejects_malformed_action(heart, session):
    """Malformed trigger_action (missing tool key) returns None."""
    executor = CensorActionExecutor(heart)
    result = await executor.execute(
        trigger_action={"args": {"query": "test"}},
        session=session,
    )
    assert result is None


async def test_execute_handles_empty_results(heart, session):
    """When recall returns nothing, executor returns a meaningful message."""
    executor = CensorActionExecutor(heart)
    result = await executor.execute(
        trigger_action={"tool": "recall", "args": {"query": "xyznonexistent999", "limit": 3}},
        session=session,
    )
    assert result is not None
    assert "no results" in result.lower() or len(result) > 0
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_censors.py::test_allowed_tools_are_read_only tests/test_censors.py::test_execute_recall_action tests/test_censors.py::test_execute_rejects_unknown_tool tests/test_censors.py::test_execute_rejects_malformed_action tests/test_censors.py::test_execute_handles_empty_results -v
```

Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Implement CensorActionExecutor**

Create `nous/heart/censor_actions.py`:

```python
"""F031: Censor action executor — runs prescribed read-only actions.

When a warn censor with trigger_action fires, this module validates
and executes the action via Heart methods directly. No ToolDispatcher
dependency — the cognitive layer stays decoupled from the runtime.

Only read-only operations are permitted.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from nous.heart.heart import Heart

logger = logging.getLogger(__name__)

# Allowed read-only tools that censors can trigger.
ALLOWED_TOOLS: set[str] = {
    "recall",
    "recall_recent",
    "search_facts",
    "search_episodes",
    "search_procedures",
    "list_tasks",
}


class CensorActionExecutor:
    """Validates and executes censor trigger_action payloads."""

    def __init__(self, heart: Heart) -> None:
        self._heart = heart

    async def execute(
        self,
        trigger_action: dict[str, Any],
        session: AsyncSession | None = None,
    ) -> str | None:
        """Execute a trigger_action and return formatted results.

        Returns None if the action is invalid or disallowed.
        Returns a formatted string of results on success.
        """
        if not isinstance(trigger_action, dict):
            logger.warning("Censor trigger_action is not a dict: %s", type(trigger_action))
            return None

        tool = trigger_action.get("tool")
        if not tool or tool not in ALLOWED_TOOLS:
            logger.warning("Censor trigger_action tool not allowed: %s", tool)
            return None

        args = trigger_action.get("args", {})
        if not isinstance(args, dict):
            logger.warning("Censor trigger_action args is not a dict: %s", type(args))
            return None

        try:
            return await self._dispatch(tool, args, session)
        except Exception:
            logger.exception("Censor action execution failed for tool=%s", tool)
            return None

    async def _dispatch(
        self,
        tool: str,
        args: dict[str, Any],
        session: AsyncSession | None,
    ) -> str:
        """Route to the appropriate Heart method and format results."""
        handler = {
            "recall": self._run_recall,
            "recall_recent": self._run_recall_recent,
            "search_facts": self._run_search_facts,
            "search_episodes": self._run_search_episodes,
            "search_procedures": self._run_search_procedures,
            "list_tasks": self._run_list_tasks,
        }.get(tool)
        if handler:
            return await handler(args, session)
        return ""

    async def _run_recall(self, args: dict[str, Any], session: AsyncSession | None) -> str:
        query = str(args.get("query", ""))
        limit = min(int(args.get("limit", 5)), 10)  # Cap at 10
        results = await self._heart.recall(query, limit=limit, session=session)
        if not results:
            return f"[Censor recall: no results for '{query}']"
        lines = [f"[Censor recall for '{query}': {len(results)} results]"]
        for i, r in enumerate(results, 1):
            lines.append(f"  {i}. [{r.type}] {r.summary} (score: {r.score:.3f})")
        return "\n".join(lines)

    async def _run_search_facts(self, args: dict[str, Any], session: AsyncSession | None) -> str:
        query = str(args.get("query", ""))
        limit = min(int(args.get("limit", 5)), 10)
        results = await self._heart.search_facts(query, limit=limit, session=session)
        if not results:
            return f"[Censor search_facts: no results for '{query}']"
        lines = [f"[Censor search_facts for '{query}': {len(results)} results]"]
        for i, r in enumerate(results, 1):
            lines.append(f"  {i}. {r.content}")
        return "\n".join(lines)

    async def _run_search_episodes(self, args: dict[str, Any], session: AsyncSession | None) -> str:
        query = str(args.get("query", ""))
        limit = min(int(args.get("limit", 5)), 10)
        results = await self._heart.search_episodes(query, limit=limit, session=session)
        if not results:
            return f"[Censor search_episodes: no results for '{query}']"
        lines = [f"[Censor search_episodes for '{query}': {len(results)} results]"]
        for i, r in enumerate(results, 1):
            lines.append(f"  {i}. {r.summary}")
        return "\n".join(lines)

    async def _run_recall_recent(self, args: dict[str, Any], session: AsyncSession | None) -> str:
        hours = min(int(args.get("hours", 24)), 168)  # Cap at 1 week
        limit = min(int(args.get("limit", 5)), 10)
        results = await self._heart.list_episodes(limit=limit, hours=hours, session=session)
        if not results:
            return f"[Censor recall_recent: no episodes in last {hours}h]"
        lines = [f"[Censor recall_recent: {len(results)} episodes in last {hours}h]"]
        for i, r in enumerate(results, 1):
            lines.append(f"  {i}. {r.summary}")
        return "\n".join(lines)

    async def _run_search_procedures(self, args: dict[str, Any], session: AsyncSession | None) -> str:
        query = str(args.get("query", ""))
        limit = min(int(args.get("limit", 5)), 10)
        results = await self._heart.search_procedures(query, limit=limit, session=session)
        if not results:
            return f"[Censor search_procedures: no results for '{query}']"
        lines = [f"[Censor search_procedures for '{query}': {len(results)} results]"]
        for i, r in enumerate(results, 1):
            lines.append(f"  {i}. {r.name}: {r.description}")
        return "\n".join(lines)

    async def _run_list_tasks(self, args: dict[str, Any], session: AsyncSession | None) -> str:
        status = args.get("status")
        subtasks = await self._heart.subtasks.list(status=status, limit=10)
        schedules = await self._heart.schedules.list(active_only=True, limit=10)
        if not subtasks and not schedules:
            return "[Censor list_tasks: no active tasks or schedules]"
        lines = ["[Censor list_tasks]"]
        if subtasks:
            for st in subtasks:
                lines.append(f"  - [{st.status}] {st.task[:80]}")
        if schedules:
            for sc in schedules:
                lines.append(f"  - [schedule] {sc.task[:80]} ({sc.cron_expr})")
        return "\n".join(lines)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_censors.py::test_allowed_tools_are_read_only tests/test_censors.py::test_execute_recall_action tests/test_censors.py::test_execute_rejects_unknown_tool tests/test_censors.py::test_execute_rejects_malformed_action tests/test_censors.py::test_execute_handles_empty_results -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add nous/heart/censor_actions.py tests/test_censors.py
git commit -m "feat(f031): add CensorActionExecutor for read-only censor actions"
```

---

## Task 4: Pipeline Integration (pre_turn)

**Files:**
- Modify: `nous/cognitive/schemas.py:166-183`
- Modify: `nous/cognitive/layer.py:516-561`
- Modify: `nous/cognitive/context.py:800-818`
- Test: `tests/test_censors.py` (append)

- [ ] **Step 1: Write failing tests for pipeline integration**

Add to `tests/test_censors.py`:

```python
from nous.cognitive.schemas import TurnContext


def test_turn_context_has_censor_injected_context():
    """TurnContext schema includes censor_injected_context field."""
    from nous.cognitive.schemas import FrameSelection
    ctx = TurnContext(
        system_prompt="test",
        frame=FrameSelection(frame_id="conversation", frame_name="Conversation", description="test"),
        censor_injected_context={"censor-id-1": "[Censor recall: 3 results]..."},
    )
    assert ctx.censor_injected_context == {"censor-id-1": "[Censor recall: 3 results]..."}


def test_turn_context_censor_injected_context_default_empty():
    """censor_injected_context defaults to empty dict."""
    from nous.cognitive.schemas import FrameSelection
    ctx = TurnContext(
        system_prompt="test",
        frame=FrameSelection(frame_id="conversation", frame_name="Conversation", description="test"),
    )
    assert ctx.censor_injected_context == {}
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_censors.py::test_turn_context_has_censor_injected_context tests/test_censors.py::test_turn_context_censor_injected_context_default_empty -v
```

Expected: FAIL — field doesn't exist.

- [ ] **Step 3: Add censor_injected_context to TurnContext**

In `nous/cognitive/schemas.py`, add to `TurnContext` (after `diagnostic_nudges` at line 182):

```python
    censor_injected_context: dict[str, str] = Field(default_factory=dict)  # F031: censor_id -> action results
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_censors.py::test_turn_context_has_censor_injected_context tests/test_censors.py::test_turn_context_censor_injected_context_default_empty -v
```

Expected: PASS

- [ ] **Step 5: Update CognitiveLayer.__init__ to create CensorActionExecutor**

In `nous/cognitive/layer.py`, add import at the top:

```python
from nous.heart.censor_actions import CensorActionExecutor
```

In `__init__()` (after existing assignments around line 126), add:

```python
        self._censor_executor = CensorActionExecutor(heart)
```

- [ ] **Step 6: Update pre_turn censor handling to execute actions for both block and warn censors**

In `nous/cognitive/layer.py`, update the censor check block (lines 520-541). Add `censor_injected: dict[str, str] = {}` before the block (before line 520).

Replace the block censor handling (lines 524-534) with:

```python
                if match.action == "block":
                    # F031: Conditional unblock — if trigger_action + unblock_pattern,
                    # execute action and check if results match unblock_pattern.
                    # Match → downgrade to warn (skip block). No match → block as normal.
                    unblocked = False
                    action_result: str | None = None
                    if match.trigger_action:
                        try:
                            action_result = await self._censor_executor.execute(
                                match.trigger_action, session=session,
                            )
                        except Exception:
                            logger.warning(
                                "Censor block action failed (session=%s, censor=%s)",
                                session_id, match.id, exc_info=True,
                            )

                        # Check unblock condition
                        if action_result and match.unblock_pattern:
                            import re as _re
                            try:
                                if _re.search(match.unblock_pattern, action_result, _re.IGNORECASE):
                                    unblocked = True
                                    logger.info(
                                        "Censor UNBLOCK: pattern matched (session=%s, censor=%s)",
                                        session_id, match.id,
                                    )
                            except _re.error:
                                logger.warning("Invalid unblock_pattern regex: %s", match.unblock_pattern)

                    if unblocked:
                        # Downgrade to warn — inject context like a warn censor
                        logger.info(
                            "Censor BLOCK→WARN downgrade (session=%s, censor=%s): %s",
                            session_id, match.id, match.trigger_pattern,
                        )
                        if action_result:
                            censor_injected[str(match.id)] = action_result
                    else:
                        # Block as normal
                        censor_blocked = True
                        censor_block_reason = (
                            f"Blocked by censor: {match.reason or match.trigger_pattern}"
                        )
                        logger.warning(
                            "Censor BLOCK on user input (session=%s, censor=%s): %s",
                            session_id, match.id, match.trigger_pattern,
                        )
                        if action_result:
                            censor_block_reason += f"\n\nRelated context:\n{action_result}"
                        if match.action_instruction:
                            censor_block_reason += f"\n\n{match.action_instruction}"
                        break  # One block is enough
```

Replace the warn censor handling (lines 535-539) with:

```python
                elif match.action == "warn":
                    logger.info(
                        "Censor WARN on user input (session=%s, censor=%s): %s",
                        session_id, match.id, match.trigger_pattern,
                    )
                    # F031: Execute trigger_action if present
                    if match.trigger_action:
                        try:
                            action_result = await self._censor_executor.execute(
                                match.trigger_action, session=session,
                            )
                            if action_result:
                                censor_injected[str(match.id)] = action_result
                                logger.info(
                                    "Censor action executed (session=%s, censor=%s, tool=%s)",
                                    session_id, match.id, match.trigger_action.get("tool"),
                                )
                        except Exception:
                            logger.warning(
                                "Censor action failed (session=%s, censor=%s)",
                                session_id, match.id, exc_info=True,
                            )
```

- [ ] **Step 7: Pass censor_injected_context to TurnContext**

In the `return TurnContext(...)` call (lines 546-561), add:

```python
            censor_injected_context=censor_injected,
```

- [ ] **Step 8: Update context formatting to include action_instruction**

In `nous/cognitive/context.py`, update `_format_censors()` (lines 800-818). After the existing line that builds each censor entry, add `action_instruction` display:

```python
    def _format_censors(self, censors: list) -> str:
        """Format active censors.

        P1-4: Use action (not severity).
        Format: - **{ACTION}:** {trigger_pattern} -- {reason}
        F031: Append action_instruction for warn censors if present.
        """
        action_order = {"absolute": 0, "block": 1, "warn": 2}
        sorted_censors = sorted(
            censors,
            key=lambda c: action_order.get(getattr(c, "action", "warn"), 3),
        )
        lines = []
        for c in sorted_censors:
            action = getattr(c, "action", "warn").upper()
            pattern = getattr(c, "trigger_pattern", "")
            reason = getattr(c, "reason", "")
            line = f"- **{action}:** {pattern} -- {reason}"
            # F031: Append action_instruction for warn censors
            instruction = getattr(c, "action_instruction", None)
            if instruction and action == "WARN":
                line += f"\n  *Instruction:* {instruction}"
            lines.append(line)
        return "\n".join(lines)
```

- [ ] **Step 9: Inject censor action results into system prompt**

In `nous/cognitive/layer.py`, in `pre_turn()`, after executing censor actions and before building the system prompt return, inject the censor results into the system prompt. Find the system prompt construction (the context engine builds it). We need to append censor-injected context.

After the censor check block and before `return TurnContext(...)`, add:

```python
        # F031: Append censor-injected context to system prompt
        if censor_injected:
            injected_section = "\n\n## Censor-Injected Context\n"
            injected_section += "The following information was automatically retrieved by active censors. Use it to inform your response:\n\n"
            for censor_id, result_text in censor_injected.items():
                injected_section += f"{result_text}\n\n"
            system_prompt += injected_section
```

- [ ] **Step 10: Run all censor tests**

```bash
uv run pytest tests/test_censors.py -v
```

Expected: all PASS.

- [ ] **Step 11: Commit**

```bash
git add nous/cognitive/schemas.py nous/cognitive/layer.py nous/cognitive/context.py
git commit -m "feat(f031): execute censor actions in pre_turn and inject results into context"
```

---

## Task 5: Post-turn Verification

**Files:**
- Modify: `nous/cognitive/layer.py:711-731`
- Test: `tests/test_censors.py` (append)

- [ ] **Step 1: Write failing test for post-turn compliance logging**

Add to `tests/test_censors.py`:

```python
import logging


async def test_post_turn_logs_censor_compliance(heart, session, caplog):
    """Post-turn logs whether the agent referenced censor-injected context."""
    # This is a behavioral test — we verify that the compliance check
    # produces the expected log output. The actual integration test requires
    # a full CognitiveLayer, but we can test the compliance logic directly.
    from nous.cognitive.layer import _check_censor_compliance

    # Agent used the injected context
    injected = {"censor-1": "[Censor recall for 'citations': 2 results]\n  1. [fact] Source A"}
    response = "Based on Source A, the data shows..."
    with caplog.at_level(logging.INFO):
        result = _check_censor_compliance(injected, response)
    assert result["censor-1"] is True

    # Agent did NOT use the injected context
    response2 = "I think the answer is 42."
    result2 = _check_censor_compliance(injected, response2)
    assert result2["censor-1"] is False
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_censors.py::test_post_turn_logs_censor_compliance -v
```

Expected: FAIL — function doesn't exist.

- [ ] **Step 3: Implement compliance check function**

In `nous/cognitive/layer.py`, add a module-level function (before the `CognitiveLayer` class):

```python
def _check_censor_compliance(
    censor_injected_context: dict[str, str],
    response_text: str,
) -> dict[str, bool]:
    """Check if the agent's response references censor-injected context.

    Returns a dict mapping censor_id -> True if the response appears to
    reference the injected content, False otherwise. Uses simple keyword
    overlap heuristic — not a semantic check.
    """
    results: dict[str, bool] = {}
    response_lower = response_text.lower()
    for censor_id, injected_text in censor_injected_context.items():
        # Extract meaningful words from injected text (skip formatting)
        words = set()
        for line in injected_text.split("\n"):
            line = line.strip()
            if line.startswith("[Censor"):
                continue  # Skip header lines
            for word in line.split():
                cleaned = word.strip(".,;:()[]'\"").lower()
                if len(cleaned) > 4:  # Skip short/common words
                    words.add(cleaned)
        # Consider compliant if at least 2 meaningful words appear in response
        matches = sum(1 for w in words if w in response_lower)
        results[censor_id] = matches >= 2
    return results
```

- [ ] **Step 4: Add compliance check to post_turn**

In `nous/cognitive/layer.py`, in `post_turn()`, after the existing censor check block (around line 731), add:

```python
        # F031: Post-turn compliance check for censor-injected context
        if turn_context.censor_injected_context:
            compliance = _check_censor_compliance(
                turn_context.censor_injected_context,
                turn_result.response_text,
            )
            for censor_id, used in compliance.items():
                if used:
                    logger.info(
                        "Censor compliance: agent referenced injected context (session=%s, censor=%s)",
                        session_id, censor_id,
                    )
                else:
                    logger.warning(
                        "Censor compliance: agent did NOT reference injected context (session=%s, censor=%s)",
                        session_id, censor_id,
                    )
```

- [ ] **Step 5: Run test to verify it passes**

```bash
uv run pytest tests/test_censors.py::test_post_turn_logs_censor_compliance -v
```

Expected: PASS

- [ ] **Step 6: Run full test suite**

```bash
uv run pytest tests/test_censors.py -v
```

Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add nous/cognitive/layer.py tests/test_censors.py
git commit -m "feat(f031): add post-turn compliance verification for censor actions"
```

---

## Task 6: Full Integration Test + Regression

**Files:**
- Test: `tests/test_censors.py` (append)

- [ ] **Step 1: Write end-to-end integration test**

Add to `tests/test_censors.py`:

```python
async def test_censor_action_end_to_end(heart, session):
    """Full flow: create censor with action -> check -> execute -> get results."""
    # 1. Seed a fact
    from nous.heart.schemas import FactInput
    await heart.learn_fact(
        FactInput(content="Paris is the capital of France", category="geography", subject="France"),
        session=session,
    )

    # 2. Create a censor with trigger_action
    inp = CensorInput(
        trigger_pattern="capital.*country|what.*capital",
        reason="Verify geographic claims",
        action="warn",
        trigger_action={"tool": "recall", "args": {"query": "capital country geography", "limit": 3}},
        action_instruction="Verify geographic claims against recalled facts.",
    )
    detail = await heart.add_censor(inp, session=session)
    assert detail.trigger_action is not None

    # 3. Check censors against matching input
    matches = await heart.check_censors("What is the capital of France?", session=session)
    assert len(matches) >= 1
    warn_match = [m for m in matches if m.action == "warn"]
    assert len(warn_match) >= 1
    assert warn_match[0].trigger_action is not None

    # 4. Execute the action
    from nous.heart.censor_actions import CensorActionExecutor
    executor = CensorActionExecutor(heart)
    result = await executor.execute(warn_match[0].trigger_action, session=session)
    assert result is not None
    assert "capital" in result.lower() or "france" in result.lower() or "paris" in result.lower()


async def test_multiple_censor_actions_all_injected(heart, session):
    """When multiple warn censors with trigger_action fire, all results are collected."""
    from nous.heart.schemas import FactInput
    await heart.learn_fact(
        FactInput(content="Python is a programming language", category="tech", subject="Python"),
        session=session,
    )
    await heart.learn_fact(
        FactInput(content="Security best practices include input validation", category="security", subject="security"),
        session=session,
    )

    # Create two censors that both match
    inp1 = CensorInput(
        trigger_pattern="python.*code",
        reason="Check coding standards",
        action="warn",
        trigger_action={"tool": "recall", "args": {"query": "python programming", "limit": 3}},
    )
    inp2 = CensorInput(
        trigger_pattern="python.*code",
        reason="Check security",
        action="warn",
        trigger_action={"tool": "search_facts", "args": {"query": "security", "limit": 3}},
    )
    await heart.add_censor(inp1, session=session)
    await heart.add_censor(inp2, session=session)

    matches = await heart.check_censors("Write python code for login", session=session)
    warn_matches = [m for m in matches if m.action == "warn" and m.trigger_action]
    assert len(warn_matches) >= 2

    from nous.heart.censor_actions import CensorActionExecutor
    executor = CensorActionExecutor(heart)
    results = {}
    for m in warn_matches:
        result = await executor.execute(m.trigger_action, session=session)
        if result:
            results[str(m.id)] = result
    assert len(results) >= 2


async def test_block_censor_with_action_enriches_reason(heart, session):
    """Block censor with trigger_action enriches the block reason with evidence."""
    from nous.heart.schemas import FactInput
    await heart.learn_fact(
        FactInput(content="Production database was accidentally deleted on 2025-12-01", category="incident", subject="production"),
        session=session,
    )

    inp = CensorInput(
        trigger_pattern="delete.*production|drop.*production",
        reason="Destructive production operations are prohibited",
        action="block",
        trigger_action={"tool": "recall", "args": {"query": "production deletion incident", "limit": 3}},
        action_instruction="Contact the infrastructure team for production changes.",
    )
    detail = await heart.add_censor(inp, session=session)
    assert detail.action == "block"
    assert detail.trigger_action is not None

    # Verify the match carries trigger_action
    matches = await heart.check_censors("Let's delete the production database", session=session)
    block_matches = [m for m in matches if m.action == "block"]
    assert len(block_matches) >= 1
    assert block_matches[0].trigger_action is not None
    assert block_matches[0].action_instruction == "Contact the infrastructure team for production changes."

    # Verify action execution works (results would be injected into block reason by layer)
    from nous.heart.censor_actions import CensorActionExecutor
    executor = CensorActionExecutor(heart)
    result = await executor.execute(block_matches[0].trigger_action, session=session)
    assert result is not None


async def test_block_censor_conditional_unblock(heart, session):
    """Block censor with unblock_pattern downgrades to warn when pattern matches action results."""
    from nous.heart.schemas import FactInput
    await heart.learn_fact(
        FactInput(content="Allowed admin: admin@company.com, ops@company.com", category="access", subject="admin-list"),
        session=session,
    )

    inp = CensorInput(
        trigger_pattern="delete.*production",
        reason="Production deletion requires admin access",
        action="block",
        trigger_action={"tool": "search_facts", "args": {"query": "allowed admin access", "limit": 5}},
        unblock_pattern=r"admin@company\.com",
        action_instruction="Contact infrastructure team if you need access.",
    )
    detail = await heart.add_censor(inp, session=session)
    assert detail.unblock_pattern is not None

    # Verify CensorActionExecutor returns results that match the unblock_pattern
    from nous.heart.censor_actions import CensorActionExecutor
    import re
    executor = CensorActionExecutor(heart)
    matches = await heart.check_censors("delete production database", session=session)
    block_match = [m for m in matches if m.trigger_pattern == "delete.*production"][0]

    result = await executor.execute(block_match.trigger_action, session=session)
    assert result is not None
    # The unblock_pattern should match the action results
    assert re.search(block_match.unblock_pattern, result, re.IGNORECASE)


async def test_block_censor_no_unblock_when_pattern_missing(heart, session):
    """Block censor without unblock_pattern always blocks (no downgrade)."""
    inp = CensorInput(
        trigger_pattern="drop.*table",
        reason="No dropping tables",
        action="block",
        trigger_action={"tool": "recall", "args": {"query": "table drops", "limit": 3}},
        # No unblock_pattern — always blocks
    )
    detail = await heart.add_censor(inp, session=session)
    assert detail.unblock_pattern is None

    matches = await heart.check_censors("drop table users", session=session)
    block_matches = [m for m in matches if m.action == "block"]
    assert len(block_matches) >= 1
    assert block_matches[0].unblock_pattern is None


async def test_backward_compat_censor_no_action(heart, session):
    """Censors without trigger_action work exactly as before."""
    inp = CensorInput(
        trigger_pattern="deploy.*friday",
        reason="No Friday deploys",
        action="warn",
    )
    detail = await heart.add_censor(inp, session=session)
    assert detail.trigger_action is None
    assert detail.action_instruction is None

    matches = await heart.check_censors("Let's deploy on Friday", session=session)
    assert len(matches) >= 1
    assert matches[0].trigger_action is None
    assert matches[0].action_instruction is None
```

- [ ] **Step 2: Run the integration tests**

```bash
uv run pytest tests/test_censors.py::test_censor_action_end_to_end tests/test_censors.py::test_backward_compat_censor_no_action -v
```

Expected: PASS

- [ ] **Step 3: Run the full test suite**

```bash
uv run pytest tests/ -v --timeout=120
```

Expected: all tests PASS, no regressions.

- [ ] **Step 4: Commit**

```bash
git add tests/test_censors.py
git commit -m "test(f031): add end-to-end integration and backward compat tests"
```

---

## Task 7: Censor Update API (Migrate Existing Censors)

**Files:**
- Modify: `nous/heart/censors.py`
- Modify: `nous/heart/heart.py`
- Modify: `nous/api/rest.py`
- Test: `tests/test_censors.py` (append)

Existing censors need to be upgradeable to the new format without recreating them. Add an `update()` method to CensorManager and a REST endpoint.

- [ ] **Step 1: Write failing test for censor update**

Add to `tests/test_censors.py`:

```python
async def test_update_censor_add_action_fields(heart, session):
    """Update an existing censor to add trigger_action and related fields."""
    # Create a basic censor (old format)
    inp = CensorInput(
        trigger_pattern="deploy.*friday",
        reason="No Friday deploys",
        action="warn",
    )
    detail = await heart.add_censor(inp, session=session)
    assert detail.trigger_action is None

    # Update it with action fields
    updated = await heart.update_censor(
        detail.id,
        trigger_action={"tool": "recall", "args": {"query": "deploy incidents", "limit": 3}},
        action_instruction="Check past deploy incidents before proceeding.",
        session=session,
    )
    assert updated.trigger_action == {"tool": "recall", "args": {"query": "deploy incidents", "limit": 3}}
    assert updated.action_instruction == "Check past deploy incidents before proceeding."
    # Original fields preserved
    assert updated.trigger_pattern == "deploy.*friday"
    assert updated.reason == "No Friday deploys"
    assert updated.action == "warn"


async def test_update_censor_add_unblock_pattern(heart, session):
    """Upgrade a block censor with unblock_pattern."""
    inp = CensorInput(
        trigger_pattern="delete.*production",
        reason="No production deletes",
        action="block",
    )
    detail = await heart.add_censor(inp, session=session)

    updated = await heart.update_censor(
        detail.id,
        trigger_action={"tool": "search_facts", "args": {"query": "allowed admins"}},
        unblock_pattern=r"admin@company\.com",
        action_instruction="Contact infra team.",
        session=session,
    )
    assert updated.unblock_pattern == r"admin@company\.com"
    assert updated.trigger_action is not None
    assert updated.action == "block"  # Action unchanged


async def test_update_censor_partial_update(heart, session):
    """Update only some fields, leaving others intact."""
    inp = CensorInput(
        trigger_pattern="test pattern",
        reason="test reason",
        action="warn",
        trigger_action={"tool": "recall", "args": {"query": "old"}},
        action_instruction="Old instruction",
    )
    detail = await heart.add_censor(inp, session=session)

    # Update only action_instruction
    updated = await heart.update_censor(
        detail.id,
        action_instruction="New instruction",
        session=session,
    )
    assert updated.action_instruction == "New instruction"
    assert updated.trigger_action == {"tool": "recall", "args": {"query": "old"}}  # Unchanged


async def test_update_censor_clear_field(heart, session):
    """Setting a field to explicit None clears it."""
    inp = CensorInput(
        trigger_pattern="test",
        reason="test",
        trigger_action={"tool": "recall", "args": {"query": "test"}},
    )
    detail = await heart.add_censor(inp, session=session)
    assert detail.trigger_action is not None

    # Clear trigger_action by passing empty dict sentinel
    updated = await heart.update_censor(
        detail.id,
        trigger_action=None,
        session=session,
    )
    assert updated.trigger_action is None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_censors.py::test_update_censor_add_action_fields tests/test_censors.py::test_update_censor_add_unblock_pattern tests/test_censors.py::test_update_censor_partial_update tests/test_censors.py::test_update_censor_clear_field -v
```

Expected: FAIL — `update_censor` doesn't exist.

- [ ] **Step 3: Implement CensorManager.update()**

In `nous/heart/censors.py`, add after the `deactivate()` method:

```python
    # ------------------------------------------------------------------
    # update() — F031: modify existing censor fields
    # ------------------------------------------------------------------

    _SENTINEL = object()  # Distinguishes "not passed" from "set to None"

    async def update(
        self,
        censor_id: UUID,
        *,
        trigger_action: dict | None | object = _SENTINEL,
        action_instruction: str | None | object = _SENTINEL,
        unblock_pattern: str | None | object = _SENTINEL,
        reason: str | None | object = _SENTINEL,
        domain: str | None | object = _SENTINEL,
        session: AsyncSession | None = None,
    ) -> CensorDetail:
        """Update specific fields on an existing censor.

        Only fields explicitly passed are updated. Pass None to clear a field.
        Fields not passed are left unchanged.
        """
        if session is None:
            async with self.db.session() as session:
                result = await self._update(
                    censor_id, trigger_action=trigger_action,
                    action_instruction=action_instruction,
                    unblock_pattern=unblock_pattern,
                    reason=reason, domain=domain, session=session,
                )
                await session.commit()
                return result
        return await self._update(
            censor_id, trigger_action=trigger_action,
            action_instruction=action_instruction,
            unblock_pattern=unblock_pattern,
            reason=reason, domain=domain, session=session,
        )

    async def _update(
        self,
        censor_id: UUID,
        *,
        trigger_action,
        action_instruction,
        unblock_pattern,
        reason,
        domain,
        session: AsyncSession,
    ) -> CensorDetail:
        result = await session.execute(
            select(Censor).where(Censor.id == censor_id).where(Censor.agent_id == self.agent_id)
        )
        censor = result.scalar_one_or_none()
        if censor is None:
            raise ValueError(f"Censor {censor_id} not found")

        SENTINEL = self._SENTINEL
        if trigger_action is not SENTINEL:
            censor.trigger_action = trigger_action
        if action_instruction is not SENTINEL:
            censor.action_instruction = action_instruction
        if unblock_pattern is not SENTINEL:
            censor.unblock_pattern = unblock_pattern
        if reason is not SENTINEL and reason is not None:
            censor.reason = reason
        if domain is not SENTINEL:
            censor.domain = domain

        censor.updated_at = datetime.now(UTC)
        await session.flush()
        return self._to_detail(censor)
```

- [ ] **Step 4: Add Heart wrapper method**

In `nous/heart/heart.py`, add after `deactivate_censor`:

```python
    async def update_censor(
        self,
        censor_id: UUID,
        *,
        trigger_action: dict | None | object = CensorManager._SENTINEL,
        action_instruction: str | None | object = CensorManager._SENTINEL,
        unblock_pattern: str | None | object = CensorManager._SENTINEL,
        reason: str | None | object = CensorManager._SENTINEL,
        domain: str | None | object = CensorManager._SENTINEL,
        session: AsyncSession | None = None,
    ) -> CensorDetail:
        """Update specific fields on an existing censor (F031)."""
        return await self.censors.update(
            censor_id,
            trigger_action=trigger_action,
            action_instruction=action_instruction,
            unblock_pattern=unblock_pattern,
            reason=reason,
            domain=domain,
            session=session,
        )
```

Import `CensorManager` from `nous.heart.censors` if not already available (it should be, since Heart creates `self.censors = CensorManager(...)`).

- [ ] **Step 5: Add REST endpoint**

In `nous/api/rest.py`, add a new endpoint function:

```python
async def update_censor(request: Request) -> JSONResponse:
    """PUT /censors/{id} - Update censor fields (F031)."""
    censor_id = request.path_params["id"]
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    kwargs = {}
    SENTINEL = heart.censors._SENTINEL
    for field in ("trigger_action", "action_instruction", "unblock_pattern", "reason", "domain"):
        if field in body:
            kwargs[field] = body[field]

    if not kwargs:
        return JSONResponse({"error": "No fields to update"}, status_code=400)

    try:
        from uuid import UUID
        detail = await heart.update_censor(UUID(censor_id), **kwargs)
        return JSONResponse(detail.model_dump(mode="json"))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except Exception as e:
        logger.error("Update censor error: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)
```

Add route to the routes list (near line 1252 where `/censors` is registered):

```python
Route("/censors/{id}", update_censor, methods=["PUT"]),
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
uv run pytest tests/test_censors.py::test_update_censor_add_action_fields tests/test_censors.py::test_update_censor_add_unblock_pattern tests/test_censors.py::test_update_censor_partial_update tests/test_censors.py::test_update_censor_clear_field -v
```

Expected: PASS

- [ ] **Step 7: Run full censor test suite**

```bash
uv run pytest tests/test_censors.py -v
```

Expected: all PASS.

- [ ] **Step 8: Commit**

```bash
git add nous/heart/censors.py nous/heart/heart.py nous/api/rest.py tests/test_censors.py
git commit -m "feat(f031): add censor update API for migrating existing censors to new format"
```

---

## Task 8: Documentation Update

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update CLAUDE.md feature table**

Add F031 to the "What's Shipped" table in `CLAUDE.md`:

```
| F031 | Censor Middleware with Action Payloads (censors execute read-only tools, conditional unblock, update API) | #TBD |
```

- [ ] **Step 2: Update REST endpoints table**

Add to the REST endpoints table:

```
| PUT | `/censors/{id}` | Update censor fields (trigger_action, action_instruction, unblock_pattern) |
```

- [ ] **Step 3: Update environment variables section**

No new env vars needed — F031 uses existing configuration.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(f031): add F031 to shipped features table and REST endpoints"
```
