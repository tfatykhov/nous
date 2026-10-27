"""F099 lineage build commands (NOUS_F099_LINEAGE_BUILD_COMMANDS).

With the lineage shell, an internal_only turn may also run read-only git, pytest, ruff and make, each with a
closed option map, in a repo's top level reached by `cd <repo> &&` (tool_policy._build_allowed, _build_repo).
pytest and make run the repo's code, so such a command runs without the GitHub token (builtin_tools._lineage_env).
"""

from __future__ import annotations

import os
import shutil
import uuid
from types import SimpleNamespace

import pytest
from test_f099_enforcement import OFFERED, _auth, _shell_runner
from test_f099_phase2d_propose import _propose

from nous.api.execution_context import ExecutionContext
from nous.api.tool_policy import (
    LINEAGE_MAKE_TARGETS,
    internal_only_call_violation,
    lineage_bash_allowed,
    lineage_bash_refusal_hint,
    lineage_runs_build,
)
from nous.config import Settings

IID, RID = (uuid.UUID(f"00000000-0000-0000-0000-00000000000{n}") for n in (1, 2))


def _internal(kind: str = "subtask") -> ExecutionContext:
    return ExecutionContext(
        kind=kind, session_id="s", authority="internal_only", intention_id=IID, root_intention_id=RID
    )


@pytest.fixture
def ws(tmp_path):
    """A workspace with one repo, ``nous``, holding a test file and a package."""
    (tmp_path / "nous" / ".git").mkdir(parents=True)
    (tmp_path / "nous" / "tests").mkdir()
    (tmp_path / "nous" / "tests" / "test_a.py").write_text("def test_a():\n    pass\n")
    (tmp_path / "nous" / "nous").mkdir()
    return tmp_path


def _allowed(command: str, ws, *, build_commands: bool = True) -> bool:
    return lineage_bash_allowed(_internal(), command, workspace_dir=str(ws), build_commands=build_commands)


ALLOWED = [
    # git, read-only
    "cd nous && git status",
    "cd nous && git status -s",
    "cd nous && git status --short --branch",
    "cd nous && git status --porcelain=v2 -uno",
    "cd nous && git log",
    "cd nous && git log --oneline -5",
    "cd nous && git log -n20 --format=%H --since=2.days",
    "cd nous && git log --graph --decorate --all --max-count=3",
    "cd nous && git log -p --stat main..HEAD -- nous tests/test_a.py",
    "cd nous && git diff",
    "cd nous && git diff --cached --stat",
    "cd nous && git diff --name-only origin/main...HEAD",
    "cd nous && git diff -U5 --no-ext-diff --no-textconv 'HEAD~1' -- tests",
    "cd nous && git diff --unified=0 --diff-filter=M --exit-code",
    # git reads no response file: `@` revisions stay (quoted: the lexer refuses a bare brace or tilde)
    "cd nous && git log --oneline '@{{u}}..'",  # str.format: {{u}} is @{u}
    "cd nous && git diff '@~1'",
    "cd nous && git show",
    "cd nous && git show --stat HEAD",
    "cd nous && git show HEAD:tests/test_a.py",
    # pytest
    "cd nous && pytest",
    "cd nous && pytest tests/test_a.py",
    "cd nous && pytest tests/test_a.py::test_a -q",
    "cd nous && pytest -x -vv tests --tb=short",
    "cd nous && pytest -k 'a and not b' -m slow --maxfail 2 -rA",
    "cd nous && pytest --co -q tests",
    "cd nous && pytest --lf --durations=10 --no-header -ql",
    "cd nous && pytest --deselect tests/test_a.py::test_b tests",
    # ruff
    "cd nous && ruff check --no-fix --no-fix-only",
    "cd nous && ruff check --no-fix nous tests --statistics --no-fix-only",
    "cd nous && ruff check --no-fix --no-fix-only --output-format concise -q .",
    "cd nous && ruff check --no-fix-only --no-fix --select E501 --ignore F401 nous",
    "cd nous && ruff format --check",
    "cd nous && ruff format --diff nous/",
    # make
    *(f"cd nous && make {target}" for target in sorted(LINEAGE_MAKE_TARGETS)),
    "cd nous && make -k -s test lint",
    "cd nous && make --no-print-directory check",
    # how the shell gets there, and what may follow
    "cd {W}/nous && git status",
    "ls; cd nous && git log --oneline | head -5",
    "cd nous && git status && pytest -x || cat {W}/nous/tests/test_a.py",
    "cd nous && pytest -q 2>&1 | tail -20",
    "cd nous && ls | head && pytest",
    "cd nous && pytest > {W}/intentions/{RID}/pytest.log 2>&1",
]


