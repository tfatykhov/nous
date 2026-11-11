"""F111 PR A: the promise detector, the backing resolver, the commit tool, the store and the settings (spec D.7 1-5).

The store tests that need ``brain.commitments`` run on Postgres only and skip until migration 107 is applied.
"""

from __future__ import annotations

import json
import statistics
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from nous.brain.commitments import (
    Commitment,
    CommitmentRefused,
    CommitmentStore,
    dedupe_key,
    format_short_id,
    normalise_what,
    parse_short_id,
    validate_due_at,
)
from nous.cognitive.promise_gate import (
    BACKING_TOOLS,
    BackingResolver,
    PromiseDetector,
    parse_artifact,
    render_footer,
)
from nous.cognitive.schemas import ToolResult
from nous.config import Settings

FIXTURES = Path(__file__).parent / "fixtures" / "promise_gate"
NOW = datetime(2026, 11, 10, 17, 45, tzinfo=UTC)


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _tr(tool: str, result: str | None = None, error: str | None = None, **args) -> ToolResult:
    return ToolResult(tool_name=tool, arguments=args, result=result, error=error)


# ---- 1. positives ------------------------------------------------------------------------------------------------


POSITIVES = _load("positives.json")["cases"]
NEGATIVES = _load("negatives.json")["cases"]


def test_the_tables_are_big_enough():
    assert len(POSITIVES) >= 25 and len(NEGATIVES) >= 25


@pytest.mark.parametrize("case", POSITIVES, ids=lambda c: c["text"][:40])
def test_each_positive_is_one_promise_with_a_due_time(case):
    promises = PromiseDetector().scan(case["text"], now=NOW)
    assert len(promises) == 1, case["text"]
    promise = promises[0]
    assert promise.consent == case.get("consent", "none")
    assert promise.kind == "action"
    assert promise.due_at > NOW


def test_a_named_time_is_parsed_and_no_time_gets_the_default():
    detector = PromiseDetector(default_due_minutes=60)
    [at_utc] = detector.scan("I'll relaunch the chain at 18:15 UTC unless you object.", now=NOW)
    assert at_utc.due_parsed and at_utc.due_at == NOW.replace(hour=18, minute=15)
    [past_utc] = detector.scan("I'll relaunch the chain at 09:00 UTC.", now=NOW)
    assert past_utc.due_at == (NOW + timedelta(days=1)).replace(hour=9, minute=0)  # a past clock time is tomorrow
    [within] = detector.scan("I'll launch a finish job within the hour.", now=NOW)
    assert within.due_parsed and within.due_at == NOW + timedelta(hours=1)
    [default] = detector.scan("I'll remove it (option A) unless you object.", now=NOW)
    assert not default.due_parsed and default.due_at == NOW + timedelta(minutes=60)
    [naive] = detector.scan("I'll relaunch it at 18:15 unless you object.", now=NOW)
    assert not naive.due_parsed  # no zone named: no guess, the default


def test_a_relative_time_goes_through_schedule_tasks_resolver():
    before = datetime.now(UTC)
    [promise] = PromiseDetector().scan("I plan to deploy the fix in 2 hours.")
    assert promise.due_parsed
    assert before + timedelta(hours=2) <= promise.due_at <= datetime.now(UTC) + timedelta(hours=2)


# ---- 2. negatives ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("case", NEGATIVES, ids=lambda c: c["text"][:40])
def test_each_negative_is_no_promise(case):
    assert PromiseDetector().scan(case["text"], now=NOW) == []


def test_an_empty_reply_is_no_promise():
    assert PromiseDetector().scan("", now=NOW) == []


# ---- 13. conditional and explanatory (R3) ------------------------------------------------------------------------


def test_a_promise_conditional_on_the_owner_is_awaiting_user():
    [promise] = PromiseDetector().scan("I'll merge it once you confirm.", now=NOW)
    assert promise.kind == "awaiting_user"


def test_explanatory_after_is_not_a_promise_but_an_action_after_an_event_is():
    detector = PromiseDetector()
    assert detector.scan("I'll explain after the table.", now=NOW) == []
    assert len(detector.scan("I'll run the tests once the build finishes.", now=NOW)) == 1


# ---- the replays at detector level -------------------------------------------------------------------------------


def test_nov10_reply_is_exactly_one_silence_promise():
    fixture = _load("nov10_hero.json")
    assert fixture["reconstructed"] is True
    [promise] = PromiseDetector().scan(fixture["reply"], now=NOW)
    assert promise.consent == "silence"
    assert "remove it (option A)" in promise.text


