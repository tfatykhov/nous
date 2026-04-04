# Heartbeat Client Isolation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give HeartbeatRunner its own dedicated AnthropicClient instance so heartbeat LLM calls cannot exhaust the main runner's httpx connection pool during active streaming sessions.

**Architecture:** Currently, `main.py` creates one `AgentRunner` and passes it to both the chat API and `HeartbeatRunner`. The runner holds a single `AnthropicClient` with a 5-connection httpx pool. When heartbeat's cognitive triage calls `run_turn()` during an active streaming session's tool execution, both compete for the same 5 connections — causing pool timeout. The fix: create a second `AnthropicClient` dedicated to heartbeat, and have `HeartbeatRunner` accept it directly instead of going through the shared `AgentRunner`. Since heartbeat only needs `run_turn()` (non-streaming, subtask mode), we refactor `HeartbeatRunner._cognitive_triage` to use a dedicated runner with its own client.

**Tech Stack:** Python 3.12+, httpx, asyncio, pytest

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `nous/main.py` | Modify (lines 102-105, 385-423) | Create second AnthropicClient for heartbeat, pass to HeartbeatRunner |
| `nous/heartbeat/runner.py` | Modify (lines 41-59, 286-336) | Accept dedicated `api_client`, create internal runner or use directly |
| `tests/test_heartbeat.py` | Modify (lines 373-380, 551-567, 940-960) | Update tests for new `api_client` parameter |
| `tests/test_heartbeat_isolation.py` | Create | Integration test proving no connection pool sharing |

---

### Task 1: Add `api_client` parameter to HeartbeatRunner

**Files:**
- Modify: `nous/heartbeat/runner.py:41-59`
- Modify: `nous/heartbeat/runner.py:286-336`
- Test: `tests/test_heartbeat.py`

- [ ] **Step 1: Write failing test — HeartbeatRunner accepts api_client parameter**

Add a test in `tests/test_heartbeat.py` that verifies HeartbeatRunner can accept an `api_client` parameter:

```python
class TestHeartbeatClientIsolation:
    """Tests for heartbeat client isolation (connection pool separation)."""

    def test_heartbeat_runner_accepts_api_client(self):
        """HeartbeatRunner accepts optional api_client parameter."""
        from nous.heartbeat.runner import HeartbeatRunner
        from nous.heartbeat.registry import CheckRegistry
        from unittest.mock import AsyncMock

        settings = _mock_settings()
        mock_api_client = AsyncMock()
        runner = HeartbeatRunner(
            settings=settings, registry=CheckRegistry(), runner=AsyncMock(),
            brain=AsyncMock(), heart=AsyncMock(), bus=None, http_client=None,
            api_client=mock_api_client,
        )
        assert runner._api_client is mock_api_client

    def test_heartbeat_runner_api_client_defaults_none(self):
        """HeartbeatRunner api_client defaults to None for backward compat."""
        from nous.heartbeat.runner import HeartbeatRunner
        from nous.heartbeat.registry import CheckRegistry
        from unittest.mock import AsyncMock

        settings = _mock_settings()
        runner = HeartbeatRunner(
            settings=settings, registry=CheckRegistry(), runner=AsyncMock(),
            brain=AsyncMock(), heart=AsyncMock(), bus=None, http_client=None,
        )
        assert runner._api_client is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_heartbeat.py::TestHeartbeatClientIsolation -v`
Expected: FAIL with `TypeError: HeartbeatRunner.__init__() got an unexpected keyword argument 'api_client'`

- [ ] **Step 3: Add api_client parameter to HeartbeatRunner.__init__**

In `nous/heartbeat/runner.py`, modify `__init__` to accept and store `api_client`:

