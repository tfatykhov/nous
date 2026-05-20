"""F062 Commit B: tests for the conditional schema builders.

Covers the three places F062 has to conditionally add a property based
on settings.subtask_payload_schema_enabled:

1. _build_spawn_task_schema(flag) — does spawn_task expose payload_schema?
2. build_submit_final_report_schema(flag) — does submit_final_report accept payload?
3. build_subtask_prefix(payload_schema=...) — does the system prompt instruct
   the model to populate payload against the schema?

These are pure unit tests against in-process dict shapes; no DB, no runner.
"""

from __future__ import annotations

import json

from nous.api.subtask_tools import (
    SUBMIT_FINAL_REPORT_SCHEMA,
    build_submit_final_report_schema,
)
from nous.api.tools import _SPAWN_TASK_SCHEMA, _build_spawn_task_schema, build_subtask_prefix


class TestBuildSpawnTaskSchema:
    def test_flag_off_omits_payload_schema_property(self) -> None:
        schema = _build_spawn_task_schema(payload_schema_enabled=False)
        assert "payload_schema" not in schema["properties"]
        # All F061 / pre-F062 properties still present
        for required in ("task", "frame_type", "await_result", "output_format"):
            assert required in schema["properties"]

    def test_flag_on_adds_payload_schema_property(self) -> None:
        schema = _build_spawn_task_schema(payload_schema_enabled=True)
        assert "payload_schema" in schema["properties"]
        assert schema["properties"]["payload_schema"]["type"] == "object"
        # task is still required; payload_schema is NOT
        assert "task" in schema["required"]
        assert "payload_schema" not in schema["required"]

    def test_module_constant_never_mutated(self) -> None:
        """Builder must not leak modifications back to the constant."""
        # Stash a snapshot before
        before = json.dumps(_SPAWN_TASK_SCHEMA, sort_keys=True)
        _build_spawn_task_schema(payload_schema_enabled=True)
        _build_spawn_task_schema(payload_schema_enabled=False)
        after = json.dumps(_SPAWN_TASK_SCHEMA, sort_keys=True)
        assert before == after


class TestBuildSubmitFinalReportSchema:
    def test_flag_off_byte_identical_to_legacy(self) -> None:
        legacy = SUBMIT_FINAL_REPORT_SCHEMA
        built = build_submit_final_report_schema(payload_property_enabled=False)
        assert built == legacy
        # additionalProperties:False is preserved — model that emits stray
        # `payload` key with the flag off gets rejected, fail-closed.
        assert built["input_schema"]["additionalProperties"] is False

    def test_flag_on_adds_payload_property(self) -> None:
        built = build_submit_final_report_schema(payload_property_enabled=True)
        props = built["input_schema"]["properties"]
        assert "payload" in props
        # Permissive JSON-typed list — accepts every JSON value.
        assert set(props["payload"]["type"]) == {
            "object", "array", "string", "number", "boolean", "null",
        }
        # additionalProperties stays False — only the explicit `payload`
        # property is opened up.
        assert built["input_schema"]["additionalProperties"] is False

    def test_constant_not_mutated(self) -> None:
        before = json.dumps(SUBMIT_FINAL_REPORT_SCHEMA, sort_keys=True)
        build_submit_final_report_schema(payload_property_enabled=True)
        build_submit_final_report_schema(payload_property_enabled=False)
        after = json.dumps(SUBMIT_FINAL_REPORT_SCHEMA, sort_keys=True)
        assert before == after


class TestBuildSubtaskPrefixSchemaInjection:
    def test_legacy_path_unchanged_when_hardening_off(self) -> None:
        out = build_subtask_prefix(
            task="x",
            frame_type="research",
            payload_schema={"type": "object"},
            hardening_enabled=False,
        )
        # Legacy path explicitly returns the F060- prompt; F062 block must
        # not leak in (the legacy path doesn't register submit_final_report
        # at all).
        assert "Result schema (REQUIRED)" not in out
        assert "payload" not in out

    def test_hardened_no_schema_omits_block(self) -> None:
        out = build_subtask_prefix(
            task="x",
            frame_type="research",
            hardening_enabled=True,
            payload_schema=None,
        )
        assert "Result schema (REQUIRED)" not in out
        # The F061 brief is still there.
        assert "# Objective" in out
        assert "submit_final_report" in out

    def test_hardened_with_schema_injects_block(self) -> None:
        schema = {
            "type": "object",
            "required": ["name"],
            "properties": {"name": {"type": "string"}},
        }
        out = build_subtask_prefix(
            task="x",
            frame_type="research",
            hardening_enabled=True,
            payload_schema=schema,
        )
        assert "Result schema (REQUIRED)" in out
        # Compact JSON serialization — no whitespace inside the schema block.
        assert '"type":"object"' in out
        assert "<schema>" in out and "</schema>" in out
        # The F061 sections are still present.
        assert "# Objective" in out
        assert "# Termination" in out