@pytest.mark.parametrize("command", ALLOWED)
def test_each_build_command_and_option_is_allowed_with_the_switch_on(command, ws):
    command = command.format(W=ws, RID=RID)
    assert _allowed(command, ws) is True


@pytest.mark.parametrize("command", ALLOWED)
def test_with_the_switch_off_every_build_command_is_refused(command, ws):
    command = command.format(W=ws, RID=RID)
    assert _allowed(command, ws, build_commands=False) is False
    assert internal_only_call_violation(_internal(), "bash", {"command": command}, workspace_dir=str(ws)) == (
        "external"
    )  # PIN: the default is the shell as it was


REFUSED = [
    # git: writes, network, and other subcommands
    *(
        f"cd nous && git {sub}"
        for sub in (
            "push origin main",
            "commit -m x",
            "checkout main",
            "reset --hard",
            "apply x.patch",
            "config user.name x",
            "fetch",
            "pull",
            "clean -fdx",
            "stash",
            "branch -D x",
            "tag v1",
            "rebase -i main",
            "am x.mbox",
            "grep x",
            "blame tests/test_a.py",
        )
    ),
    # git: global options and escapes
    "cd nous && git -C /etc status",
    "cd nous && git -c core.pager=id log",
    "cd nous && git -c core.fsmonitor=id status",
    "cd nous && git --git-dir=/tmp/x status",
    "cd nous && git --work-tree=/ status",
    "cd nous && git --exec-path=/tmp log",
    "cd nous && git -p log",
    "cd nous && git --paginate log",
    "cd nous && git log --output=/tmp/x",
    "cd nous && git log --output /tmp/x",
    "cd nous && git diff --output=x",
    "cd nous && git log --outp=x",
    "cd nous && git diff --ext-diff",
    "cd nous && git diff --textconv",
    "cd nous && git show --textconv HEAD",
    "cd nous && git diff --no-index /etc/passwd /dev/null",
    "cd nous && git diff /etc/passwd tests/test_a.py",
    "cd nous && git diff -- /etc/passwd tests/test_a.py",
    "cd nous && git diff ../x tests/test_a.py",
    "cd nous && git diff -U /etc/passwd tests/test_a.py",
    "cd nous && git diff --unified /etc/passwd x",
    "cd nous && git diff -O/etc/passwd",
    "cd nous && git diff --submodule=diff",
    "cd nous && git log -pw",
    "cd nous && git log --show-signature",
    "cd nous && git show ~/x",
    "cd nous && git status --no-such-option",
    # pytest: plugins, config, rootdir, writes, code outside the repo
    "cd nous && pytest -p evil",
    "cd nous && pytest -pevil",
    "cd nous && pytest -vp evil",
    "cd nous && pytest -p no:cacheprovider",
    "cd nous && pytest -c /tmp/pytest.ini",
    "cd nous && pytest --rootdir=/tmp",
    "cd nous && pytest --rootdir /tmp",
    "cd nous && pytest --confcutdir=/",
    "cd nous && pytest -o addopts=-pevil",
    "cd nous && pytest --override-ini=python_files=*",
    "cd nous && pytest --pyargs os",
    "cd nous && pytest --basetemp=/tmp/x",
    "cd nous && pytest --junitxml=/tmp/x.xml",
    "cd nous && pytest --import-mode=append",
    "cd nous && pytest --roo=/tmp",
    "cd nous && pytest -W error",
    "cd nous && pytest {W}/intentions/{RID}/test_evil.py",
    "cd nous && pytest ../intentions/{RID}/test_evil.py",
    "cd nous && pytest /tmp/test_evil.py",
    "cd nous && pytest .git/hooks",
    "cd nous && pytest -k",
    # a response file: its lines become arguments (pytest 8.2+, ruff), in any position
    "cd nous && pytest @tests/args",
    "cd nous && pytest @{W}/intentions/{RID}/args",
    "cd nous && pytest -k @tests/args",
    "cd nous && pytest -- @tests/args",
    "cd nous && ruff check --no-fix --no-fix-only @tests/args",
    "cd nous && ruff format --check @tests/args",
    # a plugin: pytest reads -p NAME / -pNAME from the raw arguments before parsing them, after `--` too
    "cd nous && pytest -- -p evil",
    "cd nous && pytest tests -- -pevil",
    "cd nous && pytest -- -p no:cacheprovider",
    "cd nous && pytest -k '-p evil'",
    "cd nous && pytest -- '-p' evil",
    # ruff: writes and other configs
    "cd nous && ruff check",
    "cd nous && ruff check --no-fix",
    "cd nous && ruff check --no-fix-only",
    "cd nous && ruff check -- --no-fix --no-fix-only",
    "cd nous && ruff check --no-fix -- --no-fix-only nous",
    "cd nous && ruff format -- --check nous",
    "cd nous && ruff format -- --diff",
    "cd nous && ruff check --no-fix --no-fix-only --fix",
    "cd nous && ruff check --no-fix --no-fix-only --unsafe-fixes --fix",
    "cd nous && ruff check --no-fix --no-fix-only --add-noqa",
    "cd nous && ruff check --no-fix --no-fix-only --watch",
    "cd nous && ruff check --no-fix --no-fix-only --config /tmp/ruff.toml",
    "cd nous && ruff check --no-fix --no-fix-only --config 'lint.select=[]'",
    "cd nous && ruff check --no-fix --no-fix-only --extend /tmp/x.toml",
    "cd nous && ruff check --no-fix --no-fix-only -o /tmp/x",
    "cd nous && ruff check --no-fix --no-fix-only --output-file=/tmp/x",
    "cd nous && ruff check --no-fix --no-fix-only --cache-dir /tmp/c",
    "cd nous && ruff check --no-fix --no-fix-only /etc",
    "cd nous && ruff format",
    "cd nous && ruff format nous",
    "cd nous && ruff format --check --config x.toml",
    "cd nous && ruff clean",
    "cd nous && ruff server",
    "cd nous && ruff",
    # make: another makefile or dir, variables, eval, the default goal, other targets
    "cd nous && make",
    "cd nous && make -f /tmp/Makefile test",
    "cd nous && make --file=/tmp/Makefile test",
    "cd nous && make -C /tmp test",
    "cd nous && make -I /tmp test",
    "cd nous && make --eval='x:=$(shell id)' test",
    "cd nous && make SHELL=/bin/sh test",
    "cd nous && make test CC=evil",
    "cd nous && make -j8 test",
    "cd nous && make -ks test",
    "cd nous && make -n test",
    "cd nous && make install",
    "cd nous && make deploy",
    "cd nous && make clean",
    # shell metacharacters, chains and wrappers
    "cd nous && pytest | sh",
    "cd nous && pytest; rm -rf /",
    "cd nous && pytest && curl https://example.com",
    "cd nous && pytest $(echo -p evil)",
    "cd nous && pytest `echo x`",
    "cd nous && pytest $X",
    "cd nous && git log > /tmp/x",
    "cd nous && git log >> {W}/nous/x",
    "cd nous && pytest < /etc/passwd",
    "cd nous && pytest &",
    "cd nous && (pytest)",
    "cd nous && PYTEST_ADDOPTS=-pevil pytest",
    "cd nous && GIT_PAGER=id git log",
    "cd nous && env pytest",
    "cd nous && sh -c pytest",
    "cd nous && python3 -m pytest",
    "cd nous && /usr/bin/git status",
    "cd nous && ./pytest",
    "cd nous && uv run pytest",
    "cd nous && pytest tests/*.py",
    # where it runs: not a repo's top level, or not surely there
    "git status",
    "pytest",
    "cd nous/tests && pytest",
    "cd claude-jobs && make test",
    "cd /tmp && git status",
    "cd nous; pytest",
    "cd nous && ls; pytest",
    "cd nous || true && pytest",
    "false || cd nous && pytest",
    "cd nous | pytest",
    "true | cd nous && pytest",
    "cd nous && cd tests && pytest",
    "cd nous/.git && git status",
]