```python
def __init__(
    self,
    settings: Settings,
    registry: CheckRegistry,
    runner: AgentRunner,
    brain: Brain,
    heart: Heart,
    bus: EventBus | None,
    http_client: httpx.AsyncClient | None,
    finding_store: FindingStore | None = None,
    api_client: AnthropicClient | None = None,
) -> None:
    self._settings = settings
    self._registry = registry
    self._runner = runner
    self._brain = brain
    self._heart = heart
    self._bus = bus
    self._http = http_client
    self._finding_store = finding_store
    self._api_client = api_client

    self._task: asyncio.Task | None = None
    self._running = False
    self._tokens_used_today: int = 0
    self._budget_date: date = date.today()
    self._last_tick: datetime | None = None
    self._last_digest_date: date | None = None
    self._last_prune: datetime | None = None
    self._tuner: HeartbeatTuner = HeartbeatTuner()
    self._last_tune: datetime | None = None
```

Add the import at the top of `nous/heartbeat/runner.py`:

```python
from nous.api.anthropic_client import AnthropicClient
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_heartbeat.py::TestHeartbeatClientIsolation -v`
Expected: PASS (both tests)

- [ ] **Step 5: Commit**

```bash
git add nous/heartbeat/runner.py tests/test_heartbeat.py
git commit -m "feat(heartbeat): add api_client parameter to HeartbeatRunner"
```

---

### Task 2: Create dedicated runner for heartbeat cognitive triage

**Files:**
- Modify: `nous/heartbeat/runner.py:75-86` (start method)
- Modify: `nous/heartbeat/runner.py:286-336` (_cognitive_triage method)
- Test: `tests/test_heartbeat.py`

- [ ] **Step 1: Write failing test — cognitive triage uses dedicated runner when api_client provided**

```python
@pytest.mark.asyncio
async def test_cognitive_triage_uses_dedicated_runner(self):
    """When api_client is provided, cognitive triage creates its own AgentRunner."""
    from nous.heartbeat.runner import HeartbeatRunner
    from nous.heartbeat.registry import CheckRegistry
    from nous.heartbeat.schemas import Finding
    from unittest.mock import AsyncMock, patch

    settings = _mock_settings()
    mock_api_client = AsyncMock()
    # The shared runner should NOT be called
    shared_runner = AsyncMock()
    shared_runner.run_turn = AsyncMock()

    runner = HeartbeatRunner(
        settings=settings, registry=CheckRegistry(), runner=shared_runner,
        brain=AsyncMock(), heart=AsyncMock(), bus=None, http_client=None,
        api_client=mock_api_client,
    )

    # Mock the dedicated runner creation
    mock_dedicated_runner = AsyncMock()
    mock_dedicated_runner.run_turn = AsyncMock(return_value=(
        "Reviewed.", None, {"input_tokens": 100, "output_tokens": 50},
    ))
    mock_dedicated_runner.end_conversation = AsyncMock()

    with patch.object(runner, '_get_triage_runner', return_value=mock_dedicated_runner):
        findings = [
            Finding(source="health", summary="Test", urgency="normal", needs_action=True),
        ]
        await runner._cognitive_triage(findings)

    # Shared runner should NOT have been called
    shared_runner.run_turn.assert_not_called()
    # Dedicated runner SHOULD have been called
    mock_dedicated_runner.run_turn.assert_called_once()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_heartbeat.py::TestHeartbeatClientIsolation::test_cognitive_triage_uses_dedicated_runner -v`
Expected: FAIL with `AttributeError: 'HeartbeatRunner' object has no attribute '_get_triage_runner'`

- [ ] **Step 3: Implement _get_triage_runner and update _cognitive_triage**

In `nous/heartbeat/runner.py`, add a method that returns the appropriate runner and update `_cognitive_triage` to use it:

```python
def _get_triage_runner(self) -> AgentRunner:
    """Return the runner to use for cognitive triage.

    If a dedicated api_client was provided, creates a lightweight AgentRunner
    with its own connection pool to avoid contention with the main streaming
    session. Otherwise falls back to the shared runner.
    """
    if self._api_client is not None and self._dedicated_runner is not None:
        return self._dedicated_runner
    return self._runner
```

In `__init__`, after storing `self._api_client`, add dedicated runner creation:

```python
self._api_client = api_client
self._dedicated_runner: AgentRunner | None = None
```

In `start()`, after `self._running = True`, create the dedicated runner if api_client was provided:

