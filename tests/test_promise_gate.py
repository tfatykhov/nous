"""F111 PR A: the promise detector, the backing resolver, the commit tool, the store and the settings (spec D.7 1-5).

The store tests that need ``brain.commitments`` (migration 107) run on Postgres only.
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
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


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


def test_a_clock_time_with_tomorrow_or_today_is_read_on_that_day():
    """Codex P2 on bdb41254: the clock time returned before ``tomorrow`` was read, so tomorrow 18:15 became today."""
    nine = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)
    tomorrow = datetime(2026, 11, 11, tzinfo=UTC)
    detector = PromiseDetector(default_due_minutes=60)
    [later] = detector.scan("I'll relaunch the service tomorrow at 18:15 UTC.", now=nine)
    assert later.due_parsed and later.due_at == tomorrow.replace(hour=18, minute=15)
    [earlier] = detector.scan("I'll relaunch the service tomorrow at 08:00 UTC.", now=nine)
    assert earlier.due_parsed and earlier.due_at == tomorrow.replace(hour=8)  # not two days on
    [plain] = detector.scan("I'll relaunch the service at 18:15 UTC.", now=nine)
    assert plain.due_parsed and plain.due_at == nine.replace(hour=18, minute=15)
    [today] = detector.scan("I'll relaunch the service today at 18:15 UTC.", now=nine)
    assert today.due_parsed and today.due_at == nine.replace(hour=18, minute=15)
    # "today" at a time already past: the default, not tomorrow (a day late to notice the miss).
    [past_today] = detector.scan("I'll relaunch the service today at 08:00 UTC.", now=nine)
    assert not past_today.due_parsed and past_today.due_at == nine + timedelta(minutes=60)
    [after] = detector.scan("I'll relaunch the service at 08:00 UTC tomorrow.", now=nine)
    assert after.due_at == tomorrow.replace(hour=8)
    # A day word elsewhere in the sentence does not qualify the clock time.
    [elsewhere] = detector.scan("I'll check at 10:00 UTC whether tomorrow's run is queued.", now=nine)
    assert elsewhere.due_parsed and elsewhere.due_at == nine.replace(hour=10)


# Review finding 4, run at 2026-11-11 09:00 UTC: each sentence, the deadline it names, and whether it was parsed.
NOV11_0900 = datetime(2026, 11, 11, 9, 0, tzinfo=UTC)
DEADLINES = [
    ("I'll deploy the fix on 2026-11-20 at 18:15 UTC.", datetime(2026, 11, 20, 18, 15, tzinfo=UTC), True),
    ("Tomorrow I'll relaunch at 18:15 UTC.", datetime(2026, 11, 12, 18, 15, tzinfo=UTC), True),
    ("I'll relaunch tomorrow at around 18:15 UTC.", datetime(2026, 11, 12, 18, 15, tzinfo=UTC), True),
    ("I'll relaunch it tomorrow by about 18:15 UTC.", datetime(2026, 11, 12, 18, 15, tzinfo=UTC), True),
    ("I'll send the summary by Friday.", NOV11_0900 + timedelta(minutes=60), False),
    ("I'll send the summary next week.", NOV11_0900 + timedelta(minutes=60), False),
    # A clock time with a date the parser cannot bind: the unparsed default, never a parsed "today".
    ("I'll send the summary on Friday at 18:15 UTC.", NOV11_0900 + timedelta(minutes=60), False),
    ("I'll send the summary next week at 18:15 UTC.", NOV11_0900 + timedelta(minutes=60), False),
    ("I'll send the summary on Nov 20 at 18:15 UTC.", NOV11_0900 + timedelta(minutes=60), False),
    ("I'll deploy it the day after tomorrow at 18:15 UTC.", NOV11_0900 + timedelta(minutes=60), False),
    # A time of day the parser does not read (am/pm, no zone) next to "tomorrow": not "now + 1 day", parsed.
    ("I'll deploy it tomorrow at 6 pm UTC.", NOV11_0900 + timedelta(minutes=60), False),
    ("I'll relaunch it tomorrow at 18:15.", NOV11_0900 + timedelta(minutes=60), False),
    # Re-review B: a preposition and HH:MM is a time without a zone too; with no zone it is not read.
    ("I'll relaunch the chain at 18:15.", NOV11_0900 + timedelta(minutes=60), False),
    # A number before a word is no month-day date: "tomorrow" still binds.
    ("I'll merge the 2 decision PRs tomorrow.", datetime(2026, 11, 12, 9, 0, tzinfo=UTC), True),
    ("I'll restart the 2 decoders at 18:15 UTC.", datetime(2026, 11, 11, 18, 15, tzinfo=UTC), True),
    # An explicit date with no time it binds to, or a past one: the default too.
    ("I'll deploy the fix on 2026-11-20.", NOV11_0900 + timedelta(minutes=60), False),
    ("I'll deploy the fix on 2026-11-02 at 18:15 UTC.", NOV11_0900 + timedelta(minutes=60), False),
]


@pytest.mark.parametrize(("sentence", "due_at", "parsed"), DEADLINES, ids=[d[0][:45] for d in DEADLINES])
def test_each_deadline_of_the_review_table(sentence, due_at, parsed):
    [promise] = PromiseDetector(default_due_minutes=60).scan(sentence, now=NOV11_0900)
    assert (promise.due_at, promise.due_parsed) == (due_at, parsed)


def test_a_relative_time_counts_from_the_given_now():
    """Review Minor 7: parse_when used the wall clock, so a replay at a stored reply's time was hours off."""
    [promise] = PromiseDetector().scan("I plan to deploy the fix in 2 hours.", now=NOW)
    assert promise.due_parsed and promise.due_at == NOW + timedelta(hours=2)
    [minutes] = PromiseDetector().scan("I'll check in 5 minutes.", now=NOW)
    assert minutes.due_at == NOW + timedelta(minutes=5)