def test_oct08_retrospective_reply_is_no_promise():
    assert PromiseDetector().scan(_load("oct08_items_1_3.json")["retrospective_reply"], now=NOW) == []


def test_stage1_p95_is_under_5_ms():
    """Spec D.8: p95 stage-1 overhead under 5 ms on a long reply (detection and resolution)."""
    reply = (_load("nov10_hero.json")["reply"] + "\n\n") * 12  # about 5 KB
    results = [_tr("read_file", "x"), _tr("dag_create", "Created DAG 'a' (5c3cedb0)\nStatus: running")]
    detector, resolver = PromiseDetector(), BackingResolver()
    samples = []
    for _ in range(200):
        start = time.perf_counter()
        resolver.split(detector.scan(reply), results)
        samples.append((time.perf_counter() - start) * 1000)
    p95 = statistics.quantiles(samples, n=20)[18]
    assert p95 < 5.0, f"p95 {p95:.2f} ms"


# ---- 3. resolver -------------------------------------------------------------------------------------------------

# The success text of each backing tool, as its handler writes it (nous/api/tools.py,
# nous/handlers/continuation_runner.py); a changed handler text must change this table and the parser together.
REAL_RESULTS = {
    "schedule_task": (
        "Schedule created.\nID: 0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d\nType: once\nNext fire: 2026-11-10T18:45:00+00:00",
        ("schedule", "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"),
    ),
    "spawn_task": (
        "Subtask spawned.\nID: 11111111-2222-4333-8444-555555555555\nPriority: normal\nTimeout: 600s",
        ("subtask", "11111111-2222-4333-8444-555555555555"),
    ),
    "dag_create": (
        "Created DAG 'start items 1-3' (5c3cedb0)\n  3 nodes, 2 edges\n  Wave 0: item1\nStatus: running",
        ("dag", "5c3cedb0"),
    ),
    "propose_action": (
        "Staged proposal 9f8e7d6c (schedule_task). It reaches the owner only when you end this turn with "
        "resolve_intention(decision='ask'), and it runs only if the owner approves it.",
        ("proposal", "9f8e7d6c"),
    ),
    "heartbeat_check_create": (
        'Created dynamic check: {"id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "name": "watch", '
        '"interval_seconds": 600}',
        ("check", "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"),
    ),
    "commit": (
        "Commitment C-0042 recorded: relaunch the chain (due 2026-11-10 18:15 UTC). The owner can /drop C-0042.",
        ("commitment", "C-0042"),
    ),
}


def test_the_backing_tools_are_exactly_the_six():
    assert BACKING_TOOLS == {
        "schedule_task",
        "dag_create",
        "spawn_task",
        "propose_action",
        "commit",
        "heartbeat_check_create",
    }


@pytest.mark.parametrize("tool", sorted(REAL_RESULTS))
def test_each_backing_tool_result_yields_its_id_and_backs(tool):
    text, expected = REAL_RESULTS[tool]
    assert parse_artifact(tool, text) == expected
    [promise] = PromiseDetector().scan("I'll relaunch the chain at 18:15 UTC unless you object.", now=NOW)
    backed, unbacked = BackingResolver().split([promise], [_tr(tool, text)])
    assert unbacked == [] and backed[0][1].id == expected[1]


def test_the_awaited_spawn_form_and_a_prefixed_result_parse_too():
    assert parse_artifact("spawn_task", "[Subtask 1a2b3c4d completed]\n\nok") == ("subtask", "1a2b3c4d")
    prefixed = "[input repaired] task had to be recovered.\n\n" + REAL_RESULTS["schedule_task"][0]
    assert parse_artifact("schedule_task", prefixed) == REAL_RESULTS["schedule_task"][1]


@pytest.mark.parametrize(
    "tool",
    ["record_decision", "learn_fact", "create_censor", "submit_final_report", "read_file", "recall_deep", "bash"],
)
def test_non_backing_tools_never_back(tool):
    [promise] = PromiseDetector().scan("I'll start items 1–3 today, unless you object.", now=NOW)
    backed, unbacked = BackingResolver().split([promise], [_tr(tool, "Decision recorded: 192d0e7d. ID: x")])
    assert backed == [] and unbacked == [promise]