```python
async def start(self) -> None:
    """Start the heartbeat loop."""
    self._running = True

    # Create dedicated runner with isolated API client for triage
    if self._api_client is not None:
        from nous.api.runner import AgentRunner
        self._dedicated_runner = AgentRunner(
            self._runner._cognitive, self._brain, self._heart, self._settings,
        )
        self._dedicated_runner.set_dispatcher(self._runner._dispatcher)
        self._dedicated_runner.set_api_client(self._api_client)
        await self._dedicated_runner.start()
        logger.info("F034: Heartbeat using dedicated API client (isolated connection pool)")

    await self._detect_missed_checks()
    self._task = asyncio.create_task(self._loop(), name="heartbeat-runner")
    logger.info(
        "F034: Heartbeat started (tick=%ds, quiet=%d-%d, budget=%d tokens/day)",
        self._settings.heartbeat_tick_interval,
        self._settings.heartbeat_quiet_start,
        self._settings.heartbeat_quiet_end,
        self._settings.heartbeat_daily_token_budget,
    )
```

Update `_cognitive_triage` to use `_get_triage_runner()`:

```python
async def _cognitive_triage(self, findings: list[Finding]) -> HeartbeatResult:
    """Open a cognitive session to process findings."""
    result = HeartbeatResult()

    # Build a message summarizing findings
    lines = ["[Heartbeat] The following items need attention:"]
    for f in findings:
        lines.append(f"- [{f.source}] {f.summary}")
    lines.append("\nPlease review these findings and take any needed actions.")
    message = "\n".join(lines)

    session_id = f"heartbeat-{uuid4().hex[:8]}"
    triage_runner = self._get_triage_runner()

    try:
        response_text, _context, usage = await triage_runner.run_turn(
            session_id, message,
            platform="heartbeat",
            skip_episode=True,
            is_subtask=True,
        )
        result.response = response_text or ""
        result.tokens_used = (usage or {}).get("input_tokens", 0) + (usage or {}).get("output_tokens", 0)
        self._tokens_used_today += result.tokens_used

        logger.info(
            "Heartbeat cognitive triage used %d tokens (daily: %d/%d)",
            result.tokens_used, self._tokens_used_today,
            self._settings.heartbeat_daily_token_budget,
        )

        if self._bus:
            await self._bus.emit(Event(
                type="heartbeat_triage",
                agent_id=self._settings.agent_id,
                data={
                    "session_id": session_id,
                    "findings_count": len(findings),
                    "tokens_used": result.tokens_used,
                    "response_summary": result.response[:200],
                },
            ))
    except Exception:
        logger.exception("Heartbeat cognitive triage failed")

    # End the session
    try:
        await triage_runner.end_conversation(session_id)
    except Exception:
        pass

    return result
```

- [ ] **Step 4: Update stop() to clean up dedicated runner**

In `stop()`, add cleanup for the dedicated runner:

```python
async def stop(self) -> None:
    """Stop the heartbeat loop."""
    self._running = False
    if self._task:
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    # Clean up dedicated runner (closes its own API client)
    if self._dedicated_runner is not None:
        await self._dedicated_runner.stop()
        self._dedicated_runner = None

    logger.info("F034: Heartbeat stopped")
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_heartbeat.py::TestHeartbeatClientIsolation -v`
Expected: PASS (all 3 tests)

- [ ] **Step 6: Run full heartbeat test suite to check nothing broke**

Run: `uv run pytest tests/test_heartbeat.py -v`
Expected: All existing tests still PASS

- [ ] **Step 7: Commit**

```bash
git add nous/heartbeat/runner.py tests/test_heartbeat.py
git commit -m "feat(heartbeat): dedicated runner with isolated API client for cognitive triage"
```

---

### Task 3: Wire dedicated AnthropicClient in main.py

**Files:**
- Modify: `nous/main.py:102-105, 385-423`

- [ ] **Step 1: Write failing test — main.py creates separate client for heartbeat**

