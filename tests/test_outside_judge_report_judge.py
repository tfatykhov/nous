"""F118 outside judge, finished task reports: the fixed report instructions, the report judge and the record it reads
(no database, no real model).

The report instructions are a second fixed text under their own version string, so the decision instructions and
their version do not move. The report judge is the built-in judge with that text: one call labelled ``outside_judge``,
no ``temperature`` and no ``thinking``, its answer checked against the report verdicts and labels. The record of a
report keeps the finish check's labels as evidence, outside what the author wrote, and bounds every string.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

import nous.handlers
from nous.config import Settings
from nous.judge import evidence, instructions, model_judge
from nous.judge.slot import Claim, JudgeInput
from nous.observability.model_calls import CallContext

SUBJECT_ID = uuid.UUID("00000000-0000-4000-8000-0000000b0118")


@pytest.fixture(autouse=True)
def _no_model_remembered(monkeypatch):
    monkeypatch.setattr(nous.handlers, "_REJECTS_FORCED_TOOL_CHOICE", set())


class _Client:
    supports_call_context = True

    def __init__(self, answer: Any = None, *, usage: dict[str, int] | None = None) -> None:
        self.answer = {"verdict": "not_supported", "confidence": 0.8, "labels": ["error_in_evidence"]}
        if answer is not None:
            self.answer = answer
        self.usage = usage
        self.calls: list[tuple[dict[str, Any], CallContext | None]] = []

    async def call(self, payload: dict[str, Any], call_context: CallContext | None = None) -> Any:
        self.calls.append((payload, call_context))
        block = {"type": "tool_use", "id": "t1", "name": payload["tools"][0]["name"], "input": self.answer}
        reply = SimpleNamespace(content=[block], stop_reason="tool_use")
        if self.usage is not None:
            reply.usage = self.usage
        return reply


def _report_record(items: list[dict[str, Any]] | None = None, **subject: Any) -> JudgeInput:
    return evidence.assemble(
        "task_report",
        SUBJECT_ID,
        {
            "task": "Fix the broken link on the pricing page and deploy the site",
            "success_criteria": "The live page links to the new plan",
            "output_format": None,
            "report_findings": "Changed the link in pricing.html",
            "stated_confidence": 0.9,
            **subject,
        },
        Claim(grader="agent", grade="completed", note="Fixed the link and deployed."),
        items
        if items is not None
        else [
            {
                "source": "tool_call",
                "tool": "bash",
                "status": "error",
                "is_error": True,
                "args": '{"command": "./deploy.sh"}',
                "result": "login failed: the token was refused",
                "at": 2.0,
            },
            {"source": "finish_check", "verdict": "clean", "failed": None, "why": None, "at": 3.0},
        ],
    )


# ---- the fixed report instructions --------------------------------------------------------------------------------

# A new report text or schema needs a new version string and its fingerprint here (old verdicts keep theirs).
REPORT_PINNED = {"rj-1": "91353803bf245898e1303cbcbb8b7b1c38becb7edd2a521679c582afee75a0c3"}


def test_the_report_instructions_change_only_with_their_version():
    digest = hashlib.sha256(instructions.report_fingerprint_text().encode("utf-8")).hexdigest()
    assert REPORT_PINNED.get(instructions.REPORT_INSTRUCTION_VERSION) == digest, digest


def test_the_report_version_is_its_own_and_the_decision_version_does_not_move():
    assert instructions.REPORT_INSTRUCTION_VERSION == "rj-1"
    assert instructions.INSTRUCTION_VERSION == "dj-2"
    assert instructions.REPORT_INSTRUCTIONS != instructions.DECISION_INSTRUCTIONS
    assert instructions.report_fingerprint_text() != instructions.fingerprint_text()


def test_the_report_schema_requires_only_the_verdict_and_lists_the_report_labels():
    schema = instructions.REPORT_OUTPUT_SCHEMA
    assert schema["required"] == ["verdict"]
    assert instructions.REPORT_VERDICTS == ("supported", "not_supported", "cannot_tell")
    assert schema["properties"]["verdict"]["enum"] == list(instructions.REPORT_VERDICTS)
    assert schema["properties"]["labels"]["items"]["enum"] == list(instructions.REPORT_LABELS)
    assert schema["properties"]["labels"]["maxItems"] == instructions.MAX_LABELS
    for word in (*instructions.REPORT_VERDICTS, *instructions.REPORT_LABELS, "finish_check", "outside_data"):
        assert word in instructions.REPORT_INSTRUCTIONS, word


def test_the_report_instructions_give_no_example_record_or_verdict():
    text = instructions.REPORT_INSTRUCTIONS
    assert "{" not in text and "e.g." not in text.lower() and "for example" not in text.lower()


# ---- the report judge -----------------------------------------------------------------------------------------------


def test_the_report_judge_is_the_built_in_judge_with_the_report_version():
    client = _Client()
    judge = model_judge.make_background_model_report_judge(Settings(_env_file=None), client)
    assert (judge.name, judge.version, judge.model) == (
        model_judge.NAME,
        instructions.REPORT_INSTRUCTION_VERSION,
        Settings(_env_file=None).background_model,
    )
    chosen = model_judge.make_background_model_report_judge(
        Settings(_env_file=None, outside_judge_model="claude-haiku-5-5"), client
    )
    assert chosen.model == "claude-haiku-5-5"


async def test_the_report_judge_sends_the_report_text_with_no_temperature_and_no_thinking():
    client = _Client(usage={"input_tokens": 700, "output_tokens": 30})
    judge = model_judge.BackgroundModelReportJudge(client, "claude-test")
    out = await judge.grade(_report_record(), timeout=5)
    assert (out.status, out.verdict, out.confidence, out.labels, out.model) == (
        "ok",
        "not_supported",
        0.8,
        ("error_in_evidence",),
        "claude-test",
    )
    assert (out.tokens_in, out.tokens_out, out.calls) == (700, 30, 1)
    [(payload, ctx)] = client.calls
    assert "temperature" not in payload and "thinking" not in payload
    assert payload["max_tokens"] == model_judge.MAX_TOKENS
    assert ctx.purpose == "outside_judge"
    assert payload["system"][-1]["text"] == instructions.REPORT_INSTRUCTIONS
    tool = payload["tools"][0]
    assert tool["input_schema"] == instructions.REPORT_OUTPUT_SCHEMA
    message = json.loads(payload["messages"][0]["content"][0]["text"])
    assert message["subject_kind"] == "task_report"


async def test_the_decision_judge_still_sends_the_decision_text():
    client = _Client(answer={"verdict": "success"})
    judge = model_judge.BackgroundModelJudge(client, "claude-test")
    record = evidence.assemble(
        "decision",
        SUBJECT_ID,
        {"description": "Rotate the key"},
        Claim(grader="agent", grade="success", note="Rotated"),
        [{"source": "tool_call", "tool": "bash", "status": "ok", "is_error": False, "args": "{}", "result": "ok"}],
    )
    out = await judge.grade(record, timeout=5)
    assert (out.status, out.verdict) == ("ok", "success")
    [(payload, _ctx)] = client.calls
    assert payload["system"][-1]["text"] == instructions.DECISION_INSTRUCTIONS
    assert payload["tools"][0]["input_schema"] == instructions.OUTPUT_SCHEMA


@pytest.mark.parametrize("verdict", ["success", "partial", "failure", "SUPPORTED", None, 3])
async def test_a_verdict_outside_the_report_set_is_an_error(verdict):
    judge = model_judge.BackgroundModelReportJudge(_Client(answer={"verdict": verdict}), "claude-test")
    out = await judge.grade(_report_record(), timeout=5)
    assert (out.status, out.verdict, out.labels) == ("error", "cannot_tell", ("judge_error",))


async def test_a_report_verdict_from_the_decision_judge_is_an_error():
    judge = model_judge.BackgroundModelJudge(_Client(answer={"verdict": "supported"}), "claude-test")
    out = await judge.grade(_report_record(), timeout=5)
    assert out.status == "error"


async def test_unknown_and_decision_labels_are_dropped_and_at_most_four_report_labels_kept():
    answer = {
        "verdict": "supported",
        "labels": [
            "evidence_confirms",
            "claim_backed",
            "made_up",
            "claim_backed",
            "scope_gap",
            "evidence_thin",
            "files_unverified",
            "asks_for_verdict",
        ],
        "reason": "  The   page was changed   and the deploy ran. ",
    }
    judge = model_judge.BackgroundModelReportJudge(_Client(answer=answer), "claude-test")
    out = await judge.grade(_report_record(), timeout=5)
    assert out.labels == ("claim_backed", "scope_gap", "evidence_thin", "files_unverified")
    assert out.reason == "The page was changed and the deploy ran."


async def test_a_report_judge_that_hangs_is_a_timeout():
    class _Hangs(_Client):
        async def call(self, payload, call_context=None):
            await asyncio.sleep(3600)

    out = await model_judge.BackgroundModelReportJudge(_Hangs(), "claude-test").grade(_report_record(), timeout=0.05)
    assert (out.status, out.verdict) == ("timeout", "cannot_tell")


# ---- the record of a report ---------------------------------------------------------------------------------------


def test_the_finish_check_is_evidence_outside_what_the_author_wrote():
    document = json.loads(evidence.render_record(_report_record()))
    assert document["subject_kind"] == "task_report"
    assert set(document["written_by_author"]) == {"subject", "claim"}
    assert document["written_by_author"]["claim"] == {
        "grader": "agent",
        "grade": "completed",
        "note": "Fixed the link and deployed.",
    }
    finish = [item for item in document["evidence"] if item["source"] == "finish_check"]
    assert finish == [{"source": "finish_check", "verdict": "clean", "failed": None, "why": None}]
    call = next(item for item in document["evidence"] if item["source"] == "tool_call")
    assert call["outside_data"] == {"result": "login failed: the token was refused"}
    assert call["is_error"] is True


def test_the_finish_check_is_kept_first_when_the_cap_drops_items():
    big = [
        {
            "source": "tool_call",
            "tool": "read_file",
            "status": "ok",
            "is_error": False,
            "args": "{}",
            "result": "x" * 5000,
            "at": float(i),
        }
        for i in range(40)
    ]
    flagged = {"source": "finish_check", "verdict": "flagged", "failed": "named_file_missing", "why": None, "at": 0.5}
    record = _report_record([*big, flagged])
    assert record.evidence_truncated and record.evidence_dropped > 0
    assert any(item["source"] == "finish_check" and item["verdict"] == "flagged" for item in record.evidence)
    assert len(evidence.render_record(record)) <= evidence.EVIDENCE_MAX_CHARS


def test_long_findings_and_a_long_task_are_cut_head_and_tail():
    record = _report_record(report_findings="F" * 3000, task="T" * 3000)
    assert len(record.subject["report_findings"]) < 3000 and evidence.CUT_MARKER in record.subject["report_findings"]
    assert len(record.subject["task"]) < 3000 and record.evidence_truncated


def test_a_secret_in_the_task_or_the_finish_check_is_masked():
    secret = "sk-ant-api03-" + "Z" * 40
    record = _report_record(
        [{"source": "finish_check", "verdict": "flagged", "failed": secret, "why": None, "at": 1.0}],
        task=f"Use the key {secret} to deploy",
    )
    assert secret not in evidence.render_record(record)
