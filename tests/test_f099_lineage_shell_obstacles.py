"""F099 lineage shell: the owner's write directories (NOUS_F099_LINEAGE_WRITE_DIRS) and the commands that use them,
the runner.sh spellings, and the read programs added to the allowlist.

Pure: no database. Every allowance here is an allowlist that fails closed, so each one is pinned from both sides.
"""

from __future__ import annotations

import logging
import os
import uuid

import pytest
from test_f099_enforcement import OFFERED, _auth, _shell_runner

from nous.api.execution_context import ExecutionContext
from nous.api.tool_policy import (
    _write_path_allowed,
    internal_only_call_violation,
    lineage_write_dirs,
    write_path_refusal_hint,
)
from nous.config import Settings

# Fixed ids: parametrize ids built from them must not change between collections (xdist).
IID, RID, OTHER = (uuid.UUID(f"00000000-0000-0000-0000-00000000000{n}") for n in (1, 2, 3))
needs_symlinks = pytest.mark.skipif(os.name == "nt", reason="needs symlinks")


def _internal(kind: str = "subtask", **over) -> ExecutionContext:
    base = {
        "kind": kind,
        "session_id": "s",
        "authority": "internal_only",
        "intention_id": IID,
        "root_intention_id": RID,
    }
    return ExecutionContext(**{**base, **over})


def _settings(**over) -> Settings:
    return Settings(_env_file=None, ANTHROPIC_API_KEY="k", **over)


# -- the setting ---------------------------------------------------------------------------------------------------


def test_write_dirs_setting_parses_a_comma_list(monkeypatch):
    monkeypatch.delenv("NOUS_F099_LINEAGE_WRITE_DIRS", raising=False)
    assert _settings().f099_lineage_write_dirs == []
    monkeypatch.setenv("NOUS_F099_LINEAGE_WRITE_DIRS", "")
    assert _settings().f099_lineage_write_dirs == []
    monkeypatch.setenv("NOUS_F099_LINEAGE_WRITE_DIRS", " research , notes/s0/ ,, research")
    assert _settings().f099_lineage_write_dirs == ["research", "notes/s0"]


UNSAFE_ENTRIES = [
    "claude-jobs",
    "claude-jobs/prompts",
    "claude-jobs/state",
    "intentions",
    "intentions/abc",
    f"intentions/{RID}",
    "plugins",
    "plugins/x",
    ".",
    "./research",
    "/",
    "/etc",
    "../x",
    "a/../b",
    "a//b",
    ".hidden",
    "a/.b",
    "-rf",
    "a b",
    "~x",
    "a*",
    "a\\b",
    "a\x00b",
    "a\nb",
]


@pytest.mark.parametrize("entry", UNSAFE_ENTRIES)
def test_write_dirs_setting_drops_unsafe_entries_with_a_warning(entry, caplog):
    with caplog.at_level(logging.WARNING, logger="nous.config"):
        settings = _settings(f099_lineage_write_dirs=[entry, "research"])
    assert settings.f099_lineage_write_dirs == ["research"]  # a good entry beside a bad one is kept
    assert any(
        "NOUS_F099_LINEAGE_WRITE_DIRS" in r.getMessage() and repr(entry) in r.getMessage() for r in caplog.records
    )


def test_write_dirs_setting_drops_an_unsafe_entry_from_the_env_string(monkeypatch, caplog):
    monkeypatch.setenv("NOUS_F099_LINEAGE_WRITE_DIRS", "claude-jobs,research,../x")
    with caplog.at_level(logging.WARNING, logger="nous.config"):
        assert _settings().f099_lineage_write_dirs == ["research"]
    assert sum("NOUS_F099_LINEAGE_WRITE_DIRS" in r.getMessage() for r in caplog.records) == 2


