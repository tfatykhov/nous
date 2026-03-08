# Subtask & Schedule Improvements Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix subtask model fallback, notify defaults, error swallowing, and add model/frame_type support to scheduled tasks.

**Architecture:** Five independent changes across the subtask/schedule stack. Each change is small and testable in isolation. The runner error fix (Task 3) is the most nuanced — it must re-raise after cleanup without breaking REST/Telegram callers.

**Tech Stack:** Python 3.12+, SQLAlchemy 2.0 async ORM, PostgreSQL, pytest + pytest-asyncio

**Design doc:** `docs/plans/2026-03-08-subtask-improvements-design.md`

---

### Task 1: Flip `notify` default to `False`

**Files:**
- Modify: `nous/heart/subtasks.py:33` — change default in `create()` signature
- Modify: `nous/heart/schedules.py:30` — change default in `create()` signature
- Modify: `nous/api/tools.py:853` — change default in `schedule_task()` signature
- Modify: `sql/init.sql:449` — change `DEFAULT TRUE` to `DEFAULT FALSE` for `notify` in `heart.schedules`
- Modify: `sql/init.sql` — change `DEFAULT TRUE` to `DEFAULT FALSE` for `notify` in `heart.subtasks`
- Modify: `nous/storage/models.py` — update `server_default` for `notify` on both Subtask and Schedule models
- Test: `tests/test_subtasks.py`
- Test: `tests/test_schedules.py`

**Step 1: Write failing tests**

In `tests/test_subtasks.py`, add to `TestSubtaskModel`:
```python
async def test_subtask_notify_defaults_false(self, session: AsyncSession):
    subtask = Subtask(
        agent_id="test-agent",
        task="Background check",
        priority=100,
        timeout_seconds=60,
    )
    session.add(subtask)
    await session.flush()
    assert subtask.notify is False
```

In `tests/test_schedules.py`, add to `TestScheduleManager`:
```python
async def test_schedule_notify_defaults_false(self, schedule_mgr: ScheduleManager):
    fire_at = datetime.now(UTC) + timedelta(hours=1)
    schedule = await schedule_mgr.create(
        task="Check status",
        schedule_type="once",
        fire_at=fire_at,
    )
    assert schedule.notify is False
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_subtasks.py::TestSubtaskModel::test_subtask_notify_defaults_false tests/test_schedules.py::TestScheduleManager::test_schedule_notify_defaults_false -v`
Expected: FAIL — `assert True is False`

**Step 3: Implement changes**

In `nous/heart/subtasks.py:33`, change:
```python
notify: bool = True,
```
to:
```python
notify: bool = False,
```

In `nous/heart/schedules.py:30`, change:
```python
notify: bool = True,
```
to:
```python
notify: bool = False,
```

In `nous/api/tools.py`, find `schedule_task` signature (~line 853), change:
```python
notify: bool = True,
```
to:
```python
notify: bool = False,
```

In `nous/storage/models.py`, find the Subtask model `notify` field and change `server_default="true"` to `server_default="false"`. Do the same for the Schedule model `notify` field.

In `sql/init.sql`, find the `heart.subtasks` table `notify` column and change `DEFAULT TRUE` to `DEFAULT FALSE`. Do the same for the `heart.schedules` table.

**Step 4: Fix existing test that asserts old default**

In `tests/test_subtasks.py:44`, the existing `test_create_subtask` asserts `assert subtask.notify is True`. Update it to `assert subtask.notify is False`.

**Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_subtasks.py::TestSubtaskModel tests/test_schedules.py::TestScheduleManager -v`
Expected: PASS

**Step 6: Commit**

```bash
git add nous/heart/subtasks.py nous/heart/schedules.py nous/api/tools.py nous/storage/models.py sql/init.sql tests/test_subtasks.py tests/test_schedules.py
git commit -m "fix: flip notify default to false for subtasks and schedules"
```

---

### Task 2: Subtask model fallback to `background_model`

**Files:**
- Modify: `nous/handlers/subtask_worker.py:152` — pass `background_model` as fallback
- Modify: `nous/api/tools.py:738-753` — add `background_model` fallback for inline execution
- Test: `tests/test_subtasks.py`

**Context:** Currently, when no explicit model is provided:
- `subtask_worker.py:152` passes `model_override=subtask.model` (which is `None`)
- `tools.py:807` passes `model_override=effective_model` (which is `None` if no frame default)
- `runner.py:466` then falls back to `self._settings.model` (the main chat model)

The fix: resolve the model to `settings.background_model` before passing to `run_turn()`, so the fallback chain becomes: explicit model → frame default → `background_model`.

**Step 1: Write failing tests**

In `tests/test_subtasks.py`, add a new test class:
```python
class TestSubtaskModelFallback:
    """Verify subtask model falls back to background_model, not main model."""

    async def test_worker_uses_background_model_when_no_override(self):
        """Worker should pass background_model when subtask has no model set."""
        settings = Settings(
            anthropic_api_key="test",
            model="claude-sonnet-4-5-20250514",
            background_model="claude-haiku-3-5-20241022",
        )
        mock_runner = AsyncMock()
        mock_runner.run_turn = AsyncMock(return_value=("result", MagicMock(), {}))
        mock_heart = MagicMock()
        mock_heart.subtasks = AsyncMock()
        mock_heart.subtasks.complete = AsyncMock()

        pool = SubtaskWorkerPool(
            runner=mock_runner, heart=mock_heart, settings=settings
        )

        subtask = Subtask(
            agent_id="test-agent",
            task="Do something",
            priority=100,
            timeout_seconds=60,
            model=None,          # No explicit model
            frame_type=None,     # No frame type
        )
        subtask.id = uuid.uuid4()

        await pool._execute_subtask(subtask)

        # Should have called run_turn with background_model, not main model
        call_kwargs = mock_runner.run_turn.call_args.kwargs
        assert call_kwargs["model_override"] == "claude-haiku-3-5-20241022"

    async def test_worker_uses_explicit_model_over_background(self):
        """Worker should prefer explicit model over background_model."""
        settings = Settings(
            anthropic_api_key="test",
            model="claude-sonnet-4-5-20250514",
            background_model="claude-haiku-3-5-20241022",
        )
        mock_runner = AsyncMock()
        mock_runner.run_turn = AsyncMock(return_value=("result", MagicMock(), {}))
        mock_heart = MagicMock()
        mock_heart.subtasks = AsyncMock()
        mock_heart.subtasks.complete = AsyncMock()

        pool = SubtaskWorkerPool(
            runner=mock_runner, heart=mock_heart, settings=settings
        )

        subtask = Subtask(
            agent_id="test-agent",
            task="Do something",
            priority=100,
            timeout_seconds=60,
            model="claude-opus-4-20250514",  # Explicit model
            frame_type=None,
        )
        subtask.id = uuid.uuid4()

        await pool._execute_subtask(subtask)

        call_kwargs = mock_runner.run_turn.call_args.kwargs
        assert call_kwargs["model_override"] == "claude-opus-4-20250514"
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_subtasks.py::TestSubtaskModelFallback -v`
Expected: FAIL — `assert None == "claude-haiku-3-5-20241022"` (the first test)

**Step 3: Implement the fix**

In `nous/handlers/subtask_worker.py`, find `_execute_subtask` method (~line 129). Change the `model_override` argument at line 152:

Before:
```python
model_override=subtask.model,
```

After:
```python
model_override=subtask.model or self._settings.background_model,
```

In `nous/api/tools.py`, find the inline execution block (~line 738). After the frame-default resolution, add background_model fallback:

Before (around line 738-741):
```python
# 012.2: Apply frame-default model mapping
effective_model = model
if not effective_model and frame_type:
    effective_model = settings.frame_default_models.get(frame_type)
```

After:
```python
# 012.2: Apply frame-default model mapping
effective_model = model
if not effective_model and frame_type:
    effective_model = settings.frame_default_models.get(frame_type)
