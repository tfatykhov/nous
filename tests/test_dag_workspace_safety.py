"""Tests for F064.3 — DAG workspace safety invariants.

Covers plan §6.5 acceptance criteria:
- Insert-time sanitize gate (strict, raises) when flag enabled
- Read-time transformation (lenient) for legacy unsafe names
- Unconditional containment assertion (security boundary)
- Subprocess cwd is the workspace path
"""

from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from nous.config import Settings
from nous.dag._workspace import (
    assert_inside_root,
    compute_workspace_path,
    sanitize_segment,
)
from nous.dag.orchestrator import DAGOrchestrator
from nous.dag.schemas import DAGCreateRequest, DAGNodeSpec, DAGNodeType
from nous.dag.store import DAGStore


# ---------------------------------------------------------------------------
# sanitize_segment (strict, insert-time)
# ---------------------------------------------------------------------------


class TestSanitizeSegment:
    def test_safe_alphanumeric_passes(self):
        assert sanitize_segment("research-step-1") == "research-step-1"

    def test_dot_separators_pass(self):
        assert sanitize_segment("step.v2.final") == "step.v2.final"

    def test_underscore_passes(self):
        assert sanitize_segment("step_with_underscore") == "step_with_underscore"

    def test_dotdot_rejected(self):
        with pytest.raises(ValueError, match="invalid path segment"):
            sanitize_segment("..")

    def test_single_dot_rejected(self):
        with pytest.raises(ValueError, match="invalid path segment"):
            sanitize_segment(".")

    def test_empty_rejected(self):
        with pytest.raises(ValueError, match="invalid path segment"):
            sanitize_segment("")

    def test_space_rejected(self):
        with pytest.raises(ValueError, match="A-Za-z0-9._-"):
            sanitize_segment("step with spaces")

    def test_slash_rejected(self):
        with pytest.raises(ValueError, match="A-Za-z0-9._-"):
            sanitize_segment("path/escape")

    def test_unicode_rejected(self):
        with pytest.raises(ValueError, match="A-Za-z0-9._-"):
            sanitize_segment("α-step")

    def test_traversal_pattern_rejected(self):
        with pytest.raises(ValueError, match="A-Za-z0-9._-"):
            sanitize_segment("../escape")


# ---------------------------------------------------------------------------
# compute_workspace_path (lenient transformation for legacy reads)
# ---------------------------------------------------------------------------


class TestComputeWorkspacePath:
    def test_safe_name_round_trips(self, tmp_path: Path):
        dag_id = uuid.uuid4()
        result = compute_workspace_path(dag_id, "safe-step", tmp_path)
        assert result == tmp_path / dag_id.hex[:8] / "safe-step"

    def test_legacy_spaces_transformed_not_rejected(self, tmp_path: Path):
        """Read-time path must keep working for pre-flag rows with unsafe names.
        Hash suffix is appended for collision protection (codex P1 on 37fc50f)."""
        dag_id = uuid.uuid4()
        result = compute_workspace_path(dag_id, "step with spaces", tmp_path)
        assert result.parent == tmp_path / dag_id.hex[:8]
        assert result.name.startswith("step_with_spaces-")
        # Hash suffix is 6 hex chars
        suffix = result.name.split("-")[-1]
        assert len(suffix) == 6
        assert all(c in "0123456789abcdef" for c in suffix)

    def test_legacy_unicode_transformed(self, tmp_path: Path):
        dag_id = uuid.uuid4()
        result = compute_workspace_path(dag_id, "α-step-β", tmp_path)
        # Transformation yields "_-step-_" but hash suffix guarantees uniqueness
        assert result.name.startswith("_-step-_-")

    def test_distinct_legacy_names_get_distinct_paths(self, tmp_path: Path):
        """@codex P1 on 37fc50f: 'step a' and 'step_a' must NOT collide.
        The hash suffix disambiguates them when sanitization collapses them
        to the same alphanumeric form."""
        dag_id = uuid.uuid4()
        path_a = compute_workspace_path(dag_id, "step a", tmp_path)
        path_b = compute_workspace_path(dag_id, "step_a", tmp_path)
        # Different inputs → different paths
        assert path_a != path_b
        # Sanitized stems match but suffixes differ
        assert path_a.name.startswith("step_a-")  # "step a" → "step_a" + hash
        # "step_a" passes through unchanged (already safe), no suffix
        assert path_b.name == "step_a"

    def test_same_legacy_name_resolves_deterministically(self, tmp_path: Path):
        """Repeated reads of the same legacy name must hit the same path."""
        dag_id = uuid.uuid4()
        a = compute_workspace_path(dag_id, "step with spaces", tmp_path)
        b = compute_workspace_path(dag_id, "step with spaces", tmp_path)
        assert a == b

    def test_reserved_dotdot_still_rejected_at_read(self, tmp_path: Path):
        """Even at read time, '..' has no safe transformation — must reject."""
        with pytest.raises(ValueError, match="invalid path segment"):
            compute_workspace_path(uuid.uuid4(), "..", tmp_path)


