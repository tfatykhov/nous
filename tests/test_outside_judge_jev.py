"""F118: the second judge, Jev: the request it sends, how its answer is checked, and how each failure is stored.

Every test sends its request to a fake transport (``httpx.MockTransport``); a request that would leave the process
fails the test. A status other than 200, a body that is not a JSON object, over 64 KiB or slower than the timeout, a
choice outside the four verdicts, a confidence that is not a number from 0 to 1, and an answer from another model are
each an ``error`` (or a ``timeout``) with the verdict ``cannot_tell``. The second question's answer only adds a label.
The key is sent in the one header and is never in a log record or the returned output.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest

from nous.config import Settings
from nous.judge import evidence, instructions, jev_judge, slot
from nous.judge.slot import Claim, JudgeInput

KEY = "ts-test-" + "K7" * 16
MODEL = "jev-1.14.0"
SUBJECT_ID = uuid.UUID("00000000-0000-4000-8000-00000000e118")


@pytest.fixture(autouse=True)
def _no_request_leaves_the_process(monkeypatch):
    async def refuse(self: Any, request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"a real request was attempted to {request.url.host}")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse)


def _record() -> JudgeInput:
    return evidence.assemble(
        "decision",
        SUBJECT_ID,
        {"description": "Restart the nightly job", "context": None, "stakes": "low", "stated_confidence": 0.7},
        Claim(grader="agent", grade="success", note="it ran"),
        [
            {
                "source": "tool_call",
                "tool": "bash",
                "status": "ok",
                "is_error": False,
                "args": '{"command": "systemctl restart nightly"}',
                "result": "restarted",
                "at": 1.0,
            }
        ],
    )


def _answer(**over: Any) -> dict[str, Any]:
    answer: dict[str, Any] = {
        "model": MODEL,
        "answers": {
            "verdict": {
                "type": "choice",
                "choice": "success",
                "confidence": 0.82,
                "probabilities": {"success": 0.82, "partial": 0.1, "failure": 0.05, "cannot_tell": 0.03},
            },
            "sets_verdict": {"type": "noul", "noul": 0.1},
        },
        "usage": {"input_tokens": 4100, "output_tokens": 2},
    }
    return {**answer, **over}


def _with_verdict(**fields: Any) -> dict[str, Any]:
    """An answer whose ``answers.verdict`` has ``fields`` set (a value of ``...`` removes the field)."""
    answer = _answer()
    verdict = {**answer["answers"]["verdict"], **fields}
    answer["answers"]["verdict"] = {k: v for k, v in verdict.items() if v is not ...}
    return answer


def _with_sets_verdict(value: Any) -> dict[str, Any]:
    answer = _answer()
    if value is ...:
        del answer["answers"]["sets_verdict"]
    else:
        answer["answers"]["sets_verdict"] = {"type": "noul", "noul": value}
    return answer


class Provider:
    """A fake provider: records each request and answers with ``respond(request)``."""

    def __init__(self, respond: Callable[[httpx.Request], Any]) -> None:
        self.respond = respond
        self.requests: list[httpx.Request] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = self.respond(request)
        if asyncio.iscoroutine(response):
            response = await response
        return response


def _replying(body: Any, status: int = 200, **kwargs: Any) -> Provider:
    content = body if isinstance(body, bytes) else json.dumps(body).encode()
    return Provider(lambda request: httpx.Response(status, content=content, **kwargs))


def _judge(provider: Provider) -> jev_judge.JevJudge:
    return jev_judge.JevJudge(KEY, MODEL, transport=httpx.MockTransport(provider))


async def _grade(provider: Provider, *, timeout: float = 5.0, max_calls: int | None = None) -> slot.JudgeOutput:
    return await _judge(provider).grade(_record(), timeout=timeout, max_calls=max_calls)


def _is_error(out: slot.JudgeOutput, status: str = "error") -> bool:
    return (out.status, out.verdict, out.labels, out.model, out.calls) == (
        status,
        "cannot_tell",
        ("judge_error",),
        MODEL,
        1,
    )


# ---- the request ----------------------------------------------------------------------------------------------------


def test_the_url_is_the_system_one_endpoint():
    assert jev_judge.URL == "https://api.typesafe.ai/v1/systemone"


def test_the_name_the_version_and_the_model():
    judge = jev_judge.JevJudge(KEY, "jev-1.13.0")
    assert (judge.name, judge.version, judge.model) == ("jev", "jq-3:jev-1.13.0", "jev-1.13.0")
    assert isinstance(judge, slot.Judge)


async def test_the_request_sends_the_record_as_the_state_and_asks_two_questions():
    provider = _replying(_answer())
    await _grade(provider)
    [request] = provider.requests
    assert (request.method, str(request.url)) == ("POST", jev_judge.URL)
    assert request.headers["authorization"] == f"Bearer {KEY}"
    assert request.headers["content-type"] == "application/json"
    body = json.loads(request.content)
    assert body == {
        "model": MODEL,
        "state": json.loads(evidence.render_record(_record())),
        "questions": {
            "verdict": {
                "type": "choice",
                "instructions": jev_judge.VERDICT_QUESTION,
                "criteria": jev_judge.VERDICT_CRITERIA,
            },
            "sets_verdict": {"type": "noul", "instructions": jev_judge.SETS_VERDICT_QUESTION},
        },
    }
    assert list(body["questions"]["verdict"]["criteria"]) == list(instructions.DECISION_VERDICTS)


def test_the_questions_name_the_records_fields_by_path():
    for path in ("`written_by_author.claim`", "`written_by_author.subject`", "`evidence`", "`outside_data`"):
        assert path in jev_judge.VERDICT_QUESTION, path
    for text in jev_judge.VERDICT_CRITERIA.values():
        assert text.endswith("judged from `evidence`.")
    assert "`written_by_author`" in jev_judge.SETS_VERDICT_QUESTION
    assert "`outside_data`" in jev_judge.SETS_VERDICT_QUESTION


def test_the_question_says_how_to_grade_a_record_whose_claim_is_null():
    # A decision graded by anyone but the agent is sent with no claim (``service._shown_claim``).
    sentence = "It is null when the grade is kept out of the record: then grade the decision from `evidence` alone."
    assert sentence in jev_judge.VERDICT_QUESTION
    assert "`written_by_author.claim`, when it is not null, is how the agent graded it." in jev_judge.VERDICT_QUESTION
    assert "what was claimed" not in jev_judge.VERDICT_CRITERIA["cannot_tell"]


# A new question text or label threshold needs a new QUESTION_VERSION and its fingerprint here (old verdicts keep
# theirs).
PINNED = {
    "jq-2": "9adba39deefd808c52cd66b2e24137c961551b12875ca17080b4b7410d1a82df",
    "jq-3": "0d79b8f596271d258a27c814a516a5e315e1ef20063861ed56e9c17852adc1ed",
}


def test_the_questions_change_only_with_their_version():
    digest = hashlib.sha256(jev_judge.fingerprint_text().encode("utf-8")).hexdigest()
    assert PINNED.get(jev_judge.QUESTION_VERSION) == digest, digest


def test_the_label_threshold_is_part_of_what_the_version_stands_for():
    assert json.loads(jev_judge.fingerprint_text())["sets_verdict_at"] == jev_judge.SETS_VERDICT_AT


def test_the_second_questions_threshold():
    # Measured on sample records: ordinary records read 0.38 to 0.65, a record with such a line 0.98.
    assert jev_judge.SETS_VERDICT_AT == 0.9


def test_the_label_of_the_second_question_is_one_of_the_built_in_judges_labels():
    assert jev_judge.SETS_VERDICT_LABEL == "asks_for_verdict" and "asks_for_verdict" in instructions.DECISION_LABELS


class _RecordingClient(httpx.AsyncClient):
    made: list[dict[str, Any]] = []

    def __init__(self, **kwargs: Any) -> None:
        _RecordingClient.made.append(kwargs)
        super().__init__(**kwargs)


async def test_one_client_per_request_without_redirects_and_with_certificate_checking(monkeypatch):
    monkeypatch.setattr(_RecordingClient, "made", [])
    monkeypatch.setattr(jev_judge.httpx, "AsyncClient", _RecordingClient)
    provider = _replying(_answer())
    judge = _judge(provider)
    await judge.grade(_record(), timeout=7.0)
    await judge.grade(_record(), timeout=7.0)
    assert len(_RecordingClient.made) == 2
    for kwargs in _RecordingClient.made:
        assert kwargs["follow_redirects"] is False
        assert "verify" not in kwargs  # httpx's default: certificates are checked
        assert kwargs["trust_env"] is False  # no proxy or certificate setting is read from the environment
        assert kwargs["timeout"] == httpx.Timeout(7.0)


async def test_the_request_asks_for_an_uncompressed_body():
    provider = _replying(_answer())
    await _grade(provider)
    [request] = provider.requests
    assert request.headers["accept-encoding"] == "identity"


# ---- an answer -----------------------------------------------------------------------------------------------------


async def test_an_answer_gives_the_verdict_its_confidence_and_the_tokens():
    out = await _grade(_replying(_answer()))
    assert out == slot.JudgeOutput(
        status="ok",
        verdict="success",
        confidence=0.82,
        labels=(),
        reason=None,
        model=MODEL,
        tokens_in=4100,
        tokens_out=2,
        calls=1,
    )


@pytest.mark.parametrize("verdict", instructions.DECISION_VERDICTS)
async def test_each_of_the_four_choices_is_a_verdict(verdict):
    out = await _grade(_replying(_with_verdict(choice=verdict)))
    assert (out.status, out.verdict) == ("ok", verdict)


@pytest.mark.parametrize(
    "usage",
    [None, "4100", {"input_tokens": "4100"}, {"input_tokens": True}, {"input_tokens": -1}, {"input_tokens": 2**31}],
)
async def test_tokens_that_are_not_a_count_are_unknown_and_the_answer_stands(usage):
    answer = _answer(usage=usage) if usage is not None else {k: v for k, v in _answer().items() if k != "usage"}
    out = await _grade(_replying(answer))
    assert (out.status, out.verdict, out.tokens_in) == ("ok", "success", None)


# ---- the second question adds a label only ----------------------------------------------------------------------


@pytest.mark.parametrize("value", [0.9, 0.97, 1])
async def test_a_yes_to_the_second_question_adds_the_label_and_keeps_the_verdict(value):
    out = await _grade(_replying(_with_sets_verdict(value)))
    assert (out.status, out.verdict, out.confidence, out.labels) == ("ok", "success", 0.82, ("asks_for_verdict",))


@pytest.mark.parametrize(
    "value", [0.89, 0.65, 0.5, 0.49, 0, ..., "0.9", True, 1.7, -0.5, None, [0.9], 10**400, -(10**400), 2]
)
async def test_any_other_answer_to_the_second_question_adds_no_label(value):
    out = await _grade(_replying(_with_sets_verdict(value)))
    assert (out.status, out.verdict, out.confidence, out.labels) == ("ok", "success", 0.82, ())


@pytest.mark.parametrize("choice", ["failure", "cannot_tell"])
async def test_the_second_question_never_changes_the_verdict(choice):
    answer = _with_verdict(choice=choice)
    answer["answers"]["sets_verdict"] = {"type": "noul", "noul": 0.99}
    out = await _grade(_replying(answer))
    assert (out.status, out.verdict, out.labels) == ("ok", choice, ("asks_for_verdict",))


async def test_a_malformed_second_answer_object_adds_no_label():
    answer = _answer()
    answer["answers"]["sets_verdict"] = "yes"
    out = await _grade(_replying(answer))
    assert (out.status, out.labels) == ("ok", ())


# ---- answers that are errors ---------------------------------------------------------------------------------------

BAD_ANSWERS = {
    "choice missing": _with_verdict(choice=...),
    "choice not one of the four": _with_verdict(choice="great"),
    "choice not a string": _with_verdict(choice=["success"]),
    "choice a number": _with_verdict(choice=1),
    "confidence missing": _with_verdict(confidence=...),
    "confidence above one": _with_verdict(confidence=1.7),
    "confidence below zero": _with_verdict(confidence=-0.1),
    "confidence a string": _with_verdict(confidence="0.8"),
    "confidence a boolean": _with_verdict(confidence=True),
    "confidence null": _with_verdict(confidence=None),
    "confidence a whole number of 400 digits": _with_verdict(confidence=10**400),
    "confidence a negative whole number of 400 digits": _with_verdict(confidence=-(10**400)),
    "confidence the whole number 2": _with_verdict(confidence=2),
    "model missing": {k: v for k, v in _answer().items() if k != "model"},
    "another model": _answer(model="jev-1.13.0"),
    "an alias": _answer(model="jev-latest"),
    "model not a string": _answer(model=["jev-1.14.0"]),
    "answers missing": {k: v for k, v in _answer().items() if k != "answers"},
    "answers a list": _answer(answers=[]),
    "verdict answer a string": _answer(answers={"verdict": "success"}),
}


@pytest.mark.parametrize("name", list(BAD_ANSWERS))
async def test_an_answer_that_does_not_check_is_an_error(name):
    out = await _grade(_replying(BAD_ANSWERS[name]))
    assert _is_error(out), out


@pytest.mark.parametrize("confidence", [0, 1])
async def test_a_confidence_of_the_whole_number_0_or_1_is_kept(confidence):
    out = await _grade(_replying(_with_verdict(confidence=confidence)))
    assert (out.status, out.confidence) == ("ok", float(confidence))


async def test_an_answer_that_cannot_be_read_is_an_error_and_grade_does_not_raise(monkeypatch, caplog):
    def broken(content: bytes, model: str) -> slot.JudgeOutput:
        raise OverflowError("cannot read")

    monkeypatch.setattr(jev_judge, "parse_answer", broken)
    with caplog.at_level(logging.WARNING, logger="nous.judge.jev_judge"):
        out = await _grade(_replying(_answer()))
    assert _is_error(out)
    [line] = [r.getMessage() for r in caplog.records if r.name == "nous.judge.jev_judge"]
    assert "OverflowError" in line and "cannot read" not in line


async def test_an_error_answer_keeps_its_token_counts():
    out = await _grade(_replying(_answer(model="jev-1.13.0")))
    assert _is_error(out) and (out.tokens_in, out.tokens_out) == (4100, 2)


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[]",
        b'"success"',
        b"",
        json.dumps(_answer()).replace("0.82", "NaN").encode(),
        json.dumps(_answer()).replace("0.82", "Infinity").encode(),
        b"[" * 20_000 + b"]" * 20_000,  # within the size limit, deeper than the parser goes
        b"\xff\xfe\x00",
    ],
)
async def test_a_body_that_is_not_a_json_object_or_has_no_finite_confidence_is_an_error(body):
    out = await _grade(_replying(body))
    assert _is_error(out), out


@pytest.mark.parametrize("status", [201, 204, 301, 302, 400, 401, 403, 404, 413, 415, 422, 429, 500, 502, 529])
async def test_a_status_other_than_200_is_an_error(status):
    out = await _grade(_replying(_answer(), status=status))
    assert _is_error(out), out


async def test_a_redirect_is_not_followed():
    provider = _replying(b"", status=301, headers={"Location": "https://api.typesafe.ai/v2/elsewhere"})
    out = await _grade(provider)
    assert _is_error(out) and len(provider.requests) == 1


def _stream(chunks: list[bytes], *, pause: float = 0.0, read: list[int] | None = None) -> AsyncIterator[bytes]:
    async def body() -> AsyncIterator[bytes]:
        for chunk in chunks:
            if read is not None:
                read.append(len(chunk))
            if pause:
                await asyncio.sleep(pause)
            yield chunk

    return body()


async def test_the_body_of_a_refused_request_is_not_read():
    read: list[int] = []
    provider = Provider(lambda request: httpx.Response(401, content=_stream([b'{"detail": "no"}'], read=read)))
    out = await _grade(provider)
    assert _is_error(out) and read == []


async def test_a_body_over_64_kib_is_an_error_and_reading_stops_there():
    read: list[int] = []
    chunk = b" " * 16_384
    provider = Provider(lambda request: httpx.Response(200, content=_stream([chunk] * 40, read=read)))
    out = await _grade(provider)
    assert _is_error(out)
    assert jev_judge.BODY_MAX_BYTES < sum(read) <= jev_judge.BODY_MAX_BYTES + len(chunk)


async def test_a_body_of_exactly_64_kib_is_read():
    body = json.dumps(_answer()).encode()
    body += b" " * (jev_judge.BODY_MAX_BYTES - len(body))
    out = await _grade(_replying(body))
    assert out.status == "ok"


# ---- time ----------------------------------------------------------------------------------------------------------


async def test_a_provider_that_does_not_answer_in_time_is_a_timeout():
    async def hang(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(3600)
        raise AssertionError

    started = asyncio.get_running_loop().time()
    out = await _grade(Provider(hang), timeout=0.1)
    assert _is_error(out, "timeout")
    assert asyncio.get_running_loop().time() - started < 2.0


async def test_a_body_that_arrives_slowly_ends_at_the_timeout():
    read: list[int] = []
    body = _stream([b"{"] + [b" "] * 1000, pause=0.05, read=read)
    started = asyncio.get_running_loop().time()
    out = await _grade(Provider(lambda request: httpx.Response(200, content=body)), timeout=0.3)
    assert _is_error(out, "timeout")
    assert asyncio.get_running_loop().time() - started < 2.0 and len(read) < 100


async def test_an_httpx_timeout_is_a_timeout():
    def times_out(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("connect timed out", request=request)

    assert _is_error(await _grade(Provider(times_out)), "timeout")


async def test_a_transport_failure_is_an_error():
    def fails(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    assert _is_error(await _grade(Provider(fails)))


async def test_a_cancellation_passes_through():
    async def hang(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(3600)
        raise AssertionError

    task = asyncio.ensure_future(_grade(Provider(hang), timeout=30))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ---- no request -----------------------------------------------------------------------------------------------------


async def test_a_record_that_cannot_be_rendered_sends_nothing(monkeypatch):
    def broken(record: JudgeInput) -> str:
        raise evidence.RenderError("over the cap")

    monkeypatch.setattr(jev_judge, "render_record", broken)
    provider = _replying(_answer())
    out = await _grade(provider)
    assert provider.requests == []
    assert (out.status, out.verdict, out.labels, out.calls) == ("error", "cannot_tell", ("judge_error",), 0)


async def test_with_no_request_left_today_nothing_is_sent():
    provider = _replying(_answer())
    out = await _grade(provider, max_calls=0)
    assert provider.requests == [] and (out.status, out.calls) == ("error", 0)
    assert (await _grade(provider, max_calls=1)).status == "ok" and len(provider.requests) == 1


# ---- the key ------------------------------------------------------------------------------------------------------


def _texts(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join(f"{r.getMessage()} {r.args!r} {r.exc_text or ''}" for r in caplog.records)


async def test_the_key_is_in_no_log_record_and_not_in_the_output_when_the_transport_raises_it(caplog):
    def fails(request: httpx.Request) -> httpx.Response:
        raise RuntimeError(f"the request with {request.headers['authorization']} failed")

    with caplog.at_level(logging.DEBUG):
        out = await _grade(Provider(fails))
    assert _is_error(out)
    assert KEY not in _texts(caplog) and KEY not in repr(out)
    [warning] = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert "RuntimeError" in warning.getMessage() and str(SUBJECT_ID) in warning.getMessage()


async def test_the_key_is_in_no_log_record_when_a_refusal_repeats_it(caplog):
    provider = _replying({"detail": f"invalid key {KEY}"}, status=401)
    with caplog.at_level(logging.DEBUG):
        out = await _grade(provider)
    assert _is_error(out)
    assert KEY not in _texts(caplog) and KEY not in repr(out)
    [warning] = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert "401" in warning.getMessage() and "invalid key" not in warning.getMessage()


async def test_one_warning_per_unusable_answer_without_its_text(caplog):
    with caplog.at_level(logging.DEBUG):
        out = await _grade(_replying(_with_verdict(choice="great")))
    assert _is_error(out)
    [warning] = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert str(SUBJECT_ID) in warning.getMessage() and "great" not in _texts(caplog)


def test_the_key_is_not_in_the_judges_repr():
    judge = jev_judge.JevJudge(KEY, MODEL)
    assert KEY not in repr(judge) and KEY not in str(vars(type(judge)))


# ---- the provider ---------------------------------------------------------------------------------------------------


def test_without_a_key_there_is_no_judge():
    assert jev_judge.make_jev_judge(Settings(_env_file=None)) is None
    assert jev_judge.make_jev_judge(Settings(_env_file=None, TYPESAFE_API_KEY="  ")) is None


def test_with_a_key_the_judge_uses_the_pinned_model_and_the_real_transport():
    settings = Settings(_env_file=None, TYPESAFE_API_KEY="k-1", outside_judge_jev_model="jev-1.13.0")
    assert settings.typesafe_api_key == "k-1"
    judge = jev_judge.make_jev_judge(settings)
    assert isinstance(judge, jev_judge.JevJudge)
    assert (judge.model, judge.version, judge._transport) == ("jev-1.13.0", "jq-3:jev-1.13.0", None)


def test_registering_makes_the_provider_available(monkeypatch):
    monkeypatch.setattr(slot, "PROVIDERS", {})
    jev_judge.register_jev_judge()
    assert slot.PROVIDERS == {"jev": jev_judge.make_jev_judge}


def test_importing_the_module_registers_nothing():
    import subprocess
    import sys
    from pathlib import Path

    code = "import nous.judge.jev_judge\nfrom nous.judge import slot\nprint(sorted(slot.PROVIDERS))"
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[1],
        timeout=120,
        check=True,
    )
    assert result.stdout.strip() == "[]"


# ---- the card's check (PR 3) ---------------------------------------------------------------------------------------


async def test_the_check_sends_the_fixed_record_to_the_same_address_with_the_key_in_the_header_only():
    provider = _replying({"model": MODEL, "answers": {"check": {"choice": "yes"}}})
    assert await _judge(provider).probe(5.0) == "ok"
    [request] = provider.requests
    assert (request.method, str(request.url)) == ("POST", jev_judge.URL)
    assert request.headers["authorization"] == f"Bearer {KEY}"
    assert request.headers["accept-encoding"] == "identity"
    assert json.loads(request.content) == jev_judge.check_body(MODEL)
    assert jev_judge.check_body(MODEL) == {
        "model": MODEL,
        "state": {"note": "connection check"},
        "questions": {
            "check": {
                "type": "choice",
                "instructions": "Is the state a connection check?",
                "criteria": {"yes": "The state says it is a connection check.", "no": "The state does not say so."},
            }
        },
    }
    assert KEY not in request.content.decode()


@pytest.mark.parametrize(
    ("status", "body", "word"),
    [
        (200, {"model": MODEL}, "ok"),
        (200, {"model": "jev-9.9.9"}, "refused"),
        (200, ["not", "an", "object"], "refused"),
        (200, b"not json", "refused"),
        (400, {}, "refused"),
        (401, {}, "refused"),
        (403, {}, "refused"),
        (404, {}, "refused"),
        (422, {}, "refused"),
        (301, {}, "refused"),
        (429, {}, "unreachable"),
        (500, {}, "unreachable"),
        (502, {}, "unreachable"),
        (503, {}, "unreachable"),
    ],
)
async def test_each_answer_of_the_check_is_one_word(status, body, word):
    assert await _judge(_replying(body, status=status)).probe(5.0) == word


async def test_a_check_answer_over_the_size_bound_is_refused():
    assert await _judge(_replying(b"x" * (jev_judge.BODY_MAX_BYTES + 1))).probe(5.0) == "refused"


@pytest.mark.parametrize(
    "failure",
    [httpx.ConnectError("no route"), httpx.ReadTimeout("slow"), RuntimeError("broken"), OSError("reset")],
)
async def test_a_check_that_fails_to_connect_or_raises_is_unreachable(failure):
    def respond(request: httpx.Request) -> Any:
        raise failure

    assert await _judge(Provider(respond)).probe(5.0) == "unreachable"


async def test_a_check_with_no_answer_in_time_is_unreachable():
    async def hang(request: httpx.Request) -> Any:
        await asyncio.sleep(30)

    assert await _judge(Provider(hang)).probe(0.05) == "unreachable"


async def test_the_check_logs_no_key_no_body_and_no_exception_text(caplog):
    caplog.set_level(logging.DEBUG)

    def echo_key(request: httpx.Request) -> Any:
        raise RuntimeError(f"failed with {request.headers['authorization']}")

    results = [
        await _judge(Provider(echo_key)).probe(5.0),
        await _judge(_replying({"model": MODEL, "echo": KEY})).probe(5.0),
        await _judge(_replying({"error": KEY}, status=500)).probe(5.0),
        await _judge(_replying({"error": KEY}, status=401)).probe(5.0),
    ]
    assert results == ["unreachable", "ok", "unreachable", "refused"]
    for record in caplog.records:
        logged = record.getMessage() + repr(record.args) + repr(record.exc_info)
        assert KEY not in logged and "failed with" not in logged
