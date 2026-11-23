"""F118: the short ``title`` on the decisions and reports GET /dashboard/outside-judge lists (owner-guarded).

Each listed decision (``disagreements``, ``between_judges.disagreements``) and not-supported report
(``reports.not_supported``) carries a ``title``: the decision's description or the task's text, as one line of at most
120 characters, null when the subject no longer exists or could not be read. One read per kind with rows, of this
agent's rows only; when it fails the titles of that kind are null, one WARNING names the error's class, and the
numbers are still answered.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text

from nous import judge as judge_slot
from nous.config import Settings
from nous.judge import store

OWNER_VALUE = "owner-only-test-value"
OWNER = {"X-Nous-Owner-Secret": OWNER_VALUE}
LONG = "Move the nightly\n\treport   to the  new bucket, " + "then check every file it wrote " * 6


# ---- one line -----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "line"),
    [
        ("Ship the\nfix\t now ", "Ship the fix now"),
        ("  two\r\n\r\nlines  ", "two lines"),
        ("x" * 120, "x" * 120),
        ("x" * 121, "x" * 119 + "…"),
        ("a" * 118 + " b c", "a" * 118 + "…"),  # the cut's own space is not kept
        ("", None),
        (" \n\t ", None),
        (None, None),
    ],
)
def test_one_line_folds_the_white_space_and_cuts_at_120(raw, line):
    assert store.one_line(raw) == line
    assert line is None or len(line) <= store.TITLE_MAX


# ---- the read -------------------------------------------------------------------------------------------------------


@pytest_asyncio.fixture
async def agent(db):
    name = f"test-f118-title-{uuid.uuid4().hex[:8]}"
    yield name
    async with db.session() as session:
        for table in ("nous_system.judge_verdicts", "brain.decisions", "heart.subtasks"):
            await session.execute(text(f"DELETE FROM {table} WHERE agent_id LIKE :a"), {"a": f"{name}%"})
        await session.commit()


async def _decision(db, agent: str, description: str) -> uuid.UUID:
    decision_id = uuid.uuid4()
    async with db.session() as session:
        await session.execute(
            text(
                "INSERT INTO brain.decisions (id, agent_id, description, confidence, category, stakes) "
                "VALUES (:id, :agent, :description, 0.7, 'process', 'low')"
            ),
            {"id": decision_id, "agent": agent, "description": description},
        )
        await session.commit()
    return decision_id


async def _task(db, agent: str, task: str) -> uuid.UUID:
    task_id = uuid.uuid4()
    async with db.session() as session:
        await session.execute(
            text("INSERT INTO heart.subtasks (id, agent_id, task) VALUES (:id, :agent, :task)"),
            {"id": task_id, "agent": agent, "task": task},
        )
        await session.commit()
    return task_id


class _Counting:
    """A session that passes each statement to the real one and keeps its parameters."""

    def __init__(self, session: Any) -> None:
        self.session, self.params = session, []

    async def execute(self, statement: Any, params: dict[str, Any]) -> Any:
        self.params.append(params)
        return await self.session.execute(statement, params)


class _Refusing:
    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a statement was run")


@pytest.mark.postgres_only
async def test_titles_of_reads_this_agents_decisions_and_tasks_only(db, agent):
    mine = await _decision(db, agent, "Retry the\nupload")
    theirs = await _decision(db, f"{agent}-other", "Another agent's decision")
    my_task = await _task(db, agent, "Write the\n\nweekly summary")
    their_task = await _task(db, f"{agent}-other", "Another agent's task")
    missing = str(uuid.uuid4())
    async with db.session() as session:
        decisions = await store.titles_of(session, agent, "decision", [str(mine), str(theirs), missing])
        tasks = await store.titles_of(session, agent, "task", [str(my_task), str(their_task), missing, str(mine)])
    assert decisions == {str(mine): "Retry the upload", str(theirs): None, missing: None}
    assert tasks == {str(my_task): "Write the weekly summary", str(their_task): None, missing: None, str(mine): None}


@pytest.mark.postgres_only
async def test_titles_of_asks_each_id_once_and_runs_nothing_for_no_ids(db, agent):
    decision_id = str(await _decision(db, agent, LONG))
    async with db.session() as session:
        counting = _Counting(session)
        titles = await store.titles_of(counting, agent, "decision", [decision_id, decision_id])
    assert titles == {decision_id: store.one_line(LONG)} and len(titles[decision_id]) == store.TITLE_MAX
    [params] = counting.params
    assert params["ids"] == [decision_id] and params["agent"] == agent
    assert await store.titles_of(_Refusing(), agent, "task", []) == {}


# ---- the route ------------------------------------------------------------------------------------------------------


def _app(tmp_path, database: Any, **over: Any):
    from nous.api.rest import create_app

    settings = Settings(
        _env_file=None,
        workspace_dir=str(tmp_path),
        owner_route_secret_sha256=hashlib.sha256(OWNER_VALUE.encode()).hexdigest(),
        **over,
    )
    stand_in = SimpleNamespace()
    return create_app(stand_in, stand_in, SimpleNamespace(schedules=None), stand_in, database, settings)


async def _get(app) -> httpx.Response:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://nous") as client:
        return await client.get("/dashboard/outside-judge", headers=OWNER)


async def _verdicts(db, agent: str, rows: list[dict[str, Any]]) -> None:
    async with db.session() as session:
        for row in rows:
            values = {
                "agent_id": agent,
                "subject_kind": "decision",
                "judge_name": "stub",
                "judge_version": "stub-9",
                "status": "ok",
                "verdict": "partial",
                "confidence": 0.6,
                "labels": [],
                "reason": "It did not finish.",
                "against_grader": "a2ui",
                "against_grade": "success",
                "evidence": {"tool_calls": 1},
                "calls": 1,
                "claim_at": datetime.now(UTC),
                "judged_at": datetime.now(UTC),
                **row,
            }
            await session.execute(
                text(
                    "INSERT INTO nous_system.judge_verdicts (agent_id, subject_kind, subject_id, judge_name, "
                    "judge_version, status, verdict, confidence, labels, reason, against_grader, against_grade, "
                    "claim_at, evidence, calls, judged_at) VALUES (:agent_id, :subject_kind, :subject_id, :judge_name, "
                    ":judge_version, :status, :verdict, :confidence, CAST(:labels AS jsonb), :reason, :against_grader, "
                    ":against_grade, :claim_at, CAST(:evidence AS jsonb), :calls, :judged_at)"
                ),
                {**values, "labels": json.dumps(values["labels"]), "evidence": json.dumps(values["evidence"])},
            )
        await session.commit()


class _Stub:
    def __init__(self, name: str, version: str) -> None:
        self.name, self.version, self.model = name, version, "claude-stub"

    async def grade(self, record, *, timeout, max_calls=None):  # pragma: no cover - never called here
        raise AssertionError


def _service(db, agent: str, **over: Any) -> Any:
    from nous.judge.service import OutsideJudge

    return OutsideJudge(
        db, Settings(_env_file=None, agent_id=agent, outside_judge_mode="shadow"), _Stub("stub", "stub-9"), **over
    )


@pytest.mark.postgres_only
async def test_each_disagreement_carries_its_decisions_title(tmp_path, db, agent):
    short = await _decision(db, agent, "Retry the\nupload")
    long = await _decision(db, agent, LONG)
    gone = uuid.uuid4()  # no decision row: deleted, or never there
    at = datetime.now(UTC)
    await _verdicts(
        db,
        agent,
        [
            {"subject_id": short, "judged_at": at},
            {"subject_id": long, "judged_at": at},
            {"subject_id": gone, "judged_at": at},
            {"subject_id": await _decision(db, agent, "Agreed"), "verdict": "success"},  # not listed
        ],
    )
    service = _service(db, agent)
    judge_slot.set_current(service)
    try:
        response = await _get(_app(tmp_path, db, agent_id=agent, outside_judge_mode="shadow"))
    finally:
        await service.close()
    assert response.status_code == 200, response.text
    titles = {row["subject_id"]: row["title"] for row in response.json()["disagreements"]}
    assert titles == {str(short): "Retry the upload", str(long): store.one_line(LONG), str(gone): None}
    assert len(titles[str(long)]) == 120 and titles[str(long)].endswith("…") and "\n" not in titles[str(long)]


@pytest.mark.postgres_only
async def test_with_two_judges_the_pairs_carry_a_title_and_the_second_judges_list_does_not(tmp_path, db, agent):
    from nous.judge.service import JudgeSet, OutsideJudge

    settings = Settings(_env_file=None, agent_id=agent, outside_judge_mode="shadow")
    both = JudgeSet(
        OutsideJudge(db, settings, _Stub("stub", "stub-9")), OutsideJudge(db, settings, _Stub("other", "o-1"))
    )
    differ = await _decision(db, agent, "Rotate the\nkeys")
    claim_at = datetime.now(UTC)
    await _verdicts(
        db,
        agent,
        [
            {"subject_id": differ, "verdict": "success", "claim_at": claim_at},
            {
                "subject_id": differ,
                "judge_name": "other",
                "judge_version": "o-1",
                "verdict": "failure",
                "claim_at": claim_at,
            },
        ],
    )
    judge_slot.set_current(both)
    try:
        body = (await _get(_app(tmp_path, db, agent_id=agent, outside_judge_mode="shadow"))).json()
    finally:
        await both.close()
    [pair] = body["between_judges"]["disagreements"]
    assert (pair["subject_id"], pair["title"]) == (str(differ), "Rotate the keys")
    [second] = body["second"]["disagreements"]
    assert second["subject_id"] == str(differ) and "title" not in second
    assert body["disagreements"] == []  # the first judge's verdict is the grade


@pytest.mark.postgres_only
async def test_each_not_supported_report_carries_its_tasks_title(tmp_path, db, agent):
    task_id = await _task(db, agent, "Deploy the\nnew build")
    gone = uuid.uuid4()
    report = {"subject_kind": "task_report", "judge_version": "stub-r9", "verdict": "not_supported"}
    await _verdicts(db, agent, [{**report, "subject_id": task_id}, {**report, "subject_id": gone}])
    service = _service(db, agent, report_judge=_Stub("stub", "stub-r9"))
    judge_slot.set_current(service)
    try:
        app = _app(tmp_path, db, agent_id=agent, outside_judge_mode="shadow", outside_judge_reports_enabled=True)
        body = (await _get(app)).json()
    finally:
        await service.close()
    titles = {row["subject_id"]: row["title"] for row in body["reports"]["not_supported"]}
    assert titles == {str(task_id): "Deploy the new build", str(gone): None}


@pytest.mark.postgres_only
async def test_when_the_titles_cannot_be_read_they_are_null_and_the_numbers_are_still_given(
    tmp_path, db, agent, monkeypatch, caplog
):
    first = await _decision(db, agent, "A description that is never logged")
    await _verdicts(db, agent, [{"subject_id": first}, {"subject_id": uuid.uuid4()}])

    async def broken(*args: Any, **kwargs: Any) -> Any:
        raise ConnectionError("A description that is never logged")

    monkeypatch.setattr(store, "titles_of", broken)
    service = _service(db, agent)
    judge_slot.set_current(service)
    try:
        with caplog.at_level(logging.WARNING, logger="nous.api.rest"):
            response = await _get(_app(tmp_path, db, agent_id=agent, outside_judge_mode="shadow"))
    finally:
        await service.close()
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["decisions"]["judged"] == 2 and body["cost"]["calls"] == 2
    assert [row["title"] for row in body["disagreements"]] == [None, None]
    [line] = [r.getMessage() for r in caplog.records if r.name == "nous.api.rest" and r.levelno >= logging.WARNING]
    assert "ConnectionError" in line and "never logged" not in line


@pytest.mark.postgres_only
async def test_with_nothing_listed_no_title_is_read(tmp_path, db, agent, monkeypatch):
    asked: list[Any] = []

    async def recording(*args: Any, **kwargs: Any) -> dict[str, str | None]:
        asked.append(args)
        return {}

    monkeypatch.setattr(store, "titles_of", recording)
    await _verdicts(db, agent, [{"subject_id": uuid.uuid4(), "verdict": "success"}])
    service = _service(db, agent)
    judge_slot.set_current(service)
    try:
        app = _app(tmp_path, db, agent_id=agent, outside_judge_mode="shadow", outside_judge_reports_enabled=True)
        body = (await _get(app)).json()
    finally:
        await service.close()
    assert body["decisions"]["judged"] == 1 and body["disagreements"] == []
    assert body["reports"]["not_supported"] == [] and asked == []