def test_write_dirs_setting_never_fails_the_boot(caplog):
    with caplog.at_level(logging.WARNING, logger="nous.config"):
        assert _settings(f099_lineage_write_dirs=5).f099_lineage_write_dirs == []
        assert _settings(f099_lineage_write_dirs=["research", 7]).f099_lineage_write_dirs == ["research"]


# -- lineage_write_dirs ----------------------------------------------------------------------------------------------


def test_extra_dirs_join_lineage_write_dirs(tmp_path):
    ws = tmp_path.resolve()
    got = lineage_write_dirs(_internal(), str(tmp_path), lineage_shell=True, extra_dirs=["research", "notes/s0/"])
    assert got == [ws / "intentions" / str(RID), ws / "claude-jobs" / "prompts", ws / "research", ws / "notes" / "s0"]
    assert lineage_write_dirs(_internal(), str(tmp_path), lineage_shell=True) == got[:2]  # PIN: none by default


@pytest.mark.parametrize("entry", UNSAFE_ENTRIES)
def test_an_unsafe_extra_entry_is_dropped_at_call_time_too(entry, tmp_path):
    """A caller that bypasses the setting's validator gets the same filter."""
    got = lineage_write_dirs(_internal(), str(tmp_path), lineage_shell=True, extra_dirs=[entry])
    assert got == lineage_write_dirs(_internal(), str(tmp_path), lineage_shell=True)


@needs_symlinks
@pytest.mark.parametrize(
    ("link", "target"),
    [
        ("research", "{OUT}"),  # out of the workspace
        ("r", "{W}/claude-jobs"),
        ("r", "{W}/claude-jobs/state"),
        ("r", "{W}/claude-jobs/prompts"),
        ("r", "{W}/intentions"),
        ("r", "{W}/intentions/{RID}"),
        ("r", "{W}/plugins"),
        ("r", "{W}/plugins/x"),
        ("r", "{W}"),  # the workspace root
        ("r", "{W}/.secret"),  # a hidden directory
        ("r", "{W}/r"),  # a loop
    ],
)
def test_an_extra_dir_that_resolves_outside_or_into_a_reserved_place_is_dropped_each_call(link, target, tmp_path):
    ws, out = tmp_path / "ws", tmp_path / "out"
    for d in (out, ws / "claude-jobs" / "state", ws / "claude-jobs" / "prompts", ws / "intentions" / str(RID)):
        d.mkdir(parents=True)
    (ws / "plugins" / "x").mkdir(parents=True)
    (ws / ".secret").mkdir()
    base = lineage_write_dirs(_internal(), str(ws), lineage_shell=True)
    assert lineage_write_dirs(_internal(), str(ws), lineage_shell=True, extra_dirs=[link]) == [*base, ws / link]
    (ws / link).symlink_to(target.format(OUT=out, W=ws, RID=RID))  # the entry changes after load: re-resolved
    assert lineage_write_dirs(_internal(), str(ws), lineage_shell=True, extra_dirs=[link]) == base


@needs_symlinks
def test_an_extra_dir_that_holds_a_reserved_dir_is_dropped(tmp_path):
    """claude-jobs is a link into the listed directory: writing there would reach the job directories."""
    (tmp_path / "research" / "cj").mkdir(parents=True)
    (tmp_path / "claude-jobs").symlink_to(tmp_path / "research" / "cj")
    base = lineage_write_dirs(_internal(), str(tmp_path), lineage_shell=False)
    assert lineage_write_dirs(_internal(), str(tmp_path), lineage_shell=False, extra_dirs=["research"]) == base


def test_an_extra_entry_that_is_a_file_is_dropped(tmp_path):
    (tmp_path / "repos.json").write_text("{}")
    (tmp_path / "notes").mkdir()
    base = lineage_write_dirs(_internal(), str(tmp_path), lineage_shell=True)
    got = lineage_write_dirs(_internal(), str(tmp_path), lineage_shell=True, extra_dirs=["repos.json", "notes"])
    assert got == [*base, tmp_path.resolve() / "notes"]