if not effective_model:
    effective_model = settings.background_model
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_subtasks.py::TestSubtaskModelFallback -v`
Expected: PASS

**Step 5: Commit**

```bash
git add nous/handlers/subtask_worker.py nous/api/tools.py tests/test_subtasks.py
git commit -m "fix: subtask model fallback to background_model instead of main model"
```

---

### Task 3: Re-raise exceptions from `run_turn()` after cleanup

**Files:**
- Modify: `nous/api/runner.py:382-404` — re-raise after post_turn in `run_turn()`
- Modify: `nous/api/runner.py:913-928` — re-raise after post_turn in `stream_chat()`
- Test: `tests/test_subtasks.py`

**Context:** Currently `run_turn()` at line 382-386 catches all exceptions, replaces the response with a generic message, and returns normally. The subtask worker then marks the task "completed" with the useless message. The fix: after post-turn cleanup, re-raise the original exception.

The REST API (`rest.py`) and Telegram bot (`telegram_bot.py`) both already wrap their `run_turn()` / `stream_chat()` calls in try/except, so re-raising won't crash them.

**Step 1: Write failing test**

In `tests/test_subtasks.py`, add:
```python
class TestRunTurnErrorPropagation:
    """Verify run_turn re-raises exceptions after cleanup."""

    async def test_run_turn_reraises_after_post_turn(self):
        """run_turn should re-raise API errors so callers can handle them."""
        settings = Settings(anthropic_api_key="test")
        mock_cognitive = AsyncMock()
        mock_cognitive.pre_turn = AsyncMock(return_value=MagicMock(
            frame=MagicMock(frame_id="task"),
            system_context="",
        ))
        mock_cognitive.post_turn = AsyncMock()
        mock_heart = MagicMock()

        from nous.api.runner import AgentRunner
        runner = AgentRunner(
            settings=settings,
            cognitive=mock_cognitive,
            heart=mock_heart,
        )

        # Mock _tool_loop to raise an exception
        runner._tool_loop = AsyncMock(side_effect=RuntimeError("API timeout"))
        runner._build_system_prompt = MagicMock(return_value="system")

        with pytest.raises(RuntimeError, match="API timeout"):
            await runner.run_turn(
                session_id="test-session",
                user_message="hello",
            )

        # post_turn should still have been called (cleanup)
        mock_cognitive.post_turn.assert_called_once()
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_subtasks.py::TestRunTurnErrorPropagation -v`
Expected: FAIL — `RuntimeError` not raised (currently swallowed)

**Step 3: Implement the fix**

In `nous/api/runner.py`, modify the `run_turn()` error handling block (~lines 382-404).

Before:
```python
        except Exception as e:
            logger.error("API call error: %s", e)
            error = str(e)
            thinking_blocks = []
            response_text = "I encountered an error processing your request. Please try again."
            conversation.messages.append(Message(role="assistant", content=response_text))

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

        return response_text, turn_context, usage
```

After:
```python
        except Exception as e:
            logger.error("API call error: %s", e)
            error = str(e)
            thinking_blocks = []
            response_text = "I encountered an error processing your request. Please try again."
            conversation.messages.append(Message(role="assistant", content=response_text))
            _caught_exc = e
        else:
            _caught_exc = None

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
```

For `stream_chat()` (~lines 913-928), similar change. The `except` block is inside a `try/finally`, so:

Before:
```python
        except Exception as e:
            logger.error("Streaming error: %s", e)
            error = str(e)
            response_text = "I encountered an error processing your request."
            conversation.messages.append(Message(role="assistant", content=response_text))
        finally:
            # ALWAYS call post_turn (review P1: guaranteed cleanup)
            turn_result = TurnResult(...)
            await self._cognitive.post_turn(...)
            self._check_safety_net(...)
            conversation.turn_contexts.append(turn_context)
```

After:
```python
        except Exception as e:
            logger.error("Streaming error: %s", e)
            error = str(e)
            response_text = "I encountered an error processing your request."
            conversation.messages.append(Message(role="assistant", content=response_text))
            _caught_exc = e
        else:
            _caught_exc = None
        finally:
            # ALWAYS call post_turn (review P1: guaranteed cleanup)
            turn_result = TurnResult(...)
            await self._cognitive.post_turn(...)
            self._check_safety_net(...)
            conversation.turn_contexts.append(turn_context)

        # Re-raise after cleanup so callers see the real error
        if _caught_exc is not None:
            raise _caught_exc
```

**Important:** The `_caught_exc` variable must be initialized before the try block (set `_caught_exc = None` before the outer try) in case the code path skips the except/else blocks. For `stream_chat`, since `finally` runs before the re-raise check, move the re-raise AFTER the finally block ends.

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_subtasks.py::TestRunTurnErrorPropagation -v`
Expected: PASS

**Step 5: Run full test suite to verify no regressions**

Run: `uv run pytest tests/ -v --timeout=60`
Expected: All existing tests still pass. REST/Telegram callers have their own try/except around `run_turn()` / `stream_chat()`.

**Step 6: Commit**

```bash
git add nous/api/runner.py tests/test_subtasks.py
git commit -m "fix: re-raise exceptions from run_turn/stream_chat after cleanup

Previously, run_turn() swallowed API exceptions and returned a generic
error message. This caused subtask workers to mark failed tasks as
'completed'. Now exceptions are re-raised after post_turn cleanup,
allowing callers to handle errors properly."
```

