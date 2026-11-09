"""F108 Phase 3: the shared repeat definition, error normalisation and guard classification. No database."""

from __future__ import annotations

import inspect
import uuid
from pathlib import Path

import pytest

from nous.api import work_view_dashboard
from nous.api.execution_context import ExecutionContext
from nous.api.tool_policy import write_path_refusal_hint
from nous.dag.schemas import lineage_shell_check_message
from nous.observability import tool_patterns
from nous.observability.tool_patterns import GUARD_FRAGMENTS, guard_cause, normalise_error

ROOT = Path(__file__).resolve().parent.parent


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