@pytest.mark.parametrize("command", REFUSED)
def test_every_escape_and_anything_off_the_option_maps_is_refused(command, ws):
    command = command.format(W=ws, RID=RID)
    assert _allowed(command, ws) is False


def test_a_repo_the_lineage_can_write_into_is_not_one_a_build_may_run_in(ws):
    """The lineage may write_file under intentions/<root>/ and claude-jobs/prompts/: a .git, Makefile or conftest.py
    it plants there must not turn pytest or make into run_python."""
    other_root = ws / "intentions" / "00000000-0000-0000-0000-000000000009"  # another lineage writes its own dir
    for planted in (
        ws / "intentions" / str(RID),
        ws / "intentions" / str(RID) / "x",
        ws / "claude-jobs",
        other_root,
        other_root / "evil",
        ws / "intentions",
    ):
        (planted / ".git").mkdir(parents=True)
        cd = planted.relative_to(ws).as_posix()
        for command in (f"cd {cd} && pytest", f"cd {cd} && make test", f"cd {cd} && git status"):
            assert _allowed(command, ws) is False, command


@pytest.mark.skipif(os.name == "nt", reason="needs symlinks")
def test_a_path_that_leaves_the_repo_through_a_symlink_is_refused(ws):
    (ws / "intentions" / str(RID)).mkdir(parents=True)
    (ws / "nous" / "out").symlink_to(ws / "intentions" / str(RID))
    (ws / "nous" / "etc").symlink_to("/etc")
    (ws / "elsewhere" / ".git").mkdir(parents=True)
    (ws / "link").symlink_to(ws / "nous")
    assert _allowed("cd nous && pytest out", ws) is False
    assert _allowed("cd nous && pytest out/test_evil.py::test_x", ws) is False
    assert _allowed("cd nous && git diff etc/passwd tests/test_a.py", ws) is False
    assert _allowed("cd nous && ruff check --no-fix --no-fix-only etc", ws) is False
    assert _allowed("cd link && pytest", ws) is True  # resolves to the repo itself


