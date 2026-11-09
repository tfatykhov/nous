"""F108 Phase 3: the shared repeat definition, error normalisation and guard classification. No database."""

from __future__ import annotations

import hashlib
import inspect
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text as sa_text

from nous.api import work_view_dashboard
from nous.api.execution_context import ExecutionContext
from nous.api.tool_policy import write_path_refusal_hint
from nous.dag.schemas import lineage_shell_check_message
from nous.observability import tool_patterns
from nous.observability.tool_patterns import (
    GUARD_FRAGMENTS,
    PATTERN_KINDS,
    Pattern,
    detect_patterns,
    guard_cause,
    normalise_error,
    pattern_counts,
)
from tests.test_work_view_dashboard import delete_agent_rows, insert_call, insert_turn

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def agent_id() -> str:
    return f"test-p3-tp-{uuid.uuid4().hex[:12]}"


@pytest_asyncio.fixture
async def cleanup(db, agent_id):
    yield
    await delete_agent_rows(db, agent_id)


def test_repeat_definition_has_one_copy():
    assert work_view_dashboard._REPEAT_KEY is tool_patterns.REPEAT_KEY
    src = inspect.getsource(work_view_dashboard)
    for literal in ("lag(is_error)", '"turn_id, tool_name, input_text"', "is_error AND prev_error"):
        assert literal not in src, literal
    assert tool_patterns.REPEAT_KEY == "turn_id, tool_name, input_text"
    assert tool_patterns.PREV_ERROR_EXPR == (
        "lag(is_error) OVER (PARTITION BY turn_id, tool_name, input_text ORDER BY call_index, id)"
    )
    assert tool_patterns.REPEATED_SQL == "prev_error IS NOT NULL"
    assert tool_patterns.RETRY_FAILED_SQL == "is_error AND prev_error"


def test_normalise_error():
    a = "No such file: /home/pirate/a/b.txt at 2026-11-09T10:00:00Z (id 3f2a9c1e-0000-4000-8000-000000000001)"
    b = "No such file: /var/tmp/zzz at 2027-01-02T03:04:05Z (id aaaaaaaa-1111-4111-8111-222222222222)"
    assert normalise_error(a) == normalise_error(b)
    assert "pirate" not in normalise_error(a)
    assert normalise_error('KeyError: "abc"') == "keyerror: <str>"
    assert normalise_error("exit code 127 after 12 s") == normalise_error("exit code 1 after 99 s")
    assert normalise_error(r"cannot open C:\Users\x\y.txt") == normalise_error(r"cannot open D:\other\z")
    assert "<hex>" in normalise_error("commit deadbeef1234 not found")
    for blank in (None, "", "   "):
        assert normalise_error(blank) == "(no error text)"
    assert len(normalise_error("word " * 1000)) <= 160
    assert len(normalise_error("x" * 5000)) == 160


def _ctx(root: bool) -> ExecutionContext:
    rid = uuid.UUID("00000000-0000-0000-0000-000000000002")
    return ExecutionContext(
        kind="subtask",
        session_id="s",
        authority="internal_only",
        intention_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        root_intention_id=rid if root else None,
    )


def test_guard_cause_codes_and_outcomes():
    assert guard_cause("refused", "context_policy", None) == "context_policy"
    assert guard_cause("refused", "internal_only", None) == "internal_only"
    assert guard_cause("gated", None, None) == "gated"
    assert guard_cause("refused", None, None) == "refused"
    assert guard_cause("refused", "duplicate", None) is None
    assert guard_cause("ran", "offered_set", None) is None
    assert guard_cause("suppressed", None, None) is None
    assert guard_cause("error", None, "FileNotFoundError: /x") is None
    assert guard_cause("error", None, None) is None


