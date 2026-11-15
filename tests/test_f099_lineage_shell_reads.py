"""F099 lineage shell: the reason for every refusal, and the reads added to the allowlist.

Pure policy tests on ``tmp_path`` (no database). The commands below that must be REFUSED are fixtures for the
allowlist parser; none of them is ever run, except the few that start a real ``git`` or ``grep`` in a throwaway
directory. The rules compare POSIX paths, so the module is skipped on Windows: CI (Linux) is the gate.
"""

from __future__ import annotations

import json
import os
import shutil
import string
import subprocess
import uuid
from pathlib import Path

import pytest
from test_f099_enforcement import MODES, OFFERED, _auth, _shell_runner
from test_f099_lineage_build_commands import ALLOWED, REFUSED
from test_f099_lineage_shell_obstacles import NEW_COMMANDS, _whole_ws

from nous.api import tool_policy
from nous.api.execution_context import ExecutionContext
from nous.api.tool_policy import (
    LINEAGE_REASONS,
    LineageReason,
    lineage_bash_allowed,
    lineage_bash_description,
    lineage_bash_refusal,
    lineage_bash_refusal_hint,
    lineage_runs_build,
    lineage_runs_runner,
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


# -- a reason for every refusal --


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
    for n in range(20):
        (ws / f"d{n}").mkdir()
    many = " && ".join(f"cd {ws}/d{n}" for n in range(20))
    assert _refusal(many, ws).code == "cd_many"
    assert _refusal("bash x.sh", ws).code == "bash_script"
    assert _refusal(f"cd nous && git status && {ws}/claude-jobs/runner.sh status", ws).code == "build_token"


def test_no_refusal_in_the_existing_refused_list_is_other(ws):
    others = []
    for template in REFUSED:
        reason = _refusal(_fmt(template, ws), ws)
        if reason is not None and reason.code == "other":
            others.append(template)
    assert others == []


# -- show the reason, log its code --


def _bash(r, command, kind="subtask"):
    return _auth(r, _internal(kind), "bash", OFFERED, {"command": command})


def test_the_refusal_text_puts_the_reason_first_then_the_hint(ws):
    r, _, _ = _shell_runner(ws)
    refusal = _bash(r, _fmt("sed -n 1p {W}/a.md", ws))
    hint = lineage_bash_refusal_hint(build_commands=True, workspace_dir=str(ws))
    assert "(external)" in refusal.text
    marker = "Refused: `sed` is not an allowed program."
    assert marker in refusal.text and hint in refusal.text
    assert refusal.text.index(marker) < refusal.text.index(hint)


def test_the_event_carries_the_reason_code_not_the_word(ws):
    r, _, events = _shell_runner(ws)
    _bash(r, _fmt("sed -n 1p {W}/a.md", ws))
    assert len(events) == 1
    _, data = events[0]
    assert data["reason_code"] == "program"
    assert "command_head" in data
    assert all("sed" not in str(v) for k, v in data.items() if k != "command_head")


def test_other_refusals_have_no_reason_code_key(ws):
    r, _, events = _shell_runner(ws)
    refusal = _auth(r, _internal(), "write_file", OFFERED, {"path": "/etc/x", "content": "x"})
    assert refusal is not None
    refusal = _auth(r, _internal(), "bash", ["write_file"], {"command": "ls"})
    assert refusal is not None
    assert events and all("reason_code" not in data and "command_head" not in data for _, data in events)


def test_a_command_with_a_secret_keeps_it_out_of_the_reason(ws):
    r, _, events = _shell_runner(ws)
    refusal = _bash(r, "printf x; rm --token=abc123secretvalue y")
    assert refusal is not None and "abc123secretvalue" not in refusal.text
    assert "abc123secretvalue" not in str(events[0][1].get("reason_code"))


def test_a_failing_reason_lookup_still_refuses(ws, monkeypatch):
    real, calls = tool_policy.lineage_bash_refusal, []

    def boom(*a, **k):
        calls.append(1)
        if len(calls) > 1:  # the first call is the decision, the second the reason lookup for the text
            raise RuntimeError("boom")
        return real(*a, **k)

    monkeypatch.setattr(tool_policy, "lineage_bash_refusal", boom)
    r, _, events = _shell_runner(ws)
    refusal = _bash(r, "rm -rf x")
    assert refusal is not None and "(external)" in refusal.text
    assert "Refused: this command is not on the lineage allowlist." in refusal.text
    assert events[0][1]["reason_code"] == "other"


# -- /proc and /dev paths mean something else to the shell than to this process --

PROCFS_REFUSED = [
    "cd /proc/self/cwd",
    "cd {W}/intentions/{RID} && cd /proc/self/cwd/nous && git diff",
    "cd //proc/self/cwd/nous",
    "cd /x/../proc/self/cwd/nous",
    "cat /proc/self/cwd/a.md",
    "cat /proc/self/environ",
    "cat /proc/1/cwd/a.md",
    "cat /dev/stdin",
    "cat /dev/fd/0",
    "grep x /proc/self/cwd/a.md",
    "diff /proc/self/cwd/a.md {W}/b.md",
    "echo x > /proc/self/cwd/research/n.md",
    "echo x > /dev/fd/1",
    "cp {W}/a.md /proc/self/cwd/research/c.md",
    "mkdir -p /proc/self/cwd/research/z",
    "touch /proc/self/cwd/research/z",
    "{R} launch nous --prompt-file /proc/self/cwd/claude-jobs/prompts/p.md",
    "{R} launch nous p --plugins /proc/self/cwd/plugins/x",
    "cat < /proc/self/cwd/a.md",
]


@pytest.mark.parametrize("command", PROCFS_REFUSED)
def test_proc_and_dev_paths_are_refused_even_when_the_process_cwd_is_the_workspace(command, ws, monkeypatch):
    monkeypatch.chdir(ws)  # /proc/self/cwd is then a directory the python check would call the workspace
    assert not _ok(_fmt(command, ws), ws), command


def test_dev_null_and_workspace_directories_named_proc_or_dev_stay_allowed(ws):
    for d in ("proc", "dev"):
        (ws / d).mkdir()
        (ws / d / "a.md").write_text("x")
    for command in (
        "echo x > /dev/null",
        "cat < /dev/null",
        "ls 2>/dev/null",
        "echo x 2>&1 >/dev/stderr",
        "cat {W}/proc/a.md",
        "cat {W}/dev/a.md",
    ):
        assert _ok(_fmt(command, ws), ws), command


# -- combined short flags for git reads --

CD = "cd nous && "


@pytest.mark.parametrize(
    "command",
    [
        "git status -sb",
        "git status -sbz",
        "git status -bs",
        "git status -sbv",
        "git status -s -b",
        "git log -pw",
        "git log -pu",
        "git log -psw -5",
        "git show -pw HEAD",
        "git diff -pw",
        "git diff -suw HEAD^",
        "git log -n5",
        "git status -uno",
    ],
)
def test_git_flags_may_share_a_word_when_every_letter_is_a_flag(command, ws):
    assert _ok(CD + command, ws), command


@pytest.mark.parametrize(
    ("command", "code"),
    [
        ("git status -sbu", "git_cluster"),
        ("git status -sbuno", "git_cluster"),
        ("git status -bu", "git_cluster"),
        ("git log -pU5", "git_cluster"),
        ("git log -pn5", "git_cluster"),
        ("git log -wn", "git_cluster"),
        ("git diff -pU", "git_cluster"),
        ("git status -sbx", "git_cluster"),
        ("git status -sb=x", "git_cluster"),
        ("git status -s5", "git_cluster"),
        ("git status -x", "git_opt"),
        ("git log -5p", "git_opt"),
        ("git rev-parse -qv HEAD", "git_cluster"),
        ("git status -n", "git_opt"),
        ("git log -n", "git_value"),
        ("git status -u", "git_value"),
    ],
)
def test_a_value_letter_hidden_in_a_cluster_is_refused(command, code, ws):
    reason = _refusal(CD + command, ws)
    assert reason is not None and reason.code == code, command


def test_a_cluster_quotes_nothing_of_the_command(ws):
    assert "ZQ9" not in _refusal(CD + "git status -sbZQ9canary", ws)
    assert "that word" not in _refusal(CD + "git status -sbu", ws)


def test_two_flags_in_two_words_still_work_and_a_count_beside_a_flag_word(ws):
    assert _ok(CD + "git log -5 -p", ws)
    assert _ok(CD + "git log -5 -pw", ws)


def _real_git(repo: Path, *args: str) -> str:
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(repo.parent),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        "GIT_AUTHOR_DATE": "2026-01-01T00:00:00",
        "GIT_COMMITTER_DATE": "2026-01-01T00:00:00",
    }
    done = subprocess.run(["git", *args], cwd=repo, env=env, capture_output=True, text=True, check=True)
    return done.stdout


needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


@needs_git
def test_a_cluster_means_the_same_to_git(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _real_git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("one\n")
    _real_git(repo, "add", "a.txt")
    _real_git(repo, "commit", "-q", "-m", "one")
    (repo / "a.txt").write_text("one\ntwo\n")
    assert _real_git(repo, "status", "-sb") == _real_git(repo, "status", "-s", "-b")
    assert _real_git(repo, "log", "-pw") == _real_git(repo, "log", "-p", "-w")


# -- bare git remote, git branch list forms, git ls-tree, git ls-files --

BRANCH_ALLOWED = [
    "git branch",
    "git branch -a",
    "git branch -r",
    "git branch -v",
    "git branch -vv",
    "git branch -avv",
    "git branch --list",
    "git branch --list 'feat/*'",
    "git branch --list -a 'feat*' 'fix*'",
    "git branch --show-current",
    "git branch --contains HEAD",
    "git branch --contains=HEAD",
    "git branch -a --contains main",
    "git branch --merged main",
    "git branch --no-merged",
    "git branch --merged=main --list 'x*'",
]
BRANCH_QUIRKS = [  # allowed by the shape check; the real-git test below is the authority that they create nothing
    "git branch x --list",
    "git branch --contains HEAD newname",
    "git branch --merged HEAD newname",
    "git branch --no-merged HEAD newname",
]
LS_ALLOWED = [
    "git ls-tree HEAD",
    "git ls-tree --name-only -r HEAD",
    "git ls-tree -r -t --long HEAD tests",
    "git ls-tree -rd HEAD",
    "git ls-tree --full-tree --abbrev=8 HEAD:tests",
    "git ls-files",
    "git ls-files -s",
    "git ls-files -o --exclude-standard",
    "git ls-files -c -- tests",
    "git ls-files --deleted --modified tests/test_a.py",
    "git ls-files -z --full-name",
]


@pytest.mark.parametrize("command", ["git remote", *BRANCH_ALLOWED, *BRANCH_QUIRKS, *LS_ALLOWED])
def test_the_new_git_read_forms_are_allowed(command, ws):
    assert _ok(CD + command, ws), command


REMOTE_REFUSED = [
    # printing a URL: -v and get-url (a token may sit in one); the four ways an output filter was got around
    "git remote -v | cut -d@ -f1",
    "git remote -v > {W}/research/o",
    'g""it remote -v',
    "git remote -v | head -c 100000",
    "git remote -v",
    "git remote --verbose",
    "git remote -vv",
    "git remote get-url origin",
    "git remote get-url --all origin",
    "git remote get-url --push origin",
    "git remote get-url",
    "git remote get-url origin extra",
    "git remote get-url -- origin",
    # writes
    "git remote add x url",
    "git remote add -f x url",
    "git remote rm origin",
    "git remote remove origin",
    "git remote rename a b",
    "git remote set-url origin x",
    "git remote set-url --push origin x",
    "git remote set-head origin -a",
    "git remote set-branches origin x",
    "git remote prune origin",
    # the network
    "git remote update",
    "git remote show origin",
    "git remote -f",
    "git remote -t x",
    "git remote origin",
    "git remote -- x",
]
BRANCH_REFUSED = [
    "git branch newname",
    "git branch -D x",
    "git branch -d x",
    "git branch -m a b",
    "git branch -M a b",
    "git branch -c a b",
    "git branch -f x HEAD",
    "git branch -u origin/x",
    "git branch --set-upstream-to=origin/x",
    "git branch --unset-upstream",
    "git branch -t x origin/x",
    "git branch --track x",
    "git branch --edit-description",
    "git branch -l x",
    "git branch -a newname",
    "git branch -v newname",
    "git branch -r newname",
    "git branch --show-current newname",
    "git branch -vD x",
    "git branch -aD x",
    "git branch -- newname",
    "git branch --list -- newname",
    "git branch --delete x",
    "git branch --force x",
    "git branch --li x",
    "git branch --sort=x",
    "git branch --format=x",
    "git branch --points-at HEAD",
]
LS_REFUSED = [
    "git ls-files -x '*'",
    "git ls-files --exclude='*'",
    "git ls-files -X /etc/passwd",
    "git ls-files --exclude-from=/etc/passwd",
    "git ls-files --exclude-per-directory=.x",
    "git ls-files --with-tree=HEAD",
    "git ls-files --recurse-submodules",
    "git ls-files --format=x",
    "git ls-files --error-unmatch x",
    "git ls-files /etc/passwd",
    "git ls-files ../x",
    "git ls-files -- /etc",
    "git ls-files .git",
    "git ls-files -ox",
    "git ls-tree HEAD /etc",
    "git ls-tree HEAD ../x",
    "git ls-tree --format=x HEAD",
    "git ls-tree -f HEAD",
    "git ls-remote",
    "git ls-remote origin",
    "git fetch",
    "git fetch -q",
    "git fetch --all",
]


@pytest.mark.parametrize("command", REMOTE_REFUSED + BRANCH_REFUSED + LS_REFUSED)
def test_the_new_git_forms_refuse_everything_that_writes_prints_a_url_or_reaches_out(command, ws):
    assert not _ok(_fmt(CD + command, ws), ws), command


def test_the_reasons_of_the_new_git_forms(ws):
    for command, code in (
        ("git remote -v", "git_remote"),
        ("git remote add x y", "git_remote"),
        ("git remote get-url origin", "git_remote"),
        ("git branch newname", "git_branch"),
        ("git branch --sort=x", "git_opt"),
        ("git ls-remote", "git_sub"),
        ("git fetch", "git_sub"),
        ("git ls-files -x '*'", "git_opt"),
    ):
        reason = _refusal(CD + command, ws)
        assert reason is not None and reason.code == code, command
    assert "gh repo view" in _refusal(CD + "git remote -v", ws)


def test_the_joins_and_contexts_of_the_new_git_forms(ws):
    assert _refusal(CD + "git remote && gh pr list", ws).code == "build_token"
    assert _refusal("cd nous && git branch -a; gh pr view 1", ws).code == "build_token"
    assert _refusal("git remote", ws).code == "git_repo"  # the workspace root is no repo
    assert _refusal("cd nous; git remote", ws).code == "git_dir"
    assert _refusal(CD + "git branch -a", ws, build_commands=False).code == "git_off"


def _sh(repo: Path, command: str, **env_over: str) -> subprocess.CompletedProcess:
    env = {"PATH": os.environ["PATH"], "HOME": str(repo.parent), "GIT_CONFIG_GLOBAL": "/dev/null", **env_over}
    return subprocess.run(["/bin/sh", "-c", command], cwd=repo, env=env, capture_output=True, text=True)


def _snapshot(repo: Path) -> dict[str, bytes | str]:
    snap: dict[str, bytes | str] = {"refs": _real_git(repo, "for-each-ref")}
    for name in ("config", "HEAD", "index"):
        snap[name] = (repo / ".git" / name).read_bytes()
    # the working tree's files and the hooks directory: a listing form must not create or change either
    snap["tree"] = "\n".join(
        f"{p.relative_to(repo)} {p.stat().st_size}" for p in sorted(repo.rglob("*")) if ".git/objects" not in str(p)
    )
    return snap


@pytest.fixture
def real_repo(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _real_git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("one\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_a.py").write_text("x = 1\n")
    _real_git(repo, "add", "a.txt", "tests")
    _real_git(repo, "commit", "-q", "-m", "one")
    _real_git(repo, "branch", "feat/a")
    _real_git(repo, "branch", "fix/b")
    _real_git(repo, "remote", "add", "origin", "https://ZQ9tok@example.com/x.git")
    return repo


@needs_git
@pytest.mark.parametrize("command", BRANCH_ALLOWED + BRANCH_QUIRKS)
def test_branch_listing_forms_create_nothing(command, real_repo, ws):
    assert _ok(CD + command, ws), command  # the policy allows it ...
    before = _snapshot(real_repo)
    done = _sh(real_repo, command)
    assert done.returncode == 0, done.stderr  # git accepted the form (a form it rejects would prove nothing) ...
    assert _snapshot(real_repo) == before  # ... and, run for real, changed neither a ref nor the config
    for name in ("newname", "x"):
        assert _real_git(real_repo, "branch", "--list", name).strip() == ""


@needs_git
@pytest.mark.parametrize("command", ["git remote", *LS_ALLOWED])
def test_bare_remote_ls_files_and_ls_tree_write_nothing(command, real_repo, ws):
    assert _ok(CD + command, ws), command
    before = _snapshot(real_repo)
    done = _sh(real_repo, command)
    assert done.returncode == 0, done.stderr
    assert _snapshot(real_repo) == before


@needs_git
def test_a_global_insteadof_cannot_leak_a_url_through_an_allowed_command(real_repo, ws):
    rewrite = '[url "https://ZQ9tok@example.com/"]\n\tinsteadOf = https://example.com/\n'
    (real_repo.parent / ".gitconfig").write_text(rewrite)
    for command in ["git remote", *BRANCH_ALLOWED, *LS_ALLOWED]:
        assert _ok(CD + command, ws), command
        done = _sh(real_repo, command, GIT_CONFIG_GLOBAL=str(real_repo.parent / ".gitconfig"))
        assert "ZQ9tok" not in done.stdout + done.stderr, command
    # the commands that would print a URL are the refused ones
    for command in ("git remote -v", "git remote get-url origin", "git ls-remote"):
        assert not _ok(CD + command, ws), command


NEW_GIT_COMMANDS = [
    "cd nous && git branch -a",
    "cd nous && git remote",
    "cd nous && git ls-files",
    "cd nous && git ls-tree HEAD",
    "git -C {W}/nous status",
]


@pytest.mark.parametrize(("offered_mode", "policy_mode"), MODES)
@pytest.mark.parametrize("kind", ["continuation", "subtask", "dag_node"])
def test_the_new_git_commands_run_in_every_mode(tmp_path, offered_mode, policy_mode, kind):
    ws = _whole_ws(tmp_path)
    r, _, events = _shell_runner(
        ws,
        f099_lineage_write_dirs=["research"],
        tool_offered_set_enforcement_mode=offered_mode,
        tool_context_policy_mode=policy_mode,
    )
    for command in (c.format(W=ws) for c in NEW_GIT_COMMANDS):  # ls-tree is rated a write: _LOCAL allows it
        assert _auth(r, _internal(kind), "bash", OFFERED, {"command": command}) is None, command
    assert events == []


# -- git -C <repo top> --

DASH_C_ALLOWED = [
    "git -C {W}/nous status",
    "git -C {W}/nous status -sb",
    "git -C nous log --oneline -5",
    "git -C nous diff --stat",
    "git -C nous branch -a",
    "git -C nous remote",
    "git -C nous ls-tree --name-only -r HEAD",
    "git -C nous rev-parse --show-toplevel",
    "git -C nous/ status",
    "cd nous && git -C . rev-parse HEAD",
    "git -C {W}/nous status && git -C {W}/nous log -3",
    "cd x; git -C {W}/nous status",  # an absolute -C needs no known directory
    "git -C nous status | head -5",
    "git -C nous status 2>&1 | tail -3",
    "git -C link status",
    "git -C nous diff -- tests/test_a.py",
]


@pytest.mark.parametrize("command", DASH_C_ALLOWED)
def test_git_dash_c_runs_as_cd_to_the_repo_top_would(command, ws):
    (ws / "x").mkdir()
    (ws / "link").symlink_to(ws / "nous")
    assert _ok(_fmt(command, ws), ws), command


DASH_C_REFUSED = [
    ("git -C /etc status", "git_C"),
    ("git -C {W}/../outside status", "git_C"),
    ("git -C ../outside status", "git_C"),
    ("git -C nous/../nous status", "git_C"),
    ("git -C '~' status", "git_C"),
    ("git -C /proc/self/cwd/nous status", "git_C"),
    ("git -C //proc/self/cwd/nous status", "git_C"),
    ("git -C /dev/../proc/self/cwd/nous status", "git_C"),
    ("git -C {W} status", "git_repo"),
    ("git -C {W}/nous/tests status", "git_repo"),
    ("git -C {W}/nous/.git status", "git_C"),
    ("git -C .hid status", "git_C"),
    ("git -C {W}/missing status", "git_repo"),
    ("git -C {W}/intentions/{RID} status", "git_repo"),
    ("git -C {W}/claude-jobs status", "git_repo"),
    ("git -C nous -c core.pager=x status", "git_sub"),
    ("git -C nous -C nous status", "git_sub"),
    ("git -C nous --git-dir=/tmp/x status", "git_sub"),
    ("git -C nous --work-tree=/ status", "git_sub"),
    ("git -C nous --exec-path=/tmp log", "git_sub"),
    ("git -C nous -p log", "git_sub"),
    ("git -C nous", "git_C"),
    ("git -C", "git_C"),
    ("git -C nous push", "git_sub"),
    ("git -C nous fetch", "git_sub"),
    ("git -C nous ls-remote", "git_sub"),
    ("git -C nous log --output=/tmp/x", "git_opt"),
    ("git -C nous status -sbu", "git_cluster"),
    (
        "git -Cnous status",
        "git_repo",
    ),  # no cd first: the workspace root is no repo, refused before the -Cnous word is read
    ("cd nous && git -Cnous status", "git_sub"),
    ("git -C -c status", "git_C"),
    ("git -C nous status && gh pr list", "build_token"),
    ("gh pr view 1; git -C nous status", "build_token"),
    ("git -C nous status && {R} status", "build_token"),
    ("cd nous || git -C tests status", "git_dir"),
    ("cd nous ; git -C tests status", "git_dir"),
    ("git -C nous status > /etc/x", "redirect"),
    ("git -C out status", "git_C"),
]


@pytest.mark.parametrize(("command", "code"), DASH_C_REFUSED)
def test_git_dash_c_refuses_everything_cd_would_and_every_other_option(command, code, ws):
    (ws / "claude-jobs" / ".git").mkdir()
    (ws / "intentions" / str(RID) / ".git").mkdir(parents=True)
    (ws / "out").symlink_to("/etc")
    reason = _refusal(_fmt(command, ws), ws)
    assert reason is not None and reason.code == code, (command, reason and reason.code)


@needs_git
def test_dash_c_runs_in_the_repo_it_names(tmp_path):
    repo = tmp_path / "gr"
    repo.mkdir()
    _real_git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("one\n")
    _real_git(repo, "add", "a.txt")
    (tmp_path / "claude-jobs" / "prompts").mkdir(parents=True)
    assert _ok("git -C gr status --porcelain", tmp_path)
    via_c = _sh(tmp_path, "git -C gr status --porcelain").stdout
    via_cd = _sh(tmp_path, "cd gr && git status --porcelain").stdout
    assert via_c == via_cd == "A  a.txt\n"


def test_dash_c_is_a_build_command_for_the_environment():
    assert lineage_runs_build("git -C nous status") is True
    assert lineage_runs_runner("git -C nous status") is False


# -- cut as a read filter --

CUT_ALLOWED = [
    "cut -d: -f1 {W}/a.md",
    "cut -d ' ' -f 2,3 {W}/a.md",
    "cut -f1 -d, {W}/a.md",
    "cut -c1-10 {W}/a.md",
    "cut -b1-5 {W}/a.md",
    "cut --complement -f2 {W}/a.md",
    "cut -s -d: -f1 {W}/a.md",
    "cut --delimiter=: --fields=1 {W}/a.md",
    "cut --output-delimiter=, -f1,3 {W}/a.md",
    "cat {W}/a.md | cut -d: -f1",
    "git -C nous branch -a | cut -c3-",
    "grep x {W}/a.md | cut -d: -f2 | head -3",
    "cut -f1 {W}/dir/*.md",  # a glob file operand, as for cat; a top-level glob stays refused
    "cut -d: -f1 - < {W}/a.md",
    "cut -f1 {W}/a.md {W}/b.md",
    "cut -f1 -- {W}/a.md",
]
CUT_REFUSED = [
    "cut -f1 /etc/passwd",
    "cut -f1 {W}/.env",
    "cut -f1 {W}/.hid/x",
    "cut -f1 ../outside",
    "cut -f1 '~/x'",
    "cut -f1 $HOME/x",
    "cut -f1 {W}/a.md > /tmp/x",
    "cut -f1 {W}/a.md > {W}/a.md",
    "cut --com -f1 {W}/a.md",
    "cut -x f",
    "cut --no-such f",
    "cut -f",
    "cut -f1 /dev/stdin",
    "cut -f1 /proc/self/environ",
    "cut -f1 {W}/link-to-etc",
    "cut -f1 *",
    "cut -f1 {W}/nope*",
    "cut -f1 $(echo x)",
    "cut -f1 {W}/a.md; sed -n 1p x",
]


@pytest.mark.parametrize("command", CUT_ALLOWED)
def test_cut_is_a_read_filter(command, ws):
    assert _ok(_fmt(command, ws), ws), command


@pytest.mark.parametrize("command", CUT_REFUSED)
def test_cut_reads_what_cat_reads_and_nothing_else(command, ws):
    (ws / "link-to-etc").symlink_to("/etc")
    assert not _ok(_fmt(command, ws), ws), command


def test_cut_hides_no_value_option(ws):
    """A closed table: GNU cut has no option whose value is a file; a future edit adding one needs a test."""
    for letter in string.ascii_letters + string.digits:
        if letter not in "dfcbsnz":
            assert not _ok(f"cut -{letter} x {ws}/a.md", ws), letter


def test_the_description_lists_cut():
    assert "cut" in lineage_bash_description(build_commands=True).split("reads (")[1].split(")")[0]


@pytest.mark.parametrize(
    "command",
    [
        "sort {W}/a.md",
        "sort -o {W}/research/x {W}/a.md",
        "sort --compress-program=sh {W}/a.md",
        "sort -T /tmp {W}/a.md",
        "uniq {W}/a.md",
        "uniq {W}/a.md {W}/research/out",
    ],
)
def test_sort_and_uniq_stay_refused(command, ws):
    reason = _refusal(_fmt(command, ws), ws)
    assert reason is not None and reason.code == "program"


# -- the resolved spelling of the job runner path, and the grep options that are (not) allowed --


@pytest.fixture
def linked(tmp_path):
    """A real workspace and a symlink to it: the setting names one, the shell's ``pwd`` prints the other."""
    real = _layout(tmp_path / "real")
    link = tmp_path / "link"
    link.symlink_to(real)
    return real, link


def test_the_resolved_path_of_a_symlinked_workspace_is_accepted_as_text(linked):
    real, link = linked
    for spelling in (f"{real}/claude-jobs/runner.sh", f"{link}/claude-jobs/runner.sh"):  # setting: the link
        assert _ok(f"{spelling} status", link), spelling
        assert _ok(f"bash {spelling} list", link), spelling
    assert not _ok(f"{link}/claude-jobs/runner.sh status", real)  # the setting's own text or its resolved one only
    assert lineage_runs_runner(f"bash {real}/claude-jobs/runner.sh list")


RUNNER_PINNED = [
    "{R} status",
    "bash {R} status",
    "cd {J} && ./runner.sh status",
    "cd claude-jobs && ./runner.sh status",
]


@pytest.mark.parametrize("command", RUNNER_PINNED)
def test_the_spellings_that_already_worked_still_do(command, ws):
    assert _ok(_fmt(command.replace("{J}", "{W}/claude-jobs"), ws), ws), command


def test_a_runner_reached_through_cd_dotdot_is_refused_at_the_cd(ws):
    assert _refusal(_fmt("cd {W}/claude-jobs && cd .. && ./runner.sh status", ws), ws).code == "cd"


RUNNER_PATH_REFUSED = [
    "runner.sh status",
    "bash runner.sh status",
    "cd {J} && runner.sh status",
    "cd {J} && bash runner.sh status",
    "cd {J} ; ./runner.sh status",
    "cd {J} || ./runner.sh status",
    "cd claude-jobs | ./runner.sh status",
    # the relative forms stay refused on purpose
    "claude-jobs/runner.sh status",
    "./claude-jobs/runner.sh status",
    "bash claude-jobs/runner.sh status",
    "bash ./claude-jobs/runner.sh status",
    "cd {J} && bash ./runner.sh status",
    "cd {W} && ./claude-jobs/runner.sh status",
    "../claude-jobs/runner.sh status",
    "{J}/../claude-jobs/runner.sh status",
    "claude-jobs/../claude-jobs/runner.sh status",
    "./claude-jobs/prompts/../runner.sh status",
    # the process's cwd is not the shell's cwd after a cd, so /proc/self/cwd names the wrong directory
    "/proc/self/cwd/claude-jobs/runner.sh status",
    "bash /proc/self/cwd/claude-jobs/runner.sh status",
    "cd {W}/intentions/{RID} && bash /proc/self/cwd/claude-jobs/runner.sh status",
    "bash /proc/self/root{W}/claude-jobs/runner.sh status",
    # equal in the shell but not as text: compared as text only
    "bash {W}/claude-jobs/./runner.sh status",
    "bash {W}/claude-jobs//runner.sh status",
    "bash {J}/../claude-jobs/runner.sh status",
    "{W}/runner.sh status",
    "{W}/claude-jobs/prompts/runner.sh status",
    "{W}/other/claude-jobs/runner.sh status",
    "/tmp/claude-jobs/runner.sh status",
    "{W}/claude-jobs/runner.sh.bak status",
    "bash -x {R} status",
    "bash -c '{R} status'",
    "bash --norc {R} status",
]


@pytest.mark.parametrize("command", RUNNER_PATH_REFUSED)
def test_every_other_spelling_of_the_runner_is_refused_with_its_own_reason(command, ws):
    reason = _refusal(_fmt(command.replace("{J}", "{W}/claude-jobs"), ws), ws)
    assert reason is not None and reason.code == "runner_path", (command, reason and reason.code)
    assert "<workspace>/claude-jobs/runner.sh" in reason


@pytest.mark.parametrize(
    ("command", "code"),
    [
        ("bash -lc x", "bash_script"),
        ("bash {R} status; bash {W}/claude-jobs/prompts/x.sh", "bash_script"),
        ("bash {W}/claude-jobs/prompts/x.sh", "bash_script"),
        ("bash", "bash_script"),
        ("sh {R} status", "program"),
        ("source {R}", "program"),
        (". {R}", "program"),
        ("exec {R}", "program"),
        ("env {R} status", "program"),
        ("xargs {R}", "program"),
        ("{W}/research/r.sh status", "program"),
    ],
)
def test_a_script_other_than_the_runner_is_not_the_runner(command, code, ws):
    reason = _refusal(_fmt(command, ws), ws)
    assert reason is not None and reason.code == code, (command, reason and reason.code)


def test_a_symlink_to_the_runner_inside_the_workspace_is_not_the_runner(ws):
    (ws / "research" / "runner.sh").symlink_to(ws / "claude-jobs" / "runner.sh")
    assert _refusal(f"{ws}/research/runner.sh status", ws).code == "runner_path"


def test_a_workspace_that_is_another_directory_is_not_the_one_set(tmp_path):
    ws = _layout(tmp_path / "ws")
    other = tmp_path / "other"
    other.symlink_to(tmp_path / "elsewhere")
    (tmp_path / "elsewhere").mkdir()
    assert _refusal(f"{other}/claude-jobs/runner.sh status", ws).code == "runner_path"


@pytest.mark.parametrize(
    ("args", "code"),
    [
        ("cancel job-20261010-222549-6e37c52b", "runner_cancel"),
        ("cleanup", "runner_verb"),
        ("gc", "runner_verb"),
        ("launch nous p --sys-prompt x", "runner_args"),
        ("launch nous p --timeout 5", "runner_args"),
        ("launch nous --prompt-file claude-jobs/prompts/x.md", "runner_file"),
        ("launch nous --prompt-file /etc/passwd", "runner_file"),
        ("launch 'a b' p", "runner_args"),
        ("launch nous p --plugins {W}/research", "runner_args"),
        ("status extra", "runner_args"),
        ("result", "runner_args"),
        ("list --bogus", "runner_args"),
    ],
)
def test_the_runner_verbs_are_unchanged_in_every_accepted_spelling(args, code, ws):
    for prefix in ("{R}", "bash {R}", "cd {W}/claude-jobs && ./runner.sh"):
        reason = _refusal(_fmt(f"{prefix} {args}", ws), ws)
        assert reason is not None and reason.code == code, (prefix, args, reason and reason.code)


def test_every_accepted_spelling_still_drops_gh_repo():
    for command in (
        "/w/claude-jobs/runner.sh status",
        "bash /w/claude-jobs/runner.sh list",
        "cd /w/claude-jobs && ./runner.sh status",
    ):
        assert lineage_runs_runner(command), command
        assert not lineage_runs_build(command), command


def test_a_runner_spelling_cannot_join_a_build_command(linked):
    real, link = linked
    for command in (
        f"cd nous && pytest && bash {real}/claude-jobs/runner.sh status",
        f"git -C nous status; {link}/claude-jobs/runner.sh list",
    ):
        reason = _refusal(command, link)
        assert reason is not None and reason.code == "build_token", command


GREP_ALLOWED = [
    "grep -n x {W}/a.md",
    "grep -B15 -n x {W}/a.md",
    "grep -A3 -n x {W}/a.md",
    "grep -A 3 x {W}/a.md",
    "grep -C2 x {W}/a.md",
    "grep -o x {W}/a.md",
    "grep -c x {W}/a.md",
    "grep -E 'a|b' {W}/a.md",
    "grep -i x {W}/a.md",
    "grep -nE x {W}/a.md",
    "grep -m1 x {W}/a.md",
    "grep -h x {W}/a.md {W}/b.md",
    "grep --include='*.md' x {W}/a.md",
]
GREP_R = [
    "grep -rh x {W}/dir",
    "grep -rl x {W}/dir",
    "grep -rln --include='*.md' x {W}/dir",
    "grep -r x {W}/dir",
    "grep -R x {W}/dir",
    "grep -d recurse x {W}/dir",
    "grep -drecurse x {W}/dir",
    "grep --directories=recurse x {W}/dir",
    "grep --directories recurse x {W}/dir",
    "grep --recursive x {W}/dir",
    "grep --dereference-recursive x {W}/dir",
    "grep -nr x {W}/dir",
    "grep -rn x",
    "grep -A3 -r x {W}/dir",
    "grep -e x -r {W}/dir",
]


@pytest.mark.parametrize("command", GREP_ALLOWED)
def test_grep_context_and_filter_options_are_accepted(command, ws):
    assert _ok(_fmt(command, ws), ws), command


@pytest.mark.parametrize("command", GREP_R)
def test_grep_recursion_is_refused_for_its_own_reason(command, ws):
    reason = _refusal(_fmt(command, ws), ws)
    assert reason is not None and reason.code == "grep_r", (command, reason and reason.code)
    assert "dir/*.md" in reason


@pytest.mark.skipif(shutil.which("grep") is None, reason="needs grep")
def test_grep_r_would_read_a_hidden_file_that_cat_is_refused(ws):
    done = subprocess.run(["grep", "-r", "token", str(ws / "dir")], capture_output=True, text=True)
    assert ".env" in done.stdout  # recursion descends into the hidden file ...
    assert not _ok(f"cat {ws}/dir/.env", ws)  # ... that a named read is refused
    assert not _ok(f"grep -r token {ws}/dir", ws)


# -- the hint --


def test_the_hint_lists_the_new_forms():
    on = lineage_bash_refusal_hint(build_commands=True)
    off = lineage_bash_refusal_hint(build_commands=False)
    for needle in ("ls-tree", "ls-files", "remote (names only)", "branch --list", "git -C <repo top>"):
        assert needle in on and needle not in off, needle
    for needle in ("cut", "grep takes no -r, use grep PATTERN dir/*.md"):
        assert needle in on and needle in off, needle


def test_the_hint_stays_short_with_the_longest_reason():
    hint = lineage_bash_refusal_hint(build_commands=True, extra_write_dirs=["research"], workspace_dir="/w")
    longest = max(len(f" Refused: {LineageReason(code, 'x' * 24)}.") for code in LINEAGE_REASONS)
    assert len(hint) + longest <= 1700


# -- the shapes of the production replay, and the final tripwire --

REPLAY = [
    # (command, allowed now, reason code when refused)
    ("cd nous && git status -sb", True, None),
    ("cd nous && git remote -v", False, "git_remote"),
    ("cd nous && git branch -a", True, None),
    ("cd nous && git ls-tree --name-only -r HEAD", True, None),
    ("cd nous && git ls-remote", False, "git_sub"),
    ("cd nous && git fetch -q", False, "git_sub"),
    ("cd nous; git status", False, "git_dir"),
    ("cd research && cd ../nous && git status", False, "cd"),
    ("git -C {W}/nous status", True, None),
    ("git -C nous log --oneline", True, None),
    ("git -C nous branch -a", True, None),
    ("git -C nous remote", True, None),
    ("cat {W}/dir/.env", False, "path"),
    ("ls {W}/.ssh", False, "path"),
    ("head -3 {W}/claude-jobs/.claude-home/x", False, "path"),
    ("ls {W}/nope/*", False, "glob"),
    ("bash {W}/claude-jobs/prompts/x.sh", False, "bash_script"),
    ("bash x.sh", False, "bash_script"),
    ("cut -d: -f1 {W}/a.md", True, None),
    ("cat {W}/a.md | cut -f2", True, None),
    ("sed -n 1p {W}/a.md", False, "program"),
    ("find {W} -name x", False, "program"),
    ("timeout 5 ls", False, "program"),
    ("which git", False, "program"),
    ("python3 x.py", False, "program"),
    ("bash claude-jobs/runner.sh status", False, "runner_path"),
    ("bash ./claude-jobs/runner.sh list", False, "runner_path"),
    ("cd claude-jobs && bash ./runner.sh status", False, "runner_path"),
    ("cd claude-jobs ; ./runner.sh status", False, "runner_path"),
    ("./runner.sh status", False, "runner_path"),
    ("bash runner.sh status", False, "runner_path"),
    ("{R} launch nous p --sys-prompt x", False, "runner_args"),
    ("{R} launch nous p --timeout 5", False, "runner_args"),
    ("{R} launch nous --prompt-file claude-jobs/prompts/p.md", False, "runner_file"),
    ("echo $HOME", False, "unreadable"),
    ("ls {W}/*", False, "glob"),
    ("cat $(pwd)/a.md", False, "unreadable"),
    ("grep -rh x {W}/dir", False, "grep_r"),
    ("grep -rl x {W}/dir", False, "grep_r"),
    ("grep -rln --include='*.md' x {W}/dir", False, "grep_r"),
    ("grep -B15 -n x {W}/a.md", True, None),
    ("grep -A3 -n x {W}/a.md", True, None),
    ("grep -o x {W}/a.md", True, None),
    ("grep -c x {W}/a.md", True, None),
    ("grep -E x {W}/a.md", True, None),
    ("grep -i x {W}/a.md", True, None),
    ("grep -n x {W}/a.md", True, None),
    ("cd nous && pytest -q && gh pr checks 1", False, "build_token"),
]


@pytest.mark.parametrize(("command", "allowed", "code"), REPLAY, ids=[r[0] for r in REPLAY])
def test_the_replay_shapes(command, allowed, code, ws):
    reason = _refusal(_fmt(command, ws), ws)
    if allowed:
        assert reason is None, (command, reason and reason.code)
    else:
        assert reason is not None and reason.code == code, (command, reason and reason.code)
        assert reason.code != "other"


def test_the_widening_probes_stay_refused(ws):
    """One tripwire over every 'still refused' list of the widenings; a failure names the command that got through."""
    (ws / "claude-jobs" / ".git").mkdir()
    (ws / "intentions" / str(RID) / ".git").mkdir(parents=True)
    (ws / "out").symlink_to("/etc")
    (ws / "link-to-etc").symlink_to("/etc")
    probes = [
        *(CD + c for c in REMOTE_REFUSED + BRANCH_REFUSED + LS_REFUSED),
        *(c for c, _ in DASH_C_REFUSED),
        *CUT_REFUSED,
        *RUNNER_PATH_REFUSED,
        *GREP_R,
        *PROCFS_REFUSED,
        *(
            CD + c
            for c, _ in [("git status -sbu", 0), ("git log -pU5", 0), ("git status -sb=x", 0), ("git log -wn", 0)]
        ),
    ]
    through = [c for c in probes if _ok(_fmt(c.replace("{J}", "{W}/claude-jobs"), ws), ws)]
    assert through == []


# -- a decision that cannot be made is a refusal, never an exception --


@pytest.mark.parametrize("command", ["git -C {L} status", "git -C nous/{L} status", "cd {L} && git status"])
def test_a_directory_name_over_the_filename_limit_is_refused_through_the_runner(command, ws):
    r, _, events = _shell_runner(ws)
    refusal = _bash(r, command.format(L="d" * 300))
    assert refusal is not None and "(external)" in refusal.text
    assert events[0][1]["reason_code"] in LINEAGE_REASONS


def test_an_exception_while_deciding_refuses_with_the_other_reason(ws, monkeypatch):
    def boom(*a, **k):
        raise OSError(36, "File name too long: /secret/path")

    monkeypatch.setattr(tool_policy, "lineage_bash_refusal", boom)
    r, _, events = _shell_runner(ws)
    refusal = _bash(r, "ls")
    assert refusal is not None and "(external)" in refusal.text
    assert "Refused: this command is not on the lineage allowlist." in refusal.text
    assert events[0][1]["reason_code"] == "other"
    assert "secret" not in str(events) + refusal.text
    assert not tool_policy.lineage_bash_allowed(_internal(), "ls", workspace_dir=str(ws))


@pytest.mark.parametrize(
    "command",
    [
        "cat plink/a.md",
        "cd plink",
        "ls plink",
        "git -C plink/nous status",
        "echo x > plink/research/y",
        "touch plink/research/y",
        "mkdir -p plink/research/z",
        "cp {W}/a.md plink/research/y",
        "cat {W}/plink/a.md",
        "cat chain/a.md",
        "cat dlink/null",
    ],
)
def test_a_path_that_resolves_through_a_symlink_into_proc_or_dev_is_refused(command, ws, monkeypatch):
    (ws / "plink").symlink_to("/proc/self/cwd")
    (ws / "chain").symlink_to(ws / "plink")  # a link to a link into /proc
    (ws / "dlink").symlink_to("/dev")
    monkeypatch.chdir(ws)  # /proc/self/cwd is then a directory plain resolution calls the workspace
    assert not _ok(_fmt(command, ws), ws), command


def test_symlinks_that_stay_in_the_workspace_still_resolve(ws):
    (ws / "link").symlink_to(ws / "dir")
    (ws / "rel").symlink_to("dir")
    assert _ok(_fmt("cat link/n.md", ws), ws)
    assert _ok(_fmt("cat rel/n.md", ws), ws)
    (ws / "loop").symlink_to("loop")
    assert not _ok(_fmt("cat loop/x", ws), ws)


def test_a_bare_dash_is_an_operand_of_git_branch(ws):
    assert _refusal(CD + "git branch -", ws).code == "git_branch"
    assert _refusal(CD + "git branch -a -", ws).code == "git_branch"
    assert _ok(CD + "git branch --list -", ws)  # after --list it is a pattern, like any operand


def test_git_rev_parse_takes_one_letter_per_word(ws):
    assert _ok(CD + "git rev-parse -q HEAD", ws)
    assert _ok(CD + "git rev-parse -q -q HEAD", ws)
    for command in ("git rev-parse -qq HEAD", "git rev-parse -qq"):
        assert _refusal(CD + command, ws).code == "git_cluster", command


def test_a_reason_keeps_no_copy_of_the_word(ws):
    reason = _refusal("ZQ9canaryprog arg", ws)
    assert not hasattr(reason, "word") and "ZQ9canaryprog" in reason
