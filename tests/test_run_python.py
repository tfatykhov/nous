"""Tests for 012.3 programmatic tool calling (run_python).

TDD: These tests are written FIRST before implementation.

The run_python tool allows Claude to write Python scripts that batch
memory operations, filter results, and return shaped data — reducing
token consumption compared to separate tool calls.
"""

import asyncio
import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from nous.config import PROGRAMMATIC_TOOLS_TIMEOUT_GRACE_SECONDS, Settings
from nous.observability.retrieval_logger import RETRIEVAL_PATHS

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

    # Default: the compatibility backfill finds nothing extra. Configured here
    # rather than per-test because an UNconfigured AsyncMock attribute returns a
    # coroutine, which surfaces as an opaque "argument of type 'coroutine'"
    # TypeError from inside the executed script rather than as a mock error.
    heart.facts.fetch_legacy_fields = AsyncMock(return_value={})

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


@pytest.fixture
def patched_pipeline():
    """Stub `run_recall_pipeline` — what in-script recall_deep now calls.

    Patched on the DEFINING module rather than on `nous.api.tools`, because
    `_recall_deep` imports it lazily inside the function body (mirroring the
    real tool at tools.py:1389), so the lookup happens per call and resolves
    against `nous.api.retrieval_pipeline` every time.
    """
    from nous.api.retrieval_pipeline import PipelineResult, PipelineStats

    # metadata mirrors what `Heart._to_recall_result` actually emits for a
    # FactSummary — the persisted fields live under `metadata`, which is exactly
    # why the tool has to flatten them back to the top level. Carrying `active`
    # matters: its ABSENCE is what triggers the non-primary-leg backfill, so a
    # fixture missing it would silently exercise the wrong path.
    def _meta(conf: float) -> dict:
        return {
            "category": "technical", "subject": "cache", "confidence": conf,
            "active": True, "tags": [], "superseded_by": None,
            "actionable": None, "actionable_confidence": None,
            "overrides_prior": False,
        }

    results = [
        PipelineResult(
            id=uuid4(), type="fact", description="caching uses Redis",
            score=0.85, source="heart", metadata=_meta(0.9),
        ),
        PipelineResult(
            id=uuid4(), type="fact", description="cache TTL is 300s",
            score=0.60, source="heart", metadata=_meta(0.7),
        ),
    ]
    mock = AsyncMock(return_value=(results, PipelineStats()))
    with patch("nous.api.retrieval_pipeline.run_recall_pipeline", mock):
        yield mock


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
        """programmatic_tools_timeout defaults to 90.

        Raised from 10 when in-script recall_deep became the real pipeline:
        prod `retrieval_log` (n=104) puts a pipeline retrieval at p50 5.3s /
        p95 14.7s, so 10s could not fit even one call reliably.
        """
        s = Settings()
        assert s.programmatic_tools_timeout == 90

    def test_timeout_leaves_headroom_under_tool_timeout(self):
        """The inner deadline must expire before the dispatcher's outer one.

        `_dispatch_with_keepalive` wraps EVERY tool in
        `asyncio.wait_for(..., tool_timeout)`. If run_python's own deadline
        ever met or exceeded that, the outer cancel would fire first and the
        script would surface as a generic tool timeout — losing the per-script
        error message, and losing the partial trace commit with it.
        """
        s = Settings()
        assert s.programmatic_tools_timeout < s.tool_timeout

    def test_timeout_inversion_is_rejected_at_construction(self):
        """The invariant is enforced for OVERRIDES, not just the defaults.

        Asserting only the shipped pair (90 < 120) would pass while an operator
        setting `NOUS_TOOL_TIMEOUT=60` silently inverts it — the failure mode is
        a config combination, so the guard has to live in config.
        """
        # NOTE: `tool_timeout` carries `validation_alias="NOUS_TOOL_TIMEOUT"`,
        # so it can ONLY be set by that alias. `Settings(tool_timeout=60)` is
        # treated as an extra field and, under `extra="ignore"`, silently
        # dropped — the first draft of this test passed a plain kwarg and
        # "failed to raise" against a Settings still holding the default 120.
        with pytest.raises(ValueError, match="must be < tool_timeout"):
            Settings(programmatic_tools_timeout=90, NOUS_TOOL_TIMEOUT=60)

        # Equal is also wrong: the two deadlines would race.
        with pytest.raises(ValueError, match="must be < tool_timeout"):
            Settings(programmatic_tools_timeout=60, NOUS_TOOL_TIMEOUT=60)

        # And the guard must not fire on a valid override.
        assert Settings(
            programmatic_tools_timeout=30, NOUS_TOOL_TIMEOUT=60
        ).programmatic_tools_timeout == 30

    def test_validator_accounts_for_the_dispatch_grace(self):
        """The real inner wait is `timeout + GRACE`, so the guard must use it.

        `run_python` awaits its worker on
        `asyncio.wait_for(..., timeout + PROGRAMMATIC_TOOLS_TIMEOUT_GRACE_SECONDS)`.
        Checking the nominal value alone would accept 119 against 120 while the
        inner wait actually runs to 121s — the outer cancel still wins, which is
        the exact failure the guard exists to prevent.
        """
        with pytest.raises(ValueError, match="dispatch grace"):
            Settings(programmatic_tools_timeout=119, NOUS_TOOL_TIMEOUT=120)

        s = Settings(programmatic_tools_timeout=117, NOUS_TOOL_TIMEOUT=120)
        assert (
            s.programmatic_tools_timeout + PROGRAMMATIC_TOOLS_TIMEOUT_GRACE_SECONDS
            < s.tool_timeout
        )

    def test_disabled_programmatic_tools_skip_the_coupling(self):
        """A dormant field must not refuse startup.

        With run_python disabled the invariant has no runtime consumer, so a
        deployment that turned the tool off and lowered tool_timeout should
        still boot with whatever timeout it had configured.
        """
        s = Settings(
            programmatic_tools_enabled=False,
            programmatic_tools_timeout=90,
            NOUS_TOOL_TIMEOUT=60,
        )
        assert s.programmatic_tools_timeout == 90  # untouched, not clamped

    def test_timeout_grace_is_shared(self):
        """config and tools must not drift on the grace value."""
        from nous.api import tools as tools_mod

        assert tools_mod._TIMEOUT_GRACE == PROGRAMMATIC_TOOLS_TIMEOUT_GRACE_SECONDS

    def test_lowering_tool_timeout_alone_clamps_instead_of_refusing_to_boot(self):
        """An operator who set only NOUS_TOOL_TIMEOUT must still start.

        `NOUS_TOOL_TIMEOUT=60` was valid before this field grew a 90s default
        and is not paired with any run_python override. Raising there would
        convert an existing deployment into a boot failure on upgrade — a worse
        outcome than the inversion the guard exists to prevent. CI caught this:
        `test_streaming_keepalive` sets 60 and 10.
        """
        s = Settings(NOUS_TOOL_TIMEOUT=60)
        assert s.programmatic_tools_timeout == 57  # 60 - 2s grace - 1
        assert (
            s.programmatic_tools_timeout + PROGRAMMATIC_TOOLS_TIMEOUT_GRACE_SECONDS
            < s.tool_timeout
        )

        # Clamping must never produce a value that still breaks the invariant.
        # With tool_timeout=1 and a ge=1 floor no such value exists, so this
        # refuses rather than clamping to 1 and reporting success.
        # keepalive_interval=0 is required to reach this branch at all —
        # `_validate_keepalive` guards the same ceiling and rejects the default
        # 10 first.
        with pytest.raises(ValueError, match="leaves no room"):
            Settings(NOUS_TOOL_TIMEOUT=1, NOUS_KEEPALIVE_INTERVAL=0)

    def test_custom_values(self):
        """Can override via constructor."""
        s = Settings(programmatic_tools_enabled=False, programmatic_tools_timeout=30)
        assert s.programmatic_tools_enabled is False
        assert s.programmatic_tools_timeout == 30

    def test_default_max_concurrent(self):
        """programmatic_tools_max_concurrent defaults to 4."""
        s = Settings()
        assert s.programmatic_tools_max_concurrent == 4

    def test_max_concurrent_must_be_positive(self):
        """A cap below 1 would disable the tool outright — reject it."""
        with pytest.raises(ValueError):
            Settings(programmatic_tools_max_concurrent=0)


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
# Full-builtins tests (the SAFE_BUILTINS allowlist was removed)
# ---------------------------------------------------------------------------


