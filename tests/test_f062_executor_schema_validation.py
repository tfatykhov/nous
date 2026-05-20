"""F062 Commit B: tests for the schema-validation extension inside
execute_hardened.

These exercise the post-structural-validation jsonschema check that
F062 wires in. The status-mirrors-final_outcome invariant is asserted
on every path — schema validation can only flip an in-flight
``completed`` to ``validation_failed``; it must never overwrite
``errored``/``timed_out``/``cancelled``/``incomplete_*``.

Test fixtures here mirror tests/test_f061_subtask_executor.py — same
SimpleNamespace + scripted-runner pattern, no live DB needed.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from nous.handlers.subtask_executor import execute_hardened


def _make_subtask(*, payload_schema: dict | None = None, **overrides):
    base = dict(
        id=uuid.uuid4(),
        task="research X",
        frame_type="research",
        model=None,
        output_format=None,
        success_criteria=None,
        payload_schema=payload_schema,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _make_settings(*, payload_schema_enabled: bool = True, max_attempts: int = 2):
    return SimpleNamespace(
        agent_id="agent-test",
        background_model="claude-haiku-4-5-20251001",
        subtask_max_attempts=max_attempts,
        subtask_report_min_summary_chars=50,
        subtask_tool_call_limit=20,
        subtask_force_tool_on_penultimate=True,
        subtask_payload_schema_enabled=payload_schema_enabled,
    )


def _make_heart_mock():
    h = MagicMock()
    h.subtasks = MagicMock()
    h.subtasks.complete = AsyncMock()
    h.subtasks.fail = AsyncMock()
    return h


def _scripted_runner(*, scripted_payloads, scripted_usages=None):
    runner = MagicMock()
    call_idx = {"i": 0}
    if scripted_usages is None:
        scripted_usages = [
            {"input_tokens": 100, "output_tokens": 50, "tool_calls": 1}
            for _ in scripted_payloads
        ]

    async def _run_turn(**kwargs):
        i = call_idx["i"]
        call_idx["i"] += 1
        extra = kwargs.get("extra_tools") or {}
        if "submit_final_report" in extra:
            _schema, executor = extra["submit_final_report"]
            payload = scripted_payloads[i] if i < len(scripted_payloads) else None
            if payload is not None:
                await executor(**payload)
        usage = (
            scripted_usages[i]
            if i < len(scripted_usages)
            else {"input_tokens": 0, "output_tokens": 0, "tool_calls": 0}
        )
        return (
            "Report submitted."
            if extra and i < len(scripted_payloads) and scripted_payloads[i] is not None
            else "ran",
            MagicMock(),
            usage,
        )

    runner.run_turn = AsyncMock(side_effect=_run_turn)
    return runner


# ---------------------------------------------------------------------------


_SCHEMA = {
    "type": "object",
    "required": ["name", "score"],
    "properties": {
        "name": {"type": "string"},
        "score": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "additionalProperties": False,
}


@pytest.mark.asyncio
async def test_schema_valid_payload_completes() -> None:
    runner = _scripted_runner(scripted_payloads=[{
        "summary": "Returned a valid named entity with a high confidence score.",
        "confidence": 0.9,
        "payload": {"name": "Alice", "score": 0.92},
    }])
    heart = _make_heart_mock()
    settings = _make_settings()
    subtask = _make_subtask(payload_schema=_SCHEMA)

    _final, result = await execute_hardened(
        subtask, "sess-1",
        runner=runner, heart=heart, settings=settings,
    )
    assert result.ok is True
    assert result.outcome == "completed"

    heart.subtasks.complete.assert_awaited_once()
    kwargs = heart.subtasks.complete.await_args.kwargs
    assert kwargs["final_outcome"] == "completed"
    assert kwargs["payload_schema_valid"] is True
    assert kwargs["report_jsonb"]["payload"] == {"name": "Alice", "score": 0.92}


@pytest.mark.asyncio
async def test_schema_mismatch_persists_validation_failed_after_retry_exhaustion() -> None:
    """Both attempts emit a payload that violates the schema.

    Expected: F061's retry loop runs twice (max_attempts=2), and on the
    second failure execute_hardened ends with last_result.outcome ==
    'validation_failed' and payload_schema_valid=False.
    """
    bad = {
        "summary": "Wrote a name field but with a non-string value, definitely off-schema.",
        "confidence": 0.6,
        "payload": {"name": 42, "score": 0.5},  # name must be string
    }
    runner = _scripted_runner(scripted_payloads=[bad, bad])
    heart = _make_heart_mock()
    settings = _make_settings(max_attempts=2)
    subtask = _make_subtask(payload_schema=_SCHEMA)

    _final, result = await execute_hardened(
        subtask, "sess-1",
        runner=runner, heart=heart, settings=settings,
    )
    assert result.ok is False
    assert result.outcome == "validation_failed"
    assert "payload schema mismatch" in result.reason

    heart.subtasks.fail.assert_awaited_once()
    kwargs = heart.subtasks.fail.await_args.kwargs
    assert kwargs["final_outcome"] == "validation_failed"
    assert kwargs["payload_schema_valid"] is False


@pytest.mark.asyncio
async def test_schema_mismatch_then_valid_retry_succeeds() -> None:
    """Attempt 1 violates schema; attempt 2 passes it."""
    bad = {
        "summary": "First attempt sneaks in an extra unknown field that violates the schema.",
        "confidence": 0.5,
        "payload": {"name": "Bob", "score": 0.4, "extra": "nope"},  # additionalProperties=False
    }
    good = {
        "summary": "Second attempt returns a clean object with only the required keys.",
        "confidence": 0.85,
        "payload": {"name": "Bob", "score": 0.4},
    }
    runner = _scripted_runner(scripted_payloads=[bad, good])
    heart = _make_heart_mock()
    settings = _make_settings(max_attempts=2)
    subtask = _make_subtask(payload_schema=_SCHEMA)

    _final, result = await execute_hardened(
        subtask, "sess-1",
        runner=runner, heart=heart, settings=settings,
    )
    assert result.ok is True
    assert result.outcome == "completed"

    heart.subtasks.complete.assert_awaited_once()
    kwargs = heart.subtasks.complete.await_args.kwargs
    assert kwargs["payload_schema_valid"] is True
    assert kwargs["attempts"] == 2


@pytest.mark.asyncio
async def test_no_payload_schema_leaves_validation_flag_null() -> None:
    """Subtask without a payload_schema must not set payload_schema_valid."""
    runner = _scripted_runner(scripted_payloads=[{
        "summary": "Standard F061 happy-path report with no schema required.",
        "confidence": 0.8,
    }])
    heart = _make_heart_mock()
    settings = _make_settings()  # flag on but no schema on the row
    subtask = _make_subtask(payload_schema=None)

    _final, result = await execute_hardened(
        subtask, "sess-1",
        runner=runner, heart=heart, settings=settings,
    )
    assert result.outcome == "completed"
    kwargs = heart.subtasks.complete.await_args.kwargs
    assert kwargs["payload_schema_valid"] is None


@pytest.mark.asyncio
async def test_flag_off_skips_schema_check_even_with_row_schema() -> None:
    """Operator flipped flag off; existing row still has payload_schema.

    Expected: schema validation is NOT run; an off-schema payload must
    still complete normally (and payload_schema_valid remains None).
    """
    bad_against_schema = {
        "summary": "Payload that would fail validation if schema check ran — but the flag is off.",
        "confidence": 0.7,
        # No 'payload' field at all — but flag-off means F062 doesn't even
        # look for it. submit_final_report's schema in this branch is the
        # legacy F061 one (no `payload` property), so this is well-formed.
    }
    runner = _scripted_runner(scripted_payloads=[bad_against_schema])
    heart = _make_heart_mock()
    settings = _make_settings(payload_schema_enabled=False)
    subtask = _make_subtask(payload_schema=_SCHEMA)

    _final, result = await execute_hardened(
        subtask, "sess-1",
        runner=runner, heart=heart, settings=settings,
    )
    assert result.outcome == "completed"
    kwargs = heart.subtasks.complete.await_args.kwargs
    assert kwargs["payload_schema_valid"] is None


@pytest.mark.asyncio
async def test_schema_mismatch_does_not_overwrite_errored_outcome() -> None:
    """status-mirrors-final_outcome invariant.

    If the runner raises an exception (final_outcome would be 'errored'),
    the F062 schema check must NOT run — there's no payload to validate.
    Persisted outcome must be 'errored', not 'validation_failed'.
    """
    runner = MagicMock()
    runner.run_turn = AsyncMock(side_effect=RuntimeError("downstream blew up"))
    heart = _make_heart_mock()
    settings = _make_settings()
    subtask = _make_subtask(payload_schema=_SCHEMA)

    _final, result = await execute_hardened(
        subtask, "sess-1",
        runner=runner, heart=heart, settings=settings,
    )
    assert result.outcome == "errored"
    heart.subtasks.fail.assert_awaited_once()
    kwargs = heart.subtasks.fail.await_args.kwargs
    assert kwargs["final_outcome"] == "errored"
    # Schema check never ran → flag stays None
    assert kwargs["payload_schema_valid"] is None


@pytest.mark.asyncio
async def test_schema_check_accepts_scalar_payload() -> None:
    """Schema permitting a number must round-trip a scalar payload."""
    scalar_schema = {"type": "number", "minimum": 0}
    runner = _scripted_runner(scripted_payloads=[{
        "summary": "Computed the answer as a single non-negative number response.",
        "confidence": 0.95,
        "payload": 42,
    }])
    heart = _make_heart_mock()
    settings = _make_settings()
    subtask = _make_subtask(payload_schema=scalar_schema)

    _final, result = await execute_hardened(
        subtask, "sess-1",
        runner=runner, heart=heart, settings=settings,
    )
    assert result.outcome == "completed"
    kwargs = heart.subtasks.complete.await_args.kwargs
    assert kwargs["payload_schema_valid"] is True
    assert kwargs["report_jsonb"]["payload"] == 42


@pytest.mark.asyncio
async def test_schema_check_accepts_string_payload() -> None:
    """Codex round-4 P1: validate the raw Python value directly. A string
    payload for a {"type": "string"} schema must NOT be json.loads'd into
    a JSONDecodeError; the value "ok" is a valid schema-typed string.
    """
    string_schema = {"type": "string", "minLength": 1}
    runner = _scripted_runner(scripted_payloads=[{
        "summary": "Returned the requested answer as a single short string.",
        "confidence": 0.9,
        "payload": "ok",
    }])
    heart = _make_heart_mock()
    settings = _make_settings()
    subtask = _make_subtask(payload_schema=string_schema)

    _final, result = await execute_hardened(
        subtask, "sess-1",
        runner=runner, heart=heart, settings=settings,
    )
    assert result.outcome == "completed"
    kwargs = heart.subtasks.complete.await_args.kwargs
    assert kwargs["payload_schema_valid"] is True
    assert kwargs["report_jsonb"]["payload"] == "ok"


@pytest.mark.asyncio
async def test_malformed_caller_schema_maps_to_validation_failed() -> None:
    """Codex round-6 P2: jsonschema.validate raises SchemaError when the
    *caller's* payload_schema is malformed. The executor must map that to
    validation_failed (a deterministic F062 outcome) instead of letting
    the exception escape into the generic errored path.
    """
    # `minimum: "not-a-number"` is invalid JSON-Schema → SchemaError on use.
    bad_schema = {"type": "number", "minimum": "not-a-number"}
    runner = _scripted_runner(scripted_payloads=[
        {
            "summary": "Submitted a number payload as the caller asked.",
            "confidence": 0.9,
            "payload": 42,
        },
        {
            "summary": "Same submission a second time — same broken caller schema.",
            "confidence": 0.9,
            "payload": 42,
        },
    ])
    heart = _make_heart_mock()
    settings = _make_settings(max_attempts=2)
    subtask = _make_subtask(payload_schema=bad_schema)

    _final, result = await execute_hardened(
        subtask, "sess-1",
        runner=runner, heart=heart, settings=settings,
    )
    assert result.outcome == "validation_failed"
    assert "payload_schema is malformed" in result.reason
    kwargs = heart.subtasks.fail.await_args.kwargs
    assert kwargs["final_outcome"] == "validation_failed"
    assert kwargs["payload_schema_valid"] is False


@pytest.mark.asyncio
async def test_schema_mismatch_then_api_error_resets_valid_flag() -> None:
    """Codex round-6 P2: payload_schema_valid must be reset at the start
    of each attempt. Otherwise attempt-1 False would leak into attempt-2
    runtime-error outcome and persist contradictory state
    (final_outcome='errored' AND payload_schema_valid=False).
    """
    bad_payload = {
        "summary": "First attempt sneaks an extra field that violates the schema.",
        "confidence": 0.6,
        "payload": {"name": 123, "score": 0.5},  # name must be string
    }
    runner = MagicMock()
    call_idx = {"i": 0}

    async def _run_turn(**kwargs):
        i = call_idx["i"]
        call_idx["i"] += 1
        if i == 0:
            # Attempt 1: submit the failing payload via the collector
            extra = kwargs.get("extra_tools") or {}
            _, executor = extra["submit_final_report"]
            await executor(**bad_payload)
            return (
                "Report submitted.",
                MagicMock(),
                {"input_tokens": 100, "output_tokens": 50, "tool_calls": 1},
            )
        # Attempt 2: raise a runtime error before the model can call the tool
        raise RuntimeError("upstream API blew up on retry")

    runner.run_turn = AsyncMock(side_effect=_run_turn)

    heart = _make_heart_mock()
    settings = _make_settings(max_attempts=2)
    subtask = _make_subtask(payload_schema=_SCHEMA)

    _final, result = await execute_hardened(
        subtask, "sess-1",
        runner=runner, heart=heart, settings=settings,
    )
    assert result.outcome == "errored"
    kwargs = heart.subtasks.fail.await_args.kwargs
    assert kwargs["final_outcome"] == "errored"
    # Crucial assertion: must NOT carry payload_schema_valid=False from the
    # earlier attempt's schema mismatch.
    assert kwargs["payload_schema_valid"] is None


@pytest.mark.asyncio
async def test_payload_rejected_when_flag_off_even_without_payload_schema() -> None:
    """Codex round-11/12 P1 (L55): fail-closed gate restored — when the
    F062 flag is off, validate_report must reject submissions that carry
    a `payload` key, mirroring F061's pre-F062 extra='forbid' behavior.

    Pydantic accepts the field unconditionally (transport layer); the
    structural validator at heart.subtask_validator.validate_report is
    where the gate lives now.
    """
    runner = _scripted_runner(scripted_payloads=[
        {
            "summary": "Submitted a perfectly fine report — except it includes a payload field while F062 is off.",
            "confidence": 0.9,
            "payload": {"name": "Alice", "score": 0.9},
        },
        {
            "summary": "Second attempt still tries to send a payload — should also fail-closed because F062 is off.",
            "confidence": 0.9,
            "payload": {"name": "Alice", "score": 0.9},
        },
    ])
    heart = _make_heart_mock()
    settings = _make_settings(payload_schema_enabled=False)
    subtask = _make_subtask(payload_schema=None)

    _final, result = await execute_hardened(
        subtask, "sess-1",
        runner=runner, heart=heart, settings=settings,
    )
    assert result.outcome == "validation_failed"
    assert "payload" in result.reason


@pytest.mark.asyncio
async def test_missing_payload_key_fails_validation_even_for_null_schema() -> None:
    """Codex round-7 P1: when payload_schema is supplied, the report MUST
    include an explicit `payload` field. Omitting it must NOT be treated
    as schema-valid even when the schema allows null — otherwise schemas
    like {"type":"null"} silently accept missing payload, and spawn_sync
    returns {} for a contract that promised a validated null value.
    """
    null_schema = {"type": "null"}
    # Both attempts omit `payload` entirely — summaries >= 50 chars so the
    # structural validator passes, exposing the missing-payload guard.
    runner = _scripted_runner(scripted_payloads=[
        {
            "summary": "Did the task but forgot to include the payload field on this attempt; will need to retry to deliver schema-typed output.",
            "confidence": 0.7,
        },
        {
            "summary": "Retried but again forgot to populate the payload field; the schema-validated value was never emitted by the model on either attempt.",
            "confidence": 0.7,
        },
    ])
    heart = _make_heart_mock()
    settings = _make_settings(max_attempts=2)
    subtask = _make_subtask(payload_schema=null_schema)

    _final, result = await execute_hardened(
        subtask, "sess-1",
        runner=runner, heart=heart, settings=settings,
    )
    assert result.outcome == "validation_failed"
    assert "payload field missing" in result.reason
    kwargs = heart.subtasks.fail.await_args.kwargs
    assert kwargs["final_outcome"] == "validation_failed"
    assert kwargs["payload_schema_valid"] is False


@pytest.mark.asyncio
async def test_schema_check_accepts_null_payload() -> None:
    """Schema {"type": "null"} must accept payload=None."""
    null_schema = {"type": "null"}
    runner = _scripted_runner(scripted_payloads=[{
        "summary": "Returned a null payload because the operation is intentionally void.",
        "confidence": 0.9,
        "payload": None,
    }])
    heart = _make_heart_mock()
    settings = _make_settings()
    subtask = _make_subtask(payload_schema=null_schema)

    _final, result = await execute_hardened(
        subtask, "sess-1",
        runner=runner, heart=heart, settings=settings,
    )
    assert result.outcome == "completed"
    kwargs = heart.subtasks.complete.await_args.kwargs
    assert kwargs["payload_schema_valid"] is True
