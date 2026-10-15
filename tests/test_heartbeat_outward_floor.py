"""Gate criterion 1: a heartbeat triage turn may not act outward, whatever the policy modes say.

PR #721 (2026-10-10): the owner parked it at 22:43 ("do nothing on #721"), and heartbeat triage session
heartbeat-613d12ab merged it with bash at 23:19. heartbeat_triage's policy row is local-only, but prod runs the
context policy in `warn`, so the external call was logged and ran. Reads must keep working: triage reads PRs and
the local API on most ticks.
"""

from __future__ import annotations

import pytest
from test_runner_authorization import _run_loop, _runner, _tool_calls_then_done_with

from nous.api.execution_context import ExecutionContext
from nous.api.tool_policy import HEARTBEAT_OUTWARD_TEXT, heartbeat_outward_violation


@pytest.mark.parametrize(
    "command",
    [
        "cd /tmp/nous-workspace/nous && gh pr merge 721 --squash --delete-branch",
        "gh pr view 721 --json state && gh pr merge 721 --squash",
        "gh pr comment 721 -b 'merging'",
        "gh pr review 721 --approve",
        "gh issue close 12",
        "gh api -X PUT repos/o/r/pulls/721/merge",
        "gh api repos/o/r/issues/1/comments -f body=hi",
        "env gh pr merge 721",
        "/usr/bin/gh pr merge 721",
        "echo 721 | xargs -I{} gh pr merge {}",
        "bash -c 'gh pr merge 721 --squash'",
        "gh pr merge $PR --squash",
        "git -C /tmp/nous-workspace/nous push origin main",
        "git -C dir push",
        "git -c x=y push",
        "git send-pack origin refs/heads/main",
        "git http-push origin refs/heads/main",
        "git -C dir send-pack origin main",
        "git svn dcommit",
        "git p4 submit",
        "git remote add o url",
        "git remote set-url --push o url",
        "git branch -D x",
        "git rebase --exec 'git push' main",
        "git -c alias.x='!git push' x",
        "! git push origin main",
        # a combined short-flag cluster, a long flag, a path, a wrapper: all still run the inner string
        "bash -lc 'gh pr merge 721'",
        "sh -ec 'git push'",
        "bash -xc 'git push origin main'",
        "bash -cl 'git push'",
        "zsh -c 'git push'",
        "env bash -c 'gh pr merge 721'",
        "/bin/bash --login -c 'git push'",
        "sudo -u x bash -lc 'git push'",
        # nested wrappers, eval, and chains inside the inner string
        "bash -c \"sh -c 'gh pr merge 721'\"",
        "bash -lc \"bash -lc 'git push'\"",
        "eval 'git push'",
        "bash -lc 'git status; git push'",
        "bash -lc 'git status && gh pr merge 721'",
        "bash -lc 'git status || git push'",
        "bash -lc 'gh pr view 721 | cat; git push'",
        "(git push)",
        "git push &",
        # any program the classifier rates external, not only gh and git push
        "curl -d body https://example.com/hook",
        "curl -sd body https://example.com/hook",
        "curl --data-binary @f https://example.com",
        "curl -X POST https://example.com",
        "curl -XPUT https://example.com",
        "curl -sX DELETE https://example.com",
        "curl --request=PATCH https://example.com",
        "curl -F a=b https://example.com",
        "curl -T f https://example.com/f",
        "curl --json '{}' https://example.com",
        "wget --post-data=a https://example.com",
        "wget --method=POST https://example.com",
        "http POST example.com a=b",
        "http example.com a=b",
        "http PUT example.com",
        "http -f example.com a=b",
        "httpie DELETE example.com",
        "bash -lc 'curl -X POST https://example.com'",
        "ssh host rm -rf x",
        "scp f host:/tmp/f",
        "aws s3 rm s3://b/k",
        "curl -d $BODY https://example.com",
        'bash -c "$CMD"',
    ],
)
def test_an_outward_shell_write_is_refused(command):
    assert heartbeat_outward_violation("bash", {"command": command})