---

### Task 4: Add `model` and `frame_type` to scheduled tasks

**Files:**
- Modify: `sql/init.sql:436-459` — add columns to `heart.schedules` table
- Modify: `nous/storage/models.py:581-613` — add columns to Schedule ORM model
- Modify: `nous/heart/schedules.py:23-71` — add params to `create()` method
- Modify: `nous/api/tools.py:849-921` — add params to `schedule_task()` tool
- Modify: `nous/handlers/task_scheduler.py:88-98` — pass model/frame_type to subtask creation
- Create: `sql/migrations/014_schedule_model_frame.sql` — migration script
- Test: `tests/test_schedules.py`

**Step 1: Write failing tests**

In `tests/test_schedules.py`, add to `TestScheduleManager`:
```python
async def test_create_schedule_with_model(self, schedule_mgr: ScheduleManager):
    fire_at = datetime.now(UTC) + timedelta(hours=1)
    schedule = await schedule_mgr.create(
        task="Research topic",
        schedule_type="once",
        fire_at=fire_at,
        model="claude-haiku-3-5-20241022",
    )
    assert schedule.model == "claude-haiku-3-5-20241022"

async def test_create_schedule_with_frame_type(self, schedule_mgr: ScheduleManager):
    fire_at = datetime.now(UTC) + timedelta(hours=1)
    schedule = await schedule_mgr.create(
        task="Research topic",
        schedule_type="once",
        fire_at=fire_at,
        frame_type="research",
    )
    assert schedule.frame_type == "research"

async def test_schedule_model_defaults_none(self, schedule_mgr: ScheduleManager):
    fire_at = datetime.now(UTC) + timedelta(hours=1)
    schedule = await schedule_mgr.create(
        task="Check status",
        schedule_type="once",
        fire_at=fire_at,
    )
    assert schedule.model is None
    assert schedule.frame_type is None
```

Add a new test class for scheduler integration:
```python
class TestSchedulerModelPassthrough:
    """Verify scheduler passes model/frame_type when creating subtasks."""

    async def test_scheduler_passes_model_and_frame_type(self, db):
        from unittest.mock import AsyncMock, MagicMock

        settings = Settings(anthropic_api_key="test")
        mock_heart = MagicMock()
        mock_heart.schedules = AsyncMock()
        mock_heart.subtasks = AsyncMock()
        mock_heart.subtasks.create = AsyncMock()

        # Create a mock schedule with model and frame_type
        mock_schedule = MagicMock()
        mock_schedule.id = uuid.uuid4()
        mock_schedule.task = "Research AI papers"
        mock_schedule.schedule_type = "once"
        mock_schedule.timeout_seconds = 120
        mock_schedule.notify = False
        mock_schedule.created_by_session = "session-123"
        mock_schedule.model = "claude-haiku-3-5-20241022"
        mock_schedule.frame_type = "research"

        mock_heart.schedules.get_due = AsyncMock(return_value=[mock_schedule])
        mock_heart.schedules.deactivate = AsyncMock()

        scheduler = TaskScheduler(heart=mock_heart, settings=settings)
        await scheduler._fire_due_tasks(datetime.now(UTC))

        # Verify subtask was created with model and frame_type
        create_kwargs = mock_heart.subtasks.create.call_args.kwargs
        assert create_kwargs["model"] == "claude-haiku-3-5-20241022"
        assert create_kwargs["frame_type"] == "research"
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_schedules.py::TestScheduleManager::test_create_schedule_with_model tests/test_schedules.py::TestScheduleManager::test_create_schedule_with_frame_type tests/test_schedules.py::TestSchedulerModelPassthrough -v`
Expected: FAIL — `model` attribute doesn't exist on Schedule

**Step 3: Add columns to SQL schema**

In `sql/init.sql`, find the `heart.schedules` CREATE TABLE (~line 436). Add two columns before the `CONSTRAINT` lines:

After the `metadata JSONB NOT NULL DEFAULT '{}'::jsonb,` line, add:
```sql
    model VARCHAR(100),
    frame_type VARCHAR(20),
```

**Step 4: Add columns to ORM model**

In `nous/storage/models.py`, find the Schedule class (~line 581). After the `metadata_` field (~line 613), add:
```python
    model: Mapped[str | None] = mapped_column(String(100))
    frame_type: Mapped[str | None] = mapped_column(String(20))
```