def test_a_missing_extra_dir_is_kept_so_it_can_be_created(tmp_path):
    got = lineage_write_dirs(_internal(), str(tmp_path), lineage_shell=False, extra_dirs=["research/new"])
    assert got[-1] == tmp_path.resolve() / "research" / "new"


# -- write_file ------------------------------------------------------------------------------------------------------


def _write(tmp_path, path, **kw) -> bool:
    return _write_path_allowed(_internal(), {"path": path, "content": "x"}, str(tmp_path), **kw)


def test_write_file_may_use_an_extra_dir(tmp_path):
    (tmp_path / "research").mkdir()
    research = ["research"]
    assert _write(tmp_path, "research/a.md", extra_write_dirs=research) is True
    assert _write(tmp_path, f"{tmp_path}/research/sub/a.md", extra_write_dirs=research) is True
    assert _write(tmp_path, "research/a.md") is False  # PIN: only a listed directory
    for path in ("research/../claude-jobs/runner.sh", "researchx/a.md", "claude-jobs/state/x", "/etc/x"):
        assert _write(tmp_path, path, extra_write_dirs=research) is False, path


@needs_symlinks
def test_write_file_through_a_link_inside_an_extra_dir_is_refused(tmp_path):
    (tmp_path / "ws" / "research").mkdir(parents=True)
    (tmp_path / "out").mkdir()
    (tmp_path / "ws" / "research" / "escape").symlink_to(tmp_path / "out")
    ws = tmp_path / "ws"
    assert _write(ws, "research/escape/a.md", extra_write_dirs=["research"]) is False


def test_the_refusal_hint_lists_the_extra_dirs(tmp_path):
    hint = write_path_refusal_hint(_internal(), str(tmp_path), lineage_shell=True, extra_write_dirs=["research"])
    assert f"{tmp_path.resolve() / 'research'}/" in hint
    assert "research" not in write_path_refusal_hint(_internal(), str(tmp_path), lineage_shell=True)


def test_internal_only_call_violation_passes_the_extra_dirs_to_write_file(tmp_path):
    call = {"path": "research/a.md", "content": "x"}
    kw = {"workspace_dir": str(tmp_path), "lineage_shell": True}
    assert internal_only_call_violation(_internal(), "write_file", call, **kw) == "write_path"
    assert internal_only_call_violation(_internal(), "write_file", call, extra_write_dirs=["research"], **kw) is None


async def test_a_shell_write_into_an_extra_dir_is_not_a_proposal(tmp_path):
    from test_f099_phase2d_propose import _dispatcher

    from nous.handlers.continuation_runner import ArrivalState, make_propose_action_executor

    async def stage(tool, arguments, rationale):
        staged.append(tool)
        return uuid.uuid4()

    command = f"echo x > {tmp_path}/research/a.md"
    for extra, proposal in (([], True), (["research"], False)):
        staged: list[str] = []
        executor = make_propose_action_executor(
            ArrivalState(),
            ctx=_internal("continuation", claim_token=uuid.uuid4()),
            dispatcher=_dispatcher("bash"),
            stage=stage,
            lineage_shell=True,
            workspace_dir=str(tmp_path),
            extra_write_dirs=extra,
        )
        text, is_error = await executor(tool="bash", arguments={"command": command}, rationale="r")
        assert bool(staged) is proposal and is_error is not proposal, text
        assert proposal or "call it yourself" in text


def test_the_runner_reads_the_write_dirs_setting(tmp_path):
    call = {"path": "research/a.md", "content": "x"}
    r, _, events = _shell_runner(tmp_path, f099_lineage_write_dirs=["research"])
    assert _auth(r, _internal("continuation"), "write_file", OFFERED, call) is None
    assert events == []
    r, _, _ = _shell_runner(tmp_path)  # PIN: without the setting, as before
    refusal = _auth(r, _internal("continuation"), "write_file", OFFERED, call)
    assert refusal is not None and "(write_path)" in refusal.text and "/research/" not in refusal.text
    r, _, _ = _shell_runner(tmp_path, f099_lineage_write_dirs=["research"])
    refusal = _auth(r, _internal("continuation"), "write_file", OFFERED, {"path": "x.md", "content": "x"})
    assert refusal is not None and f"{tmp_path.resolve() / 'research'}/" in refusal.text