def test_an_error_result_or_a_result_without_an_id_never_backs():
    [promise] = PromiseDetector().scan("I'll check back tomorrow.", now=NOW)
    resolver = BackingResolver()
    assert resolver.split([promise], [_tr("schedule_task", error="Tool error: bad when")])[0] == []
    assert resolver.split([promise], [_tr("dag_create", "Created something")])[0] == []
    assert resolver.split([promise], [_tr("commit", error="commit: due_at is not in the future")])[0] == []


def test_two_promises_and_two_calls_match_by_keyword_overlap():
    reply = "I'll relaunch the indexer at 18:15 UTC. I'll email the weekly report tomorrow."
    promises = PromiseDetector().scan(reply, now=NOW)
    assert len(promises) == 2
    results = [
        _tr("schedule_task", REAL_RESULTS["schedule_task"][0], task="relaunch the indexer"),
        _tr("spawn_task", REAL_RESULTS["spawn_task"][0], task="draft the slides"),
    ]
    backed, unbacked = BackingResolver().split(promises, results)
    assert [(p.text[:20], a.tool) for p, a in backed] == [("I'll relaunch the in", "schedule_task")]
    assert [p.text[:15] for p in unbacked] == ["I'll email the "]


def test_oct08_record_decision_does_not_back_but_a_dag_and_three_commits_do():
    fixture = _load("oct08_items_1_3.json")
    detector, resolver = PromiseDetector(), BackingResolver()
    promises = detector.scan(fixture["reply"], now=NOW)
    base = [ToolResult(**r) for r in fixture["tool_results"]]
    assert resolver.split(promises, base)[1] == promises  # unbacked == 1
    backed, unbacked = resolver.split(promises, [*base, ToolResult(**fixture["dag_create_result"])])
    assert unbacked == [] and backed[0][1].kind == "dag"
    backed, unbacked = resolver.split(promises, [*base, *(ToolResult(**r) for r in fixture["commit_results"])])
    assert unbacked == [] and backed[0][1].kind == "commitment"


# ---- the footer --------------------------------------------------------------------------------------------------


def test_the_footer_is_one_line_with_the_id_and_a_relative_due():
    [promise] = PromiseDetector().scan("I'll remove it (option A) unless you object.", now=NOW)
    footer = render_footer(["C-0042"], promise, NOW)
    assert "\n" not in footer and "C-0042" in footer and "in 60 min (18:45 UTC)" in footer
    assert "/drop C-0042" in footer and "committed" not in footer.lower()


# ---- 4. store helpers, the commit tool's validation and the store itself -----------------------------------------


def test_short_ids_round_trip():
    assert format_short_id(42) == "C-0042"
    for arg in ("C-0042", "c-42", "C42", "42", " C-0042 "):
        assert parse_short_id(arg) == 42
    for arg in ("", "C-", "abc", "C-0", "4 2", "1;drop"):
        assert parse_short_id(arg) is None


def test_due_at_must_be_future_and_within_90_days():
    now = datetime.now(UTC)
    assert validate_due_at(now + timedelta(hours=1), now) == now + timedelta(hours=1)
    with pytest.raises(CommitmentRefused):
        validate_due_at(now - timedelta(minutes=1), now)
    with pytest.raises(CommitmentRefused):
        validate_due_at(now + timedelta(days=91), now)
    assert validate_due_at((now + timedelta(days=2)).replace(tzinfo=None), now).tzinfo is not None


def test_the_dedupe_key_is_per_turn_and_ignores_spacing_and_case():
    assert dedupe_key("t1", "Relaunch  the chain") == dedupe_key("t1", "relaunch the chain")
    assert dedupe_key("t1", "relaunch the chain") != dedupe_key("t2", "relaunch the chain")
    assert normalise_what("a\n b ") == "a b"