def test_a_build_command_is_not_joined_with_gh_or_runner_sh(ws):
    """A build command runs the whole command without the GitHub token, so gh or runner.sh joined to it would fail:
    the pair is refused, and each half alone is allowed."""
    runner = f"{ws}/claude-jobs/runner.sh"
    for build, token in (
        ("cd nous && git status", "gh pr view 1"),
        ("cd nous && pytest -q", "gh pr checks 1"),
        ("cd nous && make test", f"{runner} status"),
    ):
        assert _allowed(build, ws) is True and _allowed(token, ws) is True
        for command in (f"{build} && {token}", f"{token}; {build}", f"{token} | head && {build}"):
            assert _allowed(command, ws) is False, command
    for command in ALLOWED:  # every allowed build command is scrubbed
        assert lineage_runs_build(command.format(W=ws, RID=RID)) is True, command


def test_the_refusal_names_what_is_allowed(ws):
    on, off = lineage_bash_refusal_hint(build_commands=True), lineage_bash_refusal_hint(build_commands=False)
    make = "make " + "|".join(sorted(LINEAGE_MAKE_TARGETS))
    for needle in (
        "git status|log|diff|show",
        "pytest",
        "ruff check",
        "ruff format --check|--diff",
        make,
        "without gh or runner.sh",
    ):
        assert needle in on and needle not in off
    for needle in ("cat", "gh api GET", "runner.sh", "propose_action", "cd <repo> &&"):
        assert needle in on
    r, _, _ = _shell_runner(ws)
    refusal = _auth(r, _internal(), "bash", OFFERED, {"command": "cd nous && pytest -p evil"})
    assert refusal is not None and "(external)" in refusal.text and on in refusal.text
    r, _, _ = _shell_runner(ws, f099_lineage_build_commands=False)
    refusal = _auth(r, _internal(), "bash", OFFERED, {"command": "cd nous && pytest"})
    assert refusal is not None and off in refusal.text


def test_the_switch_is_on_by_default_and_independent_of_the_lineage_shell(ws):
    assert Settings(_env_file=None, ANTHROPIC_API_KEY="k").f099_lineage_build_commands is True
    r, _, events = _shell_runner(ws)
    assert _auth(r, _internal(), "bash", OFFERED, {"command": "cd nous && pytest -q"}) is None
    assert events == []
    r, _, _ = _shell_runner(ws, f099_lineage_build_commands=False)
    assert _auth(r, _internal(), "bash", OFFERED, {"command": "cd nous && pytest -q"}) is not None
    assert _auth(r, _internal(), "bash", OFFERED, {"command": "gh pr view 1"}) is None  # the shell is as before
    r, _, _ = _shell_runner(ws, f099_lineage_shell=False)
    offered = r._offered_tools(_internal(), "conversation", is_subtask=True, tool_filter=None, refuse_active=False)
    assert "bash" not in {t["name"] for t in offered}  # without the shell, no bash at all