# -- runner.sh spellings ---------------------------------------------------------------------------------------------

JOB = "job-20261010-222549-6e37c52b"


def _shell(command: str, ws, **kw) -> str | None:
    return internal_only_call_violation(_internal(), "bash", {"command": command}, workspace_dir=str(ws), **kw)


@pytest.mark.parametrize(
    "command",
    [
        "bash {R} status",
        "bash {R} list --active",
        "bash {R} status " + JOB,
        "bash {R} launch nous 'do it' --model m",
        "bash {R} launch nous --prompt-file {W}/claude-jobs/prompts/p.md",
        "cd {W}/claude-jobs && ./runner.sh status",
        "cd claude-jobs && ./runner.sh result " + JOB,
        "cd {W}/claude-jobs && ./runner.sh launch nous p --effort high",
        "cd {W}/claude-jobs && ls | ./runner.sh status",
        "cd {W}/claude-jobs && bash {R} status",
        "bash {R} status && gh pr view 1",
    ],
)
def test_the_runner_may_be_run_through_bash_or_from_its_own_directory(command, tmp_path):
    (tmp_path / "claude-jobs" / "prompts").mkdir(parents=True)
    assert _shell(command.format(R=f"{tmp_path}/claude-jobs/runner.sh", W=tmp_path), tmp_path) is None


@pytest.mark.parametrize(
    "command",
    [
        "bash {R} launch nous p --sys-prompt x",  # the same argument check as the bare path
        "bash {R} launch nous --prompt-file /etc/passwd",
        "bash {R} cleanup " + JOB,
        "bash {R} gc",
        "bash {R} status ../x",
        "bash -x {R} status",
        "bash -- {R} status",
        "bash -c 'ls'",
        "bash -c '{R} status'",
        "bash {W}/claude-jobs/prompts/x.sh",
        "bash {W}/claude-jobs/runner.sh.bak status",
        "bash ./claude-jobs/runner.sh status",
        "bash claude-jobs/runner.sh status",
        "bash {R}",
        "bash",
        "sh {R} status",
        "./runner.sh status",  # the cwd is the workspace
        "cd {W}/claude-jobs ; ./runner.sh status",  # the cd may have failed
        "cd {W}/claude-jobs || ./runner.sh status",
        "cd {W}/claude-jobs | ./runner.sh status",
        "cd {W}/claude-jobs && cd .. && ./runner.sh status",
        "cd {W} && ./claude-jobs/runner.sh status",
        "cd {W}/claude-jobs/prompts && ./runner.sh status",
        "cd {W}/claude-jobs && ./runner.sh cleanup " + JOB,
        "cd {W}/claude-jobs && ./runner.sh launch nous p --sys-prompt x",
        "cd {W}/claude-jobs && runner.sh status",
        "true && cd {W}/claude-jobs || ./runner.sh status",
        "bash {R} status $(id)",
        "bash {R} status *",
        "bash {R} status > /tmp/x",
    ],
)
def test_every_other_spelling_of_the_runner_is_refused(command, tmp_path):
    (tmp_path / "claude-jobs" / "prompts").mkdir(parents=True)
    assert _shell(command.format(R=f"{tmp_path}/claude-jobs/runner.sh", W=tmp_path), tmp_path) == "external"