class FakeStore:
    """In memory, with the real validation: the same contract as CommitmentStore for the tool and the gate."""

    def __init__(self) -> None:
        self.rows: dict[str, Commitment] = {}
        self.logs: list[dict] = []
        self.calls: list[dict] = []

    async def create_idempotent(self, **kw) -> Commitment:
        self.calls.append(kw)
        what = normalise_what(kw["what"])
        if len(what) < 8:
            raise CommitmentRefused("what too short")
        due_at = validate_due_at(kw["due_at"], kw.get("now"))
        key = dedupe_key(kw.get("turn_id"), what)
        if key in self.rows:
            from dataclasses import replace

            return replace(self.rows[key], inserted=False)
        row = Commitment(
            id=uuid.uuid4(),
            short_id=len(self.rows) + 1,
            status=kw.get("status", "open"),
            kind=kw.get("kind", "action"),
            what=what,
            promise_text=kw["promise_text"],
            due_at=due_at,
            grace_seconds=kw.get("grace_seconds", 900),
            consent=kw.get("consent", "none"),
            source_kind=kw["source_kind"],
            detected_by=kw["detected_by"],
            session_id=kw.get("session_id"),
            turn_id=kw.get("turn_id"),
            artifact_kind=None,
            artifact_id=None,
            close_reason=None,
            created_at=datetime.now(UTC),
        )
        self.rows[key] = row
        return row

    async def record_gate_turn(self, rows: list[dict], log: dict) -> list[Commitment | None]:
        written: list[Commitment | None] = []
        for spec in rows:
            try:
                written.append(await self.create_idempotent(**spec))
            except CommitmentRefused:
                written.append(None)
        self.logs.append(log)
        return written

    async def log_gate_turn(self, **kw) -> None:
        self.logs.append(kw)


async def test_commit_tool_records_a_row_and_its_output_names_the_id():
    from nous.api.tools import create_commit_tool

    store = FakeStore()
    commit = create_commit_tool(store)
    out = await commit(what="relaunch the chain", due="in 2 hours", consent="silence", _session_id="s", _turn_number=3)
    text = out["content"][0]["text"]
    assert not out.get("is_error")
    assert text.startswith("Commitment C-0001 recorded: relaunch the chain")
    assert parse_artifact("commit", text) == ("commitment", "C-0001")  # the gate reads the tool's own output
    assert store.calls[0]["detected_by"] == "tool" and store.calls[0]["consent"] == "silence"
    again = await commit(what="relaunch  the chain", due="in 2 hours", _session_id="s", _turn_number=3)
    assert "C-0001" in again["content"][0]["text"] and len(store.rows) == 1  # idempotent within the turn


@pytest.mark.parametrize("due", ["2020-01-01T00:00:00+00:00", "2099-01-01T00:00:00+00:00", "whenever"])
async def test_commit_tool_rejects_past_far_and_unreadable_dates(due):
    from nous.api.tools import create_commit_tool

    store = FakeStore()
    out = await create_commit_tool(store)(what="relaunch the chain", due=due, _session_id="s", _turn_number=1)
    assert out["is_error"] is True and store.rows == {}


async def test_commit_tool_rejects_a_what_too_short_to_check():
    from nous.api.tools import create_commit_tool

    out = await create_commit_tool(FakeStore())(what="do it", due="in 1 hour", _session_id="s", _turn_number=1)
    assert out["is_error"] is True


async def test_commit_tool_refuses_an_unknown_consent():
    from nous.api.tools import create_commit_tool

    store = FakeStore()
    out = await create_commit_tool(store)(what="relaunch the chain", due="in 1 hour", consent="silent")
    assert out["is_error"] is True and "consent" in out["content"][0]["text"] and store.rows == {}


async def test_commit_tool_reports_a_failed_ledger_write_as_an_error(caplog):
    from nous.api.tools import create_commit_tool

    class Down(FakeStore):
        async def create_idempotent(self, **kw):
            raise RuntimeError("database is down")

    with caplog.at_level("WARNING", logger="nous.api.tools"):
        out = await create_commit_tool(Down())(what="relaunch the chain", due="in 1 hour", _turn_number=1)
    assert out["is_error"] is True and "may not have been recorded" in out["content"][0]["text"]
    assert "ledger write failed" in caplog.text


async def test_commit_tool_writes_a_new_row_when_the_turn_key_names_a_closed_one():
    """A continuation thread is discarded before each step, so its turn numbers restart: the same key can name a
    row an earlier step wrote and the owner has since closed."""
    from dataclasses import replace

    from nous.api.tools import create_commit_tool

    store = FakeStore()
    tool = create_commit_tool(store)
    await tool(what="check the deploy of PR 812", due="in 1 hour", _session_id="intent-x", _turn_number=1)
    [key] = store.rows
    store.rows[key] = replace(store.rows[key], status="done")
    out = await tool(what="check the deploy of PR 812", due="in 1 hour", _session_id="intent-x", _turn_number=1)
    assert "C-0002" in out["content"][0]["text"] and len(store.rows) == 2