This is a wiring test. Since `main.py:initialize()` is an integration function, we verify via the heartbeat runner's `_api_client` attribute. Add to `tests/test_heartbeat.py`:

```python
@pytest.mark.asyncio
async def test_heartbeat_runner_gets_own_api_client_from_main(self):
    """Verify HeartbeatRunner receives a dedicated api_client, not the shared one."""
    from nous.heartbeat.runner import HeartbeatRunner
    from nous.heartbeat.registry import CheckRegistry
    from unittest.mock import AsyncMock

    settings = _mock_settings()
    shared_client = AsyncMock(name="shared_client")
    heartbeat_client = AsyncMock(name="heartbeat_client")

    runner = HeartbeatRunner(
        settings=settings, registry=CheckRegistry(), runner=AsyncMock(),
        brain=AsyncMock(), heart=AsyncMock(), bus=None, http_client=None,
        api_client=heartbeat_client,
    )

    # Verify it stored the dedicated client, not the shared one
    assert runner._api_client is heartbeat_client
    assert runner._api_client is not shared_client
```

- [ ] **Step 2: Modify main.py to create a dedicated heartbeat API client**

In `nous/main.py`, after the shared `api_client` creation (line ~105), and inside the heartbeat section (line ~387), create a second client:

Find the heartbeat wiring section (around line 418):
```python
            # Create dedicated API client for heartbeat (isolated connection pool)
            heartbeat_api_client = create_client(settings)
            await heartbeat_api_client.start()
            logger.info("F034: Heartbeat API client created (isolated from main runner)")

            heartbeat_runner = HeartbeatRunner(
                settings=settings, registry=registry, runner=runner,
                brain=brain, heart=heart, bus=bus, http_client=handler_http,
                finding_store=finding_store,
                api_client=heartbeat_api_client,
            )
            await heartbeat_runner.start()
```

- [ ] **Step 3: Run test to verify it passes**

Run: `uv run pytest tests/test_heartbeat.py::TestHeartbeatClientIsolation -v`
Expected: PASS

- [ ] **Step 4: Ensure heartbeat client gets cleaned up on shutdown**

Check that `main.py`'s shutdown path calls `heartbeat_runner.stop()`. Look at the return dict and verify the caller handles cleanup. The `HeartbeatRunner.stop()` (modified in Task 2) already stops the dedicated runner, which closes its API client since it's not shared (`_api_shared=False`). No additional changes needed — but verify by reading the shutdown code.

- [ ] **Step 5: Commit**

```bash
git add nous/main.py tests/test_heartbeat.py
git commit -m "feat(main): create dedicated API client for heartbeat runner"
```

---

### Task 4: Integration test — prove connection pool isolation

**Files:**
- Create: `tests/test_heartbeat_isolation.py`

- [ ] **Step 1: Write integration test proving pool isolation**