def test_a_cancel_through_bash_without_nous_root_is_refused(tmp_path):
    """Cancel stays a proposal in every spelling."""
    runner = f"{tmp_path}/claude-jobs/runner.sh"
    for command in (
        f"{runner} cancel {JOB}",
        f"bash {runner} cancel {JOB}",
        f"cd {tmp_path}/claude-jobs && ./runner.sh cancel {JOB}",
    ):
        assert _shell(command, tmp_path) == "external", command


@needs_symlinks
def test_the_runner_directory_is_compared_once_resolved(tmp_path):
    (tmp_path / "claude-jobs").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "claude-jobs")
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "runner.sh").write_text("#!/bin/sh\n")
    assert _shell(f"cd {tmp_path}/link && ./runner.sh status", tmp_path) is None  # the same directory
    assert _shell(f"cd {tmp_path}/other && ./runner.sh status", tmp_path) == "external"


def test_a_runner_through_bash_needs_the_token_so_it_cannot_join_a_build_command(tmp_path):
    (tmp_path / "nous" / ".git").mkdir(parents=True)
    runner = f"{tmp_path}/claude-jobs/runner.sh"
    assert _shell("cd nous && pytest", tmp_path, build_commands=True) is None
    for command in (f"cd nous && pytest && bash {runner} status", f"bash {runner} status; cd nous && pytest"):
        assert _shell(command, tmp_path, build_commands=True) == "external", command


def test_the_hint_names_the_accepted_runner_forms():
    from nous.api.tool_policy import lineage_bash_refusal_hint

    for build_commands in (False, True):
        hint = lineage_bash_refusal_hint(build_commands=build_commands)
        for needle in ("bash <workspace>/claude-jobs/runner.sh", "./runner.sh", "cd <workspace>/claude-jobs &&"):
            assert needle in hint
        assert "no $(...), no globs" in hint


# -- read programs: test, [, basename, dirname, date, diff -------------------------------------------------------


def _read_ws(tmp_path):
    for name in ("a.md", "b.md", "c.md", "sub/a.md", "sub/b.md", "dir1/x", "dir2/x", "dir1/.secret", "dir2/.secret"):
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(name)
    (tmp_path / ".env").write_text("SECRET=1")
    (tmp_path / "intentions").mkdir()
    return tmp_path


@pytest.mark.parametrize(
    "command",
    [
        "test -f {W}/a.md",
        "test -d {W}/intentions",
        "test ! -e {W}/nope",
        "test -s {W}/a.md && cat {W}/a.md",
        "test -e a.md || echo missing",
        "test -L {W}/a.md",
        "test x",
        "test -n 'x y'",
        "test -z ''",
        "test 3 -lt 4",
        "test a != b",
        "test {W}/a.md -nt {W}/b.md",
        "test {W}/a.md -ef {W}/a.md",
        "test ! {W}/a.md -ot {W}/b.md",
        "[ -f {W}/a.md ] && echo yes",
        "[ ! -d {W}/nope ]",
        '[ -n "x y" ]',
        "[ 3 -lt 4 ]",
        "[ a = a ]",
        "[ ] ]",  # one word: the string ]
        "[ -f 'a*' ]",  # quoted: a literal path in the workspace
        "basename {W}/a/b.md",
        "basename -s .md /etc/x.md",
        "basename -a {W}/a.md {W}/b.md",
        "basename --suffix=.md x.md",
        "dirname {W}/a/b.md",
        "dirname /etc/passwd",  # a string: nothing is read
        "dirname -z a/b c/d",
        "date",
        "date -u +%Y%m%d",
        "date +%s",
        "date -d yesterday +%F",
        "date -d @1700000000 -u",
        "date --date='2 days ago' +%F",
        "date -r {W}/a.md +%s",
        "date --reference={W}/a.md",
        "date --iso-8601=seconds",
        "date --iso-8601",
        "date --rfc-3339=seconds",
        "date -R",
        "diff {W}/a.md {W}/b.md",
        "diff -u {W}/a.md {W}/b.md | head -20",
        "diff -q {W}/a.md {W}/b.md",
        "diff -U3 {W}/a.md {W}/b.md",
        "diff -U 3 {W}/a.md {W}/b.md",
        "diff --unified=2 {W}/a.md {W}/b.md",
        "diff --unified {W}/a.md {W}/b.md",
        "diff -wB --label x --label y {W}/a.md {W}/b.md",
        "diff a.md b.md",
        "diff -- {W}/a.md {W}/b.md",
    ],
)
def test_the_new_read_programs_are_allowed(command, tmp_path):
    ws = _read_ws(tmp_path)
    assert _shell(command.format(W=ws), ws) is None