**Step 5: Add params to `ScheduleManager.create()`**

In `nous/heart/schedules.py`, update the `create()` method signature (~line 23) to add:
```python
    model: str | None = None,
    frame_type: str | None = None,
```

In the Schedule constructor call inside `create()` (~line 50), add:
```python
    model=model,
    frame_type=frame_type,
```

**Step 6: Add params to `schedule_task()` tool**

In `nous/api/tools.py`, update `schedule_task()` signature (~line 849) to add:
```python
    model: str | None = None,
    frame_type: str | None = None,
```

Pass them through to `heart.schedules.create()` at both call sites (~line 883 and ~line 892):
```python
    model=model,
    frame_type=frame_type,
```

Also update the tool's docstring to document the new parameters.

**Step 7: Pass model/frame_type from scheduler to subtask**

In `nous/handlers/task_scheduler.py`, find the `subtasks.create()` call (~line 91). Add `model` and `frame_type`:

Before:
```python
await self._heart.subtasks.create(
    task=schedule.task,
    parent_session_id=schedule.created_by_session,
    priority="normal",
    timeout=schedule.timeout_seconds,
    notify=schedule.notify,
    metadata={"schedule_id": schedule.id.hex},
)
```

After:
```python
await self._heart.subtasks.create(
    task=schedule.task,
    parent_session_id=schedule.created_by_session,
    priority="normal",
    timeout=schedule.timeout_seconds,
    notify=schedule.notify,
    model=schedule.model,
    frame_type=schedule.frame_type,
    metadata={"schedule_id": schedule.id.hex},
)
```

**Step 8: Create migration script**

Create `sql/migrations/014_schedule_model_frame.sql`:
```sql
-- Migration: Add model and frame_type columns to heart.schedules
-- Part of subtask/schedule improvements (2026-03-08)

ALTER TABLE heart.schedules ADD COLUMN IF NOT EXISTS model VARCHAR(100);
ALTER TABLE heart.schedules ADD COLUMN IF NOT EXISTS frame_type VARCHAR(20);
```

**Step 9: Run tests to verify they pass**

Run: `uv run pytest tests/test_schedules.py -v`
Expected: PASS

**Step 10: Commit**

```bash
git add sql/init.sql sql/migrations/014_schedule_model_frame.sql nous/storage/models.py nous/heart/schedules.py nous/api/tools.py nous/handlers/task_scheduler.py tests/test_schedules.py
git commit -m "feat: add model and frame_type support to scheduled tasks

Scheduled tasks can now specify a model override and frame_type,
which are passed through to subtasks when the schedule fires.
Includes migration script for existing installations."
```

---

### Task 5: Update `schedule_task` tool registration

**Files:**
- Modify: `nous/api/tools.py` — update the tool schema/registration for `schedule_task` to expose `model` and `frame_type` parameters

**Context:** The tool dispatcher needs to know about the new parameters so the LLM can use them. Check how `spawn_task` registers its `model` and `frame_type` params and follow the same pattern.

**Step 1: Find the tool registration block**

Search for the `schedule_task` tool registration in `nous/api/tools.py`. It should be near the `spawn_task` registration. Look for the `inputSchema` or parameter definitions.

**Step 2: Add `model` and `frame_type` to the schema**

Add them with the same structure as `spawn_task`'s parameters:
```python
"model": {
    "type": "string",
    "description": "Model override for this scheduled task (default: background model)",
},
"frame_type": {
    "type": "string",
    "description": "Cognitive frame type (e.g. 'research', 'task')",
    "enum": ["task", "research", "conversation", "decision", "debug"],
},
```

**Step 3: Run full test suite**

Run: `uv run pytest tests/test_subtasks.py tests/test_schedules.py -v`
Expected: PASS

**Step 4: Commit**

```bash
git add nous/api/tools.py
git commit -m "feat: expose model and frame_type in schedule_task tool schema"
```

---

### Task 6: Final verification

**Step 1: Run full test suite**

Run: `uv run pytest tests/ -v --timeout=60`
Expected: All tests pass

**Step 2: Verify no import errors**

Run: `uv run python -c "from nous.api.tools import spawn_task, schedule_task; from nous.handlers.subtask_worker import SubtaskWorkerPool; from nous.handlers.task_scheduler import TaskScheduler; print('OK')"`
Expected: `OK`

**Step 3: Final commit (if any fixups needed)**

Only if test failures required fixes in previous steps.