class TestRunPythonFullBuiltins:
    """Full builtins are exposed — the allowlist bought no security.

    The same agent holds an unrestricted `bash` tool in the same tool belt, so
    everything the allowlist blocked was one heredoc away. It only cost
    usability (no `import`, no `except Exception`, no class bodies).
    """

    @pytest.mark.asyncio
    async def test_import_works(self, run_python_tool):
        """import statement works."""
        result = await run_python_tool(
            code='import base64\nresult = base64.b64encode(b"hi").decode()'
        )
        assert result["content"][0]["text"] == "aGk="

    @pytest.mark.asyncio
    async def test_import_os_works(self, run_python_tool):
        """import os works (parity with the bash tool the agent already has)."""
        result = await run_python_tool(code="import os\nresult = type(os.sep).__name__")
        assert result["content"][0]["text"] == "str"

    @pytest.mark.asyncio
    async def test_try_except_exception(self, run_python_tool):
        """try/except Exception works — used to NameError on Exception."""
        result = await run_python_tool(
            code="try:\n    1 / 0\nexcept Exception as e:\n    result = type(e).__name__"
        )
        assert result["content"][0]["text"] == "ZeroDivisionError"

    @pytest.mark.asyncio
    async def test_class_definition(self, run_python_tool):
        """Class definitions work — used to fail on missing __build_class__."""
        code = (
            "class Point:\n"
            "    def __init__(self, x):\n"
            "        self.x = x\n"
            "\n"
            "result = Point(7).x"
        )
        result = await run_python_tool(code=code)
        assert result["content"][0]["text"] == "7"

    @pytest.mark.asyncio
    async def test_getattr_and_dir(self, run_python_tool):
        """Reflection builtins are available."""
        result = await run_python_tool(
            code='result = getattr("abc", "upper")()'
        )
        assert result["content"][0]["text"] == "ABC"

    @pytest.mark.asyncio
    async def test_open_available(self, run_python_tool):
        """open() is available (errors come from the OS, not a sandbox)."""
        result = await run_python_tool(
            code=(
                "import tempfile, os\n"
                "path = os.path.join(tempfile.gettempdir(), 'nous_run_python_probe.txt')\n"
                "open(path, 'w').write('ok')\n"
                "result = open(path).read()\n"
                "os.remove(path)"
            )
        )
        assert result["content"][0]["text"] == "ok"


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
    async def test_recall_deep_runs_the_full_pipeline(
        self, run_python_tool, mock_heart, patched_pipeline
    ):
        """In-script recall_deep runs run_recall_pipeline, NOT a fact search.

        This is the regression lock. Until 2026-08-25 the in-script function was
        `heart.search_facts` — the facts table alone, with no episodes,
        decisions, chunks or graph expansion — under a name that promised the
        tool's full retrieval. Asserting `search_facts` is NOT called is the
        point: a future refactor that "simplifies" this back to a fact search
        would otherwise look green.
        """
        result = await run_python_tool(
            code='facts = recall_deep("caching", limit=3)\nresult = json.dumps(len(facts))'
        )
        patched_pipeline.assert_called_once()
        assert patched_pipeline.call_args.kwargs["query"] == "caching"
        assert patched_pipeline.call_args.kwargs["limit"] == 3
        mock_heart.search_facts.assert_not_called()
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
    async def test_recall_deep_returns_dicts(self, run_python_tool, patched_pipeline):
        """recall_deep results are dicts accessible with bracket notation.

        Deliberately still sorts on `f["confidence"]` and reads `f["content"]`,
        exactly as before the pipeline swap. Those are FactSummary keys that the
        pipeline nests under `metadata`; rewriting this test to the new shape
        would have been bending the test to the implementation and silently
        conceding a contract break to every stored script template.
        """
        result = await run_python_tool(
            code=(
                'facts = recall_deep("caching")\n'
                'top = sorted(facts, key=lambda f: f["confidence"], reverse=True)\n'
                'result = json.dumps(top[0]["content"])'
            )
        )
        assert "caching uses Redis" in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_recall_deep_flattens_legacy_fact_keys(
        self, run_python_tool, patched_pipeline
    ):
        """category/subject/confidence stay reachable at the top level."""
        result = await run_python_tool(
            code=(
                'f = recall_deep("caching")[0]\n'
                'result = json.dumps([f["category"], f["subject"], f["confidence"]])'
            )
        )
        assert result["content"][0]["text"] == '["technical", "cache", 0.9]'

    @pytest.mark.asyncio
    async def test_residual_state_is_not_read_when_no_recall_happens(
        self, mock_brain, mock_heart
    ):
        """A script that never calls recall_deep pays nothing for F055.

        Reading it eagerly charged every run_python call two DB reads — outside
        both the run slot and the script deadline, so a slow activation read
        could consume the dispatcher's remaining timeout before the script's own
        deadline even started.
        """
        from nous.api.tools import create_programmatic_tools

        activator = MagicMock()
        activator.current_turn = AsyncMock(return_value=3)
        activator.compute_activations = AsyncMock(return_value={})
        mock_heart._residual_activator = activator

        s = Settings(
            programmatic_tools_enabled=True,
            programmatic_tools_timeout=5,
            residual_activation_enabled=True,
        )
        tool = create_programmatic_tools(mock_brain, mock_heart, s)["run_python"]
        await tool(code="result = 1 + 1", _session_id="sess-1")

        activator.current_turn.assert_not_called()
        activator.compute_activations.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_primary_leg_facts_are_backfilled(
        self, mock_brain, mock_heart
    ):
        """A keyed/exemplar/graph fact gets its REAL values, not the defaults.

        Only the primary Heart leg populates these in metadata; the keyed,
        keyed_r2, exemplar and graph-expansion legs build their own metadata
        from narrower SELECTs — and all four are ENABLED in prod. Without the
        backfill a script filtering on `active` would silently misclassify a
        fact based on which leg happened to find it.

        The trigger is the MISSING FIELD, not the leg name, so a leg added later
        is covered without touching this.
        """
        from nous.api.retrieval_pipeline import PipelineResult, PipelineStats
        from nous.api.tools import create_programmatic_tools

        fid = uuid4()
        # Shaped like `_keyed_to_pipeline` output: no `active`/`confidence`.
        results = [PipelineResult(
            id=fid, type="fact", description="keyed hit", score=0.55,
            source="heart", metadata={"retrieval_leg": "keyed", "subject": "s"},
        )]
        mock_heart.facts.fetch_legacy_fields = AsyncMock(return_value={
            fid: {"active": True, "confidence": 0.91, "tags": ["t"],
                  "category": "technical"},
        })

        s = Settings(programmatic_tools_enabled=True, programmatic_tools_timeout=5)
        tool = create_programmatic_tools(mock_brain, mock_heart, s)["run_python"]
        with patch(
            "nous.api.retrieval_pipeline.run_recall_pipeline",
            AsyncMock(return_value=(results, PipelineStats())),
        ):
            out = await tool(code=(
                'f = recall_deep("q")[0]\n'
                'result = json.dumps([f["active"], f["confidence"], f["tags"]])'
            ))
        assert out["content"][0]["text"] == '[true, 0.91, ["t"]]'
        mock_heart.facts.fetch_legacy_fields.assert_called_once_with([fid])

    @pytest.mark.asyncio
    async def test_complete_facts_trigger_no_backfill_query(
        self, mock_brain, mock_heart, patched_pipeline
    ):
        """The extra query must not fire when the primary leg already filled in."""
        from nous.api.tools import create_programmatic_tools

        mock_heart.facts.fetch_legacy_fields = AsyncMock(return_value={})
        # `patched_pipeline`'s rows already mirror the primary Heart leg, which
        # carries `active` — so the backfill has nothing to look up.
        s = Settings(programmatic_tools_enabled=True, programmatic_tools_timeout=5)
        tool = create_programmatic_tools(mock_brain, mock_heart, s)["run_python"]
        await tool(code='recall_deep("caching")')

        mock_heart.facts.fetch_legacy_fields.assert_not_called()

    def test_fact_rows_carry_real_values_not_defaults(self):
        """`Heart._to_recall_result` must propagate the persisted fact fields.

        This is the load-bearing half of the compatibility story. Without it the
        default map stops scripts raising KeyError only to have them silently
        decide from fabricated values instead — strictly worse than the crash: a
        script filtering `[f for f in facts if f["active"]]` would drop every
        fact and look like it merely found nothing.

        Asserts against `_LEGACY_FACT_KEYS` rather than a second hand-written
        list, minus the two transient recency verdicts the resolver owns
        downstream, where absent genuinely means "no verdict".
        """
        from uuid import uuid4 as _uuid4

        from nous.api.tools import _LEGACY_FACT_KEYS
        from nous.heart.heart import Heart
        from nous.heart.schemas import FactSummary

        sup = _uuid4()
        fact = FactSummary(
            id=_uuid4(), content="c", category="technical", subject="s",
            confidence=0.9, active=True, tags=["a"], superseded_by=sup,
            actionable=True, actionable_confidence=0.7, overrides_prior=True,
        )
        # Unbound call: the conversion reads nothing off `self`.
        rr = Heart._to_recall_result(None, "fact", fact, 0.5)

        transient = {"recency_status", "recency_date"}
        for key in set(_LEGACY_FACT_KEYS) - transient:
            assert key in rr.metadata, f"fact metadata is missing {key!r}"

        # And the values are the fact's own, not the fallbacks.
        assert rr.metadata["active"] is True
        assert rr.metadata["tags"] == ["a"]
        assert rr.metadata["superseded_by"] == sup
        assert rr.metadata["actionable"] is True
        assert rr.metadata["overrides_prior"] is True

    def test_legacy_summary_fields_are_columns(self):
        """Every backfilled name must be a real `Fact` column.

        `LEGACY_SUMMARY_FIELDS` is derived from FactSummary, so a field added
        there that is NOT persisted would otherwise reach `getattr(Fact, f)` and
        blow up the SELECT at runtime. Fail here instead.
        """
        from nous.heart.facts import FactManager
        from nous.storage.models import Fact

        assert FactManager.LEGACY_SUMMARY_FIELDS
        missing = [
            f for f in FactManager.LEGACY_SUMMARY_FIELDS if not hasattr(Fact, f)
        ]
        assert not missing, f"not Fact columns: {missing}"

    def test_backfill_and_key_map_agree(self):
        """The two derived lists must not drift apart.

        They were hand-written once and immediately disagreed: the backfill
        omitted `event_date`, so a dated fact arriving via the exemplar or graph
        leg was filled with None and silently misclassified by any script
        filtering on date. Both are now derived; this asserts they stay aligned.
        """
        from nous.api.tools import _LEGACY_FACT_KEYS
        from nous.heart.facts import FactManager

        transient = {"recency_status", "recency_date"}
        assert set(FactManager.LEGACY_SUMMARY_FIELDS) == (
            set(_LEGACY_FACT_KEYS) - transient
        )

    def test_legacy_key_map_covers_every_fact_summary_field(self):
        """The compat map is DERIVED from FactSummary, so it cannot drift.

        A hand-written list drifted twice in one review cycle. This asserts the
        property directly rather than re-listing the fields, which would just
        create a third list to drift.
        """
        from nous.api.tools import _LEGACY_FACT_KEYS
        from nous.heart.schemas import FactSummary

        owned = {"id", "content", "score"}  # the result dict supplies these
        assert set(_LEGACY_FACT_KEYS) == set(FactSummary.model_fields) - owned
        # None everywhere except tags: a fabricated value would sort/compare
        # as though it were real data.
        assert _LEGACY_FACT_KEYS["tags"] == []
        assert all(v is None for k, v in _LEGACY_FACT_KEYS.items() if k != "tags")

    @pytest.mark.asyncio
    async def test_recall_deep_forwards_f071_exclusions(
        self, run_python_tool, patched_pipeline
    ):
        """The F071 exclusion set must reach the scripted pipeline call.

        `CURRENT_TURN_EXCLUDE_IDS` is a ContextVar and is NOT propagated into
        run_python's executor thread, so it has to be read on the main loop and
        passed down. Without it, in-script recall can return memories already
        in the system prompt while the advertised-equivalent tool filters them.
        """
        from nous.api.runner import CURRENT_TURN_EXCLUDE_IDS

        ids = {"fact": {uuid4()}}
        token = CURRENT_TURN_EXCLUDE_IDS.set(ids)
        try:
            await run_python_tool(code='recall_deep("caching")')
        finally:
            CURRENT_TURN_EXCLUDE_IDS.reset(token)

        assert patched_pipeline.call_args.kwargs["exclude_ids"] == ids

    @pytest.mark.asyncio
    async def test_recall_deep_carries_residual_activation(
        self, mock_brain, mock_heart, patched_pipeline
    ):
        """F055 state must flow BOTH ways on the scripted path.

        `NOUS_RESIDUAL_ACTIVATION_ENABLED=true` in prod, so without this a
        scripted recall ranks against cold state AND never advances activation
        for later turns — results diverging purely by which calling path was
        used, which is the divergence this whole PR exists to remove.
        """
        from nous.api.tools import create_programmatic_tools

        fid = uuid4()
        activator = MagicMock()
        activator.current_turn = AsyncMock(return_value=3)
        activator.compute_activations = AsyncMock(return_value={fid: 0.8})
        activator.record_surfaced = AsyncMock(return_value=None)
        mock_heart._residual_activator = activator

        s = Settings(
            programmatic_tools_enabled=True,
            programmatic_tools_timeout=5,
            residual_activation_enabled=True,
        )
        tool = create_programmatic_tools(mock_brain, mock_heart, s)["run_python"]
        await tool(code='recall_deep("caching")', _session_id="sess-1")

        # Read: the pipeline ranked with the session's activations.
        assert patched_pipeline.call_args.kwargs["residual_activations"] == {fid: 0.8}
        # Write: the surfaced set advanced the session for the next turn.
        await asyncio.sleep(0)  # let the call_soon_threadsafe callback run
        activator.record_surfaced.assert_called_once()
        assert activator.record_surfaced.call_args.kwargs["current_turn"] == 4

    @pytest.mark.asyncio
    async def test_legacy_keys_present_on_non_fact_results(self, run_python_tool):
        """Mixed result types must not raise KeyError on fact-only fields.

        The old function returned FACTS ONLY, so
        `sorted(recall_deep(q), key=lambda f: f["confidence"])` was valid. The
        pipeline returns episodes, chunks, procedures and decisions too, so
        populating these keys only when metadata carries them would still raise
        on the first episode. Values are None, never a stand-in — a fabricated
        0.0 confidence would sort and compare as if it were real.
        """
        from nous.api.retrieval_pipeline import PipelineResult, PipelineStats

        results = [
            PipelineResult(id=uuid4(), type="episode", description="an episode",
                           score=0.7, source="heart", metadata={"title": "t"}),
            PipelineResult(id=uuid4(), type="procedure", description="a proc",
                           score=0.6, source="heart", metadata={}),
        ]
        with patch(
            "nous.api.retrieval_pipeline.run_recall_pipeline",
            AsyncMock(return_value=(results, PipelineStats())),
        ):
            out = await run_python_tool(
                code=(
                    'rs = recall_deep("q")\n'
                    'ok = all("confidence" in r and "category" in r and '
                    '"subject" in r and "tags" in r for r in rs)\n'
                    'result = json.dumps([ok, rs[0]["confidence"], rs[0]["tags"]])'
                )
            )
        assert out["content"][0]["text"] == "[true, null, []]"

    @pytest.mark.asyncio
    async def test_legacy_tags_default_is_not_shared_between_rows(
        self, run_python_tool
    ):
        """Each row gets its OWN default list.

        A single shared `[]` would mean a script appending to one row's tags
        silently mutated every other row in the batch.
        """
        from nous.api.retrieval_pipeline import PipelineResult, PipelineStats

        results = [
            PipelineResult(id=uuid4(), type="episode", description="a", score=0.7,
                           source="heart", metadata={}),
            PipelineResult(id=uuid4(), type="episode", description="b", score=0.6,
                           source="heart", metadata={}),
        ]
        with patch(
            "nous.api.retrieval_pipeline.run_recall_pipeline",
            AsyncMock(return_value=(results, PipelineStats())),
        ):
            out = await run_python_tool(
                code=(
                    'rs = recall_deep("q")\n'
                    'rs[0]["tags"].append("x")\n'
                    'result = json.dumps([rs[0]["tags"], rs[1]["tags"]])'
                )
            )
        assert out["content"][0]["text"] == '[["x"], []]'

    @pytest.mark.asyncio
    async def test_recall_deep_does_not_let_metadata_shadow_canonical_keys(
        self, run_python_tool
    ):
        """A metadata key never overwrites id/type/score/source/description."""
        from nous.api.retrieval_pipeline import PipelineResult, PipelineStats

        rid = uuid4()
        results = [PipelineResult(
            id=rid, type="fact", description="real body", score=0.5,
            source="heart",
            metadata={"score": 999, "type": "bogus", "content": "hijacked",
                      "confidence": 0.4},
        )]
        with patch(
            "nous.api.retrieval_pipeline.run_recall_pipeline",
            AsyncMock(return_value=(results, PipelineStats())),
        ):
            out = await run_python_tool(
                code=(
                    'f = recall_deep("q")[0]\n'
                    'result = json.dumps([f["score"], f["type"], f["content"], '
                    'f["confidence"]])'
                )
            )
        assert out["content"][0]["text"] == '[0.5, "fact", "real body", 0.4]'

    @pytest.mark.asyncio
    async def test_recall_deep_keeps_content_alias(
        self, run_python_tool, patched_pipeline
    ):
        """`content` still resolves after the FactSummary -> PipelineResult swap.

        PipelineResult calls the body `description`. Scripts written against the
        old FactSummary shape read `content`, and script templates can live
        inside stored skills and procedures, so dropping the key would surface
        as a KeyError from agent-authored code rather than anywhere in the tree.
        """
        result = await run_python_tool(
            code=(
                'facts = recall_deep("caching")\n'
                'result = json.dumps(facts[0]["content"] == facts[0]["description"])'
            )
        )
        assert result["content"][0]["text"] == "true"

    @pytest.mark.asyncio
    async def test_recall_deep_traces_under_script_path(
        self, run_python_tool, patched_pipeline
    ):
        """The trace handed to the pipeline is opened with path='script'.

        Script recalls were absent from the F091 dashboard because they never
        reached an instrumented function. Tagging them 'script' rather than
        'pipeline' keeps them attributable: a single tool call can issue several,
        so folding them into the tool's rows would make per-turn counts wrong.
        """
        from nous.observability.retrieval_trace import NULL_TRACE

        started: list[dict] = []

        class _Rec:
            def start(self, **kw):
                started.append(kw)
                return NULL_TRACE

            def commit(self, trace):  # noqa: ARG002
                pass

        # Patch the name bound INSIDE tools.py, not the one in the observability
        # module: tools.py does `from ... import get_active as
        # get_active_retrieval_logger` at import time, so rebinding the source
        # module would leave the already-resolved reference untouched and the
        # test would pass against an unpatched call.
        with patch("nous.api.tools.get_active_retrieval_logger", _Rec):
            await run_python_tool(code='recall_deep("caching")')

        assert started and started[0]["path"] == "script"
        assert started[0]["query"] == "caching"
        assert started[0]["path"] in RETRIEVAL_PATHS

    @pytest.mark.asyncio
    async def test_trace_is_committed_on_the_main_loop(
        self, run_python_tool, patched_pipeline
    ):
        """commit() must not run on run_python's worker thread.

        `RetrievalLogger.commit` schedules the DB write through `_schedule_bg`,
        which calls `asyncio.get_running_loop()`; off-loop that raises and the
        except branch calls `coro.close()`, discarding the write silently. The
        trace would then exist only in the in-memory ring, which has no reader —
        so every script retrieval would be persisted nowhere, which is the exact
        invisibility this change exists to end. Asserting the THREAD, because
        that is the property that actually broke.
        """
        import threading

        from nous.observability.retrieval_trace import NULL_TRACE

        main_thread = threading.get_ident()
        commit_threads: list[int] = []

        class _Rec:
            def start(self, **kw):  # noqa: ARG002
                return NULL_TRACE

            def commit(self, trace):  # noqa: ARG002
                commit_threads.append(threading.get_ident())

        with patch("nous.api.tools.get_active_retrieval_logger", _Rec):
            await run_python_tool(code='recall_deep("caching")')

        assert commit_threads, "trace was never committed"
        assert commit_threads == [main_thread], (
            "commit ran off the main loop; the DB write would be dropped"
        )

    @pytest.mark.asyncio
    async def test_deadline_enforcement_survives_a_caught_recall_error(
        self, mock_brain, mock_heart
    ):
        """An ordinary recall failure must not disarm the deadline tracer.

        `_fail_trace` turns tracing off so cleanup cannot be interrupted. But an
        ordinary Exception is catchable — a script doing
        `try: recall_deep(...) except Exception: pass` keeps running — and
        leaving the tracer off would hand it a thread with no deadline
        enforcement, reopening the `while True: pass` hole that holds a worker
        and a concurrency slot indefinitely.
        """
        from nous.api.retrieval_pipeline import PipelineStats  # noqa: F401
        from nous.api.tools import create_programmatic_tools

        class _Rec:
            def start(self, **kw):  # noqa: ARG002
                from nous.observability.retrieval_trace import RetrievalTrace

                return RetrievalTrace(query="q", path="script", agent_id="a")

            def commit(self, trace):  # noqa: ARG002
                pass

        s = Settings(programmatic_tools_enabled=True, programmatic_tools_timeout=2)
        tool = create_programmatic_tools(mock_brain, mock_heart, s)["run_python"]
        with patch("nous.api.tools.get_active_retrieval_logger", _Rec), patch(
            "nous.api.retrieval_pipeline.run_recall_pipeline",
            AsyncMock(side_effect=RuntimeError("recall boom")),
        ):
            # Bounded. Verified by reverting the restore: without it this call
            # never returns — the spin loop owns the worker thread forever. A
            # bare await would wedge the whole suite instead of failing, so the
            # bound converts a hang into a clean, readable failure.
            out = await asyncio.wait_for(
                tool(code=(
                    'try:\n'
                    '    recall_deep("q")\n'
                    'except Exception:\n'
                    '    pass\n'
                    'while True:\n'
                    '    pass\n'
                )),
                timeout=30,
            )

        # The spin loop must be interrupted by the in-thread deadline, not left
        # running until the outer wait_for gives up on it.
        assert out.get("is_error")
        assert "timed out" in out["content"][0]["text"].lower()

    @pytest.mark.asyncio
    async def test_duplicate_across_the_limit_is_not_reported_dropped(
        self, mock_brain, mock_heart
    ):
        """A memory delivered in the prefix stays delivered.

        Candidates are keyed on (id, type) and the graph and Brain legs
        deliberately do not cross-dedup, so the same decision can appear twice.
        Marking the tail copy would downgrade the shared candidate, and
        `undeliver_all` cannot undo it — it only rewrites `rendered` ones.
        """
        from nous.api.retrieval_pipeline import PipelineResult, PipelineStats
        from nous.api.tools import create_programmatic_tools

        dup = uuid4()
        results = [
            PipelineResult(id=dup, type="decision", description="d", score=0.9,
                           source="brain", metadata={}),
            PipelineResult(id=uuid4(), type="fact", description="f", score=0.8,
                           source="heart", metadata={"active": True}),
            PipelineResult(id=dup, type="decision", description="d", score=0.7,
                           source="graph_expanded", metadata={}),
        ]
        marked: list = []

        class _Trace:
            id = "t"
            enabled = True

            def mark_not_delivered(self, item_id, item_type, disp, stage):
                marked.append((item_id, item_type))

            def __getattr__(self, _name):
                return lambda *a, **k: None

        class _Rec:
            def start(self, **kw):  # noqa: ARG002
                return _Trace()

            def commit(self, trace):  # noqa: ARG002
                pass

        s = Settings(programmatic_tools_enabled=True, programmatic_tools_timeout=5)
        tool = create_programmatic_tools(mock_brain, mock_heart, s)["run_python"]
        with patch("nous.api.tools.get_active_retrieval_logger", _Rec), patch(
            "nous.api.retrieval_pipeline.run_recall_pipeline",
            AsyncMock(return_value=(results, PipelineStats())),
        ):
            await tool(code='recall_deep("q", limit=2)')

        assert marked == [], "the duplicate was delivered in the prefix"

    @pytest.mark.asyncio
    async def test_legacy_python_types_are_preserved(
        self, mock_brain, mock_heart
    ):
        """`id` stays a UUID and `event_date` a date, as model_dump() gave.

        `str(uuid)` and an ISO string would both be friendlier to json.dumps,
        but the old return was `FactSummary.model_dump()` in python mode, and
        `recall_recent` still returns python-mode values — so converting here
        breaks `f["id"].hex`, breaks date arithmetic, and makes the two wrappers
        disagree. A date comparison against a string does not raise; it silently
        stops matching, which is the worse failure.
        """
        from datetime import date as _date

        from nous.api.retrieval_pipeline import PipelineResult, PipelineStats
        from nous.api.tools import create_programmatic_tools

        fid = uuid4()
        results = [PipelineResult(
            id=fid, type="fact", description="d", score=0.5, source="heart",
            metadata={"active": True, "event_date": "2026-03-04"},
        )]
        s = Settings(programmatic_tools_enabled=True, programmatic_tools_timeout=5)
        tool = create_programmatic_tools(mock_brain, mock_heart, s)["run_python"]
        with patch(
            "nous.api.retrieval_pipeline.run_recall_pipeline",
            AsyncMock(return_value=(results, PipelineStats())),
        ):
            out = await tool(code=(
                'f = recall_deep("q")[0]\n'
                'result = json.dumps([str(type(f["id"]).__name__), '
                'str(type(f["event_date"]).__name__), f["event_date"].year])'
            ))
        assert out["content"][0]["text"] == '["UUID", "date", 2026]'
        assert _date(2026, 3, 4)  # sanity: the fixture date is well-formed

    @pytest.mark.asyncio
    async def test_limit_does_not_surface_excess_to_residual_activation(
        self, mock_brain, mock_heart
    ):
        """F055 must record only what the script actually received.

        Recording the rows the limit removed would boost memories the
        interpreter never saw on later turns — the telemetry saying "excluded"
        while ranking state says "delivered".
        """
        from nous.api.retrieval_pipeline import PipelineResult, PipelineStats
        from nous.api.tools import create_programmatic_tools

        activator = MagicMock()
        activator.current_turn = AsyncMock(return_value=1)
        activator.compute_activations = AsyncMock(return_value={})
        activator.record_surfaced = AsyncMock(return_value=None)
        mock_heart._residual_activator = activator

        many = [
            PipelineResult(
                id=uuid4(), type="fact", description=f"f{i}", score=1.0 - i / 100,
                source="heart", metadata={"active": True},
            )
            for i in range(6)
        ]
        s = Settings(
            programmatic_tools_enabled=True, programmatic_tools_timeout=5,
            residual_activation_enabled=True,
        )
        tool = create_programmatic_tools(mock_brain, mock_heart, s)["run_python"]
        with patch(
            "nous.api.retrieval_pipeline.run_recall_pipeline",
            AsyncMock(return_value=(many, PipelineStats())),
        ):
            await tool(code='recall_deep("q", limit=2)', _session_id="s1")

        await asyncio.sleep(0)
        activator.record_surfaced.assert_called_once()
        surfaced = activator.record_surfaced.call_args.kwargs["surfaced"]
        assert len(surfaced) == 2, "recorded rows the script never received"

    @pytest.mark.asyncio
    async def test_limit_caps_what_the_script_receives(
        self, mock_brain, mock_heart
    ):
        """`limit` bounds the returned list, as `search_facts` always did.

        The pipeline treats `limit` as the core Heart/Brain allotment only —
        chunks use `episode_chunk_recall_limit` (30 in prod), keyed/exemplar use
        their own K, graph rows append independently — so without a cap
        `recall_deep(q, limit=2)` could hand back dozens of rows and blow the
        processing bound a stored script relies on.
        """
        from nous.api.retrieval_pipeline import PipelineResult, PipelineStats
        from nous.api.tools import create_programmatic_tools

        many = [
            PipelineResult(
                id=uuid4(), type="fact", description=f"f{i}", score=1.0 - i / 100,
                source="heart", metadata={"active": True},
            )
            for i in range(12)
        ]
        s = Settings(programmatic_tools_enabled=True, programmatic_tools_timeout=5)
        tool = create_programmatic_tools(mock_brain, mock_heart, s)["run_python"]
        with patch(
            "nous.api.retrieval_pipeline.run_recall_pipeline",
            AsyncMock(return_value=(many, PipelineStats())),
        ):
            out = await tool(code=(
                'rs = recall_deep("q", limit=2)\n'
                'result = json.dumps([len(rs), rs[0]["description"]])'
            ))
        assert out["content"][0]["text"] == '[2, "f0"]'

    @pytest.mark.asyncio
    async def test_rows_cut_by_the_limit_are_not_marked_delivered(
        self, mock_brain, mock_heart
    ):
        """Telemetry must not claim a delivery the script never received.

        A row cut by the script limit reaches the script no more than a row cut
        at a gate does, so it has to leave `returned_to_script` behind.
        """
        from nous.api.retrieval_pipeline import PipelineResult, PipelineStats
        from nous.api.tools import create_programmatic_tools
        from nous.observability.retrieval_trace import SLICED_OFF

        marked: list[tuple] = []

        class _Trace:
            id = "t"
            enabled = True

            def mark_not_delivered(self, item_id, item_type, disp, stage):
                marked.append((item_type, disp, stage))

            def __getattr__(self, _name):
                return lambda *a, **k: None

        class _Rec:
            def start(self, **kw):  # noqa: ARG002
                return _Trace()

            def commit(self, trace):  # noqa: ARG002
                pass

        many = [
            PipelineResult(
                id=uuid4(), type="fact", description=f"f{i}", score=1.0 - i / 100,
                source="heart", metadata={"active": True},
            )
            for i in range(5)
        ]
        s = Settings(programmatic_tools_enabled=True, programmatic_tools_timeout=5)
        tool = create_programmatic_tools(mock_brain, mock_heart, s)["run_python"]
        with patch("nous.api.tools.get_active_retrieval_logger", _Rec), patch(
            "nous.api.retrieval_pipeline.run_recall_pipeline",
            AsyncMock(return_value=(many, PipelineStats())),
        ):
            await tool(code='recall_deep("q", limit=2)')

        assert marked == [("fact", SLICED_OFF, "script_limit")] * 3

    @pytest.mark.asyncio
    async def test_post_pipeline_failure_takes_the_error_path(
        self, mock_brain, mock_heart, patched_pipeline
    ):
        """A failure AFTER the pipeline must not commit a success trace.

        The success trace used to be committed before the legacy backfill and
        the dict construction. The `_schedule` deadline can expire in either, so
        the persisted row would assert `returned_to_script` for every survivor
        while the script actually received a timeout and nothing at all.
        """
        from nous.api.tools import create_programmatic_tools
        from nous.observability.retrieval_trace import (
            RETURNED_TO_SCRIPT,
            SLICED_OFF,
        )

        calls: list[tuple] = []

        class _Trace:
            id = "t"
            enabled = True

            def undeliver_all(self, disposition, stage):
                calls.append((disposition, stage))

            def __getattr__(self, _name):
                return lambda *a, **k: None

        class _Rec:
            def start(self, **kw):  # noqa: ARG002
                return _Trace()

            def commit(self, trace):  # noqa: ARG002
                pass

        # Raises the REAL deadline exception, not a stand-in. An earlier version
        # of this test used RuntimeError and passed against an `except
        # Exception` that could never catch the actual failure:
        # ScriptDeadlineExceeded derives from BaseException on purpose, so agent
        # code cannot swallow its own timeout — and the deadline firing
        # mid-conversion is the likeliest way this block fails.
        from nous.api.tools import ScriptDeadlineExceeded

        class _Boom:
            id = uuid4()
            type = "fact"
            description = "d"
            score = 0.5
            source = "heart"
            edge_relation = None

            @property
            def metadata(self):
                raise ScriptDeadlineExceeded("execution timed out (5s)")

        from nous.api.retrieval_pipeline import PipelineStats

        s = Settings(programmatic_tools_enabled=True, programmatic_tools_timeout=5)
        tool = create_programmatic_tools(mock_brain, mock_heart, s)["run_python"]
        with patch("nous.api.tools.get_active_retrieval_logger", _Rec), patch(
            "nous.api.retrieval_pipeline.run_recall_pipeline",
            AsyncMock(return_value=([_Boom()], PipelineStats())),
        ):
            out = await tool(code='recall_deep("caching")')

        assert out.get("is_error"), "the script should have surfaced the failure"
        assert (SLICED_OFF, "script_recall_failed") in calls
        assert (RETURNED_TO_SCRIPT, "run_python") not in calls, (
            "a post-pipeline failure must not persist a success disposition"
        )

    @pytest.mark.asyncio
    async def test_results_are_returned_to_script_not_rendered(
        self, run_python_tool, patched_pipeline
    ):
        """Script results must not be counted as having reached the model.

        `run_recall_pipeline` calls `finalize`, whose contract is that results
        are authoritative about what REACHED THE MODEL. That holds on the tool
        path and not here: the script decides what it prints or returns, and
        filtering is the whole purpose of run_python. Left as `rendered`, every
        disposition rollup would be inflated by a population the model may never
        have seen.
        """
        from nous.observability.retrieval_trace import RETURNED_TO_SCRIPT

        calls: list[tuple] = []

        class _Trace:
            id = "t"
            enabled = True

            def undeliver_all(self, disposition, stage):
                calls.append((disposition, stage))

            def __getattr__(self, _name):
                return lambda *a, **k: None

        class _Rec:
            def start(self, **kw):  # noqa: ARG002
                return _Trace()

            def commit(self, trace):  # noqa: ARG002
                pass

        with patch("nous.api.tools.get_active_retrieval_logger", _Rec):
            await run_python_tool(code='recall_deep("caching")')

        assert calls == [(RETURNED_TO_SCRIPT, "run_python")]


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