def test_guard_cause_pins_the_guard_wording(tmp_path):
    texts = [
        write_path_refusal_hint(_ctx(True), str(tmp_path), lineage_shell=True),
        write_path_refusal_hint(_ctx(False), str(tmp_path), lineage_shell=True),
        lineage_shell_check_message(["x"]),
    ]
    for text in texts:
        assert guard_cause("error", None, text) == "fragment", text

    # Inline literals in handlers that are not edited: read the sources, never lift the strings.
    sentences = {
        "nous/api/builtin_tools.py": "Refused to write '",
        "nous/api/tools.py": "is not part of this lineage",
        "nous/api/email_tools.py": "is not on the email allowlist",
    }
    for rel, sentence in sentences.items():
        assert sentence in (ROOT / rel).read_text(encoding="utf-8"), (rel, sentence)
        assert guard_cause("error", None, f"Tool error: x {sentence} y") == "fragment", sentence


def test_fragments_are_lowercase():
    assert all(f == f.lower() for f in GUARD_FRAGMENTS)


@pytest.mark.parametrize("text", ["ok: file written", "timeout after 30s", "KeyError: 'a'"])
def test_ordinary_errors_are_not_guards(text):
    assert guard_cause("error", None, text) is None


def test_normalise_error_pairs_every_quote():
    # A short quoted value must not shift the pairing of the quotes after it.
    a = normalise_error("unknown option 'x' in section 'foo'")
    assert a == "unknown option 'x' in section <str>"
    assert normalise_error("unknown option 'y' in section 'bar'") == a.replace("'x'", "'y'")
    # Short values stay as they are; 3+ character values are replaced.
    assert normalise_error("bad key 'ab'") == "bad key 'ab'"
    assert normalise_error('KeyError: "ab"') == 'keyerror: "ab"'
    assert normalise_error("Refused to write 'a': nope") == "refused to write 'a': nope"
    # Two errors differing only in a 3+ character quoted value share one key.
    assert normalise_error("no such table 'users'") == normalise_error("no such table 'orders'")
    assert normalise_error('KeyError: "abc"') == "keyerror: <str>"


def test_long_digit_runs_are_numbers_not_hex():
    assert normalise_error("exit code 12345678") == normalise_error("exit code 1") == "exit code <n>"
    assert normalise_error("commit deadbeef1234 not found") == "commit <hex> not found"
    assert normalise_error("object 12345678ab") == "object <hex>"


def test_transient_network_errors_are_not_guards():
    text = "ConnectionRefusedError: [Errno 111] Connection refused"
    assert guard_cause("error", None, text) is None
    assert guard_cause("error", None, "curl: (7) Failed to connect: Connection refused") is None
    # A real guard sentence next to one is still a guard.
    assert guard_cause("error", None, f"{text}; Refused to write 'a': it is inside the undo journal") == "fragment"


def test_runner_model_mistakes_are_not_guards_and_a_ledger_failure_is():
    offered = "Tool error: 'x' is not available in this turn. Use only the tools offered to you."
    assert guard_cause("refused", "offered_set", offered) is None
    for text in (
        "A send to the same recipients from this task is error since 2026-11-09T10:00:00; "
        "not sending again, to avoid a duplicate.",
        "Already sent at 2026-11-09T10:00:00; not sending it again.",
        "This turn already sent to the same recipients at 2026-11-09T10:00:00. If this is "
        "intentionally a second message, call again with a distinct send_label.",
    ):
        assert guard_cause("suppressed", None, text) is None, text
        assert guard_cause("refused", "duplicate", text) is None, text
    ledger = (
        "Send refused: the execution ledger could not record this send, so a duplicate "
        "cannot be ruled out. Retry later."
    )
    # Still a guard label on purpose: a ledger that cannot record a send is an infrastructure fault only the owner
    # can fix, so it goes to the owner card and a note to Nous could not help.
    assert guard_cause("suppressed", None, ledger) == "fragment"
    assert guard_cause("error", None, ledger) == "fragment"


# ---------------------------------------------------------------------------------------------------------------------
# Detection and rates (real Postgres; scratch rows under a test agent id, deleted by the ``cleanup`` fixture)
# ---------------------------------------------------------------------------------------------------------------------