async def test_commit_tool_takes_its_source_kind_from_the_turn_kind():
    from nous.api.tools import create_commit_tool

    store = FakeStore()
    tool = create_commit_tool(store)
    await tool(what="propose the review date", due="in 1 day", _context_kind="subtask", _turn_number=1)
    await tool(what="check the triage result", due="in 1 day", _context_kind="heartbeat_triage", _turn_number=1)
    await tool(what="answer the owner's question", due="in 1 day", _context_kind="interactive", _turn_number=1)
    assert [c["source_kind"] for c in store.calls] == ["subtask", "heartbeat", "chat_turn"]


# Store against Postgres: skipped until brain.commitments exists (migration 107).


async def _ledger_tables_exist(db) -> bool:
    from sqlalchemy import text

    async with db.session() as s:
        found = (await s.execute(text("SELECT to_regclass('brain.commitments') IS NOT NULL"))).scalar()
    return bool(found)


MIGRATION_107 = Path(__file__).resolve().parents[1] / "sql" / "migrations" / "107_commitments.sql"
NO_MIGRATION_YET = "sql/migrations/107_commitments.sql is not in the tree yet (its DDL: docs/features/F111)"


@pytest.fixture
async def store(db):
    if not MIGRATION_107.exists():
        pytest.skip(NO_MIGRATION_YET)
    assert await _ledger_tables_exist(db), "migration 107 is in the tree but brain.commitments does not exist"
    agent = f"pg-test-{uuid.uuid4().hex[:8]}"
    yield CommitmentStore(db, agent)
    from sqlalchemy import text

    async with db.session() as s:
        await s.execute(text("DELETE FROM brain.commitments WHERE agent_id = :a"), {"a": agent})
        await s.execute(text("DELETE FROM nous_system.promise_gate_log WHERE agent_id = :a"), {"a": agent})
        await s.commit()


def _new(**over) -> dict:
    base = dict(
        source_kind="chat_stream",
        detected_by="gate",
        promise_text="I'll remove it (option A) unless you object.",
        what="I'll remove it (option A) unless you object.",
        due_at=datetime.now(UTC) + timedelta(hours=1),
        session_id="s1",
        turn_id="t1",
        consent="silence",
    )
    return {**base, **over}


def test_the_migration_file_is_the_ddl_the_feature_doc_records():
    if not MIGRATION_107.exists():
        pytest.skip(NO_MIGRATION_YET)
    doc = (Path(__file__).resolve().parents[1] / "docs" / "features" / "F111-obligation-ledger.md").read_text()
    ddl = doc.split("```sql\n", 1)[1].split("```", 1)[0]
    assert MIGRATION_107.read_text() == ddl


@pytest.mark.postgres_only
async def test_store_insert_is_idempotent_on_the_dedupe_key(store):
    first = await store.create_idempotent(**_new())
    second = await store.create_idempotent(**_new())
    assert first.inserted and not second.inserted and first.id == second.id
    third = await store.create_idempotent(**_new(turn_id="t2"))
    assert third.id != first.id and third.short_id > first.short_id
    assert first.status == "open" and first.consent == "silence" and first.label.startswith("C-")


@pytest.mark.postgres_only
async def test_store_refuses_a_past_or_far_due_at(store):
    with pytest.raises(CommitmentRefused):
        await store.create_idempotent(**_new(due_at=datetime.now(UTC) - timedelta(minutes=5)))
    with pytest.raises(CommitmentRefused):
        await store.create_idempotent(**_new(due_at=datetime.now(UTC) + timedelta(days=120)))


@pytest.mark.postgres_only
async def test_store_link_mark_and_watchable(store):
    now = datetime.now(UTC)
    late = await store.create_idempotent(**_new(turn_id="late", due_at=now + timedelta(minutes=1)))
    linked = await store.create_idempotent(**_new(turn_id="linked", due_at=now + timedelta(minutes=1)))
    awaiting = await store.create_idempotent(
        **_new(turn_id="awaiting", kind="awaiting_user", due_at=now + timedelta(minutes=1))
    )
    dropped = await store.create_idempotent(**_new(turn_id="dropped", due_at=now + timedelta(minutes=1)))
    deferred = await store.create_idempotent(**_new(turn_id="deferred", due_at=now + timedelta(minutes=1)))
    backed = await store.link_artifact(linked.short_id, "dag", "5c3cedb0")
    assert backed is not None and backed.status == "backed" and backed.artifact_id == "5c3cedb0"
    row, changed = await store.mark(dropped.short_id, "cancelled", close_reason="owner_drop")
    assert changed and row.status == "cancelled"
    row, changed = await store.mark(dropped.short_id, "cancelled")
    assert not changed and row.status == "cancelled"  # a repeat /drop changes nothing
    await store.mark(deferred.short_id, "deferred")
    assert await store.mark(999_999_999, "done") == (None, False)
    after_grace = now + timedelta(minutes=1, seconds=901)
    watch = await store.list_watchable(now=after_grace)
    assert [c.id for c in watch] == [late.id]  # never backed, awaiting_user, cancelled or deferred rows
    assert await store.list_watchable(now=now) == []  # nothing is overdue before due + grace
    assert awaiting.kind == "awaiting_user"