async def _wait_for_idle(timeout: float = 3.0) -> int:
    """Poll until no run_python execution occupies a worker thread."""
    from nous.api.tools import run_python_active_runs

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if run_python_active_runs() == 0:
            break
        await asyncio.sleep(0.02)
    return run_python_active_runs()


class TestRunPythonTimeout:
    """Test timeout enforcement.

    The deadline is enforced by a sys.settrace hook running inside the worker
    thread, so Python-level runaway code is actually interrupted — not merely
    abandoned by the awaiting coroutine.
    """

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

    @pytest.mark.asyncio
    async def test_builtinless_spin_loop_is_stopped(self):
        """`while True: pass` uses zero builtins — it must still be killed.

        Asserts on the observable: the call returns a timeout error, no live
        thread is left behind, and total wall time stays near the deadline.
        """
        from nous.api.tools import create_programmatic_tools, run_python_active_runs

        assert await _wait_for_idle() == 0, "test started with a run in flight"
        baseline_threads = threading.active_count()

        tools = create_programmatic_tools(
            AsyncMock(),
            AsyncMock(),
            Settings(programmatic_tools_enabled=True, programmatic_tools_timeout=1),
        )
        started = time.monotonic()
        result = await tools["run_python"](code="while True: pass")
        elapsed = time.monotonic() - started

        assert result["is_error"] is True
        assert "timed out" in result["content"][0]["text"].lower()
        # The trace hook, not the outer wait_for, ended the run.
        assert elapsed < 1 + 2.0, f"run outlived its deadline ({elapsed:.1f}s)"
        assert run_python_active_runs() == 0
        # Worker thread is gone (executor teardown is async — allow a moment).
        deadline = time.monotonic() + 3.0
        while threading.active_count() > baseline_threads and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        assert threading.active_count() <= baseline_threads

    @pytest.mark.asyncio
    async def test_timeout_not_swallowed_by_except_exception(self):
        """A script catching Exception cannot swallow its own deadline."""
        from nous.api.tools import create_programmatic_tools

        tools = create_programmatic_tools(
            AsyncMock(),
            AsyncMock(),
            Settings(programmatic_tools_enabled=True, programmatic_tools_timeout=1),
        )
        code = "try:\n    while True:\n        pass\nexcept Exception:\n    result = 'swallowed'"
        result = await tools["run_python"](code=code)
        assert result["is_error"] is True
        assert "timed out" in result["content"][0]["text"].lower()
        assert await _wait_for_idle() == 0