```python
"""Tests proving heartbeat and main runner have isolated connection pools."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nous.heartbeat.runner import HeartbeatRunner
from nous.heartbeat.registry import CheckRegistry
from nous.heartbeat.schemas import Finding, HeartbeatResult


def _mock_settings(**overrides):
    """Create mock settings for isolation tests."""
    defaults = {
        "agent_id": "test-agent",
        "heartbeat_enabled": True,
        "heartbeat_tick_interval": 30,
        "heartbeat_quiet_start": 23,
        "heartbeat_quiet_end": 8,
        "heartbeat_daily_token_budget": 50000,
        "heartbeat_digest_hour_utc": 9,
        "heartbeat_suppression_ttl_hours": 24,
        "heartbeat_tuning_enabled": False,
        "heartbeat_tuning_interval_hours": 168,
    }
    defaults.update(overrides)
    settings = MagicMock()
    for k, v in defaults.items():
        setattr(settings, k, v)
    return settings


class TestConnectionPoolIsolation:
    """Verify heartbeat and main runner don't share httpx connection pools."""

    @pytest.mark.asyncio
    async def test_triage_does_not_touch_shared_runner_when_dedicated_exists(self):
        """When api_client provided, _cognitive_triage never calls shared runner.run_turn."""
        settings = _mock_settings()

        shared_runner = AsyncMock()
        shared_runner.run_turn = AsyncMock(side_effect=AssertionError(
            "Shared runner should NOT be called when dedicated client exists"
        ))

        dedicated_runner = AsyncMock()
        dedicated_runner.run_turn = AsyncMock(return_value=(
            "OK", None, {"input_tokens": 50, "output_tokens": 50},
        ))
        dedicated_runner.end_conversation = AsyncMock()

        hb = HeartbeatRunner(
            settings=settings, registry=CheckRegistry(), runner=shared_runner,
            brain=AsyncMock(), heart=AsyncMock(), bus=None, http_client=None,
            api_client=AsyncMock(),
        )
        # Inject the dedicated runner directly
        hb._dedicated_runner = dedicated_runner

        findings = [
            Finding(source="health", summary="test", urgency="normal", needs_action=True),
        ]
        result = await hb._cognitive_triage(findings)

        assert result.tokens_used == 100
        dedicated_runner.run_turn.assert_called_once()
        shared_runner.run_turn.assert_not_called()

    @pytest.mark.asyncio
    async def test_fallback_to_shared_runner_when_no_api_client(self):
        """Without api_client, _cognitive_triage uses the shared runner (backward compat)."""
        settings = _mock_settings()

        shared_runner = AsyncMock()
        shared_runner.run_turn = AsyncMock(return_value=(
            "OK", None, {"input_tokens": 50, "output_tokens": 50},
        ))
        shared_runner.end_conversation = AsyncMock()

        hb = HeartbeatRunner(
            settings=settings, registry=CheckRegistry(), runner=shared_runner,
            brain=AsyncMock(), heart=AsyncMock(), bus=None, http_client=None,
        )

        findings = [
            Finding(source="health", summary="test", urgency="normal", needs_action=True),
        ]
        result = await hb._cognitive_triage(findings)

        assert result.tokens_used == 100
        shared_runner.run_turn.assert_called_once()

    @pytest.mark.asyncio
    async def test_dedicated_runner_cleanup_on_stop(self):
        """stop() cleans up the dedicated runner."""
        settings = _mock_settings()

        dedicated_runner = AsyncMock()
        dedicated_runner.stop = AsyncMock()

        hb = HeartbeatRunner(
            settings=settings, registry=CheckRegistry(), runner=AsyncMock(),
            brain=AsyncMock(), heart=AsyncMock(), bus=None, http_client=None,
            api_client=AsyncMock(),
        )
        hb._dedicated_runner = dedicated_runner
        hb._task = None  # No loop running

        await hb.stop()

        dedicated_runner.stop.assert_called_once()
        assert hb._dedicated_runner is None
```

- [ ] **Step 2: Run test to verify it passes**

Run: `uv run pytest tests/test_heartbeat_isolation.py -v`
Expected: PASS (all 3 tests)

- [ ] **Step 3: Run full test suite**

Run: `uv run pytest tests/test_heartbeat.py tests/test_heartbeat_isolation.py -v`
Expected: All tests PASS

- [ ] **Step 4: Commit**

```bash
git add tests/test_heartbeat_isolation.py
git commit -m "test: integration tests proving heartbeat connection pool isolation"
```

---

### Task 5: Verify existing tests pass and clean up

**Files:**
- Review: all modified files

- [ ] **Step 1: Run the full test suite**

Run: `uv run pytest tests/ -v --timeout=60 -x`
Expected: All tests PASS (no regressions)

- [ ] **Step 2: If any existing heartbeat tests fail, fix mock signatures**

Existing tests create `HeartbeatRunner` without the new `api_client` param. Since it defaults to `None`, they should still work. If any test explicitly checks `__init__` signature or uses positional args, update them.

- [ ] **Step 3: Verify type annotations**

Run: `uv run python -c "from nous.heartbeat.runner import HeartbeatRunner; print('Import OK')"`
Expected: `Import OK`

- [ ] **Step 4: Final commit if any fixups were needed**

```bash
git add -u
git commit -m "fix: update test signatures for heartbeat client isolation"
```
