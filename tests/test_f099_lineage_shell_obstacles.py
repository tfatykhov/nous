"""F099 lineage shell: the owner's write directories (NOUS_F099_LINEAGE_WRITE_DIRS) and the commands that use them,
the runner.sh spellings, and the read programs added to the allowlist.

Pure: no database. Every allowance here is an allowlist that fails closed, so each one is pinned from both sides.
"""

from __future__ import annotations

import logging
import os
import uuid

import pytest
from test_f099_enforcement import MODES, OFFERED, _auth, _shell_runner

from nous.api.execution_context import ExecutionContext
from nous.api.tool_policy import (
    _write_path_allowed,
    internal_only_call_violation,
    lineage_bash_allowed,
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


def test_write_dirs_setting_drops_an_entry_that_resolves_out_of_place_at_load(tmp_path, caplog):
    """Text is checked first; then each entry is resolved against the workspace once at load (and again at every
    call, below): an owner's misconfigured entry shows up as a WARNING at boot."""
    (tmp_path / "research").mkdir()
    (tmp_path / "repos.json").write_text("{}")
    with caplog.at_level(logging.WARNING, logger="nous.config"):
        settings = _settings(workspace_dir=str(tmp_path), f099_lineage_write_dirs=["research", "repos.json", "new"])
    assert settings.f099_lineage_write_dirs == ["research", "new"]  # a missing one is kept
    assert any("'repos.json'" in r.getMessage() and "a file" in r.getMessage() for r in caplog.records)


@needs_symlinks
@pytest.mark.parametrize(("link", "target"), [("out", "{OUT}"), ("jobs", "{W}/claude-jobs/state"), ("top", "{W}")])
def test_write_dirs_setting_drops_a_symlinked_entry_at_load(link, target, tmp_path, caplog):
    ws, out = tmp_path / "ws", tmp_path / "out"
    (ws / "claude-jobs" / "state").mkdir(parents=True)
    out.mkdir()
    (ws / link).symlink_to(target.format(OUT=out, W=ws))
    with caplog.at_level(logging.WARNING, logger="nous.config"):
        settings = _settings(workspace_dir=str(ws), f099_lineage_write_dirs=[link, "research"])
    assert settings.f099_lineage_write_dirs == ["research"]
    assert any(repr(link) in r.getMessage() for r in caplog.records)


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


# -- mkdir, touch and cp into the write dirs -------------------------------------------------------------------------


def _write_ws(tmp_path):
    """``tmp_path/ws`` with a listed ``research/``, the root's own directory, claude-jobs, and ``tmp_path/outside``."""
    ws = tmp_path / "ws"
    for d in ("research/sub", f"intentions/{RID}", f"intentions/{OTHER}", "claude-jobs/prompts", "claude-jobs/state"):
        (ws / d).mkdir(parents=True)
    for f in ("research/a.md", "other/src.md", "other/two.md", "claude-jobs/runner.sh", "repos.json", ".env"):
        (ws / f).parent.mkdir(parents=True, exist_ok=True)
        (ws / f).write_text(f)
    (ws / "claude-jobs" / "state" / "submit_guard.enabled").write_text("")
    (tmp_path / "outside").mkdir()
    return ws


def _w(command: str, ws, extra=("research",), ctx: ExecutionContext | None = None) -> bool:
    command = command.format(W=ws, RID=RID, OTHER=OTHER)
    return lineage_bash_allowed(ctx or _internal(), command, workspace_dir=str(ws), extra_write_dirs=list(extra))


@pytest.mark.parametrize(
    "command",
    [
        "mkdir -p {W}/research/a/b",
        "mkdir {W}/research/new",
        "mkdir --parents {W}/research/x {W}/research/y",
        "mkdir -p {W}/research",  # the listed directory itself
        "mkdir -p {W}/intentions/{RID}/notes",
        "mkdir -p -- {W}/research/z",
        "touch {W}/research/x.md",
        "touch -c {W}/research/x.md {W}/research/y.md",
        "touch -am {W}/research/a.md",
        "touch --no-create {W}/research/a.md",
        "touch {W}/intentions/{RID}/x.md",
        "touch {W}/claude-jobs/prompts/p.md",
        "cp {W}/other/src.md {W}/research/copy.md",
        "cp -n {W}/other/src.md {W}/research/sub",
        "cp -nv {W}/other/src.md {W}/other/two.md {W}/research/sub/",
        "cp {W}/other/src.md {W}/research/",
        "cp {W}/other/src.md {W}/research",  # an existing directory: the copy goes into it
        "cp {W}/other/src.md {W}/other/two.md {W}/research/new/",
        "cp --no-clobber --verbose --force {W}/other/src.md {W}/research/c.md",
        "cp {W}/other/src.md {W}/claude-jobs/prompts/p.md",
        "cp {W}/research/a.md {W}/research/b.md",
        "cp {W}/claude-jobs/runner.sh {W}/research/runner-copy.sh",  # a read of claude-jobs, a write into research
        "cd {W}/research && touch x.md",
        "touch research/x",  # relative, from the workspace: the shell's cwd until a cd
        "cd research && mkdir -p a && cp {W}/other/src.md a/",
        "cd {W}/research && cp {W}/other/src.md .",
        "mkdir -p {W}/research/a && touch {W}/research/a/x",
        "mkdir -p {W}/research/a && cp {W}/other/src.md {W}/research/a",
        "cd {W}/research && mkdir -p a && cd a && touch x",
        "test -d {W}/research/a || mkdir -p {W}/research/a",
    ],
)
def test_mkdir_touch_and_cp_may_write_into_the_write_dirs(command, tmp_path):
    assert _w(command, _write_ws(tmp_path)) is True


@pytest.mark.parametrize(
    "command",
    [
        # outside the write dirs
        "mkdir {W}/other/x",
        "mkdir -p {W}/research/../other/x",
        "mkdir -p {W}/researchx/a",
        "mkdir -p {W}/claude-jobs/evil",
        "mkdir -p {W}/claude-jobs",
        "mkdir -p {W}/claude-jobs/state/x",
        "mkdir -p {W}/claude-jobs/prompts/../state/x",
        "mkdir -p {W}/intentions/{OTHER}/x",
        "mkdir -p {W}/intentions/new",
        "mkdir -p {W}/plugins/x",
        "mkdir /tmp/x",
        "mkdir {W}/x",
        "mkdir x",
        "touch {W}/other/x",
        "touch {W}/claude-jobs/runner.sh",
        "touch {W}/claude-jobs/prompts/../runner.sh",
        "touch {W}/claude-jobs/state/submit_guard.enabled",
        "touch {W}/claude-jobs/prompts/../state/submit_guard.enabled",
        "touch {W}/repos.json",
        "touch {W}/research",  # the listed directory itself: never turned into a file
        "touch {W}/intentions/{RID}",
        "cp {W}/other/src.md {W}/other/dst.md",
        "cp {W}/other/src.md {W}/claude-jobs/runner.sh",
        "cp {W}/other/src.md {W}/claude-jobs/state/submit_guard.enabled",
        "cp {W}/other/src.md {W}/claude-jobs/prompts/../state/submit_guard.enabled",
        "cp {W}/other/src.md {W}/claude-jobs/",
        "cp {W}/other/src.md {W}/research/../repos.json",
        # hidden names below a write dir
        "mkdir {W}/research/.hid",
        "mkdir -p {W}/research/a/.b/c",
        "touch {W}/research/.x",
        "cp {W}/other/src.md {W}/research/.git",
        # options off the tables
        "mkdir -m 777 {W}/research/a",
        "mkdir -m777 {W}/research/a",
        "mkdir --mode=777 {W}/research/a",
        "mkdir -v {W}/research/a",
        "mkdir -Z {W}/research/a",
        "mkdir --par {W}/research/a",  # GNU takes the abbreviation; the closed table does not
        "touch -r {W}/other/src.md {W}/research/x",
        "touch --reference={W}/other/src.md {W}/research/x",
        "touch -d 2020-01-01 {W}/research/x",
        "touch -t 202001010000 {W}/research/x",
        "touch -h {W}/research/x",
        "touch --time=atime {W}/research/x",
        "touch --ti=atime {W}/research/x",
        "cp -r {W}/other {W}/research/o",
        "cp -R {W}/other {W}/research/o",
        "cp -a {W}/other/src.md {W}/research/o",
        "cp -d {W}/other/src.md {W}/research/o",
        "cp -P {W}/other/src.md {W}/research/o",
        "cp -L {W}/other/src.md {W}/research/o",
        "cp -l {W}/other/src.md {W}/research/o",
        "cp -s {W}/other/src.md {W}/research/o",
        "cp -u {W}/other/src.md {W}/research/o",
        "cp -i {W}/other/src.md {W}/research/o",
        "cp -p {W}/other/src.md {W}/research/o",
        "cp -t {W}/research {W}/other/src.md",
        "cp --target-directory={W}/research {W}/other/src.md",
        "cp -T {W}/other/src.md {W}/research/o",
        "cp --parents other/src.md {W}/research",
        "cp --backup {W}/other/src.md {W}/research/o",
        "cp --remove-destination {W}/other/src.md {W}/research/o",
        "cp --reflink {W}/other/src.md {W}/research/o",
        "cp --recur {W}/other {W}/research/o",
        "cp --sparse=always {W}/other/src.md {W}/research/o",
        # operands: too few, an option after --, a source outside the workspace or hidden
        "mkdir",
        "mkdir -p",
        "touch",
        "cp {W}/other/src.md",
        "cp",
        "mkdir -p -- -rf",
        "touch -- -x",
        "cp -- {W}/other/src.md -r",
        "cp -- -r {W}/research/o",
        "cp - {W}/research/o",
        "cp /etc/passwd {W}/research/p",
        "cp {W}/.env {W}/research/p",
        "cp {W}/other/src.md {W}/other/two.md {W}/research/new.md",  # several sources need a directory
        "cp {W}/other/src.md {W}/research/a.md/",
        # expansions, globs, chains
        "mkdir ~/x",
        "touch ~/x",
        "cp {W}/other/*.md {W}/research/",
        "touch {W}/research/*.md",
        "mkdir {W}/research/{{a,b}}",
        'cp "$HOME/x" {W}/research/',
        "mkdir -p {W}/research/a; rm -rf {W}/other",
        "mkdir -p {W}/research/a && sh -c 'id'",
        "touch {W}/research/x > {W}/other/y",
        # where a relative target lands: only from a cwd the shell is sure of
        "cd {W}/research ; touch x.md",
        "cd {W}/research || touch x.md",
        "cd {W}/research | touch x.md",
        "cd {W}/claude-jobs && mkdir -p research/x",
        "cd {W}/claude-jobs && touch research/x",
        "cd {W}/claude-jobs && cp {W}/other/src.md research/x",
        "cd {W}/other && cp src.md two.md",
        "true || cd {W}/research && touch x.md",
        # writable from the workspace, but the shell may be in claude-jobs by then: not sure, so refused
        "cd {W}/claude-jobs ; mkdir -p research/x",
        "cd {W}/claude-jobs ; touch research/x",
        "cd claude-jobs ; cp {W}/other/src.md research/x",
        "cd {W}/claude-jobs | touch research/x",
        "cd {W}/claude-jobs || touch research/x",
    ],
)
def test_mkdir_touch_and_cp_are_refused_everywhere_else(command, tmp_path):
    assert _w(command, _write_ws(tmp_path)) is False


def test_without_a_listed_dir_only_the_old_write_dirs_take_mkdir_touch_and_cp(tmp_path):
    ws = _write_ws(tmp_path)
    assert _w("touch {W}/research/x.md", ws, extra=()) is False
    assert _w("touch {W}/intentions/{RID}/x.md", ws, extra=()) is True
    assert _w("cp {W}/other/src.md {W}/claude-jobs/prompts/p.md", ws, extra=()) is True


def test_a_lineage_without_a_root_has_no_intentions_dir_to_write(tmp_path):
    ws = _write_ws(tmp_path)
    damaged = _internal(root_intention_id=None, intention_id=None)
    assert _w("touch {W}/intentions/{RID}/x.md", ws, ctx=damaged) is False
    assert _w("touch {W}/intentions/None/x.md", ws, ctx=damaged) is False


@needs_symlinks
def test_a_write_through_a_symlink_out_of_a_write_dir_is_refused(tmp_path):
    ws = _write_ws(tmp_path)
    out = tmp_path / "outside"
    (ws / "research" / "escape").symlink_to(out)
    (ws / "research" / "evil.md").symlink_to(out / "target")  # dangling: touch and cp would create the target
    (ws / "research" / "jobs").symlink_to(ws / "claude-jobs")
    (ws / "research" / "state").symlink_to(ws / "claude-jobs" / "state" / "submit_guard.enabled")
    for command in (
        "mkdir -p {W}/research/escape/x",
        "mkdir -p {W}/research/jobs/evil",
        "touch {W}/research/evil.md",
        "touch {W}/research/escape",
        "touch {W}/research/state",
        "touch {W}/research/jobs/runner.sh",
        "cp {W}/other/src.md {W}/research/evil.md",
        "cp {W}/other/src.md {W}/research/escape",
        "cp {W}/other/src.md {W}/research/escape/",
        "cp {W}/other/src.md {W}/research/state",
        "cp {W}/other/src.md {W}/research/jobs/runner.sh",
        "cd {W}/research/escape && touch x",
        "cd {W}/research && touch escape/x",
    ):
        assert _w(command, ws) is False, command
    (ws / "research").rename(ws / "research-real")
    (ws / "research").symlink_to(out)  # the listed entry itself becomes a link out: dropped at this call
    assert _w("touch {W}/research/x.md", ws) is False


@needs_symlinks
def test_a_planted_link_where_cp_puts_the_copy_is_refused(tmp_path):
    """cp into a directory writes dir/<source's name>: that child is resolved too."""
    ws = _write_ws(tmp_path)
    (ws / "research" / "sub" / "src.md").symlink_to(tmp_path / "outside" / "t")
    assert _w("cp {W}/other/two.md {W}/research/sub", ws) is True
    assert _w("cp {W}/other/src.md {W}/research/sub", ws) is False
    assert _w("cp {W}/other/src.md {W}/research/sub/", ws) is False
    assert _w("cp {W}/other/two.md {W}/other/src.md {W}/research/sub", ws) is False


@needs_symlinks
def test_cp_names_the_copy_after_the_source_as_written(tmp_path):
    """A source `.x` that links to a plain file reads as allowed, but cp would create research/.x."""
    ws = _write_ws(tmp_path)
    (ws / "other" / ".x").symlink_to(ws / "other" / "src.md")
    (ws / "other" / "y").symlink_to(ws / "other" / "src.md")
    assert _w("cp {W}/other/y {W}/research/", ws) is True
    assert _w("cp {W}/other/.x {W}/research/", ws) is False
    assert _w("cp {W}/other/.x {W}/research/new", ws) is False  # new may be a directory by the time cp runs


@pytest.mark.skipif(os.name == "nt", reason="a POSIX shell and GNU cp")
async def test_what_the_policy_checks_is_where_the_shell_writes(tmp_path):
    """Parity with the shell that runs the command: a relative target lands in the directory the policy took as
    sure, and a copy into a directory is named after the source as written (a link's name, not its target's)."""
    from types import SimpleNamespace

    from nous.api.builtin_tools import register_builtin_tools
    from nous.api.tools import ToolDispatcher

    ws = _write_ws(tmp_path)
    (ws / "other" / "y").symlink_to(ws / "other" / "src.md")
    command = (
        f"cd {ws}/research && touch x.md && mkdir -p d && cp {ws}/other/y . && cp {ws}/other/two.md d && "
        f"cp {ws}/other/src.md d/renamed.md"
    )
    assert _w(command, ws) is True
    d = ToolDispatcher()
    register_builtin_tools(d, SimpleNamespace(workspace_dir=str(ws)))
    text, _ = await d.dispatch("bash", {"command": command}, context=_internal())
    assert text.endswith("Exit code: 0"), text
    research = ws / "research"
    assert (research / "x.md").is_file() and not (ws / "x.md").exists()
    assert (research / "y").is_file() and not (research / "y").is_symlink() and not (research / "src.md").exists()
    assert (research / "d" / "two.md").is_file() and (research / "d" / "renamed.md").read_text() == "other/src.md"


@pytest.mark.skipif(not hasattr(os, "link") or os.name == "nt", reason="needs hard links")
def test_cp_refuses_a_destination_that_is_a_hard_link(tmp_path):
    ws = _write_ws(tmp_path)
    os.link(ws / "claude-jobs" / "runner.sh", ws / "research" / "hard.sh")
    assert _w("cp {W}/other/src.md {W}/research/hard.sh", ws) is False
    assert _w("cp {W}/other/src.md {W}/research/a.md", ws) is True  # a file with one link


@pytest.mark.parametrize(
    "path",
    [
        "{W}/claude-jobs/state/submit_guard.enabled",
        "{W}/claude-jobs/prompts/../state/submit_guard.enabled",
        "{W}/claude-jobs/runner.sh",
        "{W}/claude-jobs/repos.json",
        "{W}/claude-jobs/job-20261027-101010-0a1b2c3d/nous_root",
    ],
)
def test_no_write_path_reaches_claude_jobs_outside_prompts(path, tmp_path):
    """write_file, a redirection, cp, touch and mkdir, with the write dirs listed, a reserved entry listed, or
    none: the job directories and the runner's state stay out of reach."""
    ws = _write_ws(tmp_path)
    (ws / "claude-jobs" / "job-20261027-101010-0a1b2c3d").mkdir()
    path = path.format(W=ws)
    for extra in ((), ("research",), ("claude-jobs/state", "claude-jobs", "research")):
        assert not _write_path_allowed(
            _internal(), {"path": path, "content": "x"}, str(ws), lineage_shell=True, extra_write_dirs=extra
        )
        for command in (f"echo x > {path}", f"cp {ws}/other/src.md {path}", f"touch {path}", f"mkdir -p {path}"):
            assert _w(command, ws, extra=extra) is False, (command, extra)


def test_a_repo_inside_an_extra_write_dir_is_not_a_build_repo(tmp_path):
    ws = _write_ws(tmp_path)
    (ws / "research" / "repo" / ".git").mkdir(parents=True)
    (ws / "notes" / ".git").mkdir(parents=True)
    for command, extra in (
        ("cd research/repo && git status", ["research"]),
        ("cd research/repo && pytest", ["research"]),
        ("cd notes && git status", ["notes/s0"]),  # around a listed directory
    ):
        kw = {"workspace_dir": str(ws), "build_commands": True}
        assert not lineage_bash_allowed(_internal(), command, extra_write_dirs=extra, **kw), command
        assert lineage_bash_allowed(_internal(), command, **kw), command  # PIN: without the listing, a build repo


# -- the whole shell, through the runner -----------------------------------------------------------------------------

NEW_COMMANDS = [
    "mkdir -p {W}/research/a",
    "touch {W}/research/x.md",
    "cp {W}/a.md {W}/research/a.md",
    "echo x > {W}/research/n.md",
    "test -f {W}/a.md",
    "[ -d {W}/research ]",
    "basename {W}/a.md",
    "dirname {W}/a.md",
    "date +%s",
    "diff {W}/a.md {W}/b.md",
    "bash {R} status",
    "cd {W}/claude-jobs && ./runner.sh status",
    "cd nous && git rev-parse HEAD",
    "cd nous && git merge-base HEAD main",
    # joined with gh: the classifier rates the call external, so the policy's rescue must see the write dirs too
    "mkdir -p {W}/research/a && gh pr view 1",
    "cp {W}/a.md {W}/research/c.md; gh pr checks 1",
    "echo x > {W}/research/n.md && gh pr view 1",
    "[ -f {W}/a.md ] && gh pr view 1",
]


def _whole_ws(tmp_path):
    for d in ("research", "claude-jobs/prompts", "nous/.git"):
        (tmp_path / d).mkdir(parents=True)
    for f in ("a.md", "b.md"):
        (tmp_path / f).write_text(f)
    return tmp_path


@pytest.mark.parametrize(("offered_mode", "policy_mode"), MODES)
@pytest.mark.parametrize("kind", ["continuation", "subtask", "dag_node"])
def test_every_new_command_runs_in_every_mode(tmp_path, offered_mode, policy_mode, kind):
    ws = _whole_ws(tmp_path)
    r, _, events = _shell_runner(
        ws,
        f099_lineage_write_dirs=["research"],
        tool_offered_set_enforcement_mode=offered_mode,
        tool_context_policy_mode=policy_mode,
    )
    for command in NEW_COMMANDS:
        command = command.format(W=ws, R=f"{ws}/claude-jobs/runner.sh")
        assert _auth(r, _internal(kind), "bash", OFFERED, {"command": command}) is None, command
    assert events == []


def test_the_new_commands_are_not_offered_with_the_shell_off(tmp_path):
    ws = _whole_ws(tmp_path)
    r, _, events = _shell_runner(ws, f099_lineage_shell=False, f099_lineage_write_dirs=["research"])
    offered = r._offered_tools(_internal(), "conversation", is_subtask=True, tool_filter=None, refuse_active=False)
    names = [t["name"] for t in offered]
    assert "bash" not in names  # PIN
    refusal = _auth(r, _internal(), "bash", names, {"command": f"mkdir -p {ws}/research/a"})
    assert refusal is not None and "(not_offered)" in refusal.text


def test_the_hint_lists_the_new_programs_and_the_write_dirs(tmp_path):
    from nous.api.tool_policy import lineage_bash_refusal_hint

    for build_commands in (False, True):
        hint = lineage_bash_refusal_hint(build_commands=build_commands, extra_write_dirs=["research", "notes/s0"])
        for needle in (
            "mkdir -p",
            "touch",
            "cp",
            "test",
            "[ ... ]",
            "basename",
            "dirname",
            "date",
            "diff",
            "intentions/<root>/",
            "claude-jobs/prompts/",
            "research/",
            "notes/s0/",
            "cancel a job with propose_action",
        ):
            assert needle in hint, needle
        assert len(hint) < 1200  # appended to the tool error of every refusal
    assert "research/" not in lineage_bash_refusal_hint(build_commands=False)
    r, _, _ = _shell_runner(_whole_ws(tmp_path), f099_lineage_write_dirs=["research"])
    refusal = _auth(r, _internal(), "bash", OFFERED, {"command": "rm -rf x"})
    assert refusal is not None and "research/" in refusal.text