# ---------------------------------------------------------------------------
# Concurrency cap tests
# ---------------------------------------------------------------------------


class TestRunPythonConcurrencyCap:
    """Test the max-concurrent-executions cap."""

    @pytest.mark.asyncio
    async def test_rejects_when_saturated(self):
        """A run beyond the cap is rejected instead of stacking threads."""
        from nous.api.tools import create_programmatic_tools, run_python_active_runs

        assert await _wait_for_idle() == 0, "test started with a run in flight"

        tools = create_programmatic_tools(
            AsyncMock(),
            AsyncMock(),
            Settings(
                programmatic_tools_enabled=True,
                programmatic_tools_timeout=5,
                programmatic_tools_max_concurrent=1,
            ),
        )
        # time.sleep releases the GIL, so the event loop stays responsive.
        occupied = asyncio.create_task(
            tools["run_python"](code="import time\ntime.sleep(1)\nresult = 'done'")
        )
        for _ in range(100):
            await asyncio.sleep(0.02)
            if run_python_active_runs() == 1:
                break
        assert run_python_active_runs() == 1

        rejected = await tools["run_python"](code="result = 1")
        assert rejected["is_error"] is True
        assert "concurrent" in rejected["content"][0]["text"].lower()

        assert (await occupied)["content"][0]["text"] == "done"
        assert await _wait_for_idle() == 0

    @pytest.mark.asyncio
    async def test_slot_released_after_run(self, run_python_tool):
        """A completed run frees its slot."""
        from nous.api.tools import run_python_active_runs

        await run_python_tool(code="result = 1")
        assert await _wait_for_idle() == 0
        assert run_python_active_runs() == 0


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