def _ago(**kw) -> datetime:
    return datetime.now(UTC) - timedelta(**kw)


async def _detect(db, agent: str, **kw) -> tuple[list[Pattern], int]:
    kw.setdefault("now", datetime.now(UTC))
    async with db.session() as session:
        return await detect_patterns(session, agent, **kw)


def _of(patterns: list[Pattern], kind: str, tool: str = "bash") -> list[Pattern]:
    return [p for p in patterns if p.kind == kind and p.tool_name == tool]


async def _calls(db, agent, turn, rows, *, tool="bash", session_id="s1"):
    """rows: (input_text, is_error, error_text) tuples inserted as one turn's calls in order."""
    for i, (inp, err, etext) in enumerate(rows):
        await insert_call(
            db, agent, turn, i, session_id=session_id, tool=tool, input_text=inp, is_error=err,
            outcome="error" if err else "ran", error_text=etext, started_at=_ago(hours=1, seconds=-i),
        )  # fmt: skip


def test_pattern_kinds_and_id_and_rate():
    assert PATTERN_KINDS == ("error_repeat", "call_repeat", "retry_failed")
    assert (tool_patterns.WINDOW_DAYS, tool_patterns.MIN_HITS, tool_patterns.ERROR_ROWS_CAP) == (14, 3, 5000)
    p = Pattern("error_repeat", "bash", "k", 2, 0, None, "ex")
    assert p.id == f"error_repeat:bash:{hashlib.sha1(b'k').hexdigest()[:8]}"
    assert p.rate == 0.0
    assert Pattern("call_repeat", "bash", "", 3, 6, None, "ex").rate == 0.5


@pytest.mark.postgres_only
async def test_error_repeat_groups_by_normalised_error(db, agent_id, cleanup):
    turn = await insert_turn(db, agent_id, "s1")
    errs = [(f"cat /tmp/f{i}", True, f"No such file /tmp/{c}") for i, c in enumerate(["a1", "b2", "c3", "d4"])]
    oks = [(f"echo {i}", False, None) for i in range(6)]
    await _calls(db, agent_id, turn, errs + oks)
    patterns, skipped = await _detect(db, agent_id)
    assert skipped == 0
    rep = _of(patterns, "error_repeat")
    assert len(rep) == 1
    assert rep[0].hits == 4 and rep[0].calls == 10 and rep[0].rate == 0.4
    assert rep[0].guard is None
    assert rep[0].key == normalise_error("No such file /tmp/a1")
    assert rep[0].example == rep[0].key


@pytest.mark.postgres_only
async def test_error_repeat_needs_min_hits(db, agent_id, cleanup):
    turn = await insert_turn(db, agent_id, "s1")
    rows = [(f"cat /tmp/f{i}", True, f"No such file /tmp/x{i}") for i in range(2)]
    await _calls(db, agent_id, turn, rows)
    assert _of((await _detect(db, agent_id))[0], "error_repeat") == []
    assert len(_of((await _detect(db, agent_id, min_hits=2))[0], "error_repeat")) == 1


@pytest.mark.postgres_only
async def test_call_repeat_counts_repeats_within_a_turn(db, agent_id, cleanup):
    t1 = await insert_turn(db, agent_id, "s1")
    t2 = await insert_turn(db, agent_id, "s1", turn_number=2)
    await _calls(db, agent_id, t1, [("ls", False, None)] * 3)
    await _calls(db, agent_id, t2, [("ls", False, None)] * 3)
    # The same input in a turnless run never counts.
    await _calls(db, agent_id, None, [("ls", False, None)] * 3, session_id="s2")
    patterns, _ = await _detect(db, agent_id)
    rep = _of(patterns, "call_repeat")
    assert len(rep) == 1
    assert rep[0].hits == 4 and rep[0].calls == 9 and rep[0].key == ""
    assert rep[0].example == "ls"
    assert _of(patterns, "retry_failed") == []


