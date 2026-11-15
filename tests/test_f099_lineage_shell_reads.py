"""F099 lineage shell: the reason for every refusal, and the reads added to the allowlist.

Pure policy tests on ``tmp_path`` (no database). The commands below that must be REFUSED are fixtures for the
allowlist parser; none of them is ever run, except the few that start a real ``git`` or ``grep`` in a throwaway
directory. The rules compare POSIX paths, so the module is skipped on Windows: CI (Linux) is the gate.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest
from test_f099_lineage_build_commands import ALLOWED, REFUSED
from test_f099_lineage_shell_obstacles import NEW_COMMANDS

from nous.api.execution_context import ExecutionContext
from nous.api.tool_policy import (
    LINEAGE_REASONS,
    LineageReason,
    lineage_bash_allowed,
    lineage_bash_refusal,
)

pytestmark = pytest.mark.skipif(os.name == "nt", reason="the rules read POSIX paths; CI is the gate")

IID, RID = (uuid.UUID(f"00000000-0000-0000-0000-00000000000{n}") for n in (1, 2))
FIXTURE = Path(__file__).parent / "fixtures" / "lineage_shell_decisions.json"


def _internal(kind: str = "subtask") -> ExecutionContext:
    return ExecutionContext(
        kind=kind, session_id="s", authority="internal_only", intention_id=IID, root_intention_id=RID
    )


def _layout(root: Path) -> Path:
    """The workspace the decisions fixture was recorded in: one repo, the job directories, two notes."""
    for d in ("nous/.git", "nous/tests", "nous/nous", "claude-jobs/prompts", "research"):
        (root / d).mkdir(parents=True)
    (root / "nous/tests/test_a.py").write_text("def test_a():\n    pass\n")
    for f in ("a.md", "b.md"):
        (root / f).write_text(f)
    return root


@pytest.fixture
def ws(tmp_path):
    """The recorded layout plus a few more files the reads below name."""
    _layout(tmp_path)
    (tmp_path / "dir").mkdir()
    (tmp_path / "dir" / "n.md").write_text("n")
    (tmp_path / "dir" / ".env").write_text("token=1")
    return tmp_path


def _refusal(command, ws, *, build_commands=True, extra=("research",)):
    return lineage_bash_refusal(
        _internal(), command, workspace_dir=str(ws), build_commands=build_commands, extra_write_dirs=extra
    )


def _ok(command, ws, **kw):
    return _refusal(command, ws, **kw) is None


def _fmt(template, ws):
    return template.format(W=ws, RID=RID, R=f"{ws}/claude-jobs/runner.sh")


# -- Task 1: a reason for every refusal -------------------------------------------------------------------------------


def test_the_decisions_did_not_change(tmp_path):
    """lineage_bash_allowed over every command of the existing lists equals what it returned before the reasons
    existed (tests/fixtures/lineage_shell_decisions.json, recorded on Linux), and the reason is None exactly when
    it is allowed. A later change to a decision updates the fixture in the same commit, so its diff is the review
    surface."""
    ws = _layout(tmp_path.resolve())
    rows = json.loads(FIXTURE.read_text())
    assert len(rows) > 400
    for template, build, extra, expected in rows:
        command = _fmt(template, ws)
        kw = {"workspace_dir": str(ws), "build_commands": build, "extra_write_dirs": extra}
        reason = lineage_bash_refusal(_internal(), command, **kw)
        assert lineage_bash_allowed(_internal(), command, **kw) is expected, (template, build)
        assert (reason is None) is expected, (template, build)
        if reason is not None:
            assert reason.code in LINEAGE_REASONS, (template, reason.code)


def test_the_fixture_covers_the_existing_lists():
    templates = {row[0] for row in json.loads(FIXTURE.read_text())}
    assert set(ALLOWED) | set(REFUSED) | set(NEW_COMMANDS) <= templates


def test_the_table_is_closed():
    for code, text in LINEAGE_REASONS.items():
        assert text.count("{w}") <= 1, code
        assert "{" not in text.replace("{w}", ""), code
        assert "}" not in text.replace("{w}", ""), code
        assert LineageReason(code, "x").code == code
    with pytest.raises(KeyError):
        LineageReason("nope")


CANARY = "ZQ9canary"


@pytest.mark.parametrize(
    "command",
    [
        f"cat {{W}}/{CANARY}/.hid/x",
        f"cat /etc/{CANARY}",
        f"grep {CANARY} /etc/passwd",
        f"git -C /etc/{CANARY} status",
        f"cd nous && git log --output={CANARY}",
        f"cd nous && git diff {CANARY}/../../etc",
        f"gh api {CANARY} -X POST",
        f"gh pr merge {CANARY}",
        f"{{W}}/claude-jobs/runner.sh launch nous p --sys-prompt {CANARY}",
        f"{{W}}/claude-jobs/runner.sh cancel {CANARY}",
        f"echo hi > /tmp/{CANARY}",
        f"cp {{W}}/a.md /tmp/{CANARY}",
        f"mkdir {{W}}/{CANARY}",
        f"echo ${CANARY}",
        f"ls {CANARY}*",
        f"curl https://{CANARY}.example/x",
    ],
)
def test_a_reason_never_quotes_a_path_value_or_url(command, ws):
    reason = _refusal(_fmt(command, ws), ws)
    assert reason is not None
    assert CANARY not in reason


def test_a_program_or_option_name_is_quoted_once_and_clipped(ws):
    for command in (
        f"cd nous && git log -S{CANARY}",
        f"cd nous && git log -S{CANARY}={CANARY}2",
        f"cd nous && git status -sb{CANARY}",
        f"grep --ZQ9opt={CANARY}value x {{W}}/a.md",
    ):
        reason = _refusal(_fmt(command, ws), ws)
        assert reason is not None
        assert CANARY not in reason, command
    reason = _refusal("ZQ9canaryprog arg", ws)
    assert reason.code == "program"
    assert reason.count("ZQ9canaryprog") == 1 and "arg" not in reason
    assert _refusal(f"{'a' * 200} x", ws) == "that word is not an allowed program"
    reason = _refusal("/usr/bin/ZQ9tool x", ws)
    assert "`ZQ9tool`" in reason and "/usr" not in reason
    reason = _refusal(_fmt("grep --ZQ9opt=ZQ9value x {W}/a.md", ws), ws)
    assert reason.code == "option" and "`--ZQ9opt`" in reason and "ZQ9value" not in reason
    for odd in ("a\nb", "a'b", "a$b", "café"):
        assert "that word" in LineageReason("program", odd)
    assert "that word" in LineageReason("program", 5)  # not a string


def test_the_reasons_of_the_brief(ws):
    def check(command, code, text=None):
        reason = _refusal(_fmt(command, ws), ws)
        assert reason is not None and reason.code == code, (command, reason and reason.code)
        if text:
            assert str(reason) == text

    check("sed -n 1p {W}/a.md", "program", "`sed` is not an allowed program")
    check("cat {W}/.env", "path")
    check("cd {W}; git status", "git_dir")
    check("cd {W}; pytest -q", "build_dir")
    check("ls {W}/nope/*", "glob")
    check("cd nous && git ls-remote", "git_sub", "git: `ls-remote` is not an allowed subcommand")


EXIT_CODES = {
    "": "empty",
    "ls &": "join",
    "> {W}/a.md": "redirect",
    "echo x > /etc/passwd": "redirect",
    "cat < /etc/passwd": "input",
    "echo ~": "unreadable",
    "echo x*": "glob",
    "jq env {W}/a.md": "jq_filter",
    "grep -d recurse x {W}/dir": "grep_r",
    "grep -drecurse x {W}/dir": "grep_r",
    "cat {W}/*": "glob",
    "grep -r x {W}": "grep_r",
    "grep --bogus x {W}/a.md": "option",
    "cd a b": "cd",
    "gh pr merge 1": "gh",
    "printf -v x y": "printf",
    "date -s x": "args",
    "cat {W}/.env": "path",
    "touch /etc/x": "write",
    "cd nous && pytest -p x": "args",
    "cd nous && pytest && gh pr view 1": "build_token",
    "rm -rf x": "program",
}


@pytest.mark.parametrize(("command", "code"), list(EXIT_CODES.items()), ids=[repr(c) for c in EXIT_CODES])
def test_every_loop_exit_has_a_specific_code(command, code, ws):
    reason = _refusal(_fmt(command, ws), ws)
    assert reason is not None
    assert reason.code == code
    assert reason.code != "other"


def test_the_exits_that_need_other_settings(ws):
    assert _refusal("cd nous && pytest", ws, build_commands=False).code == "git_off"
    many = " && ".join(f"cd {d}" for d in ["nous", "dir", "research", "claude-jobs"] * 6)
    assert _refusal(many, ws).code in ("cd_many", "cd")
    assert _refusal("bash x.sh", ws).code == "bash_script"
    assert _refusal(f"cd nous && git status && {ws}/claude-jobs/runner.sh status", ws).code == "build_token"


def test_no_refusal_in_the_existing_refused_list_is_other(ws):
    others = []
    for template in REFUSED:
        reason = _refusal(_fmt(template, ws), ws)
        if reason is not None and reason.code == "other":
            others.append(template)
    assert others == []
