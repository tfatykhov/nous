"""Tests for 012.3 programmatic tool calling (run_python).

TDD: These tests are written FIRST before implementation.

The run_python tool allows Claude to write Python scripts that batch
memory operations, filter results, and return shaped data — reducing
token consumption compared to separate tool calls.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from nous.config import Settings


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def settings():
    """Settings with programmatic tools enabled and short timeout."""
    return Settings(
        programmatic_tools_enabled=True,
        programmatic_tools_timeout=5,
    )


@pytest.fixture
def mock_heart():
    """Mocked Heart for run_python memory wrappers."""
    heart = AsyncMock()

    # search_facts returns pydantic-model-like objects with .model_dump()
    fact1 = MagicMock()
    fact1.model_dump.return_value = {
        "content": "caching uses Redis",
        "category": "technical",
        "confidence": 0.9,
        "score": 0.85,
    }
    fact2 = MagicMock()
    fact2.model_dump.return_value = {
        "content": "cache TTL is 300s",
        "category": "technical",
        "confidence": 0.7,
        "score": 0.6,
    }
    heart.search_facts = AsyncMock(return_value=[fact1, fact2])

    # list_episodes returns EpisodeSummary-like objects
    episode = MagicMock()
    episode.model_dump.return_value = {
        "title": "Discussed caching",
        "summary": "Talked about Redis caching strategy",
        "started_at": "2026-03-03T10:00:00",
    }
    heart.list_episodes = AsyncMock(return_value=[episode])

    # subtasks.list returns Subtask-like objects
    heart.subtasks = MagicMock()
    subtask_obj = MagicMock()
    subtask_obj.model_dump.return_value = {
        "id": "abc123",
        "task": "Research caching",
        "status": "completed",
    }
    heart.subtasks.list = AsyncMock(return_value=[subtask_obj])

    # learn returns LearnResult-like object
    learn_result = MagicMock()
    learn_result.id = "fake-fact-id"
    learn_result.contradiction_warning = None
    heart.learn = AsyncMock(return_value=learn_result)

    return heart


@pytest.fixture
def mock_brain():
    """Mocked Brain (unused in current wrappers but passed for consistency)."""
    return AsyncMock()


@pytest.fixture
def run_python_tool(mock_brain, mock_heart, settings):
    """Create run_python tool closure with mocked dependencies."""
    from nous.api.tools import create_programmatic_tools

    tools = create_programmatic_tools(mock_brain, mock_heart, settings)
    return tools["run_python"]


# ---------------------------------------------------------------------------
# Config tests
# ---------------------------------------------------------------------------


class TestRunPythonConfig:
    """Test config settings for programmatic tools."""

    def test_default_enabled(self):
        """programmatic_tools_enabled defaults to True."""
        s = Settings()
        assert s.programmatic_tools_enabled is True

    def test_default_timeout(self):
        """programmatic_tools_timeout defaults to 10."""
        s = Settings()
        assert s.programmatic_tools_timeout == 10

    def test_custom_values(self):
        """Can override via constructor."""
        s = Settings(programmatic_tools_enabled=False, programmatic_tools_timeout=30)
        assert s.programmatic_tools_enabled is False
        assert s.programmatic_tools_timeout == 30


# ---------------------------------------------------------------------------
# Creation tests
# ---------------------------------------------------------------------------


class TestRunPythonCreation:
    """Test that create_programmatic_tools returns expected structure."""

    def test_returns_run_python_callable(self, mock_brain, mock_heart, settings):
        from nous.api.tools import create_programmatic_tools

        tools = create_programmatic_tools(mock_brain, mock_heart, settings)
        assert "run_python" in tools
        assert callable(tools["run_python"])


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------


class TestRunPythonSchema:
    """Test the run_python JSON schema."""

    def test_has_code_property(self):
        from nous.api.tools import _RUN_PYTHON_SCHEMA

        assert "code" in _RUN_PYTHON_SCHEMA["properties"]

    def test_code_is_required(self):
        from nous.api.tools import _RUN_PYTHON_SCHEMA

        assert _RUN_PYTHON_SCHEMA["required"] == ["code"]


# ---------------------------------------------------------------------------
# Basic execution tests
# ---------------------------------------------------------------------------


class TestRunPythonExecution:
    """Test basic code execution in run_python."""

    @pytest.mark.asyncio
    async def test_print_output(self, run_python_tool):
        """print() captures output to stdout buffer."""
        result = await run_python_tool(code='print("hello world")')
        assert result["content"][0]["text"] == "hello world\n"

    @pytest.mark.asyncio
    async def test_result_variable(self, run_python_tool):
        """Setting result variable returns its string representation."""
        result = await run_python_tool(code="result = 42")
        assert result["content"][0]["text"] == "42"

    @pytest.mark.asyncio
    async def test_stdout_takes_precedence(self, run_python_tool):
        """If both print() and result are set, stdout wins."""
        result = await run_python_tool(code='print("printed"); result = 42')
        assert "printed" in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_no_output_returns_ok(self, run_python_tool):
        """No print or result returns 'OK'."""
        result = await run_python_tool(code="x = 1 + 1")
        assert result["content"][0]["text"] == "OK"

    @pytest.mark.asyncio
    async def test_multiline_code(self, run_python_tool):
        """Multi-line code executes correctly."""
        code = "x = [1, 2, 3]\ny = [i * 2 for i in x]\nresult = str(y)"
        result = await run_python_tool(code=code)
        assert result["content"][0]["text"] == "[2, 4, 6]"


# ---------------------------------------------------------------------------
# Safe builtins tests
# ---------------------------------------------------------------------------


class TestRunPythonSafeBuiltins:
    """Test that safe builtins are available."""

    @pytest.mark.asyncio
    async def test_len(self, run_python_tool):
        result = await run_python_tool(code="result = len([1, 2, 3])")
        assert result["content"][0]["text"] == "3"

    @pytest.mark.asyncio
    async def test_sorted(self, run_python_tool):
        result = await run_python_tool(code="result = sorted([3, 1, 2])")
        assert result["content"][0]["text"] == "[1, 2, 3]"

    @pytest.mark.asyncio
    async def test_dict_and_list(self, run_python_tool):
        result = await run_python_tool(
            code="result = list(dict(a=1, b=2).keys())"
        )
        text = result["content"][0]["text"]
        assert "a" in text and "b" in text

    @pytest.mark.asyncio
    async def test_type_conversions(self, run_python_tool):
        result = await run_python_tool(code='result = int("42") + float("0.5")')
        assert result["content"][0]["text"] == "42.5"

    @pytest.mark.asyncio
    async def test_range_enumerate_zip(self, run_python_tool):
        result = await run_python_tool(
            code='result = list(zip(range(3), ["a","b","c"]))'
        )
        text = result["content"][0]["text"]
        assert "0" in text and "a" in text

    @pytest.mark.asyncio
    async def test_max_min_sum(self, run_python_tool):
        result = await run_python_tool(
            code="result = (max([1,5,3]), min([1,5,3]), sum([1,5,3]))"
        )
        assert result["content"][0]["text"] == "(5, 1, 9)"

    @pytest.mark.asyncio
    async def test_map_filter(self, run_python_tool):
        result = await run_python_tool(
            code="result = list(filter(lambda x: x > 2, [1, 2, 3, 4]))"
        )
        assert result["content"][0]["text"] == "[3, 4]"


# ---------------------------------------------------------------------------
# Blocked builtins tests
# ---------------------------------------------------------------------------


class TestRunPythonBlockedBuiltins:
    """Test that dangerous builtins are blocked."""

    @pytest.mark.asyncio
    async def test_dunder_import_blocked(self, run_python_tool):
        """__import__ is not available."""
        result = await run_python_tool(code='__import__("os")')
        assert "Error" in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_open_blocked(self, run_python_tool):
        """open() is not available."""
        result = await run_python_tool(code='open("/etc/passwd")')
        assert "Error" in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_eval_blocked(self, run_python_tool):
        """eval() is not available."""
        result = await run_python_tool(code='eval("1+1")')
        assert "Error" in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_exec_blocked(self, run_python_tool):
        """exec() is not available (inside exec'd code)."""
        result = await run_python_tool(code='exec("x=1")')
        assert "Error" in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_import_statement_blocked(self, run_python_tool):
        """import statement is blocked (no __import__ in builtins)."""
        result = await run_python_tool(code="import os")
        assert "Error" in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_os_not_in_namespace(self, run_python_tool):
        """os module is not injected into the namespace."""
        result = await run_python_tool(code='os.system("echo hi")')
        assert "Error" in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_subprocess_not_in_namespace(self, run_python_tool):
        """subprocess module is not injected."""
        result = await run_python_tool(code='subprocess.run(["echo"])')
        assert "Error" in result["content"][0]["text"]


# ---------------------------------------------------------------------------
# Safe stdlib modules tests
# ---------------------------------------------------------------------------


class TestRunPythonSafeModules:
    """Test that whitelisted stdlib modules are available."""

    @pytest.mark.asyncio
    async def test_json_dumps(self, run_python_tool):
        result = await run_python_tool(code='result = json.dumps({"a": 1})')
        assert result["content"][0]["text"] == '{"a": 1}'

    @pytest.mark.asyncio
    async def test_json_loads(self, run_python_tool):
        result = await run_python_tool(
            code='d = json.loads(\'{"x": 42}\'); result = d["x"]'
        )
        assert result["content"][0]["text"] == "42"

    @pytest.mark.asyncio
    async def test_re_match(self, run_python_tool):
        result = await run_python_tool(
            code='result = bool(re.match(r"\\d+", "123"))'
        )
        assert result["content"][0]["text"] == "True"

    @pytest.mark.asyncio
    async def test_math_sqrt(self, run_python_tool):
        result = await run_python_tool(code="result = math.sqrt(16)")
        assert result["content"][0]["text"] == "4.0"

    @pytest.mark.asyncio
    async def test_datetime_now(self, run_python_tool):
        result = await run_python_tool(
            code="result = type(datetime.datetime.now()).__name__"
        )
        assert result["content"][0]["text"] == "datetime"

    @pytest.mark.asyncio
    async def test_collections_counter(self, run_python_tool):
        result = await run_python_tool(
            code="c = collections.Counter([1,1,2]); result = c[1]"
        )
        assert result["content"][0]["text"] == "2"

    @pytest.mark.asyncio
    async def test_itertools_chain(self, run_python_tool):
        result = await run_python_tool(
            code="result = list(itertools.chain([1,2], [3,4]))"
        )
        assert result["content"][0]["text"] == "[1, 2, 3, 4]"

    @pytest.mark.asyncio
    async def test_functools_reduce(self, run_python_tool):
        result = await run_python_tool(
            code="result = functools.reduce(lambda a, b: a+b, [1,2,3])"
        )
        assert result["content"][0]["text"] == "6"

    @pytest.mark.asyncio
    async def test_statistics_mean(self, run_python_tool):
        result = await run_python_tool(
            code="result = statistics.mean([1, 2, 3, 4, 5])"
        )
        assert result["content"][0]["text"] == "3"


# ---------------------------------------------------------------------------
# Memory wrapper tests
# ---------------------------------------------------------------------------


class TestRunPythonMemoryWrappers:
    """Test that memory functions call Heart/Brain correctly."""

    @pytest.mark.asyncio
    async def test_recall_deep_calls_search_facts(self, run_python_tool, mock_heart):
        """recall_deep calls heart.search_facts with correct args."""
        result = await run_python_tool(
            code='facts = recall_deep("caching", limit=3)\nresult = json.dumps(len(facts))'
        )
        mock_heart.search_facts.assert_called_once_with("caching", limit=3)
        assert result["content"][0]["text"] == "2"

    @pytest.mark.asyncio
    async def test_recall_recent_calls_list_episodes(self, run_python_tool, mock_heart):
        """recall_recent calls heart.list_episodes with correct args."""
        result = await run_python_tool(
            code='eps = recall_recent(hours=12, limit=5)\nresult = json.dumps(len(eps))'
        )
        mock_heart.list_episodes.assert_called_once_with(limit=5, hours=12)
        assert result["content"][0]["text"] == "1"

    @pytest.mark.asyncio
    async def test_list_tasks_calls_subtasks(self, run_python_tool, mock_heart):
        """list_tasks calls heart.subtasks.list with correct args."""
        result = await run_python_tool(
            code='tasks = list_tasks(status="completed")\nresult = json.dumps(len(tasks))'
        )
        mock_heart.subtasks.list.assert_called_once_with(status="completed")
        assert result["content"][0]["text"] == "1"

    @pytest.mark.asyncio
    async def test_learn_fact_calls_heart_learn(self, run_python_tool, mock_heart):
        """learn_fact calls heart.learn and returns confirmation."""
        result = await run_python_tool(
            code='msg = learn_fact("Redis is fast", category="technical")\nresult = msg'
        )
        mock_heart.learn.assert_called_once()
        assert "stored" in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_recall_deep_returns_dicts(self, run_python_tool):
        """recall_deep results are dicts accessible with bracket notation."""
        result = await run_python_tool(
            code=(
                'facts = recall_deep("caching")\n'
                'top = sorted(facts, key=lambda f: f["confidence"], reverse=True)\n'
                'result = json.dumps(top[0]["content"])'
            )
        )
        assert "caching uses Redis" in result["content"][0]["text"]


# ---------------------------------------------------------------------------
# Write cap tests
# ---------------------------------------------------------------------------


class TestRunPythonWriteCap:
    """Test write cap enforcement on learn_fact."""

    @pytest.mark.asyncio
    async def test_within_cap(self, run_python_tool, mock_heart):
        """5 learn_fact calls succeed."""
        lines = [f'learn_fact("fact {i}", category="technical")' for i in range(5)]
        lines.append('result = "all good"')
        result = await run_python_tool(code="\n".join(lines))
        assert result["content"][0]["text"] == "all good"
        assert mock_heart.learn.call_count == 5

    @pytest.mark.asyncio
    async def test_exceeds_cap(self, run_python_tool, mock_heart):
        """6th learn_fact call raises RuntimeError with write cap message."""
        lines = [f'learn_fact("fact {i}", category="technical")' for i in range(6)]
        result = await run_python_tool(code="\n".join(lines))
        text = result["content"][0]["text"]
        assert "Error" in text
        assert "write cap" in text.lower()
        # Only first 5 should have been stored
        assert mock_heart.learn.call_count == 5


# ---------------------------------------------------------------------------
# Timeout tests
# ---------------------------------------------------------------------------


class TestRunPythonTimeout:
    """Test timeout enforcement."""

    @pytest.mark.asyncio
    async def test_timeout_triggers(self):
        """Code exceeding timeout returns timeout error."""
        short_settings = Settings(
            programmatic_tools_enabled=True,
            programmatic_tools_timeout=1,
        )
        from nous.api.tools import create_programmatic_tools

        tools = create_programmatic_tools(
            AsyncMock(), AsyncMock(), short_settings
        )
        # while loop with increment is slow in CPython (~15s for 10**8)
        result = await tools["run_python"](code="x = 0\nwhile x < 10**8: x += 1")
        text = result["content"][0]["text"]
        assert "Error" in text
        assert "timed out" in text.lower()


# ---------------------------------------------------------------------------
# Error handling tests
# ---------------------------------------------------------------------------


class TestRunPythonErrors:
    """Test error handling for various failure modes."""

    @pytest.mark.asyncio
    async def test_syntax_error(self, run_python_tool):
        """Syntax error returns error with type name."""
        result = await run_python_tool(code="def f(")
        text = result["content"][0]["text"]
        assert "Error" in text
        assert "SyntaxError" in text

    @pytest.mark.asyncio
    async def test_runtime_error(self, run_python_tool):
        """Division by zero returns ZeroDivisionError."""
        result = await run_python_tool(code="1/0")
        text = result["content"][0]["text"]
        assert "Error" in text
        assert "ZeroDivisionError" in text

    @pytest.mark.asyncio
    async def test_name_error(self, run_python_tool):
        """Undefined variable returns NameError."""
        result = await run_python_tool(code="result = undefined_var")
        text = result["content"][0]["text"]
        assert "Error" in text
        assert "NameError" in text

    @pytest.mark.asyncio
    async def test_type_error(self, run_python_tool):
        """Type mismatch returns TypeError."""
        result = await run_python_tool(code='result = "hello" + 42')
        text = result["content"][0]["text"]
        assert "Error" in text
        assert "TypeError" in text


# ---------------------------------------------------------------------------
# Registration tests
# ---------------------------------------------------------------------------


class TestRunPythonRegistration:
    """Test conditional tool registration."""

    def test_registered_when_enabled(self):
        """run_python is registered when programmatic_tools_enabled=True."""
        from nous.api.tools import ToolDispatcher, register_programmatic_tools

        settings = Settings(programmatic_tools_enabled=True)
        dispatcher = ToolDispatcher()
        register_programmatic_tools(dispatcher, AsyncMock(), AsyncMock(), settings)
        tool_names = [t["name"] for t in dispatcher.tool_definitions()]
        assert "run_python" in tool_names

    def test_not_registered_when_disabled(self):
        """run_python is NOT registered when programmatic_tools_enabled=False."""
        from nous.api.tools import ToolDispatcher, register_programmatic_tools

        settings = Settings(programmatic_tools_enabled=False)
        dispatcher = ToolDispatcher()
        register_programmatic_tools(dispatcher, AsyncMock(), AsyncMock(), settings)
        tool_names = [t["name"] for t in dispatcher.tool_definitions()]
        assert "run_python" not in tool_names


# ---------------------------------------------------------------------------
# Frame access tests
# ---------------------------------------------------------------------------


class TestRunPythonFrameAccess:
    """Test that run_python is in the correct FRAME_TOOLS entries."""

    def test_in_conversation_frame(self):
        from nous.api.runner import FRAME_TOOLS

        assert "run_python" in FRAME_TOOLS["conversation"]

    def test_in_question_frame(self):
        from nous.api.runner import FRAME_TOOLS

        assert "run_python" in FRAME_TOOLS["question"]

    def test_in_debug_frame(self):
        from nous.api.runner import FRAME_TOOLS

        assert "run_python" in FRAME_TOOLS["debug"]

    def test_in_task_frame_via_wildcard(self):
        """task frame uses '*' wildcard — run_python available implicitly."""
        from nous.api.runner import FRAME_TOOLS

        assert "*" in FRAME_TOOLS["task"]

    def test_not_in_decision_frame(self):
        from nous.api.runner import FRAME_TOOLS

        assert "run_python" not in FRAME_TOOLS["decision"]

    def test_not_in_creative_frame(self):
        from nous.api.runner import FRAME_TOOLS

        assert "run_python" not in FRAME_TOOLS["creative"]

    def test_not_in_initiation_frame(self):
        from nous.api.runner import FRAME_TOOLS

        assert "run_python" not in FRAME_TOOLS["initiation"]
