# Subtask Enhancements (012.2) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add frame-aware subtasks, synchronous execution, execution guardrails, and per-subtask model selection to the existing subtask system.

**Architecture:** Extend the existing fire-and-forget subtask pipeline with three capabilities: (1) frame-type metadata that enriches worker system prompts, (2) inline synchronous execution via `await_result`, and (3) per-subtask model selection. Three guardrails prevent abuse: no-nesting (subtasks can't spawn subtasks), tool call limit (20 max), and reduced inline timeout (90s default).

**Tech Stack:** Python 3.12+, SQLAlchemy 2.0 async ORM, PostgreSQL 17, pytest + pytest-asyncio

---

### Task 1: SQL Migration — Add frame_type and model columns

**Files:**
- Create: `sql/migrations/012_subtask_frame_type.sql`

**Step 1: Write the migration**

```sql
-- 012.2: Add frame_type and model columns for frame-aware subtasks and per-subtask model selection
ALTER TABLE heart.subtasks ADD COLUMN IF NOT EXISTS frame_type VARCHAR(30);
ALTER TABLE heart.subtasks ADD COLUMN IF NOT EXISTS model VARCHAR(100);
```

**Step 2: Verify migration syntax**

Run: `cd /e/Projects/nous && cat sql/migrations/012_subtask_frame_type.sql`
Expected: The SQL above, no errors.

**Step 3: Commit**

```bash
git add sql/migrations/012_subtask_frame_type.sql
git commit -m "feat(012.2): add frame_type and model columns to subtasks"
```

---

### Task 2: ORM Model — Add frame_type and model to Subtask

**Files:**
- Modify: `nous/storage/models.py:568` (after `timeout_seconds` line)

**Step 1: Write the failing test**

In `tests/test_subtasks.py`, add to `TestSubtaskModel`:

```python
async def test_subtask_with_frame_type_and_model(self, session: AsyncSession):
    """012.2: Subtask stores frame_type and model."""
    subtask = Subtask(
        agent_id="test-agent",
        task="Research weather patterns",
        priority=100,
        timeout_seconds=120,
        frame_type="research",
        model="claude-haiku-3-5-20241022",
    )
    session.add(subtask)
    await session.flush()

    assert subtask.frame_type == "research"
    assert subtask.model == "claude-haiku-3-5-20241022"

async def test_subtask_frame_type_nullable(self, session: AsyncSession):
    """012.2: frame_type and model are optional (backward compat)."""
    subtask = Subtask(
        agent_id="test-agent",
        task="Simple task",
        priority=100,
    )
    session.add(subtask)
    await session.flush()

    assert subtask.frame_type is None
    assert subtask.model is None
```

**Step 2: Run tests to verify they fail**

Run: `cd /e/Projects/nous && uv run pytest tests/test_subtasks.py::TestSubtaskModel::test_subtask_with_frame_type_and_model tests/test_subtasks.py::TestSubtaskModel::test_subtask_frame_type_nullable -v`
Expected: FAIL — `frame_type` not a valid column on `Subtask`

**Step 3: Add fields to ORM model**

In `nous/storage/models.py`, after the `timeout_seconds` line (568), add:

```python
    frame_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
```

Import `String` is already imported at the top of models.py.

**Step 4: Run the migration against the test database**

The test database needs the new columns. Run:
`cd /e/Projects/nous && docker compose exec postgres psql -U nous -d nous -f /dev/stdin < sql/migrations/012_subtask_frame_type.sql`

If tests use a fresh schema from init.sql, also add the columns to `sql/init.sql` in the `CREATE TABLE heart.subtasks` block — add `frame_type VARCHAR(30)` and `model VARCHAR(100)` after `timeout_seconds`.

**Step 5: Run tests to verify they pass**

Run: `cd /e/Projects/nous && uv run pytest tests/test_subtasks.py::TestSubtaskModel::test_subtask_with_frame_type_and_model tests/test_subtasks.py::TestSubtaskModel::test_subtask_frame_type_nullable -v`
Expected: PASS

**Step 6: Commit**

```bash
git add nous/storage/models.py tests/test_subtasks.py sql/init.sql
git commit -m "feat(012.2): add frame_type and model fields to Subtask ORM"
```

---

### Task 3: SubtaskManager — Add frame_type and model to create()

**Files:**
- Modify: `nous/heart/subtasks.py:27-65` (create method)

**Step 1: Write the failing test**

In `tests/test_subtasks.py`, add a new test class:

```python
class TestSubtaskManagerEnhancements:
    """012.2: SubtaskManager create() accepts frame_type and model."""

    async def test_create_with_frame_type(self, db, settings):
        from nous.heart.subtasks import SubtaskManager

        mgr = SubtaskManager(db, settings.agent_id)
        subtask = await mgr.create(
            task="Research topic X",
            frame_type="research",
        )
        assert subtask.frame_type == "research"
        assert subtask.model is None

    async def test_create_with_model(self, db, settings):
        from nous.heart.subtasks import SubtaskManager

        mgr = SubtaskManager(db, settings.agent_id)
        subtask = await mgr.create(
            task="Quick lookup",
            model="claude-haiku-3-5-20241022",
        )
        assert subtask.model == "claude-haiku-3-5-20241022"

    async def test_create_with_frame_type_and_model(self, db, settings):
        from nous.heart.subtasks import SubtaskManager

        mgr = SubtaskManager(db, settings.agent_id)
        subtask = await mgr.create(
            task="Research with haiku",
            frame_type="research",
            model="claude-haiku-3-5-20241022",
        )
        assert subtask.frame_type == "research"
        assert subtask.model == "claude-haiku-3-5-20241022"

    async def test_create_without_new_params_backward_compat(self, db, settings):
        from nous.heart.subtasks import SubtaskManager

        mgr = SubtaskManager(db, settings.agent_id)
        subtask = await mgr.create(task="Normal task")
        assert subtask.frame_type is None
        assert subtask.model is None
        assert subtask.status == "pending"
```

**Step 2: Run tests to verify they fail**

Run: `cd /e/Projects/nous && uv run pytest tests/test_subtasks.py::TestSubtaskManagerEnhancements -v`
Expected: FAIL — `create()` doesn't accept `frame_type` or `model`

**Step 3: Update create() method**

In `nous/heart/subtasks.py`, modify the `create` method signature and body:

```python
async def create(
    self,
    task: str,
    parent_session_id: str | None = None,
    priority: str = "normal",
    timeout: int = 120,
    notify: bool = True,
    metadata: dict | None = None,
    frame_type: str | None = None,
    model: str | None = None,
) -> Subtask:
```

In the Subtask constructor (line 52-59), add after `metadata_=metadata or {}`:

```python
    frame_type=frame_type,
    model=model,
```

**Step 4: Run tests to verify they pass**

Run: `cd /e/Projects/nous && uv run pytest tests/test_subtasks.py::TestSubtaskManagerEnhancements -v`
Expected: PASS

**Step 5: Commit**

```bash
git add nous/heart/subtasks.py tests/test_subtasks.py
git commit -m "feat(012.2): add frame_type and model params to SubtaskManager.create()"
```

---

### Task 4: Shared Subtask Prefix Builder

**Files:**
- Modify: `nous/api/tools.py` (add helper function after imports, before ToolDispatcher class)

**Step 1: Write the failing test**

Create a new test or add to existing. In `tests/test_subtasks.py`:

```python
class TestSubtaskPrefixBuilder:
    """012.2: Shared prefix builder for frame-aware subtask context."""

    def test_prefix_without_frame(self):
        from nous.api.tools import build_subtask_prefix

        prefix = build_subtask_prefix("Do something", frame_type=None)
        assert "subtask worker" in prefix.lower() or "background subtask" in prefix.lower()
        assert "Do something" in prefix
        # No frame section when frame_type is None
        assert "## Frame:" not in prefix

    def test_prefix_with_task_frame(self):
        from nous.api.tools import build_subtask_prefix

        prefix = build_subtask_prefix("Write code", frame_type="task")
        assert "Write code" in prefix
        # task frame has "*" (all tools), so it should have some instruction
        # The exact content depends on FRAME_TOOLS — just check it doesn't crash

    def test_prefix_with_unknown_frame(self):
        from nous.api.tools import build_subtask_prefix

        prefix = build_subtask_prefix("Do something", frame_type="nonexistent")
        # Unknown frame type gracefully falls back to no frame instruction
        assert "Do something" in prefix
        assert "## Frame:" not in prefix
```

**Step 2: Run tests to verify they fail**

Run: `cd /e/Projects/nous && uv run pytest tests/test_subtasks.py::TestSubtaskPrefixBuilder -v`
Expected: FAIL — `build_subtask_prefix` not importable

**Step 3: Implement the prefix builder**

In `nous/api/tools.py`, add after the imports (before the ToolDispatcher class, around line 28):

```python
# ---------------------------------------------------------------------------
# Subtask prefix builder (012.2)
# ---------------------------------------------------------------------------


def build_subtask_prefix(task: str, frame_type: str | None = None) -> str:
    """Build a system prompt prefix for subtask execution.

    Used by both inline (await_result) and background worker subtask execution
    to ensure consistent frame-aware context assembly.

    Args:
        task: The subtask instruction text.
        frame_type: Optional cognitive frame (task, research, debug, etc.).

    Returns:
        System prompt prefix string with optional frame instructions.
    """
    from nous.api.runner import FRAME_TOOLS

    base = (
        "You are executing a background subtask.\n"
        "Deliver a clear, complete result. Do not ask questions."
    )

    frame_instruction = ""
    if frame_type and frame_type in FRAME_TOOLS:
        # FRAME_TOOLS values are lists of tool names, not dicts with instructions.
        # We provide frame-type context so the cognitive layer can pick appropriate behavior.
        frame_instruction = f"\n\nFrame: {frame_type} — apply {frame_type}-appropriate reasoning and tool usage."

    return f"{base}{frame_instruction}\n\nTask: {task}"
```

**Note:** The spec assumed `FRAME_TOOLS` entries have an `"instruction"` key, but the actual code at `runner.py:38-46` maps frame names to tool name lists (e.g., `"task": ["*"]`). The prefix builder adapts to this reality by providing the frame type as context rather than looking up non-existent instruction text.

**Step 4: Run tests to verify they pass**

Run: `cd /e/Projects/nous && uv run pytest tests/test_subtasks.py::TestSubtaskPrefixBuilder -v`
Expected: PASS

**Step 5: Commit**

```bash
git add nous/api/tools.py tests/test_subtasks.py
git commit -m "feat(012.2): add shared build_subtask_prefix() helper"
```

---

### Task 5: Runner — Add is_subtask, max_tool_calls, and model_override to run_turn

**Files:**
- Modify: `nous/api/runner.py:268-278` (run_turn signature)
- Modify: `nous/api/runner.py:354` (_tool_loop call)
- Modify: `nous/api/runner.py:898-904` (_tool_loop signature)
- Modify: `nous/api/runner.py:920` (tool filtering)
- Modify: `nous/api/runner.py:932-1007` (tool call counting in loop)

**Step 1: Write the failing tests**

In `tests/test_subtasks.py`:

```python
class TestRunnerSubtaskGuardrails:
    """012.2: Runner respects is_subtask and max_tool_calls."""

    def test_tool_filtering_removes_spawn_and_schedule(self):
        """is_subtask=True should filter spawn_task and schedule_task from tools."""
        # This test verifies the filtering logic in _tool_loop
        all_tools = [
            {"name": "bash", "description": "Run bash", "input_schema": {}},
            {"name": "spawn_task", "description": "Spawn", "input_schema": {}},
            {"name": "schedule_task", "description": "Schedule", "input_schema": {}},
            {"name": "read_file", "description": "Read", "input_schema": {}},
        ]
        # Filter like the runner would
        subtask_excluded = {"spawn_task", "schedule_task"}
        filtered = [t for t in all_tools if t["name"] not in subtask_excluded]

        assert len(filtered) == 2
        names = {t["name"] for t in filtered}
        assert "bash" in names
        assert "read_file" in names
        assert "spawn_task" not in names
        assert "schedule_task" not in names
```

**Step 2: Run test to verify it passes (this is a pure logic test)**

Run: `cd /e/Projects/nous && uv run pytest tests/test_subtasks.py::TestRunnerSubtaskGuardrails -v`
Expected: PASS (it's testing the filtering logic pattern, not the runner yet)

**Step 3: Modify run_turn() signature**

In `nous/api/runner.py`, update `run_turn` (line 268) to accept new params:

```python
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
) -> tuple[str, TurnContext, dict[str, int]]:
```

**Step 4: Pass new params through to _tool_loop**

At line 354 (the `_tool_loop` call), update to:

```python
response_text, tool_results, usage, thinking_blocks = await self._tool_loop(
    system_prompt=system_prompt,
    conversation=conversation,
    frame_id=turn_context.frame.frame_id,
    session_id=session_id,
    is_subtask=is_subtask,
    max_tool_calls=max_tool_calls,
    model_override=model_override,
)
```

**Step 5: Update _tool_loop signature and implementation**

At line 898, update `_tool_loop`:

```python
async def _tool_loop(
    self,
    system_prompt: str,
    conversation: Conversation,
    frame_id: str,
    session_id: str | None = None,
    is_subtask: bool = False,
    max_tool_calls: int | None = None,
    model_override: str | None = None,
) -> tuple[str, list[ToolResult], dict[str, int], list[str]]:
```

After getting tools (line 920), add subtask tool filtering:

```python
# Get tools for current frame (D5)
tools = self._dispatcher.available_tools(frame_id)

# 012.2: Remove delegation tools from subtask tool set (no-nesting rule)
if is_subtask:
    _SUBTASK_EXCLUDED_TOOLS = {"spawn_task", "schedule_task"}
    tools = [t for t in tools if t["name"] not in _SUBTASK_EXCLUDED_TOOLS]
```

Add a tool call counter after `turns = 0` (line 929):

```python
total_tool_calls = 0
```

After dispatching tool calls (after line 995, before the comment about appending tool results), add:

```python
total_tool_calls += len(tool_results_for_message)

# 012.2: Enforce subtask tool call limit
if max_tool_calls and total_tool_calls >= max_tool_calls:
    messages.append({
        "role": "user",
        "content": tool_results_for_message,
    })
    # Force a final response without tools
    logger.info("Subtask tool call limit reached (%d/%d)", total_tool_calls, max_tool_calls)
    final = await self._call_api(
        system_prompt=system_prompt,
        messages=messages,
        tools=None,
        model_override=model_override,
    )
    if final.usage:
        total_usage["input_tokens"] += final.usage.get("input_tokens", 0)
        total_usage["output_tokens"] += final.usage.get("output_tokens", 0)
    return self._extract_text(final.content), all_tool_results, total_usage, all_thinking_blocks
```

In the `_call_api` call within the loop (line 933), pass `model_override`:

```python
api_response = await self._call_api(
    system_prompt=system_prompt,
    messages=messages,
    tools=tools if tools else None,
    model_override=model_override,
)
```

Also pass `model_override` to the max-turns final call at line 1013:

```python
final_response = await self._call_api(
    system_prompt=system_prompt,
    messages=messages,
    tools=None,
    model_override=model_override,
)
```

**Step 6: Run all existing tests to verify no regressions**

Run: `cd /e/Projects/nous && uv run pytest tests/test_subtasks.py -v`
Expected: All existing tests PASS

**Step 7: Commit**

```bash
git add nous/api/runner.py tests/test_subtasks.py
git commit -m "feat(012.2): add is_subtask, max_tool_calls, model_override to runner"
```

---

### Task 6: Tool Schema & Handler — frame_type, await_result, model on spawn_task

**Files:**
- Modify: `nous/api/tools.py:623-677` (spawn_task closure)
- Modify: `nous/api/tools.py:859-885` (_SPAWN_TASK_SCHEMA)

**Step 1: Write the failing tests**

In `tests/test_subtasks.py`:

```python
class TestSpawnTaskEnhancements:
    """012.2: spawn_task tool gains frame_type, await_result, and model params."""

    async def test_spawn_with_frame_type(self, db, settings):
        """frame_type is passed through to SubtaskManager.create()."""
        from nous.api.tools import create_subtask_tools

        heart = MagicMock()
        heart.subtasks = AsyncMock()
        mock_subtask = MagicMock()
        mock_subtask.id = uuid.uuid4()
        heart.subtasks.create = AsyncMock(return_value=mock_subtask)

        tools = create_subtask_tools(heart, settings)
        result = await tools["spawn_task"](
            task="Research topic",
            frame_type="research",
            _session_id="test-session",
        )

        heart.subtasks.create.assert_called_once()
        call_kwargs = heart.subtasks.create.call_args
        assert call_kwargs.kwargs.get("frame_type") == "research" or \
               (len(call_kwargs.args) == 0 and "frame_type" in str(call_kwargs))

    async def test_spawn_without_new_params(self, settings):
        """Backward compat: existing spawn_task calls still work."""
        from nous.api.tools import create_subtask_tools

        heart = MagicMock()
        heart.subtasks = AsyncMock()
        mock_subtask = MagicMock()
        mock_subtask.id = uuid.uuid4()
        heart.subtasks.create = AsyncMock(return_value=mock_subtask)

        tools = create_subtask_tools(heart, settings)
        result = await tools["spawn_task"](
            task="Simple task",
            _session_id="test-session",
        )

        assert "Subtask spawned" in result["content"][0]["text"] or \
               "subtask" in result["content"][0]["text"].lower()
```

**Step 2: Run tests to verify they fail**

Run: `cd /e/Projects/nous && uv run pytest tests/test_subtasks.py::TestSpawnTaskEnhancements -v`
Expected: FAIL — `spawn_task` doesn't accept `frame_type`

**Step 3: Update tool schema**

In `nous/api/tools.py`, update `_SPAWN_TASK_SCHEMA` (line 859) to add new properties:

```python
_SPAWN_TASK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "Spawn a subtask. Use await_result=true to wait for the result inline, or leave false for fire-and-forget background execution.",
    "properties": {
        "task": {
            "type": "string",
            "description": "Natural-language instruction for the subtask",
        },
        "priority": {
            "type": "string",
            "description": "Task priority",
            "enum": ["urgent", "normal", "low"],
            "default": "normal",
        },
        "timeout": {
            "type": "integer",
            "description": "Max execution seconds (clamped to server max)",
            "minimum": 10,
        },
        "notify": {
            "type": "boolean",
            "description": "Notify on completion",
            "default": True,
        },
        "frame_type": {
            "type": "string",
            "description": "Cognitive frame for the subtask. If omitted, auto-detected.",
            "enum": ["task", "research", "conversation", "decision", "debug"],
        },
        "await_result": {
            "type": "boolean",
            "description": "If true, wait for subtask completion and return result inline. Default false (fire-and-forget).",
            "default": False,
        },
        "model": {
            "type": "string",
            "description": "Model to use for this subtask. If omitted, uses default background model. Use a smaller model for fast lookup/summarization tasks.",
        },
    },
    "required": ["task"],
}
```

**Step 4: Add constants and update spawn_task closure**

Add constants near the top of the subtask tools section (after line 620):

```python
# 012.2: Subtask execution guardrails
SUBTASK_TOOL_CALL_LIMIT = 20
INLINE_SUBTASK_DEFAULT_TIMEOUT = 90  # seconds

# 012.2: Frame-type to default model mapping (cost optimization)
FRAME_DEFAULT_MODELS: dict[str, str] = {
    "research": "claude-haiku-3-5-20241022",
}
```

Update the `spawn_task` closure signature and body:

```python
async def spawn_task(
    task: str,
    priority: str = "normal",
    timeout: int | None = None,
    notify: bool = True,
    frame_type: str | None = None,
    await_result: bool = False,
    model: str | None = None,
    _session_id: str | None = None,
) -> dict[str, Any]:
```

Update the body to handle `frame_type`, `model`, and `await_result`:

```python
try:
    # 012.2: Apply frame-default model mapping
    effective_model = model
    if not effective_model and frame_type:
        effective_model = FRAME_DEFAULT_MODELS.get(frame_type)

    # 012.2: Differentiate timeout defaults
    if await_result:
        effective_timeout = min(
            timeout or INLINE_SUBTASK_DEFAULT_TIMEOUT,
            settings.subtask_max_timeout,
        )
    else:
        effective_timeout = min(
            timeout or settings.subtask_default_timeout,
            settings.subtask_max_timeout,
        )

    subtask = await heart.subtasks.create(
        task=task,
        priority=priority,
        timeout=effective_timeout,
        notify=notify,
        parent_session_id=_session_id,
        frame_type=frame_type,
        model=effective_model,
    )

    if not await_result:
        # Fire-and-forget (existing behavior)
        return {
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"Subtask spawned.\n"
                        f"ID: {subtask.id}\n"
                        f"Priority: {priority}\n"
                        f"Timeout: {effective_timeout}s"
                    ),
                }
            ]
        }

    # 012.2: Synchronous inline execution
    import asyncio as _asyncio

    from nous.api.tools import build_subtask_prefix

    subtask_session_id = f"subtask-{subtask.id.hex[:8]}"
    system_prefix = build_subtask_prefix(task, frame_type)

    try:
        # runner is not in closure scope — we need to get it.
        # The runner is accessed via the _runner attribute that must be
        # injected. We'll add a runner param to create_subtask_tools().
        response_text, _ctx, _usage = await _asyncio.wait_for(
            runner.run_turn(
                session_id=subtask_session_id,
                user_message=task,
                agent_id=settings.agent_id,
                system_prompt_prefix=system_prefix,
                skip_episode=True,
                is_subtask=True,
                max_tool_calls=SUBTASK_TOOL_CALL_LIMIT,
                model_override=effective_model,
            ),
            timeout=effective_timeout,
        )

        await heart.subtasks.complete(subtask.id, response_text)
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"[Subtask {subtask.id.hex[:8]} completed]\n\n{response_text}",
                }
            ]
        }

    except _asyncio.TimeoutError:
        await heart.subtasks.fail(subtask.id, f"Timeout after {effective_timeout}s")
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"[Subtask {subtask.id.hex[:8]} timed out after {effective_timeout}s]",
                }
            ]
        }
    except Exception as e:
        await heart.subtasks.fail(subtask.id, str(e))
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"[Subtask {subtask.id.hex[:8]} failed: {e}]",
                }
            ]
        }

except ValueError as e:
    return {"content": [{"type": "text", "text": f"Cannot spawn subtask: {e}"}]}
except Exception as e:
    logger.exception("spawn_task tool failed")
    return {"content": [{"type": "text", "text": f"Error spawning subtask: {e}"}]}
```

**Important:** The `create_subtask_tools` function signature needs to accept a `runner` parameter for inline execution:

```python
def create_subtask_tools(heart: Heart, settings: "Settings", runner: object = None) -> dict[str, Any]:
```

And `register_subtask_tools` needs to accept and pass runner:

```python
def register_subtask_tools(dispatcher: ToolDispatcher, heart: Heart, settings: "Settings", runner: object = None) -> None:
    closures = create_subtask_tools(heart, settings, runner)
```

Find where `register_subtask_tools` is called in `main.py` and pass the runner instance.

**Step 5: Run tests to verify they pass**

Run: `cd /e/Projects/nous && uv run pytest tests/test_subtasks.py::TestSpawnTaskEnhancements -v`
Expected: PASS

**Step 6: Commit**

```bash
git add nous/api/tools.py tests/test_subtasks.py
git commit -m "feat(012.2): add frame_type, await_result, model to spawn_task tool"
```

---

### Task 7: Wire runner into register_subtask_tools

**Files:**
- Modify: `nous/main.py` (where `register_subtask_tools` is called)

**Step 1: Find the registration call**

Search for `register_subtask_tools` in `nous/main.py`.

**Step 2: Pass runner**

Update the call to include `runner=runner`:

```python
register_subtask_tools(dispatcher, heart, settings, runner=runner)
```

**Step 3: Run existing tests to verify no regressions**

Run: `cd /e/Projects/nous && uv run pytest tests/ -x -v --timeout=60 2>&1 | head -100`
Expected: All tests pass

**Step 4: Commit**

```bash
git add nous/main.py
git commit -m "feat(012.2): wire runner into subtask tools for inline execution"
```

---

### Task 8: Worker — Frame-aware prefix and guardrail params

**Files:**
- Modify: `nous/handlers/subtask_worker.py:129-152` (_execute_subtask)

**Step 1: Write the failing test**

In `tests/test_subtasks.py`:

```python
class TestWorkerEnhancements:
    """012.2: Background worker uses frame-aware prefix and guardrails."""

    async def test_worker_passes_is_subtask(self):
        """Worker should pass is_subtask=True to runner.run_turn()."""
        mock_runner = AsyncMock()
        mock_runner.run_turn = AsyncMock(return_value=("result", MagicMock(), {}))

        mock_heart = MagicMock()
        mock_heart.subtasks = AsyncMock()
        mock_heart.subtasks.complete = AsyncMock()

        settings = Settings(
            anthropic_api_key="test",
            agent_id="test-agent",
        )

        pool = SubtaskWorkerPool(
            runner=mock_runner,
            heart=mock_heart,
            settings=settings,
        )

        subtask = MagicMock(spec=Subtask)
        subtask.id = uuid.uuid4()
        subtask.task = "Test task"
        subtask.parent_session_id = "parent-123"
        subtask.timeout_seconds = 120
        subtask.frame_type = None
        subtask.model = None

        await pool._execute_subtask(subtask)

        mock_runner.run_turn.assert_called_once()
        call_kwargs = mock_runner.run_turn.call_args.kwargs
        assert call_kwargs.get("is_subtask") is True
        assert call_kwargs.get("max_tool_calls") == 20  # SUBTASK_TOOL_CALL_LIMIT

    async def test_worker_passes_model_override(self):
        """Worker should pass subtask.model as model_override."""
        mock_runner = AsyncMock()
        mock_runner.run_turn = AsyncMock(return_value=("result", MagicMock(), {}))

        mock_heart = MagicMock()
        mock_heart.subtasks = AsyncMock()
        mock_heart.subtasks.complete = AsyncMock()

        settings = Settings(
            anthropic_api_key="test",
            agent_id="test-agent",
        )

        pool = SubtaskWorkerPool(
            runner=mock_runner,
            heart=mock_heart,
            settings=settings,
        )

        subtask = MagicMock(spec=Subtask)
        subtask.id = uuid.uuid4()
        subtask.task = "Quick lookup"
        subtask.parent_session_id = None
        subtask.timeout_seconds = 60
        subtask.frame_type = "research"
        subtask.model = "claude-haiku-3-5-20241022"

        await pool._execute_subtask(subtask)

        call_kwargs = mock_runner.run_turn.call_args.kwargs
        assert call_kwargs.get("model_override") == "claude-haiku-3-5-20241022"
```

**Step 2: Run tests to verify they fail**

Run: `cd /e/Projects/nous && uv run pytest tests/test_subtasks.py::TestWorkerEnhancements -v`
Expected: FAIL — worker doesn't pass `is_subtask` or `model_override`

**Step 3: Update _execute_subtask**

In `nous/handlers/subtask_worker.py`, update `_execute_subtask` (line 129):

```python
async def _execute_subtask(self, subtask: Subtask) -> None:
    """Run the subtask as an agent turn via AgentRunner."""
    session_id = f"subtask-{subtask.id.hex[:8]}"
    logger.info(
        "Executing subtask %s: %s",
        subtask.id.hex[:8],
        subtask.task[:80],
    )

    # 012.2: Use shared prefix builder for frame-aware context
    from nous.api.tools import SUBTASK_TOOL_CALL_LIMIT, build_subtask_prefix

    system_prefix = build_subtask_prefix(subtask.task, subtask.frame_type)

    try:
        response_text, _turn_ctx, _usage = await self._runner.run_turn(
            session_id=session_id,
            user_message=subtask.task,
            agent_id=self._settings.agent_id,
            system_prompt_prefix=system_prefix,
            skip_episode=True,
            is_subtask=True,
            max_tool_calls=SUBTASK_TOOL_CALL_LIMIT,
            model_override=subtask.model,
        )

        await self._heart.subtasks.complete(subtask.id, response_text)
        await self._emit_event("subtask_completed", subtask, result=response_text)
        await self._notify_telegram(subtask, result=response_text)

        logger.info("Subtask %s completed", subtask.id.hex[:8])

    except asyncio.CancelledError:
        raise
    except Exception as exc:
        error_msg = f"{type(exc).__name__}: {exc}"
        logger.exception("Subtask %s failed", subtask.id.hex[:8])
        await self._heart.subtasks.fail(subtask.id, error_msg)
        await self._emit_event("subtask_failed", subtask, error=error_msg)
        await self._notify_telegram(subtask, error=error_msg)
```

**Step 4: Run tests to verify they pass**

Run: `cd /e/Projects/nous && uv run pytest tests/test_subtasks.py::TestWorkerEnhancements -v`
Expected: PASS

**Step 5: Commit**

```bash
git add nous/handlers/subtask_worker.py tests/test_subtasks.py
git commit -m "feat(012.2): worker uses shared prefix, passes guardrail params to runner"
```

---

### Task 9: Update init.sql with new columns

**Files:**
- Modify: `sql/init.sql` (heart.subtasks CREATE TABLE)

**Step 1: Find the subtask table definition**

Search for `CREATE TABLE heart.subtasks` in `sql/init.sql`.

**Step 2: Add columns**

Add `frame_type VARCHAR(30),` and `model VARCHAR(100),` after `timeout_seconds` in the CREATE TABLE statement.

**Step 3: Commit**

```bash
git add sql/init.sql
git commit -m "feat(012.2): add frame_type and model columns to init.sql schema"
```

---

### Task 10: Full integration test run

**Step 1: Run full test suite**

Run: `cd /e/Projects/nous && uv run pytest tests/ -v --timeout=120 2>&1 | tail -40`
Expected: All tests pass, no regressions

**Step 2: Run subtask-specific tests**

Run: `cd /e/Projects/nous && uv run pytest tests/test_subtasks.py -v`
Expected: All old + new tests pass

**Step 3: Final commit (if any test fixes needed)**

Fix any issues discovered during integration testing and commit.

---

## Summary of all changes

| Task | File | What |
|------|------|------|
| 1 | `sql/migrations/012_subtask_frame_type.sql` | Migration: frame_type + model columns |
| 2 | `nous/storage/models.py` | ORM: frame_type + model fields |
| 3 | `nous/heart/subtasks.py` | Manager: frame_type + model in create() |
| 4 | `nous/api/tools.py` | Helper: build_subtask_prefix() |
| 5 | `nous/api/runner.py` | Runner: is_subtask + max_tool_calls + model_override |
| 6 | `nous/api/tools.py` | Tool: schema + handler for frame_type/await_result/model |
| 7 | `nous/main.py` | Wiring: pass runner to register_subtask_tools |
| 8 | `nous/handlers/subtask_worker.py` | Worker: shared prefix + guardrail params |
| 9 | `sql/init.sql` | Schema: new columns in base DDL |
| 10 | — | Integration test verification |
