"""F099: the outcome text of an approved bash proposal that printed nothing (pure, no database)."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from nous.brain import continuation

SETTINGS = SimpleNamespace(result_inbox_body_max_chars=4000)


def _body(
    tool: str, result: str | None, state: str = continuation.PROPOSAL_EXECUTED, *, command: str = "mkdir -p out"
) -> tuple[str, str]:
    proposal = SimpleNamespace(id=uuid4(), tool=tool, arguments={"command": command}, result=result, error=None)
    _title, body = continuation._proposal_outcome(proposal, state, SETTINGS)
    return continuation.short_id(proposal.id), body


def test_a_silent_bash_success_says_only_what_was_observed():
    sid, body = _body("bash", "Exit code: 0")
    assert body == f"The owner approved your proposal {sid} (bash). {continuation.SILENT_EXIT_ZERO_TEXT}"
    assert "printed nothing and exited 0" in body
    assert "status of the last command it ran" in body and "check its effect" in body
    assert "(no output)" not in body


def test_a_silent_compound_command_is_not_said_to_have_succeeded_in_every_step():
    """`false && touch out || true` exits 0 with `touch` never run: exit 0 is the last list's status only."""
    _sid, body = _body("bash", "Exit code: 0", command="false && touch out || true")
    lowered = body.lower()
    assert "exited 0" in lowered
    for claim in ("every step", "all steps", "succeeded", "each step"):
        assert claim not in lowered


def test_a_bash_success_with_output_is_unchanged():
    sid, body = _body("bash", "hello\nExit code: 0")
    assert body == f"The owner approved your proposal {sid} (bash) and it ran. Its result:\nhello\nExit code: 0"


def test_a_non_bash_tool_with_the_same_text_is_unchanged():
    sid, body = _body("send_email", "Exit code: 0")
    assert body == f"The owner approved your proposal {sid} (send_email) and it ran. Its result:\nExit code: 0"


def test_nonzero_exit_only_result_is_not_called_silent_success():
    _sid, body = _body("bash", "Exit code: 3")
    assert "exited 0" not in body and "Exit code: 3" in body


def test_a_bash_result_of_none_is_still_the_old_text():
    sid, body = _body("bash", None)
    assert body == f"The owner approved your proposal {sid} (bash) and it ran. Its result:\n(no output)"