@pytest.mark.parametrize(
    "command",
    [
        # test and [: a file outside the workspace or hidden, a shape outside the grammar, a glob
        "test -f /etc/passwd",
        "test -f {W}/.env",
        "test -e {W}/../x",
        "test ! -f /etc/passwd",
        "test {W}/a.md -nt /etc/passwd",
        "test /etc/passwd -ef {W}/a.md",
        "test -nt {W}/a.md /etc/passwd",
        "test -v HOME",
        "test -t 0",
        "test -O {W}/a.md",
        "test -G {W}/a.md",
        "test -S {W}/a.md",
        "test -p {W}/a.md",
        "test -c /dev/null",
        "test -b /dev/sda",
        "test -u {W}/a.md",
        "test -k {W}/a.md",
        "test -N {W}/a.md",
        "test -f a -a -f b",
        "test -f a -o -f b",
        "test a -a b",
        "test '(' -f a ')'",
        "test a == a",
        "test a '<' b",
        "test",
        "test !",
        "test 1 2 3 4",
        "test -f a*",
        "test -f dir1/*",
        "[ -f {W}/a.md",  # no ]
        "[ -f {W}/a.md ] ]",
        "[ -f /etc/passwd ]",
        "[ -f a* ]",
        "[ -f dir1/* ]",
        "[[ -f a ]]",
        "[ -f a ] > /tmp/x",
        "[ -f $HOME ]",
        "[ -f `id` ]",
        "test -f a; rm -rf x",
        # basename and dirname: options off the table, expansions, globs
        "basename $(id)",
        "basename *",
        "basename dir1/*",
        "basename",
        "basename --zer x",
        "dirname -q x",
        "dirname",
        "dirname ~",
        # date: setting the clock, reading a file, a zone file, an operand that is not a format
        "date -s '2026-01-01'",
        "date --set=x",
        "date --set x",
        "date -f {W}/a.md",
        "date --file={W}/a.md",
        "date --fil={W}/a.md",
        "date 010203042026",
        "date -u 010203042026",
        "date -- 010203042026",
        "date +%s 010203042026",
        "date -",
        "date -r /etc/passwd",
        "date -r {W}/.env",
        "date --reference=/etc/passwd",
        "date -d 'TZ=\"/etc/passwd\" now'",
        "date -d 'tz=\"x\" now'",
        "date -Iseconds",
        "date -I",
        "date --resolution",
        "date +%s > {W}/x",
        # diff: recursion, pr, other files, too many or too few operands, directories
        "diff -r {W}/dir1 {W}/dir2",
        "diff --recursive {W}/dir1 {W}/dir2",
        "diff {W}/dir1 {W}/dir2",  # a directory: diff would compare the hidden files in both
        "diff {W}/dir1/x {W}/dir2",
        "diff -l {W}/a.md {W}/b.md",
        "diff --paginate {W}/a.md {W}/b.md",
        "diff {W}/a.md",
        "diff {W}/a.md {W}/b.md {W}/c.md",
        "diff - {W}/a.md",
        "diff {W}/a.md -",
        "diff {W}/a.md /etc/passwd",
        "diff {W}/a.md {W}/.env",
        "diff {W}/a.md {W}/nope.md",
        "diff -x '*' {W}/a.md {W}/b.md",
        "diff -X {W}/a.md {W}/a.md {W}/b.md",
        "diff -S x {W}/a.md {W}/b.md",
        "diff --from-file {W}/a.md {W}/b.md",
        "diff --to-file={W}/a.md {W}/b.md",
        "diff -D X {W}/a.md {W}/b.md",
        "diff --line-format=%L {W}/a.md {W}/b.md",
        "diff --new-line-format=%L {W}/a.md {W}/b.md",
        "diff -N {W}/a.md {W}/b.md",
        "diff -s {W}/a.md {W}/b.md",
        "diff --unif=2 {W}/a.md {W}/b.md",
        "diff {W}/*.md",
        "diff {W}/a.md {W}/b*",
        # what stays refused
        "awk 1 {W}/a.md",
        "ps aux",
        "find {W} -name x",
        "sort -o {W}/x {W}/a.md",
    ],
)
def test_the_new_read_programs_refuse_everything_off_their_tables(command, tmp_path):
    ws = _read_ws(tmp_path)
    assert _shell(command.format(W=ws), ws) == "external"