async def test_a_build_command_is_not_a_proposal_and_a_refused_one_still_is(ws):
    executor, _state, staged = _propose(lineage_shell=True, build_commands=True, workspace_dir=str(ws))
    text, is_error = await executor(tool="bash", arguments={"command": "cd nous && pytest"}, rationale="r")
    assert is_error is True and "call it yourself" in text and staged == []
    _text, is_error = await executor(tool="bash", arguments={"command": "cd nous && git push"}, rationale="r")
    assert is_error is False and staged


@pytest.mark.parametrize(
    ("command", "build"),
    [
        ("ls", False),
        ("gh pr view 1", False),
        ("cd nous && git status", True),
        ("echo x; pytest", True),
        ("make test", True),
        ("ruff check", True),
        ("/usr/bin/git log", True),
        ("echo $(id)", True),  # unreadable: the scrub fails closed
        ("echo 'x", True),
        # a glob or a `[` is read, not taken as unreadable: the token stays for gh and runner.sh
        ("ls *.md && gh pr view 1", False),
        ("[ -f a ] && gh pr view 1", False),
        ("test -f a && gh pr view 1", False),
        ("cd nous && [ -f a ] && pytest", True),
        ("cd nous && pytest tests/*.py", True),
    ],
)
def test_dispatch_marks_a_command_that_runs_a_build_program(command, build):
    assert lineage_runs_build(command) is build


@pytest.mark.skipif(os.name == "nt", reason="a POSIX shell")
@pytest.mark.parametrize("command", ["[ -d nous ] && printenv GITHUB_TOKEN", "ls nous/* && printenv GITHUB_TOKEN"])
async def test_a_bracket_or_a_glob_does_not_cost_a_lineage_command_its_token(command, ws, secrets):
    text, _ = await _bash_dispatcher(ws).dispatch("bash", {"command": command}, context=_internal())
    assert "planted-gh-token" in text


# -- the environment of a build command ---------------------------------------------------------------------

_SECRETS = {
    "GITHUB_TOKEN": "planted-gh-token",
    "GH_TOKEN": "planted-gh-token2",
    "ANTHROPIC_API_KEY": "planted-anthropic-key",
    "DB_PASSWORD": "planted-db-password",
    "DATABASE_URL": "postgresql://nous:planted-db-url@db/nous",
    "NOUS_TEST_PLANTED_SECRET": "planted-s3cret",
}


def _bash_dispatcher(ws):
    from nous.api.builtin_tools import register_builtin_tools
    from nous.api.tools import ToolDispatcher

    d = ToolDispatcher()
    register_builtin_tools(d, SimpleNamespace(workspace_dir=str(ws)))
    return d


@pytest.fixture
def secrets(monkeypatch):
    for key, value in _SECRETS.items():
        monkeypatch.setenv(key, value)


def _no_secret(text: str) -> None:
    leaked = [key for key, value in _SECRETS.items() if value in text]
    assert leaked == [], f"leaked into the child env: {leaked}\n{text}"


@pytest.mark.skipif(shutil.which("pytest") is None, reason="needs pytest on PATH")
async def test_pytest_runs_without_any_secret_in_its_environment(ws, secrets):
    (ws / "nous" / "tests" / "test_env.py").write_text(
        "import os\n\ndef test_env():\n    print('ENV-DUMP', sorted(os.environ.items()))\n"
    )
    command = "cd nous && pytest -s -q tests/test_env.py"
    text, _ = await _bash_dispatcher(ws).dispatch("bash", {"command": command}, context=_internal())
    assert "ENV-DUMP" in text and "1 passed" in text
    _no_secret(text)
    assert "PATH" in text and "GIT_PAGER" in text


@pytest.mark.skipif(os.name == "nt", reason="a POSIX shell")
async def test_make_runs_without_any_secret_in_its_environment(ws, secrets, tmp_path_factory, monkeypatch):
    (ws / "nous" / "Makefile").write_text("test:\n\t@env\n")
    if shutil.which("make") is None:  # a stand-in that runs the recipe, so the test runs where make is missing
        bin_dir = tmp_path_factory.mktemp("bin")
        (bin_dir / "make").write_text("#!/bin/sh\nenv\n")
        (bin_dir / "make").chmod(0o755)
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    text, _ = await _bash_dispatcher(ws).dispatch("bash", {"command": "cd nous && make test"}, context=_internal())
    assert "PATH=" in text
    _no_secret(text)