# ---------------------------------------------------------------------------
# assert_inside_root (unconditional security boundary)
# ---------------------------------------------------------------------------


class TestAssertInsideRoot:
    def test_path_inside_root_passes(self, tmp_path: Path):
        target = tmp_path / "foo" / "bar"
        target.mkdir(parents=True)
        assert_inside_root(target, tmp_path)  # no raise

    def test_path_equal_to_root_passes(self, tmp_path: Path):
        assert_inside_root(tmp_path, tmp_path)  # no raise

    def test_path_outside_root_rejected(self, tmp_path: Path):
        outside = tmp_path.parent
        with pytest.raises(ValueError, match="escapes root"):
            assert_inside_root(outside, tmp_path)

    @pytest.mark.skipif(
        os.name == "nt", reason="symlinks require admin on Windows in CI"
    )
    def test_symlink_escape_rejected(self, tmp_path: Path):
        """A symlink pointing outside the root must be caught via resolve()."""
        external = tmp_path.parent / f"outside-{uuid.uuid4().hex[:8]}"
        external.mkdir()
        link = tmp_path / "trap"
        try:
            link.symlink_to(external)
            with pytest.raises(ValueError, match="escapes root"):
                assert_inside_root(link, tmp_path)
        finally:
            if link.exists() or link.is_symlink():
                link.unlink()
            shutil.rmtree(external, ignore_errors=True)


# ---------------------------------------------------------------------------
# Insert-time sanitize gate (DAGNodeSpec.model_validator)
# ---------------------------------------------------------------------------


class TestInsertTimeSanitizeGate:
    def test_safe_name_passes_when_flag_on(self, monkeypatch):
        """With NOUS_DAG_WORKSPACE_SAFETY_ENABLED=true a safe name validates."""
        monkeypatch.setenv("NOUS_DAG_WORKSPACE_SAFETY_ENABLED", "true")
        # No exception
        DAGCreateRequest(
            name="safe-dag",
            nodes=[DAGNodeSpec(name="step-1", type=DAGNodeType.subtask)],
        )

    def test_unsafe_name_rejected_when_flag_on(self, monkeypatch):
        monkeypatch.setenv("NOUS_DAG_WORKSPACE_SAFETY_ENABLED", "true")
        with pytest.raises(ValueError, match="A-Za-z0-9._-"):
            DAGCreateRequest(
                name="bad-dag",
                nodes=[DAGNodeSpec(name="step with spaces", type=DAGNodeType.subtask)],
            )

    def test_dotdot_name_rejected_when_flag_on(self, monkeypatch):
        monkeypatch.setenv("NOUS_DAG_WORKSPACE_SAFETY_ENABLED", "true")
        with pytest.raises(ValueError):
            DAGCreateRequest(
                name="bad-dag",
                nodes=[DAGNodeSpec(name="..", type=DAGNodeType.subtask)],
            )

    def test_unsafe_name_allowed_when_flag_off(self, monkeypatch):
        """Backward compat: unsafe names accepted when flag disabled. Existing
        rows from before flag-flip must continue to insert cleanly."""
        monkeypatch.setenv("NOUS_DAG_WORKSPACE_SAFETY_ENABLED", "false")
        # No exception — the model_validator skips the sanitize call
        DAGCreateRequest(
            name="legacy-dag",
            nodes=[DAGNodeSpec(name="step with spaces", type=DAGNodeType.subtask)],
        )


# ---------------------------------------------------------------------------
# Orchestrator integration — _read_node_result containment
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def store(db):
    return DAGStore(db, f"test-ws-{uuid.uuid4().hex[:8]}", Settings())