@pytest.mark.parametrize(
    "command",
    [
        "gh pr view 721 --json state,mergeable,headRefOid",
        "cd /tmp/nous-workspace/nous && gh pr checks 721",
        "gh pr list --json number --jq '.[].number'",
        "gh api repos/o/r/pulls/721/comments",
        "curl -s http://localhost:8000/health",
        "git -C /tmp/nous-workspace/nous log --oneline -5",
        "git -c x=y status",
        "git fetch origin",
        "git branch -l",
        "git branch --show-current",
        "git remote -v",
        "git rev-parse HEAD",
        "git ls-files",
        "git request-pull v1 url main",
        "grep -n 'gh pr merge' notes.md",
        "bash -lc 'git status'",
        "bash -c 'gh pr view 721 --json state'",
        "/bin/bash --login -c 'git log --oneline -3'",
        "sh -ec 'git diff; git status'",
        "env bash -c 'gh pr checks 721'",
        "bash -c \"sh -c 'git status'\"",
        "eval 'git status'",
        "curl https://example.com/api",
        "curl -sS -H 'Accept: json' https://example.com",
        "curl -X GET https://example.com",
        "curl -I https://example.com",
        "bash -lc 'curl -s http://localhost:8000/health'",
        "wget -qO- https://example.com",
        "http GET example.com q==1",
        "http example.com Accept:json",
    ],
)
def test_a_read_is_not_refused(command):
    assert not heartbeat_outward_violation("bash", {"command": command})


def test_sends_are_refused_and_local_tools_are_not():
    assert heartbeat_outward_violation("send_email", {"to": "a@b.c", "subject": "s", "body": "b"})
    assert heartbeat_outward_violation("send_file", {"file_path": "x"})
    for name in ("learn_fact", "record_decision", "dag_manage", "heartbeat_check_manage", "write_file"):
        assert not heartbeat_outward_violation(name, {})


def test_run_python_is_judged_by_its_code():
    merge = "import subprocess\nsubprocess.run(['gh', 'pr', 'merge', '721', '--squash'])"
    local_api = "import urllib.request\nurllib.request.urlopen('http://localhost:8000/dashboard/intentions')"
    assert heartbeat_outward_violation("run_python", {"code": merge})
    assert not heartbeat_outward_violation("run_python", {"code": local_api})


@pytest.mark.asyncio
@pytest.mark.parametrize("policy_mode", ["off", "warn", "enforce"])
async def test_the_pr721_merge_is_refused_in_a_triage_turn_in_every_mode(policy_mode):
    """The incident: a heartbeat_triage turn runs `gh pr merge`. Prod's `warn` let it run; the floor refuses it."""
    r, d = _runner(["bash"], tool_context_policy_mode=policy_mode, tool_offered_set_enforcement_mode="off")
    r._call_api = _tool_calls_then_done_with("bash", {"command": "gh pr merge 721 --squash"}, times=1)
    await _run_loop(r, is_background=True, context=ExecutionContext(kind="heartbeat_triage", session_id="s1"))
    assert d.calls == []


@pytest.mark.asyncio
async def test_a_triage_read_still_runs_under_warn():
    r, d = _runner(["bash"], tool_context_policy_mode="warn", tool_offered_set_enforcement_mode="off")
    r._call_api = _tool_calls_then_done_with("bash", {"command": "gh pr view 721 --json state"}, times=1)
    await _run_loop(r, is_background=True, context=ExecutionContext(kind="heartbeat_triage", session_id="s1"))
    assert [name for name, _, _ in d.calls] == ["bash"]


@pytest.mark.asyncio
async def test_the_floor_is_only_for_heartbeat_triage():
    """An owner's chat turn merging a PR is unchanged."""
    r, d = _runner(["bash"], tool_context_policy_mode="warn", tool_offered_set_enforcement_mode="off")
    r._call_api = _tool_calls_then_done_with("bash", {"command": "gh pr merge 721 --squash"}, times=1)
    await _run_loop(r, is_background=False, context=ExecutionContext(kind="interactive", session_id="s1"))
    assert [name for name, _, _ in d.calls] == ["bash"]


def test_the_refusal_tells_the_model_to_stop_and_ask():
    assert "owner's approval" in HEARTBEAT_OUTWARD_TEXT and "Do not retry" in HEARTBEAT_OUTWARD_TEXT