@pytest.mark.postgres_only
async def test_same_input_across_two_turns_is_not_a_repeat(db, agent_id, cleanup):
    for n in (1, 2, 3, 4):
        turn = await insert_turn(db, agent_id, "s1", turn_number=n)
        await _calls(db, agent_id, turn, [("ls", False, None)])
    patterns, _ = await _detect(db, agent_id, min_hits=1)
    assert _of(patterns, "call_repeat") == []


@pytest.mark.postgres_only
async def test_retry_failed_counts_error_after_identical_error(db, agent_id, cleanup):
    turn = await insert_turn(db, agent_id, "s1")
    rows = [("make", True, "boom"), ("make", True, "boom"), ("make", True, "boom"), ("make", False, None)]
    await _calls(db, agent_id, turn, rows)
    patterns, _ = await _detect(db, agent_id, min_hits=1)
    retry = _of(patterns, "retry_failed")
    assert len(retry) == 1 and retry[0].hits == 2 and retry[0].calls == 4
    assert retry[0].example == "make"
    assert _of(patterns, "call_repeat")[0].hits == 3


@pytest.mark.postgres_only
async def test_guard_caused_pattern_is_flagged(db, agent_id, cleanup):
    turn = await insert_turn(db, agent_id, "s1")
    for i in range(4):
        await insert_call(
            db, agent_id, turn, i, session_id="s1", tool="write_file", input_text=f"w{i}", is_error=True,
            outcome="refused", refused_by="context_policy", error_text="blocked here", started_at=_ago(hours=1),
        )  # fmt: skip
    patterns, _ = await _detect(db, agent_id)
    rep = _of(patterns, "error_repeat", "write_file")
    assert len(rep) == 1 and rep[0].guard == "context_policy" and rep[0].hits == 4

    # The same call repeated within a turn is flagged too.
    t2 = await insert_turn(db, agent_id, "s1", turn_number=2)
    for i in range(4):
        await insert_call(
            db, agent_id, t2, i, session_id="s1", tool="edit_file", input_text="same", is_error=True,
            outcome="refused", refused_by="context_policy", error_text=f"nope {i}", started_at=_ago(hours=1),
        )  # fmt: skip
    patterns, _ = await _detect(db, agent_id)
    assert _of(patterns, "call_repeat", "edit_file")[0].guard == "context_policy"


@pytest.mark.postgres_only
async def test_ordinary_errors_are_not_guard_caused(db, agent_id, cleanup):
    turn = await insert_turn(db, agent_id, "s1")
    await _calls(db, agent_id, turn, [(f"x{i}", True, "timeout after 30s") for i in range(4)])
    rep = _of((await _detect(db, agent_id))[0], "error_repeat")
    assert len(rep) == 1 and rep[0].guard is None


@pytest.mark.postgres_only
async def test_three_guarded_plus_one_ordinary_hit_is_guard_caused(db, agent_id, cleanup):
    turn = await insert_turn(db, agent_id, "s1")
    for i in range(3):
        await insert_call(
            db, agent_id, turn, i, session_id="s1", tool="bash", input_text=f"g{i}", is_error=True,
            outcome="refused", refused_by="context_policy", error_text="blocked here", started_at=_ago(hours=1),
        )  # fmt: skip
    await insert_call(
        db, agent_id, turn, 3, session_id="s1", tool="bash", input_text="o", is_error=True, outcome="error",
        error_text="blocked here", started_at=_ago(hours=1),
    )  # fmt: skip
    rep = _of((await _detect(db, agent_id))[0], "error_repeat")
    assert len(rep) == 1 and rep[0].hits == 4 and rep[0].guard == "context_policy"