@needs_symlinks
def test_a_new_read_through_a_symlink_out_of_the_workspace_is_refused(tmp_path):
    ws = _read_ws(tmp_path / "ws")
    (tmp_path / "out.md").write_text("x")
    (ws / "link.md").symlink_to(tmp_path / "out.md")
    (ws / "linkdir").symlink_to(tmp_path)
    for command in (
        "test -f {W}/link.md",
        "[ -e {W}/linkdir ]",
        "test {W}/a.md -nt {W}/link.md",
        "date -r {W}/link.md",
        "diff {W}/a.md {W}/link.md",
        "diff {W}/a.md {W}/linkdir/out.md",
    ):
        assert _shell(command.format(W=ws), ws) == "external", command


@needs_symlinks
def test_a_relative_new_read_is_checked_from_every_directory_an_earlier_cd_could_reach(tmp_path):
    ws = _read_ws(tmp_path / "ws")
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "a.md").write_text("x")
    (ws / "sub2").mkdir()
    (ws / "sub2" / "a.md").symlink_to(tmp_path / "out" / "a.md")  # a.md from sub2 leaves the workspace
    for program in ("test -f a.md", "date -r a.md", "diff a.md a.md", "[ -s a.md ]", "test a.md -nt b.md"):
        assert _shell(program, ws) is None, program
        assert _shell(f"cd sub && {program}", ws) is None, program  # both ws/a.md and ws/sub/a.md
        assert _shell(f"cd sub2 && {program}", ws) == "external", program
        assert _shell(f"cd sub2; {program}", ws) == "external", program  # checked from both, the cd may fail


def test_diff_reads_only_regular_files(tmp_path):
    ws = _read_ws(tmp_path)
    assert _shell(f"diff {ws}/dir1/x {ws}/dir2/x", ws) is None
    assert _shell(f"diff {ws}/dir1 {ws}/dir2", ws) == "external"  # PIN: both hold a .secret
    assert _shell("cd sub && diff a.md b.md", ws) is None  # a.md and b.md are files from both cwds
    assert _shell("cd dir1 && diff x x", ws) == "external"  # {W}/x is not


def test_the_bracket_word_is_the_only_glob_word_a_test_may_hold(tmp_path):
    ws = _read_ws(tmp_path)
    assert _shell(f"[ -f {ws}/a.md ]", ws) is None
    assert _shell("[ -f a* ]", ws) == "external"
    assert _shell("[ -f 'a*' ]", ws) is None
    assert _shell("ls [ab].md", ws) == "external"  # still refused at the workspace's top level
    assert _shell("cat sub/[ab].md", ws) is None  # PIN: the existing glob rule is unchanged
    assert _shell("[ x", ws) == "external"
    assert _shell("echo [", ws) == "external"  # a [ anywhere else is a glob word, refused as before