@pytest.mark.skipif(shutil.which("ruff") is None, reason="needs ruff")
async def test_ruff_runs_without_any_secret_in_its_environment(ws, secrets):
    # ruff runs no repo code; the environment it and the rest of the command see is the shell's.
    command = "cd nous && ruff check --no-fix --no-fix-only --no-cache nous; printenv"
    text, _ = await _bash_dispatcher(ws).dispatch("bash", {"command": command}, context=_internal())
    assert "All checks passed" in text and "PATH=" in text
    _no_secret(text)


@pytest.mark.skipif(shutil.which("ruff") is None, reason="needs ruff")
async def test_ruff_check_does_not_write_when_the_repos_config_fixes(ws):
    """`ruff check` applies fixes when a repo's config sets fix or fix-only; --no-fix alone does not stop fix-only."""
    (ws / "nous" / "pyproject.toml").write_text('[tool.ruff]\nfix-only = true\n[tool.ruff.lint]\nselect = ["F401"]\n')
    source = ws / "nous" / "nous" / "a.py"
    source.write_text("import os\n")
    assert _allowed("cd nous && ruff check --no-fix nous", ws) is False
    command = "cd nous && ruff check --no-fix --no-fix-only --no-cache nous"
    assert _allowed(command, ws) is True
    text, _ = await _bash_dispatcher(ws).dispatch("bash", {"command": command}, context=_internal())
    assert "F401" in text  # ruff ran and found the unused import
    assert source.read_text() == "import os\n"


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
async def test_git_runs_without_the_token_and_ignores_the_repos_fsmonitor(ws, secrets):
    command = "cd nous && git status; printenv"
    text, _ = await _bash_dispatcher(ws).dispatch("bash", {"command": command}, context=_internal())
    _no_secret(text)
    assert "GIT_CONFIG_KEY_0=core.fsmonitor" in text and "GIT_CONFIG_VALUE_0=false" in text


@pytest.mark.skipif(os.name == "nt" or shutil.which("git") is None, reason="a POSIX shell and git")
async def test_a_signature_placeholder_does_not_run_the_repos_gpg_program(ws, tmp_path_factory):
    """A `%G` format placeholder verifies the commit's signature with gpg.program, which repo config names."""
    import subprocess

    repo, marker = ws / "nous", tmp_path_factory.mktemp("gpg") / "ran"
    shutil.rmtree(repo / ".git")
    fake_gpg = repo / "fake-gpg.sh"
    fake_gpg.write_text(f"#!/bin/sh\ntouch {marker}\n")
    fake_gpg.chmod(0o755)

    def git(*args: str, stdin: str = "") -> str:
        return subprocess.run(["git", *args], cwd=repo, input=stdin, capture_output=True, text=True, check=True).stdout

    git("init", "-q")
    git("config", "gpg.program", str(fake_gpg))
    tree = git("mktree").strip()
    sig = " -----BEGIN PGP SIGNATURE-----\n \n iQ==\n -----END PGP SIGNATURE-----"
    commit = f"tree {tree}\nauthor a <a@b> 0 +0000\ncommitter a <a@b> 0 +0000\ngpgsig{sig}\n\nsigned\n"
    git("update-ref", "HEAD", git("hash-object", "-t", "commit", "-w", "--stdin", stdin=commit).strip())
    command = "cd nous && git log --format=%GK"
    assert _allowed(command, ws) is True

    git("log", "--format=%GK")  # control: without the pins, git runs the repo's gpg program
    assert marker.exists()
    marker.unlink()
    await _bash_dispatcher(ws).dispatch("bash", {"command": command}, context=_internal())
    assert not marker.exists()


@pytest.mark.skipif(os.name == "nt", reason="a POSIX shell")
async def test_a_lineage_command_with_no_build_program_keeps_the_token_and_the_model_cannot_unscrub(ws, secrets):
    d = _bash_dispatcher(ws)
    text, _ = await d.dispatch("bash", {"command": "printenv GITHUB_TOKEN"}, context=_internal())
    assert "planted-gh-token" in text  # gh and runner.sh launch still get it (#733)
    forged = {"command": "cd nous && git status; printenv", "_lineage_build": False, "_lineage": False}
    text, _ = await d.dispatch("bash", forged, context=_internal())
    _no_secret(text)