@pytest.mark.postgres_only
async def test_detect_agrees_with_phase2_wasted_steps(db, agent_id, cleanup):
    other = f"{agent_id}-other"
    try:
        for tool in ("bash", "read_file"):
            t1 = await insert_turn(db, agent_id, "s1", turn_number=1)
            rows = [("a", False, None)] * 3 + [("b", True, "e1 7"), ("b", True, "e1 8"), ("b", False, None)]
            await _calls(db, agent_id, t1, rows + [("c", True, "e2")], tool=tool)
            t2 = await insert_turn(db, agent_id, "s1", turn_number=2)
            await _calls(db, agent_id, t2, [("a", False, None), ("b", True, "e1 9"), ("b", True, "e1 9")], tool=tool)
        await _calls(db, agent_id, None, [("a", True, "e1 1")] * 3, session_id="s9")  # turnless
        # Older than the window.
        old = await insert_turn(db, agent_id, "s1", turn_number=3, started_at=_ago(days=20))
        for i in range(4):
            await insert_call(db, agent_id, old, i, session_id="s1", input_text="z", is_error=True,
                              error_text="old", started_at=_ago(days=20))  # fmt: skip
        # Another agent.
        ot = await insert_turn(db, other, "s1")
        await _calls(db, other, ot, [("a", True, "e1")] * 5)

        patterns, _ = await _detect(db, agent_id, min_hits=1, days=14)
        async with db.session() as session:
            phase2 = await work_view_dashboard.get_patterns(session, agent_id, days=14)
        assert sum(p.hits for p in patterns if p.kind == "call_repeat") == phase2["wasted"]["repeated_calls"] > 0
        retried = phase2["wasted"]["retried_and_failed_again"]
        assert sum(p.hits for p in patterns if p.kind == "retry_failed") == retried > 0
        errors = {t["tool"]: t["errors"] for t in phase2["tools"]}
        assert set(errors) == {"bash", "read_file"}
        for tool, n in errors.items():
            assert sum(p.hits for p in patterns if p.kind == "error_repeat" and p.tool_name == tool) <= n
    finally:
        await delete_agent_rows(db, other)


@pytest.mark.postgres_only
async def test_window_agent_scope_and_empty(db, agent_id, cleanup):
    other = f"{agent_id}-other"
    try:
        assert await _detect(db, agent_id) == ([], 0)
        old = await insert_turn(db, agent_id, "s1", started_at=_ago(days=20))
        for i in range(4):
            await insert_call(db, agent_id, old, i, session_id="s1", input_text="z", is_error=True,
                              error_text="old", started_at=_ago(days=20))  # fmt: skip
        ot = await insert_turn(db, other, "s1")
        await _calls(db, other, ot, [("z", True, "theirs")] * 5)
        assert await _detect(db, agent_id) == ([], 0)
        # A longer window sees the old rows.
        patterns, _ = await _detect(db, agent_id, days=30)
        assert {p.kind for p in patterns} == {"error_repeat", "call_repeat", "retry_failed"}
        assert all(p.calls == 4 for p in patterns)
    finally:
        await delete_agent_rows(db, other)


@pytest.mark.postgres_only
async def test_results_are_ordered_most_hits_first_then_id(db, agent_id, cleanup):
    turn = await insert_turn(db, agent_id, "s1")
    await _calls(db, agent_id, turn, [(f"p{i}", True, "alpha failure") for i in range(3)])
    await _calls(db, agent_id, turn, [(f"q{i}", True, "beta failure") for i in range(5)], tool="read_file")
    patterns, _ = await _detect(db, agent_id)
    assert [p.hits for p in patterns] == sorted((p.hits for p in patterns), reverse=True)
    assert patterns[0].tool_name == "read_file"