@pytest.fixture
def subtask_mgr():
    mgr = AsyncMock()
    mgr.create.return_value = SimpleNamespace(id=uuid.uuid4(), status="pending")
    return mgr


@pytest.fixture
def dynamic_loader():
    loader = AsyncMock()
    loader._registry = MagicMock()
    loader._registry.get_check.return_value = None
    return loader


class TestOrchestratorReadContainment:
    @pytest.mark.asyncio
    async def test_read_falls_back_to_node_result_when_workspace_invalid(
        self, store, subtask_mgr, dynamic_loader, tmp_path: Path, db
    ):
        """A node row whose name resolves outside the workspace root — the
        read path returns node.result rather than raising or accessing a
        bad path. Logged at WARNING level."""
        settings = Settings(dag_workspace_root=tmp_path)
        # Fresh store with the override settings + fresh orchestrator
        s = DAGStore(db, store._agent_id, settings)
        orch = DAGOrchestrator(
            store=s, subtask_mgr=subtask_mgr,
            dynamic_loader=dynamic_loader, settings=settings,
        )
        # Build a DAG row with a known-good name first.
        req = DAGCreateRequest(
            name="ws-test",
            nodes=[DAGNodeSpec(name="step", type=DAGNodeType.subtask)],
        )
        dag = await s.create(req)
        node = dag.nodes[0]
        node.result = "fallback-result"
        # Read with a malicious name swap (simulates a row that escaped sanitization)
        node.name = ".."  # raises in compute_workspace_path → fallback to node.result
        result = await orch._read_node_result(node, dag)
        assert result == "fallback-result"

    @pytest.mark.asyncio
    async def test_read_uses_safe_path_for_legacy_unsafe_name(
        self, store, subtask_mgr, dynamic_loader, tmp_path: Path, db
    ):
        """Legacy node with name 'step with spaces' should read from the
        sanitized + hash-suffixed path (lenient transformation + codex P1
        collision-protection)."""
        settings = Settings(dag_workspace_root=tmp_path)
        s = DAGStore(db, store._agent_id, settings)
        orch = DAGOrchestrator(
            store=s, subtask_mgr=subtask_mgr,
            dynamic_loader=dynamic_loader, settings=settings,
        )
        req = DAGCreateRequest(
            name="legacy-name-dag",
            nodes=[DAGNodeSpec(name="step", type=DAGNodeType.subtask)],
        )
        dag = await s.create(req)
        node = dag.nodes[0]
        # Mutate in-memory to simulate a legacy row
        node.name = "step with spaces"
        # Write a result file at the SANITIZED + HASH-SUFFIXED path
        expected_path = compute_workspace_path(dag.id, node.name, tmp_path)
        expected_path.mkdir(parents=True)
        (expected_path / "result").write_text("legacy-content")
        result = await orch._read_node_result(node, dag)
        assert result == "legacy-content"

    @pytest.mark.asyncio
    async def test_completion_check_fails_when_workspace_escapes_root(
        self, store, subtask_mgr, dynamic_loader, tmp_path: Path, db
    ):
        """@codex P2 on 37fc50f: when workspace containment fails, the
        completion check must FAIL — not fall through to cwd=None
        (orchestrator process cwd). That fallthrough would defeat the
        security boundary in the exact case it's meant to protect.
        """
        from nous.dag.orchestrator import CheckResult

        # Use a root that *can't* be escaped, then mutate node.name to '..'
        # which forces compute_workspace_path to raise ValueError.
        settings = Settings(dag_workspace_root=tmp_path)
        s = DAGStore(db, store._agent_id, settings)
        orch = DAGOrchestrator(
            store=s, subtask_mgr=subtask_mgr,
            dynamic_loader=dynamic_loader, settings=settings,
        )
        req = DAGCreateRequest(
            name="check-fail-dag",
            nodes=[
                DAGNodeSpec(
                    name="step",
                    type=DAGNodeType.subtask,
                    completion_check="echo ok",
                ),
            ],
        )
        dag = await s.create(req)
        node = dag.nodes[0]
        node.name = ".."  # forces ValueError in compute_workspace_path
        result = await orch._run_completion_check(node)
        assert isinstance(result, CheckResult)
        assert result.status == "failed"
        assert "workspace" in result.detail.lower()