def test_a_huge_relative_time_is_capped_not_an_overflow():
    """Review Minor 7: "in 3000000 days" raised OverflowError out of scan and lost every promise of the reply."""
    promises = PromiseDetector().scan("I'll retry in 3000000 days. I'll check back tomorrow.", now=NOW)
    assert len(promises) == 2 and NOW < promises[0].due_at <= NOW + timedelta(days=92)


# ---- 2. negatives ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("case", NEGATIVES, ids=lambda c: c["text"][:40])
def test_each_negative_is_no_promise(case):
    assert PromiseDetector().scan(case["text"], now=NOW) == []


@pytest.mark.parametrize(
    "text",
    [
        "I'll send it to Dr. Smith tomorrow.",
        "I'll rerun the slow suites, e.g. the DAG one, tomorrow.",
        "I'll rerun the slowest one, i.e. the DAG suite, tomorrow.",
        "I'll compare A vs. B tomorrow.",
        "I'll fix ticket No. 3 tomorrow.",
    ],
)
def test_an_abbreviation_does_not_split_a_promise(text):
    """Review Minor 8: the split after "Dr." left neither half with a future form and a marker."""
    [promise] = PromiseDetector().scan(text, now=NOW)
    assert promise.text == text


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


@pytest.mark.parametrize("unit", ["“a ", "„alpha“ "], ids=["unclosed-curly-quotes", "german-quotes"])
def test_a_200_kb_line_of_quote_marks_scans_in_milliseconds(unit):
    """Review finding 2: the quoted-string pattern rescanned the line from every unclosed “ (193 KB took 35 s of a
    blocked event loop); German quoting („alpha“, where “ closes) hit the same path."""
    reply = unit * (200_000 // len(unit))
    start = time.perf_counter()
    PromiseDetector().scan(reply, now=NOW)
    assert time.perf_counter() - start < 0.1


def test_the_detector_reads_the_last_32_kb_only():
    from nous.cognitive.promise_gate import SCAN_MAX_CHARS

    filler = "This paragraph explains the result. " * (SCAN_MAX_CHARS // 30)
    early = PromiseDetector().scan("I'll check back tomorrow. " + filler, now=NOW)
    late = PromiseDetector().scan(filler + "I'll check back tomorrow.", now=NOW)
    assert early == [] and len(late) == 1


def test_a_long_reply_whose_cut_lands_in_a_code_block_keeps_its_closing_promise():
    """Re-review A: the raw reply was cut to 32 KB before the code was stripped, so the tail began inside a fence,
    its closing marker read as an opening one, and the promise after it was dropped as code. Strip first, cap the
    prose."""
    from nous.cognitive.promise_gate import SCAN_MAX_CHARS

    code = "print('step')\n" * (42_000 // 14)
    reply = f"Intro.\n```python\n{code}```\nSummary. I'll check back tomorrow."
    assert len(reply) > SCAN_MAX_CHARS and reply.rfind("```python") < len(reply) - SCAN_MAX_CHARS
    [promise] = PromiseDetector().scan(reply, now=NOW)
    assert promise.text == "I'll check back tomorrow."


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
    backed, unbacked = BackingResolver().split([promise], [_tr(tool, text, what="relaunch the chain")])
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


def test_a_lone_commit_backs_only_the_promise_it_names():
    """Review Minor 10: one commit(what="deploy staging") backed the email promise too, which left no row."""
    promises = PromiseDetector().scan("I'll deploy staging tomorrow. I'll email the report tomorrow.", now=NOW)
    commit = _tr("commit", REAL_RESULTS["commit"][0], what="deploy staging", due="in 1 day")
    backed, unbacked = BackingResolver().split(promises, [commit])
    assert [p.text for p, _ in backed] == ["I'll deploy staging tomorrow."]
    assert [p.text for p in unbacked] == ["I'll email the report tomorrow."]
    schedule = _tr("schedule_task", REAL_RESULTS["schedule_task"][0], task="nightly jobs")
    assert BackingResolver().split(promises, [schedule])[1] == []  # any other lone artifact still backs them all


async def test_the_same_promise_twice_is_counted_and_named_once():
    """Review Minor 9: two identical sentences gave one row but unbacked=2 and a footer naming C-0001 twice."""
    store = FakeStore()
    reply = "I'll relaunch it tomorrow. I'll relaunch it tomorrow."
    outcome = await _gate("warn", store).run(
        reply, [], path="stream", session_id="s", turn_id="t", context_kind=None, now=NOW
    )
    assert (outcome.detected, outcome.unbacked, store.logs[0]["unbacked"]) == (1, 1, 1)
    assert outcome.footer.count("C-0001") == 2  # once in the ids, once in "/drop C-0001"
    assert "C-0001, C-0001" not in outcome.footer and len(store.rows) == 1


def test_two_proposals_match_their_promises_by_their_real_keys():
    """Codex P2 on b946f69f: propose_action has only tool, arguments and rationale, none of which was read, so with
    two artifacts no proposal could ever back a promise."""
    reply = "I'll deploy staging tomorrow. I'll email the weekly report tomorrow."
    promises = PromiseDetector().scan(reply, now=NOW)
    staged = REAL_RESULTS["propose_action"][0]
    results = [
        ToolResult(
            tool_name="propose_action",
            result=staged,
            arguments={
                "tool": "schedule_task",
                "arguments": {"task": "deploy staging", "when": "in 1 day"},
                "rationale": "Needs approval.",
            },
        ),
        ToolResult(
            tool_name="propose_action",
            result=staged.replace("9f8e7d6c", "1a2b3c4d"),
            arguments={
                "tool": "send_email",
                "arguments": {"subject": "Weekly report", "body": "Attached."},
                "rationale": "Needs approval.",
            },
        ),
    ]
    backed, unbacked = BackingResolver().split(promises, results)
    assert unbacked == [] and [a.id for _, a in backed] == ["9f8e7d6c", "1a2b3c4d"]


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
    footer = render_footer([("C-0042", promise.due_at)], NOW)
    assert "\n" not in footer and "C-0042" in footer and "in 60 min (18:45 UTC)" in footer
    assert "/drop C-0042" in footer and "committed" not in footer.lower()


async def test_the_footer_shows_the_deadline_the_row_stores():
    """Codex P2 on b946f69f: the row is clamped to MAX_DUE_DAYS, the footer read the unclamped promise."""
    store = FakeStore()
    outcome = await _gate("warn", store).run(
        "I'll revisit the pricing in 20 weeks.",
        [],
        path="stream",
        session_id="s",
        turn_id="t",
        context_kind=None,
        now=NOW,
    )
    [row] = store.rows.values()
    assert row.due_at < NOW + timedelta(days=90)
    assert f"due {row.due_at:%Y-%m-%d %H:%M} UTC" in outcome.footer


async def test_a_footer_for_two_commitments_gives_each_its_own_deadline():
    """Codex on 793faa6a: the footer gave every listed id the first row's deadline."""
    store = FakeStore()
    outcome = await _gate("warn", store).run(
        "I'll deploy staging in 2 hours. I'll email the report in 30 hours.",
        [],
        path="stream",
        session_id="s",
        turn_id="t",
        context_kind=None,
        now=NOW,
    )
    assert "\n" not in outcome.footer
    assert "C-0001 due in 2 h (19:45 UTC)" in outcome.footer
    assert "C-0002 due 2026-11-11 23:45 UTC" in outcome.footer
    assert "/drop C-0001" in outcome.footer


@pytest.mark.parametrize(("tool", "key"), [("submit_final_report", "summary"), ("resolve_intention", "note")])
async def test_a_terminal_tool_turn_is_scanned_on_its_report(tool, key):
    """Codex P2 on b946f69f: such a turn returns "Report submitted.", so the report's promise was never seen."""
    store = FakeStore()
    report = _tr(tool, "accepted", **{key: "Checked the hero section. I'll redeploy the site tomorrow."})
    outcome = await _gate(store=store).run(
        "Report submitted.", [report], path="run_turn", session_id="s", turn_id="t", context_kind="subtask", now=NOW
    )
    assert outcome.detected == 1 and [r.what for r in store.rows.values()] == ["I'll redeploy the site tomorrow."]
    failed = _tr(tool, error="bad payload", **{key: "I'll redeploy the site tomorrow."})
    outcome = await _gate(store=FakeStore()).run(
        "Report submitted.", [failed], path="run_turn", session_id="s", turn_id="t2", context_kind="subtask", now=NOW
    )
    assert outcome.detected == 0  # a refused call carried no report


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


def _params(**over) -> dict:
    """The real store's bind values (no database needed: ``_params`` runs before any SQL)."""
    spec = {
        "source_kind": "chat_turn",
        "detected_by": "gate",
        "promise_text": "I'll relaunch it tomorrow.",
        "what": "I'll relaunch it tomorrow.",
        "due_at": datetime.now(UTC) + timedelta(hours=1),
        "turn_id": "t1",
        **over,
    }
    return CommitmentStore(None, "a")._params(**spec)


def test_a_key_in_a_promise_is_redacted_in_both_columns_and_the_key():
    """Review finding 3: the ledger stored ``sk-ant-…`` verbatim in ``what`` and ``promise_text``."""
    key = "sk-ant-api03-" + "A" * 40
    text = f"I'll retry with token {key} tomorrow."
    params = _params(what=text, promise_text=text)
    assert key not in params["what"] and key not in params["promise_text"]
    assert "[REDACTED_KEY]" in params["what"] and "[REDACTED_KEY]" in params["promise_text"]
    assert params["dedupe_key"] == dedupe_key("t1", params["what"])  # keyed on the redacted text


def test_a_secret_straddling_the_cut_is_redacted_before_the_cut():
    """Cut first, the key's first characters ("sk-ant-api03-AAAA") would match no pattern and be stored."""
    from nous.brain.commitments import PROMISE_TEXT_MAX, WHAT_MAX

    key = "sk-ant-api03-" + "A" * 40
    what = "I'll rotate the key tomorrow, " + "x" * (WHAT_MAX - 30 - 18) + " " + key
    promise_text = "I'll rotate the key tomorrow, " + "x" * (PROMISE_TEXT_MAX - 30 - 18) + " " + key
    params = _params(what=what, promise_text=promise_text)
    assert "sk-ant" not in params["what"] and "sk-ant" not in params["promise_text"]


@pytest.mark.parametrize("cut", ["what", "sentence", "cap"])
def test_a_secret_straddling_a_detector_cut_is_redacted_whole(cut):
    """Re-review 3 and Codex on 793faa6a: the detector cut the sentence to 500 and ``what`` to 300 before anything
    was redacted, so a key across either cut reached the store as a fragment no pattern matches. Re-review G: the
    32 KB prose cap is a third cut; the partial sentence it leaves at the tail's start is not scanned."""
    from nous.brain.commitments import PROMISE_TEXT_MAX, WHAT_MAX
    from nous.cognitive.promise_gate import SCAN_MAX_CHARS

    key = "sk-ant-api03-" + "A" * 40
    if cut == "cap":
        sentence = f"The key {key} is stale and I'll rotate it tomorrow."
        cap_at = len("The key ") + 25  # the cap falls 25 characters into the key
        filler = "This paragraph explains the result. "
        closing = "I'll check back tomorrow."  # a whole promise after the cut is still read
        after_len = SCAN_MAX_CHARS - (len(sentence) - cap_at) - 1 - len(closing)
        after = filler * (after_len // len(filler))
        reply = filler * 1500 + sentence + " " + after.ljust(after_len) + closing
        assert len(reply) - SCAN_MAX_CHARS == len(filler) * 1500 + cap_at
        promises = PromiseDetector().scan(reply, now=NOW)
        assert [p.text for p in promises] == [closing]
        for promise in promises:
            params = _params(what=promise.what, promise_text=promise.text)
            for text in (promise.what, promise.text, params["what"], params["promise_text"]):
                assert "sk-ant" not in text and "AAAA" not in text, text
        return
    head = "I'll rotate the key tomorrow, "
    boundary = WHAT_MAX if cut == "what" else PROMISE_TEXT_MAX
    sentence = head + "x" * (boundary - len(head) - 10) + " " + key + " now."
    assert sentence.index(key) < boundary < sentence.index(key) + len(key)
    [promise] = PromiseDetector().scan(sentence, now=NOW)
    params = _params(what=promise.what, promise_text=promise.text)
    for text in (promise.what, promise.text, params["what"], params["promise_text"]):
        assert "sk-ant" not in text and "AAAA" not in text, text
    assert "[REDACTED" in promise.text  # the marker may itself be cut; the key never is


def test_control_characters_are_stripped_and_whitespace_kept():
    text = "I'll relaunch\x00 it ‮tomorrow\x1b[31m.\tthen\nreport"
    params = _params(what=text, promise_text=text)
    for column in ("what", "promise_text"):
        assert not any(ord(c) < 32 or c == "‮" for c in params[column]), params[column]
    assert params["what"] == "I'll relaunch it tomorrow[31m. then report"


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


@pytest.mark.parametrize(
    "due",
    ["Friday 9am EST", "tomorrow at 18:15 UTC", "2026-11-20 18:15", "next monday 8am", "in 2 hours please", "soon"],
)
async def test_commit_tool_accepts_only_the_two_documented_forms(due):
    """Review finding 4: the fuzzy dateutil fallback read "Friday 9am EST" as 09:00 UTC (the zone dropped) and
    "tomorrow at 18:15 UTC" as today."""
    from nous.api.tools import create_commit_tool

    store = FakeStore()
    out = await create_commit_tool(store)(what="send the summary", due=due, _session_id="s", _turn_number=1)
    assert out["is_error"] is True and store.calls == []


async def test_commit_tool_reads_an_offset_time_and_a_relative_one():
    from nous.api.tools import create_commit_tool

    store = FakeStore()
    tool = create_commit_tool(store)
    at = (datetime.now(UTC) + timedelta(days=3)).replace(microsecond=0)
    out = await tool(what="send the summary", due=at.astimezone().isoformat(), _session_id="s", _turn_number=1)
    assert not out.get("is_error") and store.calls[0]["due_at"] == at
    assert store.calls[0]["evidence"] == {"due_parsed": True}
    before = datetime.now(UTC)
    await tool(what="send the other summary", due="in 3 hours", _session_id="s", _turn_number=1)
    assert before + timedelta(hours=3) <= store.calls[1]["due_at"] <= datetime.now(UTC) + timedelta(hours=3)


@pytest.mark.parametrize("value", ["false", "true", 1, "yes"])
async def test_commit_tool_reads_awaiting_owner_only_as_a_real_true(value):
    """Review Minor 11: the string "false" made the row awaiting_user, which the watcher never reports."""
    from nous.api.tools import create_commit_tool

    store = FakeStore()
    await create_commit_tool(store)(what="send the summary", due="in 1 hour", awaiting_owner=value, _turn_number=1)
    assert store.calls[0]["kind"] == "action"
    await create_commit_tool(store)(what="merge the PR once approved", due="in 1 hour", awaiting_owner=True)
    assert store.calls[1]["kind"] == "awaiting_user"


async def test_commit_tool_stores_the_configured_grace_and_survives_a_huge_amount():
    """Review Minor 11: the row got the store's 900 s, not NOUS_PROMISE_GATE_GRACE_SECONDS; and "in 99999999999
    days" raised OverflowError out of the tool."""
    from nous.api.tools import create_commit_tool

    store = FakeStore()
    tool = create_commit_tool(store, grace_seconds=120)
    await tool(what="send the summary", due="in 1 hour", _turn_number=1)
    assert store.calls[0]["grace_seconds"] == 120
    out = await tool(what="send the summary", due="in 99999999999 days", _turn_number=2)
    assert out["is_error"] is True and len(store.calls) == 1


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


async def test_commit_retries_after_a_closed_row_land_on_one_new_row():
    """Codex P2 on bdb41254: each retry past a closed row minted a fresh key, so two identical calls in the new turn
    made two live rows."""
    from dataclasses import replace

    from nous.api.tools import create_commit_tool

    store = FakeStore()
    tool = create_commit_tool(store)
    args = {"what": "check the deploy of PR 812", "due": "in 1 hour", "_session_id": "intent-x", "_turn_number": 1}
    await tool(**args)
    [key] = store.rows
    store.rows[key] = replace(store.rows[key], status="done")
    first, second = await tool(**args), await tool(**args)
    assert "C-0002" in first["content"][0]["text"] and "C-0002" in second["content"][0]["text"]
    assert [r.status for r in store.rows.values()].count("open") == 1 and len(store.rows) == 2


async def test_commit_keys_on_the_turn_id_when_the_runner_gives_one():
    """The runner's per-turn id: a restarted turn number is a new turn, and calls without a number still dedupe."""
    from nous.api.tools import create_commit_tool

    store = FakeStore()
    tool = create_commit_tool(store)
    args = {"what": "check the deploy of PR 812", "due": "in 1 hour", "_session_id": "intent-x"}
    await tool(**args, _turn_id="turn-a")
    await tool(**args, _turn_id="turn-a")
    assert len(store.rows) == 1 and store.calls[0]["turn_id"] == "turn-a"
    await tool(**args, _turn_number=1, _turn_id="turn-b")
    assert len(store.rows) == 2


async def test_commit_tool_takes_its_source_kind_from_the_turn_kind():
    from nous.api.tools import create_commit_tool

    store = FakeStore()
    tool = create_commit_tool(store)
    await tool(what="propose the review date", due="in 1 day", _context_kind="subtask", _turn_number=1)
    await tool(what="check the triage result", due="in 1 day", _context_kind="heartbeat_triage", _turn_number=1)
    await tool(what="answer the owner's question", due="in 1 day", _context_kind="interactive", _turn_number=1)
    assert [c["source_kind"] for c in store.calls] == ["subtask", "heartbeat", "chat_turn"]


# Store against Postgres (brain.commitments, migration 107).


async def _ledger_tables_exist(db) -> bool:
    from sqlalchemy import text

    async with db.session() as s:
        found = (await s.execute(text("SELECT to_regclass('brain.commitments') IS NOT NULL"))).scalar()
    return bool(found)


MIGRATION_107 = Path(__file__).resolve().parents[1] / "sql" / "migrations" / "107_commitments.sql"


@pytest.fixture
async def store(db):
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
    doc = (Path(__file__).resolve().parents[1] / "docs" / "features" / "F111-obligation-ledger.md").read_text(
        encoding="utf-8"
    )
    ddl = doc.split("```sql\n", 1)[1].split("```", 1)[0]
    assert MIGRATION_107.read_text(encoding="utf-8") == ddl


@pytest.mark.postgres_only
async def test_store_insert_is_idempotent_on_the_dedupe_key(store):
    first = await store.create_idempotent(**_new())
    second = await store.create_idempotent(**_new())
    assert first.inserted and not second.inserted and first.id == second.id
    third = await store.create_idempotent(**_new(turn_id="t2"))
    assert third.id != first.id and third.short_id > first.short_id
    assert first.status == "open" and first.consent == "silence" and first.label.startswith("C-")


def _gate(mode: str = "shadow", store=None):
    from nous.cognitive.promise_gate import PromiseGate

    settings = SimpleNamespace(
        promise_gate_enabled=True,
        promise_gate_mode=mode,
        promise_gate_default_due_minutes=60,
        promise_gate_grace_seconds=900,
    )
    return PromiseGate(store if store is not None else FakeStore(), settings)


async def test_the_gate_stores_whether_each_deadline_was_parsed():
    """Review finding 4: Promise.due_parsed was dropped, so no row told a named deadline from the default."""
    store = FakeStore()
    reply = "I'll remove it (option A) unless you object. I'll relaunch it tomorrow at 18:15 UTC."
    await _gate(store=store).run(reply, [], path="stream", session_id="s", turn_id="t", context_kind=None, now=NOW)
    assert [c["evidence"] for c in store.calls] == [{"due_parsed": False}, {"due_parsed": True}]


@pytest.mark.postgres_only
async def test_store_keeps_the_evidence_json(store):
    from sqlalchemy import text

    row = await store.create_idempotent(**_new(turn_id="ev", evidence={"due_parsed": False}))
    async with store._db.session() as s:
        evidence = (
            await s.execute(
                text("SELECT evidence->>'due_parsed' FROM brain.commitments WHERE id = :id"), {"id": row.id}
            )
        ).scalar()
    assert evidence == "false"


@pytest.mark.postgres_only
async def test_store_writes_a_promise_with_a_nul_in_it(store):
    """Postgres text cannot hold a NUL: before review finding 3 this insert, and the turn's whole gate transaction,
    failed."""
    text = "I'll relaunch\x00 it \u202etomorrow\x1b[31m."
    rows = await store.record_gate_turn(
        [_new(turn_id="nul", what=text, promise_text=text)],
        {"path": "stream", "mode": "shadow", "detected": 1, "backed": 0, "unbacked": 1, "outcome": "clean"},
    )
    assert rows[0] is not None and "\x00" not in rows[0].what and "\u202e" not in rows[0].promise_text


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
        render_footer([("C-0042", promise.due_at)], NOW),
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


# ---- F111 PR B1: the passive with a deadline, and the C-1 class (plan §6) ------------------------------------------

# A future passive of an action verb with a `by <time>` deadline is the agent's own promise, when the existing _due_at
# parser reads the deadline (plan §6: "a deadline phrase the existing _due_at parser resolves", due_parsed true).
PASSIVE_HITS = [
    ("It'll be deployed by tomorrow.", True),
    ("The fix will be deployed by tomorrow at 09:00 UTC.", True),
    ("The new index is going to be built by tomorrow.", True),
    ("The chain will be relaunched by 18:15 UTC.", True),
]
PASSIVE_MISSES = [
    "Your order will be delivered by Friday.",
    "The PR was merged by Tim.",
    "It will be reviewed by Tim.",
    "This was sent by morning courier.",
    "The fix will not be deployed by tomorrow.",
    # Deadlines _due_at does not read: a hit would get the default due, and the watcher would ping 75 minutes later
    # about something due days away (reported on the PR).
    "The fix will be deployed by morning.",
    "The summary will be sent by November 20.",
    # Not in ACTION_VERBS ("do", "ready" are not), so the rule does not reach them (reported on the PR).
    "It'll be done by tomorrow.",
    "The report is going to be ready by 5pm.",
    # Review I4 (and Codex on 391e7849): someone else does it, or it is negated, before the deadline.
    "It will be deployed by Tim by tomorrow.",
    "The invoice will be sent by the vendor by tomorrow.",
    "The cache will be deleted by the vendor by 09:00 UTC.",
    "The app will be deployed by Tim, not by tomorrow.",
]


@pytest.mark.parametrize(("sentence", "parsed"), PASSIVE_HITS)
def test_a_future_passive_with_a_deadline_is_one_promise(sentence, parsed):
    [promise] = PromiseDetector(default_due_minutes=60).scan(sentence, now=NOW)
    assert promise.kind == "action" and promise.due_parsed is parsed
    assert promise.due_at > NOW


@pytest.mark.parametrize("sentence", PASSIVE_MISSES)
def test_a_passive_that_is_not_the_agents_deadline_is_no_promise(sentence):
    assert PromiseDetector().scan(sentence, now=NOW) == []


@pytest.mark.parametrize("unit", ["will be sent ", "will be run by "])
def test_a_200_kb_reply_ending_in_a_long_passive_sentence_scans_in_milliseconds(unit):
    """Review I1: one regex spanning the passive head, a lazy gap and the deadline rescanned the clause from every head
    (a 31.9 KB sentence of "will be sent " took 0.8 s, of "will be run by " 1.4 s, on the event loop). Best of 3: the
    linear scan takes tens of ms on a loaded host, the quadratic one about 1 s on every run."""
    filler = "This paragraph explains the result. " * (170_000 // 36)
    reply = filler + unit * (31_900 // len(unit)) + "."
    assert len(reply) > 200_000
    timings = []
    for _ in range(3):
        start = time.perf_counter()
        PromiseDetector().scan(reply, now=NOW)
        timings.append(time.perf_counter() - start)
    assert min(timings) < 0.1, timings


def test_the_pr_merged_by_then_stays_a_stage1_positive():
    """Plan K: 'The market will be closed by then.' has the same shape, so it is stage 2's to reject, not stage 1's."""
    assert len(PromiseDetector().scan("The PR will be merged by then.", now=NOW)) == 1


def test_the_c1_class_is_left_to_stage_2():
    """Plan A: no stage-1 change for the C-1 class. Stage 1 as it stands at e09791f4: the conditional deploy is
    flagged; the standing policy is not ("close" is not an action verb), and stage 2 labels it if it ever is."""
    detector = PromiseDetector()
    assert len(detector.scan("I'll deploy it when CI is green.", now=NOW)) == 1
    assert len(detector.scan("I'll send you the summary once the job finishes.", now=NOW)) == 1
    assert detector.scan("I'll close each row myself when the work is done or dropped.", now=NOW) == []