@pytest.mark.postgres_only
async def test_example_is_redacted(db, agent_id, cleanup):
    token = "sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAA"
    turn = await insert_turn(db, agent_id, "s1")
    rows = [(f"curl -H 'Authorization: Bearer {token}' u{i}", True, f"denied Bearer {token} x") for i in range(4)]
    await _calls(db, agent_id, turn, rows)
    t2 = await insert_turn(db, agent_id, "s1", turn_number=2)
    await _calls(db, agent_id, t2, [(f"curl Bearer {token}", False, None)] * 4, tool="web")
    patterns, _ = await _detect(db, agent_id)
    err = _of(patterns, "error_repeat")
    rep = _of(patterns, "call_repeat", "web")
    assert err and rep
    for p in (*err, *rep):
        assert token not in p.example and token not in p.key, p
        assert "aaaaaaaa" not in p.example.lower() and "aaaaaaaa" not in p.key.lower(), p
        assert len(p.example) <= 200
    assert "bearer" in rep[0].example.lower()


@pytest.mark.postgres_only
async def test_example_is_cut_to_200_characters(db, agent_id, cleanup):
    turn = await insert_turn(db, agent_id, "s1")
    await _calls(db, agent_id, turn, [("x" * 500, False, None)] * 4)
    rep = _of((await _detect(db, agent_id))[0], "call_repeat")
    assert rep[0].example == "x" * 200


@pytest.mark.postgres_only
async def test_pattern_counts_between_bounds(db, agent_id, cleanup):
    turn = await insert_turn(db, agent_id, "s1")
    for i, h in enumerate((10, 9, 8, 3, 2, 1)):  # hours ago: three calls early, three late
        await insert_call(db, agent_id, turn, i, session_id="s1", input_text="ls", is_error=True,
                          error_text="boom 1", started_at=_ago(hours=h))  # fmt: skip
    since, until = _ago(hours=5), _ago(minutes=30)
    key = normalise_error("boom 1")

    async def counts(kind, k, **kw):
        async with db.session() as session:
            return await pattern_counts(session, agent_id, "bash", kind, k, **kw)

    assert await counts("error_repeat", key, since=since, until=until) == (3, 3)
    assert await counts("call_repeat", "", since=since, until=until) == (2, 3)  # the first is not a repeat
    assert await counts("retry_failed", "", since=since, until=until) == (2, 3)
    assert await counts("error_repeat", "other", since=since, until=until) == (0, 3)
    assert await counts("error_repeat", key, since=_ago(days=1), until=datetime.now(UTC)) == (6, 6)
    assert await counts("call_repeat", "", since=_ago(days=3), until=_ago(days=2)) == (0, 0)
    assert await counts("error_repeat", key, since=_ago(days=3), until=_ago(days=2)) == (0, 0)


@pytest.mark.postgres_only
async def test_error_row_cap_skips_error_repeat_for_that_tool(db, agent_id, cleanup, monkeypatch):
    monkeypatch.setattr(tool_patterns, "ERROR_ROWS_CAP", 5)
    turn = await insert_turn(db, agent_id, "s1")
    await _calls(db, agent_id, turn, [(f"i{i}", True, "same failure") for i in range(6)])
    await _calls(db, agent_id, turn, [(f"r{i}", True, "other failure") for i in range(3)], tool="read_file")
    patterns, skipped = await _detect(db, agent_id)
    assert skipped == 1
    assert _of(patterns, "error_repeat", "bash") == []
    assert len(_of(patterns, "error_repeat", "read_file")) == 1
    since, until = _ago(days=1), datetime.now(UTC)
    async with db.session() as session:
        assert await pattern_counts(session, agent_id, "bash", "error_repeat", "x", since=since, until=until) is None
        assert await pattern_counts(session, agent_id, "bash", "call_repeat", "", since=since, until=until) == (0, 6)
        counted = await pattern_counts(
            session, agent_id, "read_file", "error_repeat", normalise_error("other failure"), since=since, until=until
        )
    assert counted == (3, 3)


@pytest.mark.postgres_only
async def test_detect_runs_under_a_10s_statement_timeout(db, agent_id, cleanup):
    async with db.session() as session:
        await detect_patterns(session, agent_id, now=datetime.now(UTC))
        shown = (await session.execute(sa_text("SHOW statement_timeout"))).scalar_one()
    assert shown == "10s"