@pytest.mark.postgres_only
async def test_store_records_a_gate_turn_in_one_transaction(store):
    rows = await store.record_gate_turn(
        [_new(turn_id="g1"), _new(turn_id="g1", what="x"), _new(turn_id="g1", what="I'll check back tomorrow.")],
        {"path": "stream", "mode": "warn", "detected": 3, "backed": 0, "unbacked": 3, "outcome": "footer"},
    )
    assert rows[0] is not None and rows[1] is None and rows[2] is not None  # the refused one is skipped
    stats = await store.stats_week()
    assert stats["gate"]["turns"] == 1 and stats["commitments"] == {"open": 2}


@pytest.mark.postgres_only
async def test_a_failed_gate_turn_writes_nothing(store, monkeypatch):
    import nous.brain.commitments as commitments

    monkeypatch.setattr(commitments, "_LOG", commitments.text("INSERT INTO nous_system.no_such_table VALUES (1)"))
    with pytest.raises(Exception):
        await store.record_gate_turn(
            [_new(turn_id="g2")],
            {"path": "stream", "mode": "shadow", "detected": 1, "backed": 0, "unbacked": 1, "outcome": "clean"},
        )
    monkeypatch.undo()
    assert (await store.stats_week())["commitments"] == {}  # the row before the failing log insert rolled back


@pytest.mark.postgres_only
async def test_store_logs_the_gate_and_counts_the_week(store):
    await store.log_gate_turn(path="stream", mode="shadow", detected=1, backed=0, unbacked=1, outcome="clean")
    await store.log_gate_turn(path="run_turn", mode="warn", detected=2, backed=1, unbacked=1, outcome="footer")
    await store.log_gate_turn(path="stream", mode="warn", detected=0, backed=0, unbacked=0, outcome="error")
    await store.create_idempotent(**_new())
    stats = await store.stats_week()
    assert stats["gate"] == {"turns": 3, "detected": 3, "backed": 1, "unbacked": 2, "footers": 1, "errors": 1}
    assert stats["commitments"] == {"open": 1}


# ---- 5. settings -------------------------------------------------------------------------------------------------


def test_tool_context_policy_mode_stays_warn_in_code():
    """Regression guard (spec D.7 item 5): the ledger work never changes the tool-policy default."""
    assert Settings.model_fields["tool_context_policy_mode"].default == "warn"


def test_promise_gate_defaults():
    fields = Settings.model_fields
    assert fields["promise_gate_enabled"].default is True
    assert fields["promise_gate_mode"].default == "shadow"
    assert fields["promise_gate_max_reentries"].default == 2
    assert fields["promise_gate_default_due_minutes"].default == 60
    assert fields["promise_gate_grace_seconds"].default == 900
    assert fields["promise_gate_classifier_enabled"].default is False


# ---- 16. claim verifier ------------------------------------------------------------------------------------------


def test_the_commit_wording_does_not_trip_the_vcs_commit_claim():
    from nous.cognitive.claim_verifier import ClaimVerifier

    [promise] = PromiseDetector().scan("I'll remove it (option A) unless you object.", now=NOW)
    texts = [
        REAL_RESULTS["commit"][0],
        render_footer(["C-0042"], promise, NOW),
        "I've committed to relaunching it at 18:15 UTC (C-0042).",
    ]
    for reply in texts:
        result = ClaimVerifier().verify(reply, ["commit"], None)
        assert "vcs_commit" not in [c.kind for c in result.claims], (reply, result.claims)
        assert result.verified, (reply, result.violations)


def test_settings_object_reads_the_gate_mode():
    s = SimpleNamespace(promise_gate_enabled=False, promise_gate_mode="warn", promise_gate_default_due_minutes=60)
    from nous.cognitive.promise_gate import PromiseGate

    assert PromiseGate(FakeStore(), s).mode == "off"