# ---------------------------------------------------------------------------
# P1 bug fix tests (settrace bypass + BaseException handling)
# ---------------------------------------------------------------------------


class TestP1Fixes:
    """Tests for P1 bugs fixed post-PR-575 initial review."""

    async def test_settrace_bypass_blocked(self, run_python_tool):
        """Script calling sys.settrace(None) then spinning must still timeout.

        P1 Fix 1: Without the shim, settrace(None) uninstalls the deadline
        hook and the thread spins forever, holding its concurrency slot.
        """
        code = """
import sys
sys.settrace(None)  # Attempt to bypass deadline tracer
while True:
    pass  # Infinite spin
"""
        result = await run_python_tool(code)
        # Must return timeout error, not hang
        assert result["is_error"] is True
        assert "timed out" in result["content"][0]["text"]

        # Slot must be released after timeout
        from nous.api.tools import run_python_active_runs
        await asyncio.sleep(0.5)  # Grace for thread cleanup
        assert run_python_active_runs() == 0

    async def test_system_exit_is_error(self, run_python_tool):
        """Script calling sys.exit() must return is_error, not crash.

        P1 Fix 2: SystemExit is a BaseException, so it escapes `except Exception`.
        Without the BaseException catch, it would propagate through
        ToolDispatcher.dispatch and crash the API process.
        """
        code = """
import sys
sys.exit(1)
"""
        result = await run_python_tool(code)
        assert result["is_error"] is True
        assert "SystemExit" in result["content"][0]["text"]

    async def test_keyboard_interrupt_is_error(self, run_python_tool):
        """Script raising KeyboardInterrupt must return is_error, not crash.

        P1 Fix 2: KeyboardInterrupt is also a BaseException.
        """
        code = """
raise KeyboardInterrupt("user interrupted")
"""
        result = await run_python_tool(code)
        assert result["is_error"] is True
        assert "KeyboardInterrupt" in result["content"][0]["text"]
