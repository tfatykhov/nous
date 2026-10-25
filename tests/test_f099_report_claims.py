"""F099 gate hardening B: a subtask's final report and a continuation note are checked against their tool log.

The soak failed gate criterion 5: on 2026-10-07 a continuation turn wrote "F099 test 6 FAILED. The cancel did not
take effect." when nobody had issued a cancel, stored it as a fact, and the ``learn_fact`` earned the arrival its
progress credit. ``ClaimVerifier.verify_report`` reads the verdicts and passive forms a report uses and checks them
against the calls the work recorded; an unbacked claim is annotated and, in enforce, not credited. Nothing is blocked.
"""

from __future__ import annotations

import contextlib
import re
import signal
import threading
import time
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from f099_support import (
    env_factory,  # noqa: F401
    finish,
    make_child,
    make_root,
    record,
    runner_env,  # noqa: F401
    use,
)
from sqlalchemy import select

from nous.brain import continuation
from nous.brain.continuation import Resolution, RootLimits
from nous.cognitive.claim_verifier import UNVERIFIED_MARKER, Claim, ClaimVerifier, Evidence, evidence_level, runner_path
from nous.cognitive.ledger_store import _summary
from nous.config import Settings
from nous.handlers.continuation_runner import ContinuationRunner, _ledger_row_evidence, build_arrival_prompt
from nous.storage.models import ExecutionLedgerEntry, IntentionArrival

# The note arrival 1 of root 9851eb16 committed, verbatim.
TEST_6_NOTE = (
    "F099 test 6 FAILED. The cancel did not take effect. The `sleep 240` subtask (8f10c3ba) finished at 22:06 UTC "
    "and still woke me with a continuation turn for the chain that was supposed to be cancelled. I recorded the "
    "failure as a fact (0b194534) and spawned nothing, so the chain is closed. Next step: check the cancel path "
    "(/cancel_intention or POST /intentions/{root_id}/cancel). Either it never marked the root intention "
    "cancelled, or the result inbox ignores a cancelled status when it delivers a wake-up."
)
# The fact it stored (0b194534).
TEST_6_FACT = (
    "F099 test 6 FAIL: the cancel did NOT work. Subtask 8f10c3ba (sleep 240, wake_policy=continue) finished at "
    "2026-10-07 22:06 UTC and still delivered a continuation turn to the root intention that was meant to be cancelled."
)
# All the incident's lineage recorded: the learn_fact of the false claim.
INCIDENT_LOG = [Evidence("learn_fact", {"category": "technical"})]


RUNNER = runner_path("/tmp/nous-workspace")


def _verify(text, evidence=(), **kwargs):
    return ClaimVerifier(runner_path=RUNNER).verify_report(text, list(evidence), **kwargs)


def _kinds(text):
    return [c.kind for c in _verify(text).claims]


# ---- the verifier ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("text", [TEST_6_NOTE, TEST_6_FACT])
def test_the_test_6_cancel_verdict_is_unbacked(text):
    result = _verify(text, INCIDENT_LOG)
    assert not result.verified
    assert [v.expected_tool for v in result.violations] == ["cancel_task"]
    assert result.correction.startswith(UNVERIFIED_MARKER) and "cancel did not" in result.correction.lower()


def test_a_cancel_that_was_issued_backs_the_verdict_even_when_it_failed():
    """'The cancel did not take effect' says a cancel was tried, not that it worked."""
    tried = Evidence("cancel_task", {"task_id": str(uuid.uuid4())}, ok=False)
    assert _verify(TEST_6_NOTE, [*INCIDENT_LOG, tried]).verified


def test_an_owner_cancel_backs_the_verdict():
    """The owner's cancel (REST, the companion) is no tool call: the lineage's cancel marker backs it."""
    assert _verify(TEST_6_NOTE, INCIDENT_LOG, cancelled=True).verified


def test_a_false_email_sent_claim_is_unbacked():
    result = _verify("Email sent.", INCIDENT_LOG)
    assert not result.verified and result.violations[0].expected_tool == "send_email"


def test_a_send_email_call_backs_email_sent():
    assert _verify("Email sent.", [Evidence("send_email", {"to": "tim@example.com"})]).verified
    assert _verify("Email sent to tim@example.com.", [Evidence("send_email", {"to": "tim@example.com"})]).verified


def test_a_failed_send_does_not_back_email_sent():
    assert not _verify("Email sent.", [Evidence("send_email", {"to": "tim@example.com"}, ok=False)]).verified


def test_a_send_to_someone_else_does_not_back_the_named_recipient():
    assert not _verify("Email sent to tim@example.com.", [Evidence("send_email", {"to": "bob@example.com"})]).verified


@pytest.mark.parametrize(
    ("text", "command", "verified"),
    [
        ("PR #4 merged.", "gh pr merge 4 --squash", True),
        ("PR #4 merged.", "gh pr view 4", False),
        ("Opened PR #12 for review.", "gh pr create --fill", True),
        ("Opened PR #12 for review.", "git push origin HEAD", False),
        ("All tests passed.", "uv run pytest tests/ -q", True),
        ("All tests passed.", "ls tests", False),
    ],
)
def test_a_command_backs_only_the_action_it_runs(text, command, verified):
    assert _verify(text, [Evidence("bash", {"command": command}, exit_code=0)]).verified is verified


@pytest.mark.parametrize(
    ("text", "command", "verified"),
    [
        # a command that only MENTIONS the token backs nothing
        ("PR #4 merged.", "echo merge", False),
        ("PR #4 merged.", "echo 'gh pr merge 4'", False),
        ("PR #4 merged.", "grep -r 'gh pr merge 4' docs/", False),
        ("PR #4 merged.", "# gh pr merge 4\nls", False),
        ("All tests passed.", "cat pytest.log", False),
        ("All tests passed.", "echo pytest passed", False),
        ("Opened PR #12 for review.", "echo gh pr create", False),
        # a different PR than the one claimed
        ("PR #4 merged.", "gh pr merge 5", False),
        ("PR #4 merged.", "gh pr merge https://github.com/o/r/pull/5", False),
        # real invocations
        ("PR #4 merged.", "gh pr merge 4", True),
        ("PR #4 merged.", "gh pr merge --squash", True),
        ("PR #4 merged.", "gh api -X PUT repos/o/r/pulls/4/merge", True),
        ("All tests passed.", "python -m pytest -q", True),
        ("All tests passed.", "npm run test", True),
        # wrapped invocations
        ("PR #4 merged.", "bash -lc 'cd /repo && gh pr merge 4 --squash'", True),
        ("PR #4 merged.", "env GH_TOKEN=x gh pr merge 4", True),
        ("PR #4 merged.", "cd /repo && gh pr merge 4", True),
        ("All tests passed.", "cd /repo && uv run pytest tests/ -q", True),
        ("All tests passed.", "bash -lc 'echo start; pytest -q'", True),
        ("All tests passed.", "bash -lc 'echo pytest'", False),
    ],
)
def test_report_evidence_is_the_executed_program_not_the_command_text(text, command, verified):
    assert _verify(text, [Evidence("bash", {"command": command}, exit_code=0)]).verified is verified


def test_run_python_backs_a_report_only_by_what_it_runs():
    ran = "import subprocess\nsubprocess.run(['gh', 'pr', 'merge', '4'], check=True)"
    assert _verify("PR #4 merged.", [Evidence("run_python", {"code": ran})]).verified
    assert not _verify("PR #4 merged.", [Evidence("run_python", {"code": "print('gh pr merge 4')"})]).verified


@pytest.mark.parametrize(
    "text",
    [
        "The subtask said the email was sent, but I could not confirm it.",
        "The subtask reported PR #4 merged; I did not check.",
        "According to the log, tests passed.",
        "Tim told me the PR #4 was merged.",
        "The task said: Email sent.",
        "The instructions say the tests passed before the change.",
    ],
)
def test_reported_speech_is_not_the_agents_claim(text):
    assert _verify(text).claims == []


def test_the_agents_own_statement_is_still_a_claim():
    assert [c.kind for c in _verify("I confirmed the PR #4 was merged.").claims] == ["merge"]
    assert [c.kind for c in _verify("I reported that the email was sent.").claims] == ["email"]


def test_a_test_run_that_failed_still_ran_the_tests():
    assert _verify("The tests failed.", [Evidence("bash", {"command": "pytest -q"}, exit_code=1)]).verified


@pytest.mark.parametrize(
    ("text", "command", "exit_code", "verified"),
    [
        ("All tests passed.", "pytest -q", 0, True),
        ("All tests passed.", "pytest -q", 1, False),
        ("The tests failed.", "pytest -q", 1, True),
        ("The tests failed.", "pytest -q", 0, False),
        ("CI is green.", "gh pr checks 4", 1, False),
        # no exit status, or one another command gave
        ("All tests passed.", "pytest -q", None, False),
        ("All tests passed.", "pytest -q || true", 0, False),
        ("The tests failed.", "pytest -q; echo done", 1, False),
        # "ran the tests" claims no outcome
        ("I ran the tests.", "pytest -q", 1, True),
    ],
)
def test_a_test_claim_must_match_the_exit_status(text, command, exit_code, verified):
    assert _verify(text, [Evidence("bash", {"command": command}, exit_code=exit_code)]).verified is verified


def test_a_test_claim_is_not_backed_by_a_run_python_exit():
    ran = "import subprocess\nsubprocess.run(['pytest', '-q'])"
    assert not _verify("All tests passed.", [Evidence("run_python", {"code": ran})]).verified


@pytest.mark.parametrize(
    ("command", "verified"),
    [
        # gh api defaults to GET, or POST once a field is given
        ("gh api repos/o/r/pulls/4/merge", False),
        ("gh api repos/o/r/pulls/4/merge -f merge_method=squash", False),
        ("gh api repos/o/r/pulls/4/merge --raw-field merge_method=squash", False),
        ("gh api -X GET repos/o/r/pulls/4/merge", False),
        ("gh api -X POST repos/o/r/pulls/4/merge", False),
        ("gh api -X PUT repos/o/r/pulls/4/merge", True),
        ("gh api -XPUT repos/o/r/pulls/4/merge", True),
        ("gh api --method PUT repos/o/r/pulls/4/merge", True),
        ("gh api --method=put repos/o/r/pulls/4/merge -f merge_method=squash", True),
        ("gh api -X PUT repos/o/r/pulls/5/merge", False),
    ],
)
def test_a_rest_merge_needs_put(command, verified):
    assert _verify("PR #4 merged.", [Evidence("bash", {"command": command}, exit_code=0)]).verified is verified


@pytest.mark.parametrize(
    ("command", "level"),
    [
        # an option's value is text, not the PR the command acts on
        ("gh pr merge 12 --body 'see https://github.com/o/r/pull/99'", "none"),
        ("gh pr merge 12 -b 'closes #99' --squash", "none"),
        ("gh pr merge 12 --subject 99", "none"),
        ("gh pr merge 12 -t 99", "none"),
        ("gh api -X PUT repos/o/r/pulls/12/merge -f commit_title='Merge /pull/99'", "none"),
        ("gh api -X PUT repos/o/r/pulls/12/merge -f commit_title=repos/o/r/pulls/99/merge", "none"),
        ("gh pr merge 99 --subject 12", "exact"),
        ("gh pr merge https://github.com/o/r/pull/99 --body 'replaces #12'", "exact"),
        ("gh api -X PUT repos/o/r/pulls/99/merge -f commit_title='Merge #12'", "exact"),
        # a global option before the subcommand
        ("gh -R o/r pr merge 99", "exact"),
        ("gh --repo o/r pr merge 12", "none"),
        ("gh --repo o/r pr merge 99", "exact"),
        # a boolean takes no value: listing one as value-taking would swallow the PR number
        ("gh pr merge -s 12", "none"),
        ("gh pr merge -m 12", "none"),
        ("gh pr merge -r 12", "none"),
        ("gh pr merge -d 12", "none"),
        ("gh pr merge --squash 12", "none"),
        ("gh pr merge --delete-branch 12", "none"),
        ("gh pr merge -s 99", "exact"),
    ],
)
def test_a_merge_names_its_pr_by_an_operand_not_an_option_value(command, level):
    result = _verify("PR #99 merged.", [Evidence("bash", {"command": command}, exit_code=0)])
    assert [c.evidence for c in result.claims] == [level]


@pytest.mark.parametrize(
    ("command", "verified"),
    [
        ("gh api repos/o/r/pulls", False),  # a list
        ("gh api -X GET repos/o/r/pulls -f state=open", False),
        ("gh api repos/o/r/pulls -f title=x -f head=b -f base=main", True),
        ("gh api --method POST repos/o/r/pulls --input pr.json", True),
    ],
)
def test_a_rest_pr_create_needs_post(command, verified):
    assert (
        _verify("Opened PR #12 for review.", [Evidence("bash", {"command": command}, exit_code=0)]).verified is verified
    )


def test_reported_speech_ends_at_its_clause():
    claims = _verify("The build reported a warning, but I pushed the changes.").claims
    assert [c.kind for c in claims] == ["vcs_push"]
    assert [c.kind for c in _verify("The subtask said the build broke and I merged PR #4.").claims] == ["merge"]
    # still reported within its own clause
    assert [c.kind for c in _verify("I pushed it, but the subtask said the email was sent.").claims] == ["vcs_push"]


def test_a_merge_that_exited_non_zero_does_not_back_merged():
    assert not _verify("PR #4 merged.", [Evidence("bash", {"command": "gh pr merge 4"}, exit_code=1)]).verified


def test_a_durable_row_keeps_no_command_so_any_shell_call_is_plausible():
    """The durable ledger hashes a command: names-only evidence, which cannot confirm a merge (round 3) and
    is worded as unconfirmed, not as refuted."""
    result = _verify("PR #4 merged.", [Evidence("bash", {})])
    assert [c.evidence for c in result.claims] == ["plausible"]
    assert not result.verified and "could not confirm" in result.correction.lower()


@pytest.mark.parametrize(
    ("text", "command", "verified"),
    [
        ("PR #4 merged.", "gh pr merge 4 --squash", True),
        ("PR #4 merged.", "gh pr merge 4 --auto --squash", False),
        ("PR #4 merged.", "gh pr merge 4 --auto=true", False),
        ("PR #4 merged.", "gh pr merge 4 --disable-auto", False),
        ("PR #4 merged.", "gh pr merge --help", False),
        # a local merge merges a branch, not a pull request
        ("PR #4 merged.", "git merge feature", False),
        ("I merged the pull request.", "git merge feature", False),
        ("I merged the branch.", "git merge feature", True),
    ],
)
def test_a_merge_claim_needs_a_merge_that_happened(text, command, verified):
    assert _verify(text, [Evidence("bash", {"command": command}, exit_code=0)]).verified is verified


@pytest.mark.parametrize(
    ("command", "verified"),
    [
        ("gh pr create --fill", True),
        ("gh pr create --dry-run --fill", False),
        ("gh pr create --web", False),
        ("gh pr create -w", False),
        ("gh pr create -h", False),
    ],
)
def test_a_pr_create_claim_needs_a_command_that_creates(command, verified):
    assert _verify("Opened PR #12.", [Evidence("bash", {"command": command}, exit_code=0)]).verified is verified


@pytest.mark.parametrize(
    ("command", "verified"),
    [
        ("pytest -q", True),
        ("python -m pytest tests/", True),
        ("pytest -l -q", True),  # --showlocals
        ("pytest --collect-only -q", False),
        ("pytest --co", False),
        ("python -m pytest --co tests/", False),
        ("uv run pytest --help", False),
        ("pytest -h", False),
        ("pytest --version", False),
        ("pytest -V", False),
        ("pytest --fixtures", False),
        ("pytest --markers", False),
        ("jest --listTests", False),
        ("vitest list", False),
        ("tox -l", False),
        ("tox --notest", False),
        ("nox --list-sessions", False),
        ("phpunit --list-tests", False),
        ("mvn test -Dmaven.test.skip=true", False),
        ("go test -list . ./...", False),
        ("cargo test --no-run", False),
        ("cargo test -- --list", False),
    ],
)
def test_a_test_mode_that_runs_nothing_backs_no_pass(command, verified):
    assert _verify("All tests passed.", [Evidence("bash", {"command": command}, exit_code=0)]).verified is verified


@pytest.mark.parametrize(
    ("summary", "exit_code"),
    [
        ("exit code 0; 812 chars of output, not stored", 0),
        ("exit code 1; 812 chars of output", 1),
        ("Command timed out after 30s.; exit code -9; 12 chars of output", -9),
        ("40 chars of output, error excerpt: x; exit code 0", None),  # output, not the trailer
        ("12 chars of output, not stored", None),
        ("never dispatched", None),
        (None, None),
    ],
)
def test_a_durable_bash_row_keeps_its_exit_code(summary, exit_code):
    row = SimpleNamespace(
        tool_name="bash",
        status="success",
        key_args={},
        input_text="pytest -q",
        result_summary=summary,
        side_effect_type="write",
    )
    assert _ledger_row_evidence(row).exit_code == exit_code


@pytest.mark.parametrize("exit_code", [0, 1])
def test_the_exit_code_the_ledger_store_writes_is_the_one_read_back(exit_code):
    summary = _summary(f"ok\nExit code: {exit_code}", "bash", "success")
    row = SimpleNamespace(
        tool_name="bash",
        status="success",
        key_args={},
        input_text="pytest -q",
        result_summary=summary,
        side_effect_type="write",
    )
    evidence = _ledger_row_evidence(row)
    assert evidence.exit_code == exit_code
    assert _verify("All tests passed.", [evidence]).verified is (exit_code == 0)
    assert _verify("The tests failed.", [evidence]).verified is (exit_code == 1)


@pytest.mark.parametrize(
    "text",
    [
        "No email was sent.",
        "The email was not sent.",
        "Next: open a PR.",
        "Make sure the tests pass.",
        "Once merged, redeploy.",
        "Tim merged PR #4.",  # someone else's action
        "The subtask was cancelled by Tim.",
        "Was the email sent?",
        "If the cancel failed, check the inbox.",
        'The subtask said "Email sent" but I could not confirm it.',  # quoted
    ],
)
def test_denied_planned_asked_and_quoted_actions_are_not_claims(text):
    assert _kinds(text) == []


def test_first_person_claims_are_read_too_and_not_twice():
    result = _verify("I saved the report to /tmp/a.md and the email was sent.")
    assert [c.kind for c in result.claims] == ["file_write", "email"]
    assert [c.kind for c in _verify("I sent the email to tim@example.com.").claims] == ["email"]


def test_the_annotation_carries_no_claim_of_its_own():
    """A note read again (the next arrival's prompt, a copy into a later note) is not flagged for the annotation."""
    annotated = f"Done.\n\n{_verify('Email sent. PR #4 merged.').correction}"
    assert _kinds(annotated) == []


def test_the_chat_path_is_unchanged():
    """verify (the interactive path) does not read the report-only forms."""
    assert ClaimVerifier().verify("Email sent. PR #4 merged.", [], None).verified


# ---- progress credit -------------------------------------------------------------------------------------------


async def test_an_unbacked_claim_is_not_credited_as_progress():
    """The incident's arrival claimed progress and wrote memory (the false fact), so it was credited."""
    resolution = Resolution("report", TEST_6_NOTE, True, 0.9)
    kwargs = {"since": None, "resolution": resolution, "outcome": continuation.OUTCOME_RESOLVED, "gate_reason": None}
    assert await continuation._verified_progress(None, "a", [], wrote_memory=True, **kwargs) is True
    assert (
        await continuation._verified_progress(None, "a", [], wrote_memory=True, unverified_claims=True, **kwargs)
        is False
    )


# ---- the prompt ------------------------------------------------------------------------------------------------


def test_the_prompt_flags_a_result_whose_claims_its_work_did_not_record():
    settings = Settings(_env_file=None, result_inbox_enabled=True, intentions_enabled=True, continuation_enabled=True)
    limits = RootLimits(depth=1, spawns=0, turns=0, tokens=0, stalls=0, spawn_blocked=False, escalate=None)
    row = SimpleNamespace(
        msg_type="INFORM",
        source_kind="subtask",
        source_id=uuid.uuid4(),
        created_at=datetime(2026, 10, 7, 22, 6, tzinfo=UTC),
        title="Mail Tim",
        body="Email sent.",
    )
    intention = SimpleNamespace(intent="mail Tim", depth=0, origin_kind="interactive", origin_decision_id=None)
    claim = SimpleNamespace(intentions=[intention], inbox_rows=[row])
    violations = _verify(row.body).violations
    prompt = build_arrival_prompt(claim, [], [], limits, settings, unverified=[(row, violations)])
    assert "## Claims in these results that no recorded call backs" in prompt
    assert f"Mail Tim (subtask {str(row.source_id)[:8]}): {UNVERIFIED_MARKER}" in prompt
    assert "## Claims in these results" not in build_arrival_prompt(claim, [], [], limits, settings)


# ---- end to end ------------------------------------------------------------------------------------------------

LEARN_FACT_SCHEMA = {
    "type": "object",
    "properties": {"content": {"type": "string"}, "category": {"type": "string"}},
    "required": ["content"],
}


def _register_learn_fact(env) -> list[dict]:
    calls: list[dict] = []

    async def learn_fact(**kwargs):
        calls.append(kwargs)
        return {"content": [{"type": "text", "text": "Learned."}]}

    env.dispatcher.register("learn_fact", learn_fact, LEARN_FACT_SCHEMA)
    return calls


def _cont(env) -> ContinuationRunner:
    return ContinuationRunner(
        database=env.db, settings=env.settings, runner=env.runner, heart=env.heart, brain=env.brain, bus=env.bus
    )


async def _arrival(env, root_id) -> IntentionArrival:
    async with env.db.session() as s:
        query = select(IntentionArrival).where(
            IntentionArrival.agent_id == env.agent, IntentionArrival.root_id == root_id
        )
        return (await s.execute(query)).scalar_one()


async def _incident(runner_env, **settings):  # noqa: F811
    """The 2026-10-07 shape: nobody cancelled the root; its child finished and woke it; the turn stored the false
    verdict as a fact and reported it, claiming progress."""
    env = await runner_env(
        [use("learn_fact", content=TEST_6_FACT, category="technical")],
        [use("resolve_intention", decision="report", note=TEST_6_NOTE, progress=True, confidence=0.9)],
        **settings,
    )
    learned = _register_learn_fact(env)
    root = await make_root(env)
    child = await make_child(env, root)
    await finish(env, await env.heart.subtasks.get(uuid.UUID(child.source_id)))
    await record(env, child, body="Slept 240 seconds.")
    assert await _cont(env).run_arrival(root.id) is not None
    assert learned  # the memory write happened: without the check it would earn the credit
    return await _arrival(env, root.id)


@pytest.mark.postgres_only
async def test_the_incident_arrival_is_annotated_and_not_credited(runner_env):  # noqa: F811
    arrival = await _incident(runner_env)
    assert arrival.note.startswith(TEST_6_NOTE)
    assert UNVERIFIED_MARKER in arrival.note and "cancel did not take effect" in arrival.note
    assert arrival.progress_claimed is True and arrival.progress is False  # counted in claimed_not_credited


@pytest.mark.postgres_only
async def test_warn_annotates_and_still_credits(runner_env):  # noqa: F811
    arrival = await _incident(runner_env, claim_verification_mode="warn")
    assert UNVERIFIED_MARKER in arrival.note and arrival.progress is True


@pytest.mark.postgres_only
async def test_shadow_and_off_leave_the_arrival_alone(runner_env):  # noqa: F811
    for settings in ({"claim_verification_mode": "shadow"}, {"claim_verification_reports_enabled": False}):
        arrival = await _incident(runner_env, **settings)
        assert arrival.note == TEST_6_NOTE and arrival.progress is True


async def _email_result(runner_env, *, sent: bool):  # noqa: F811
    env = await runner_env(
        [use("resolve_intention", decision="drop", note="Nothing to do.", progress=False, confidence=1)]
    )
    root = await make_root(env)
    if sent:  # what the subtask's own send_email left in the durable ledger
        async with env.db.session() as s:
            s.add(
                ExecutionLedgerEntry(
                    agent_id=env.agent,
                    session_id=f"subtask-{root.source_id[:8]}",
                    context_kind="subtask",
                    subtask_id=uuid.UUID(root.source_id),
                    tool_name="send_email",
                    side_effect_type="external",
                    key_args={"to": "tim@example.com"},
                    status="success",
                )
            )
            await s.commit()
    await record(env, root, body="Email sent to tim@example.com.")
    assert await _cont(env).run_arrival(root.id) is not None
    return str(env.model.calls[0]["messages"])


@pytest.mark.postgres_only
async def test_a_subtask_report_of_an_email_it_never_sent_is_flagged(runner_env):  # noqa: F811
    sent = await _email_result(runner_env, sent=False)
    assert "Claims in these results that no recorded call backs" in sent and UNVERIFIED_MARKER in sent


@pytest.mark.postgres_only
async def test_a_subtask_report_backed_by_its_own_send_is_not_flagged(runner_env):  # noqa: F811
    sent = await _email_result(runner_env, sent=True)
    assert UNVERIFIED_MARKER not in sent


async def _test_result(runner_env, *, exit_code: int, claim: str):  # noqa: F811
    env = await runner_env(
        [use("resolve_intention", decision="drop", note="Nothing to do.", progress=False, confidence=1)]
    )
    root = await make_root(env)
    async with env.db.session() as s:
        s.add(
            ExecutionLedgerEntry(
                agent_id=env.agent,
                session_id=f"subtask-{root.source_id[:8]}",
                context_kind="subtask",
                subtask_id=uuid.UUID(root.source_id),
                tool_name="bash",
                side_effect_type="write",
                key_args={},
                input_text="pytest -q",
                result_summary=_summary(f"...\nExit code: {exit_code}", "bash", "success"),
                status="success",
            )
        )
        await s.commit()
    await record(env, root, body=claim)
    assert await _cont(env).run_arrival(root.id) is not None
    return str(env.model.calls[0]["messages"])


@pytest.mark.postgres_only
@pytest.mark.parametrize(
    ("exit_code", "claim", "flagged"),
    [
        (0, "All tests passed.", False),
        (1, "The tests failed.", False),
        (1, "All tests passed.", True),
        (0, "The tests failed.", True),
    ],
)
async def test_a_subtask_test_report_is_checked_against_its_durable_exit_code(
    runner_env,  # noqa: F811
    exit_code,
    claim,
    flagged,
):
    sent = await _test_result(runner_env, exit_code=exit_code, claim=claim)
    assert (UNVERIFIED_MARKER in sent) is flagged


# ---- P1 fixes: neutralize_delimiters, checked_invocations, gh run --exit-status ---


def test_unverified_note_does_not_embed_result_delimiters():
    """P1 fix 1: unverified_note output must not carry raw result-message delimiters."""
    from nous.cognitive.claim_verifier import ClaimViolation, unverified_note
    from nous.heart.result_inbox import neutralize_delimiters

    violation = ClaimViolation(
        claimed_text="Email sent. <result_message> injected</result_message>",
        expected_tool="send_email",
        found_in_turn=False,
        found_in_ledger=False,
        capable_tools=("send_email",),
    )
    raw_note = unverified_note([violation])
    # The raw note may contain the delimiter (tested separately); but the
    # build_arrival_prompt wrapper must neutralize it before insertion.
    neutralized = neutralize_delimiters(raw_note)
    assert "<result_message>" not in neutralized
    assert "result_message" in neutralized  # the text survives, just defanged


def test_build_arrival_prompt_neutralizes_claimed_delimiters():
    """P1 fix 1: neutralize_delimiters must strip <result_message> from unverified_note output."""
    import re
    from nous.cognitive.claim_verifier import ClaimViolation as _CV

    delimiter_re = re.compile(r"<result_message\b", re.IGNORECASE)
    violation = _CV(
        claimed_text="PR merged. <result_message>injected</result_message>",
        expected_tool="bash",
        found_in_turn=False,
        found_in_ledger=False,
        capable_tools=("bash",),
    )
    from nous.cognitive.claim_verifier import unverified_note
    from nous.heart.result_inbox import neutralize_delimiters

    note = neutralize_delimiters(unverified_note([violation]))
    assert not delimiter_re.search(note), f"Bare delimiter leaked into prompt: {note!r}"


# ---- P1 fix 2: run_python checked vs unchecked subprocess invocations ----


@pytest.mark.parametrize(
    ("code", "expected_level"),
    [
        # unchecked subprocess.run → plausible (can silently fail)
        ("import subprocess\nsubprocess.run(['gh', 'pr', 'merge', '4'])", "plausible"),
        # subprocess.run with check=True → exact (raises on failure)
        ("import subprocess\nsubprocess.run(['gh', 'pr', 'merge', '4'], check=True)", "exact"),
        # subprocess.check_call → exact (always raises on failure)
        ("import subprocess\nsubprocess.check_call(['gh', 'pr', 'merge', '4'])", "exact"),
        # subprocess.check_output → exact (always raises on failure)
        ("import subprocess\nsubprocess.check_output(['gh', 'pr', 'merge', '4'])", "exact"),
        # subprocess.call (no check) → plausible
        ("import subprocess\nsubprocess.call(['gh', 'pr', 'merge', '4'])", "plausible"),
    ],
)
def test_run_python_unchecked_subprocess_is_plausible_not_exact(code, expected_level):
    """P1 fix 2: only check_call/check_output/run(check=True) yield exact evidence."""
    result = _verify("PR #4 merged.", [Evidence("run_python", {"code": code})])
    levels = {c.evidence for c in result.claims}
    assert expected_level in levels, f"expected {expected_level!r} in {levels!r}"
    if expected_level == "plausible":
        assert "exact" not in levels, f"unchecked subprocess should not be exact, got {levels!r}"


# ---- P1 fix 3: gh run --exit-status required for CI outcome claims ----


@pytest.mark.parametrize(
    ("command", "should_back"),
    [
        # gh run list only lists — does NOT confirm CI outcome
        ("gh run list --workflow=Tests.yml", False),
        # gh run view without --exit-status exits 0 even on a failed run
        ("gh run view 12345", False),
        # gh run watch without --exit-status same
        ("gh run watch 12345", False),
        # with --exit-status, view/watch reflect the run's outcome
        ("gh run view 12345 --exit-status", True),
        ("gh run watch 12345 --exit-status", True),
        # gh pr checks still works as before
        ("gh pr checks 4", True),
        ("gh pr checks 4 --watch", True),
    ],
)
def test_gh_run_requires_exit_status_for_ci_claims(command, should_back):
    """P1 fix 3: gh run list/view/watch without --exit-status cannot back CI pass claims."""
    result = _verify("All tests passed.", [Evidence("bash", {"command": command}, exit_code=0)])
    if should_back:
        assert result.verified, f"{command!r} should back the CI claim but didn't"
    else:
        assert not result.verified, f"{command!r} should NOT back the CI claim but did"


# ---- F099 round 3: the #754 inline P1s, the runner.sh status false positive, canaries C1-C7 ----

JOB = "job-20261021-010336-2b31b663"
OTHER_JOB = "job-20261022-000000-aaaa1111"
STATUS_OUTPUT = (
    f"Job: {JOB}\nStatus: cancelled\nPID: 4242 (alive: no)\nStarted: 2026-10-21T01:03:36Z\n"
    "Finished: 2026-10-21T01:05:15Z\nConfig:\n{}\nExit code: 1"
)


def _status_read(output=STATUS_OUTPUT, command=f"bash {RUNNER} status {JOB}", exit_code=1):
    """A `runner.sh status` call as the in-memory ledger records it: bash, exit 1, the status kept."""
    from nous.cognitive.claim_verifier import ledger_evidence
    from nous.cognitive.execution_ledger import ExecutionLedger

    ledger = ExecutionLedger(session_id="s")
    ledger.record("bash", {"command": command}, output.replace("Exit code: 1", f"Exit code: {exit_code}"), "success")
    return ledger_evidence(ledger.actions)


@pytest.mark.parametrize(
    ("text", "evidence"),
    [
        ("Email sent to x@example.com.", INCIDENT_LOG),  # C1: no send at all
        (TEST_6_NOTE, INCIDENT_LOG),  # C2: the test-6 verdict, no cancel issued
        ("Sent an email to alice@example.com.", [Evidence("send_email", {"to": "bob@example.com"})]),  # C3
        (
            "I cancelled task 8f10c3ba.",  # C4: the cancel was of another task
            [Evidence("cancel_task", {"task_id": "1b2c3d4e-0000-4000-8000-000000000000"})],
        ),
        (
            "PR #4 merged.",  # C5: an unchecked subprocess.run can fail and exit 0
            [Evidence("run_python", {"code": "import subprocess\nsubprocess.run(['gh', 'pr', 'merge', '4'])"})],
        ),
    ],
    ids=["C1-email-no-send", "C2-test-6", "C3-wrong-recipient", "C4-wrong-task", "C5-unchecked-merge"],
)
def test_canary_an_unbacked_completion_claim_fires(text, evidence):
    result = _verify(text, evidence)
    assert not result.verified
    assert result.correction.startswith(UNVERIFIED_MARKER)


@pytest.mark.parametrize(
    ("text", "evidence"),
    [
        (f"Job {JOB} was cancelled.", _status_read()),
        ("The job was cancelled.", _status_read()),  # the live arrival 0a5f2b67 sentence: no id named
        (f"{JOB} was cancelled.", _status_read()),
        (f"I cancelled job {JOB}.", [Evidence("bash", {"command": f"bash {RUNNER} cancel {JOB}"}, exit_code=0)]),
        ("Sent an email to alice@example.com.", [Evidence("send_email", {"to": "alice@example.com"})]),
        ("All tests passed.", [Evidence("bash", {"command": "pytest -q"}, exit_code=0)]),
    ],
    ids=[
        "C6-status-read",
        "C6-status-read-no-id",
        "C6-status-read-bare-id",
        "C6-runner-cancel",
        "C6-right-recipient",
        "C6-pytest",
    ],
)
def test_canary_a_backed_claim_does_not_fire(text, evidence):
    assert _verify(text, evidence).verified


@pytest.mark.parametrize(
    "text",
    [
        "The subtask said the email was sent, but I could not confirm it.",
        "The subtask reported that job 8f10c3ba was cancelled.",
    ],
)
def test_canary_reported_speech_does_not_fire(text):  # C7
    assert _verify(text).verified and _verify(text).claims == []


# P1 :1841 -- plausible evidence no longer verifies a completion claim


@pytest.mark.parametrize(
    ("text", "evidence"),
    [
        ("PR #4 merged.", [Evidence("bash", {})]),  # a durable row that kept no command
        ("Email sent.", [Evidence("send_email", {})]),  # names only
        ("Opened PR #12.", [Evidence("bash", {"command": "./ship.sh"}, exit_code=0)]),  # a script may have
    ],
)
def test_plausible_evidence_is_a_soft_violation_for_a_completion_claim(text, evidence):
    result = _verify(text, evidence)
    assert not result.verified
    assert [v.evidence for v in result.violations] == ["plausible"]
    assert "could not confirm" in result.correction.lower()
    assert "no recorded tool call" not in result.correction.lower()


def test_a_checked_subprocess_merge_still_verifies():
    code = "import subprocess\nsubprocess.run(['gh', 'pr', 'merge', '4'], check=True)"
    assert _verify("PR #4 merged.", [Evidence("run_python", {"code": code})]).verified


def test_the_note_words_unbacked_and_unconfirmed_claims_apart():
    unchecked = "import subprocess\nsubprocess.run(['gh', 'pr', 'merge', '4'])"
    result = _verify("Email sent to x@example.com. PR #4 merged.", [Evidence("run_python", {"code": unchecked})])
    assert [v.evidence for v in result.violations] == ["none", "plausible"]
    note = result.correction
    assert "No recorded tool call of this work backs" in note and "Could not confirm" in note


@pytest.mark.parametrize(
    ("text", "evidence"),
    [
        # kinds with no exact form, or that claim no completed outward action, keep plausible
        ("Saved to /tmp/report.md.", [Evidence("bash", {"command": "./build.sh"}, exit_code=0)]),
        ("All tests passed.", [Evidence("bash", {"command": "./ci.sh"}, exit_code=0)]),
        ("The cancel did not take effect.", [Evidence("dag_manage", {})]),
        ("I cancelled the task.", [Evidence("dag_manage", {})]),  # no id named: any cancel backs it
    ],
)
def test_plausible_still_backs_a_kind_without_an_exact_form(text, evidence):
    assert _verify(text, evidence).verified


# P1 :1692 -- a cancel backs only a claim about the work item it cancelled

TASK = "8f10c3ba-1111-4222-8333-444455556666"
DAG_ID = "5e6f7a8b-1111-4222-8333-444455556666"


@pytest.mark.parametrize(
    ("text", "evidence", "verified"),
    [
        ("I cancelled task 8f10c3ba.", Evidence("cancel_task", {"task_id": TASK}), True),
        (f"Task {TASK} was cancelled.", Evidence("cancel_task", {"task_id": TASK}), True),
        (
            "Task 8f10c3ba was cancelled.",
            Evidence("cancel_task", {"task_id": "9a9a9a9a-0000-4000-8000-000000000000"}),
            False,
        ),
        ("I cancelled the task.", Evidence("cancel_task", {"task_id": TASK}), True),
        ("I cancelled the 8f10c3ba subtask.", Evidence("cancel_task", {"task_id": TASK}), True),
        ("I cancelled DAG 5e6f7a8b.", Evidence("dag_manage", {"action": "cancel", "dag_id": DAG_ID}), True),
        ("I cancelled DAG 7c7c7c7c.", Evidence("dag_manage", {"action": "cancel", "dag_id": DAG_ID}), False),
        # a status read of the DAG is no cancel of it
        ("I cancelled DAG 5e6f7a8b.", Evidence("dag_manage", {"action": "status", "dag_id": DAG_ID}), False),
        # "the DAG because ..." names no DAG
        ("I cancelled the DAG because it hung.", Evidence("dag_manage", {"action": "cancel", "dag_id": DAG_ID}), True),
    ],
)
def test_a_cancel_backs_only_the_named_work_item(text, evidence, verified):
    assert _verify(text, [evidence]).verified is verified


@pytest.mark.parametrize(
    ("text", "evidence"),
    [
        # a job id and a UUID cannot be compared: not confirmed
        (f"I cancelled job {JOB}.", Evidence("cancel_task", {"task_id": TASK})),
        # a hex id the call did not keep (a names-only row)
        ("I cancelled DAG 5e6f7a8b.", Evidence("dag_manage", {"action": "cancel"})),
    ],
)
def test_a_named_work_item_the_call_cannot_show_is_unconfirmed(text, evidence):
    result = _verify(text, [evidence])
    assert [c.evidence for c in result.claims] == ["plausible"] and not result.verified


@pytest.mark.parametrize(
    ("text", "evidence"),
    [
        # a DAG or task NAME: no cancel records one, so the claim reads as naming no id, like "the DAG"
        ("I cancelled DAG nightly-sync.", Evidence("dag_manage", {"action": "cancel", "dag_id": DAG_ID})),
        (
            "I cancelled the DAG mid-run because it hung.",
            Evidence("dag_manage", {"action": "cancel", "dag_id": DAG_ID}),
        ),
        ("I cancelled task facade-1.", Evidence("cancel_task", {"task_id": TASK})),  # no hex id
        # a DAG cancel cancels the DAG: a node name it was given is not what it cancelled
        (
            "I cancelled DAG 5e6f7a8b.",
            Evidence("dag_manage", {"action": "cancel", "dag_id": DAG_ID, "node_name": "7c7c7c7c"}),
        ),
    ],
)
def test_a_cancel_of_a_named_but_unrecorded_item_is_backed_by_any_cancel(text, evidence):
    assert _verify(text, [evidence]).verified


@pytest.mark.parametrize("text", [f"I cancelled {JOB}.", f"Cancelled {JOB}.", f"I've cancelled {JOB} as asked."])
def test_a_cancel_naming_a_bare_job_id_is_a_claim(text):
    assert not _verify(text).verified
    assert _verify(text, [Evidence("bash", {"command": f"bash {RUNNER} cancel {JOB}"}, exit_code=0)]).verified


@pytest.mark.parametrize(
    ("text", "verified"),
    [
        (f"I cancelled the job ({JOB}).", True),
        (f"I cancelled the job: {JOB}.", True),
        (f"I cancelled the job ({OTHER_JOB}).", False),
        ("I cancelled the 20261021 job.", True),  # a date is no id
    ],
)
def test_the_named_id_may_follow_the_noun_in_brackets_or_after_a_colon(text, verified):
    assert (
        _verify(text, [Evidence("bash", {"command": f"bash {RUNNER} cancel {JOB}"}, exit_code=0)]).verified is verified
    )


def test_a_durable_dag_cancel_keeps_the_short_id_it_was_given():
    """`dag_manage list` prints 8-character ids and the tool takes them; a hex prefix holds no free text."""
    from nous.cognitive.ledger_store import durable_key_args

    kept = durable_key_args("dag_manage", {"action": "cancel", "dag_id": "5E6F7A8B"})
    assert kept == {"action": "cancel", "dag_id": "5e6f7a8b"}
    row = SimpleNamespace(
        tool_name="dag_manage",
        status="success",
        key_args=kept,
        input_text=None,
        result_summary=None,
        side_effect_type="write",
    )
    assert _verify("I cancelled DAG 5e6f7a8b.", [_ledger_row_evidence(row)]).verified
    assert not _verify("I cancelled DAG 7c7c7c7c.", [_ledger_row_evidence(row)]).verified
    # anything else that is no UUID is still hashed, not kept
    assert "dag_id" not in durable_key_args("dag_manage", {"action": "cancel", "dag_id": "nightly-sync"})
    # only dag_manage takes a short id: cancel_task needs a UUID, so a prefix there is still hashed
    assert "task_id" not in durable_key_args("cancel_task", {"task_id": "8f10c3ba"})


def test_the_in_memory_ledger_keeps_the_cancelled_id():
    from nous.cognitive.execution_ledger import evidence_args

    assert evidence_args("cancel_task", {"task_id": TASK, "reason": "x"}) == {"task_id": TASK}
    assert evidence_args("dag_manage", {"action": "cancel", "dag_id": DAG_ID}) == {"action": "cancel", "dag_id": DAG_ID}


# P1 :1621 -- an opening "Sent an email to <addr>" names its recipient


def test_an_opening_send_claim_carries_its_recipient():
    result = _verify("Sent an email to Alice@Example.com.", [Evidence("send_email", {"to": "bob@example.com"})])
    assert not result.verified
    assert _verify(
        "Sent an email to Alice@Example.com.", [Evidence("send_email", {"to": "alice@example.com"})]
    ).verified
    assert _verify("Sent the email.", [Evidence("send_email", {"to": "bob@example.com"})]).verified


# the runner.sh status false positive


@pytest.mark.parametrize(
    "text",
    [
        f"Job {OTHER_JOB} was cancelled.",  # another job
        f"{OTHER_JOB} was cancelled.",
        "Job aaaa1111 was cancelled.",  # another job's hex tail
        f"I cancelled job {JOB}.",  # a status read shows the state, not that this work cancelled it
        "PR #4 merged.",
        "All tests passed.",
    ],
)
def test_a_status_read_backs_only_the_cancelled_state_of_its_own_job(text):
    assert not _verify(text, _status_read()).verified


@pytest.mark.parametrize(
    ("output", "command"),
    [
        (STATUS_OUTPUT.replace("Status: cancelled", "Status: completed"), f"bash {RUNNER} status {JOB}"),
        (STATUS_OUTPUT.replace("Status: cancelled", "Status: running"), f"bash {RUNNER} status {JOB}"),
        # the status must be the runner's own output, not a second command's
        ("Status: cancelled\nExit code: 1", f"bash {RUNNER} list; echo 'Status: cancelled'"),
        ("Status: cancelled\nExit code: 1", f"echo 'Status: cancelled' # runner.sh status {JOB}"),
        (f"Job: {JOB}\nStatus: cancelled\nExit code: 1", f"bash {RUNNER} status {JOB}; echo done"),
        # a status line past the head of the output is not read
        ("x" * 400 + "\nStatus: cancelled\nExit code: 1", f"bash {RUNNER} status {JOB}"),
    ],
)
def test_only_the_runners_own_cancelled_status_backs_the_claim(output, command):
    assert not _verify(f"Job {JOB} was cancelled.", _status_read(output, command)).verified


@pytest.mark.parametrize(
    "command",
    [
        # bash runs the -c string, not the next word: an unreadable string stays one invocation
        f"""bash -c 'printf "Status: cancelled\\n"; " x/runner.sh' status {JOB}""",
        f"""bash -xc 'printf "Status: cancelled\\n"; " {RUNNER}' status {JOB}""",
        f"bash -c {RUNNER} status {JOB}",  # the runner runs with no arguments: status is its $0
        # -s: bash runs its stdin (here a heredoc) with the words as $1...
        f"""bash -s {RUNNER} status {JOB} <<'EOF'\nprintf 'Status: cancelled\\n'\nEOF""",
        # an option that takes a value
        f"bash -o pipefail {RUNNER} status {JOB}",
    ],
)
def test_a_bash_option_that_runs_something_else_is_no_runner_read(command):
    output = f"Job: {JOB}\nStatus: cancelled\nExit code: 1"
    assert _status_read(output, command)[0].runs is not None  # read, not unreadable
    assert not _verify(f"Job {JOB} was cancelled.", _status_read(output, command)).verified


@pytest.mark.parametrize(
    "command",
    [
        f"bash runner.sh status {JOB}",  # whichever runner.sh the cwd has
        f"./runner.sh status {JOB}",
        f"/tmp/evil/runner.sh status {JOB}",
        f"bash /tmp/evil/claude-jobs/runner.sh status {JOB}",
        f"bash {RUNNER} status {JOB} {OTHER_JOB}",  # one read of one job
    ],
)
def test_only_the_workspaces_runner_reads_back_a_job(command):
    assert not _verify(f"Job {JOB} was cancelled.", _status_read(command=command)).verified


JOBS_DIR = "/tmp/nous-workspace/claude-jobs"
CANCEL_ELSEWHERE = f"cd /tmp/x && bash -c './runner.sh cancel {JOB}'"


@pytest.mark.parametrize(
    "command",
    [
        f"cd {JOBS_DIR} && ./runner.sh status {JOB}",
        f"cd {JOBS_DIR} && bash runner.sh status {JOB}",
        f"cd {JOBS_DIR}/ && ./runner.sh status {JOB}",  # a trailing slash on the cd
        f"cd /tmp/nous-workspace && claude-jobs/runner.sh status {JOB}",
    ],
)
def test_a_relative_runner_after_an_absolute_cd_reads_back_a_job(command):
    assert _verify(f"Job {JOB} was cancelled.", _status_read(command=command)).verified


@pytest.mark.parametrize(
    "command",
    [
        f"cd /tmp/x && ./runner.sh status {JOB}",  # another directory's runner.sh
        f"cd {JOBS_DIR}; ./runner.sh status {JOB}",  # the cd may have failed
        f"cd claude-jobs && ./runner.sh status {JOB}",  # a relative cd: from wherever the cwd was
        f"cd {JOBS_DIR} && runner.sh status {JOB}",  # a bare name is looked up in PATH, not the cwd
        f"cd {JOBS_DIR} && R=./runner.sh && $R status {JOB}",  # a variable stays unreadable
        f"false || cd {JOBS_DIR} && ./runner.sh status {JOB}",  # the cd is skipped when the first succeeds
        f"(cd {JOBS_DIR}) && ./runner.sh status {JOB}",  # the cd ran in a subshell
        f"bash -c 'cd {JOBS_DIR}' && ./runner.sh status {JOB}",  # ... or in a child shell
        f"cd {JOBS_DIR} && ./sub/../runner.sh status {JOB}",  # `..` resolves past a symlink
    ],
)
def test_a_relative_runner_without_a_certain_absolute_cd_reads_back_nothing(command):
    assert not _verify(f"Job {JOB} was cancelled.", _status_read(command=command)).verified


@pytest.mark.parametrize(
    ("command", "exit_code", "verified"),
    [
        (f"cd {JOBS_DIR} && ./runner.sh cancel {JOB}", 0, True),
        (f"cd {JOBS_DIR} && bash runner.sh cancel {JOB}", 0, True),
        (f"cd {JOBS_DIR} && ./runner.sh cancel {JOB}", 1, False),  # the cancel failed
        (f"cd {JOBS_DIR}; ./runner.sh cancel {JOB}", 0, False),
        (f"cd /tmp/x && ./runner.sh cancel {JOB}", 0, False),
        # the same call twice: the second ran in the jobs dir and certainly succeeded
        (f"./runner.sh cancel {JOB}; cd {JOBS_DIR} && ./runner.sh cancel {JOB}", 0, True),
        (f"cd {JOBS_DIR} && ./runner.sh cancel {JOB}; cd /tmp/x && ./runner.sh cancel {JOB}", 0, False),
        # the cd into the jobs dir never runs, and the cancel that does is another directory's runner.sh
        (f"false && cd {JOBS_DIR} && ./runner.sh cancel {JOB}; {CANCEL_ELSEWHERE}", 0, False),
        (f"case x in y) cd {JOBS_DIR} && ./runner.sh cancel {JOB};; esac; {CANCEL_ELSEWHERE}", 0, False),
        # an assignment before the cd runs nothing; a line break after `&&` continues the command
        (f"X=1; cd {JOBS_DIR} && ./runner.sh cancel {JOB}", 0, True),
        (f"cd {JOBS_DIR} &&\n./runner.sh cancel {JOB}", 0, True),
        (f"cd {JOBS_DIR} && X=1; ./runner.sh cancel {JOB}", 0, False),
        (f"cd {JOBS_DIR} && X=1 && ./runner.sh cancel {JOB}", 0, False),
        (f"cd {JOBS_DIR}\n./runner.sh cancel {JOB}", 0, False),
        (f"cd {JOBS_DIR} &&\n\n# then\n./runner.sh cancel {JOB}", 0, True),
        # the cd never runs, however many blank or comment lines follow the `||`
        (f"true ||\ncd {JOBS_DIR} && ./runner.sh cancel {JOB}", 0, False),
        (f"true ||\n\ncd {JOBS_DIR} && ./runner.sh cancel {JOB}", 0, False),
        (f"true ||\n# c\ncd {JOBS_DIR} && ./runner.sh cancel {JOB}", 0, False),
        (f"true |\n\ncd {JOBS_DIR} && ./runner.sh cancel {JOB}", 0, False),
        (f"X=$Y; cd {JOBS_DIR} && ./runner.sh cancel {JOB}", 0, False),
    ],
)
def test_a_relative_runner_cancel_after_an_absolute_cd(command, exit_code, verified):
    evidence = [Evidence("bash", {"command": command}, exit_code=exit_code)]
    assert _verify(f"I cancelled job {JOB}.", evidence).verified is verified


@pytest.mark.parametrize(
    "text",
    [
        "I sent the report via Telegram.",
        "Message sent.",
        "I sent you the report on Telegram.",
        # time and content words, and the owner, name no other destination
        "I sent the report via Telegram with a summary.",
        "I sent the report via Telegram on Monday.",
        "I sent the report on time.",
        "I sent the report at 7am.",
        "I sent the report with the chart attached.",
        "I sent the report as a .docx.",
        "I sent the report to the owner.",
        "I sent the report via the Telegram bot.",
        "I sent you the report to review.",
        "I sent the email summary via Telegram.",
    ],
)
@pytest.mark.parametrize("args", [{"file_path": "/tmp/r.pdf"}, {"file_path": "/tmp/r.pdf", "chat_id": "42"}])
def test_a_telegram_send_backs_an_untargeted_send_claim(text, args):
    result = _verify(text, [Evidence("send_file", args)])
    assert result.verified and result.claims[0].evidence == "exact"


@pytest.mark.parametrize(
    "text",
    [
        "Email sent.",
        "I emailed the report.",
        "I mailed the report.",
        "I sent the report via email.",
        # a recipient or channel a Telegram send reaches only through a chat id it was given
        "I sent the report to Bob.",
        "Message sent to Bob.",
        "I sent the report via Slack.",
        "I sent the report to Tim via WhatsApp.",
        "I sent the report on WhatsApp.",
        "I sent the report to the team.",
        "I sent the report to stakeholders.",
        "I sent the report to everyone.",
        "I sent the report to them.",
        "I sent the report to bob.",
        "I sent the report to your team.",
        "I sent the report to the Telegram group.",
        "I sent the report in the email thread.",
        "I sent the report through the mail server.",
        "I sent the report by text.",
        "I sent the email.",
        "I sent the report by e-mail on Monday.",
    ],
)
def test_a_telegram_send_only_could_have_backed_a_send_elsewhere(text):
    result = _verify(text, [Evidence("send_file", {"file_path": "/tmp/r.pdf"})])
    assert not result.verified and result.claims[0].evidence == "plausible"


@pytest.mark.parametrize("text", ["Email sent to alice@example.com.", "I emailed the report to alice@example.com."])
def test_a_telegram_send_backs_no_addressed_email_claim(text):
    result = _verify(text, [Evidence("send_file", {"file_path": "/tmp/r.pdf"})])
    assert not result.verified and result.claims[0].evidence == "none"


@pytest.mark.parametrize("text", ["Message sent to alice@example.com.", "Messages were sent to alice@example.com."])
def test_a_passive_send_names_its_recipient(text):
    assert _verify(text, [Evidence("send_email", {"to": "alice@example.com"})]).verified
    result = _verify(text, [Evidence("send_email", {"to": "bob@x.io"})])
    assert [c.evidence for c in result.claims] == ["none"]


@pytest.mark.parametrize(
    "text",
    ["Message sent by email.", "Message sent by text.", "Message sent by Friday.", "Message sent by this morning."],
)
def test_a_send_by_a_channel_or_a_time_is_still_the_agents_claim(text):
    # "by" names a channel or a time here, not someone else who sent it
    assert [c.kind for c in _verify(text).claims] == ["email"]
    assert not _verify(text).verified


def test_a_send_by_someone_else_is_not_the_agents_claim():
    assert _verify("Message sent by the scheduler.").claims == []


# THE RULE (claim_verifier._ELSEWHERE; the NOUS_CLAIM_VERIFICATION_REPORTS_ENABLED row says the same): with one
# successful send_file (the owner's Telegram chat), a send claim is "exact" unless its clause names a
# DIFFERENT destination, then "plausible"; one naming an e-mail address is "none". A different destination
# is mail (as the verb, the thing sent, the channel or the send_email tool), a named channel other than
# Telegram, or a recipient (a name, a handle, a channel, a person or group) after "to", "shared ... with" or
# "cc". You, the owner, the user, the chat, a DM, the bot and Telegram are not; nor is an infinitive, a
# format, a language, an acronym, a time, an idiom ("due to", "a link to", "converted to"), a content word,
# a tool other than send_email, a link, or anything after via/through/using that is no channel or mail.
# The owner's own name is not known to the verifier, so "to Tim" stays plausible.
E, P = "exact", "plausible"
SEND_TAILS = [
    ("", E),
    # times ("by" a time is no destination)
    (" on Monday", E),
    (" on time", E),
    (" at 7am", E),
    (" today", E),
    (" tonight", E),
    (" this morning", E),
    (" by 5pm", E),
    (" by Friday", E),
    (" by tomorrow", E),
    (" by this morning", E),
    # content
    (" with a summary", E),
    (" with the chart attached", E),
    (" as a .docx", E),
    (" as PDF", E),
    (" in a zip", E),
    (" in two parts", E),
    (" with the email summary", E),
    (" with an email-style layout", E),
    (" with Gmail-style formatting", E),
    (" in plain text", E),
    (" with a link to the report", P),  # as on main (main's rule finds it)
    # format conversions and other "to"s that name no one
    (" converted to PDF", P),  # as on main (main's rule finds it)
    (" converted to HTML", P),  # as on main (main's rule finds it)
    (" converted to DOCX", P),  # as on main (main's rule finds it)
    (" converted to CSV", P),  # as on main (main's rule finds it)
    (" converted to JSON", P),  # as on main (main's rule finds it)
    (" converted to pdf", P),  # as on main (main's rule finds it)
    (" converted to Markdown", P),  # as on main (main's rule finds it)
    (" translated to French", P),  # as on main (main's rule finds it)
    (" to review", E),
    (" to read on Monday", E),
    (" up to date", E),
    (" due to a delay", P),  # as on main (main's rule finds it)
    (" according to plan", P),  # as on main (main's rule finds it)
    (" moved to Friday", P),  # as on main (main's rule finds it)
    (" to the end", P),  # as on main (main's rule finds it)
    # you, the owner, the user, the chat
    (" to you", E),
    (" to the owner", E),
    (" to the user", E),
    (" to your DMs", E),
    (" to me", E),
    (" to Tim", P),  # the owner's name is not known to the verifier
    # Telegram
    (" via Telegram", E),
    (" on Telegram", E),
    (" to Telegram", E),
    (" through the bot", E),
    (" over the chat", E),
    (" via the Telegram bot", E),
    (" via DM", E),
    (" over Telegram", E),
    # the sending tool, by its name (not any word with "_": "via send_slack" is another transport), and links
    (" via send_file", E),
    (" using send_file", E),
    (" through send_file", E),
    (" via a link", P),  # as on main (main's rule finds it)
    (" as a link", E),
    (" via a shared link", P),  # as on main (main's rule finds it)
    (" through send_email", P),
    (" via send_email", P),  # the mail tool names mail
    # mail
    (" via email", P),
    (" by email", P),
    (" by e-mail", P),
    (" over email", P),
    (" through email", P),
    (" using email", P),
    (" in the email thread", P),
    (" through the mail server", P),
    (" via e-mail", P),
    # another channel
    (" via Gmail", P),
    (" via SMS", P),
    (" by text", P),
    (" via Slack", P),
    (" on WhatsApp", P),
    (" on Signal", P),
    (" via Teams", P),
    (" on Discord", P),
    (" over iMessage", P),
    (" in Outlook", P),
    # another recipient
    (" to Bob", P),
    (" to @bob", P),
    (" to #general", P),
    (" in #general", P),
    (" to the team channel", P),
    (" to the Telegram group", P),
    (" to the Telegram channel", P),
    (" to stakeholders", P),
    (" to them", P),
    (" to your team", P),
    (" to the team", P),
    (" to bob", P),
    (" to everyone", P),
    (" to Google Drive", P),
    (" to the CEO", P),
    (" to Éric", P),
    (" to HR", P),  # an acronym that is no format is a recipient
    (" to IT", P),
    (" to QA", P),
    (" to ALICE", P),
    (" to S3", P),
    (" to 3 people", P),
    (" to 12 stakeholders", P),
    (" to Иван", P),
    (" to Łukasz", P),
    (" to O'Brien", P),
    (" to _Bob_", P),
    (" to Mark", P),  # a capitalised word is a name, not an infinitive
    (" to mark the changes", P),  # as on main (main's rule finds it)
    (" to May", P),  # as on main (main's rule finds it)
    (" moved to 5pm", P),  # as on main (main's rule finds it)
    # any transport after via/through/using/over/on that is not Telegram, a tool, a link or a time
    (" via SMTP", P),
    (" via sendmail", P),
    (" via msmtp", P),
    (" via curl", P),
    (" via the API", P),
    (" via SendGrid", P),
    (" via Mailgun", P),
    (" via a webhook", P),
    (" through Matrix", P),
    (" using Google Chat", P),
    (" over Zulip", P),
    (" on Skype", P),
    (" on the wiki", P),
    (" over the weekend", E),
    (" on 24 October", E),
    (" via 3 people", P),
    (" on request", E),
    (" via the chat here", E),
    # combined
    (" via Telegram on Monday with a summary", E),
    (" as PDF to the owner", E),
    (" to you on time", E),
    (" via a link to the report", P),  # as on main (main's rule finds it)
    (" via send_file at 7am", E),
    (" to the user as a .docx", E),
    (" on time via email", P),
    (" to you via Slack", P),
    (" on Monday to Bob", P),
    (" via a link to Bob", P),
    (" with a summary to stakeholders", P),
    (" converted to PDF by email", P),
    (" via Telegram to Bob", P),
    (" to the owner and Bob", P),
    (" to you and the team", P),
    (" to you and @bob", P),
    (" to you and on time", E),
    (" to you and bob", P),
    (" to you and stakeholders", P),
    (" to you and cc'd the team", P),
    (" to you and bcc'd Bob", P),
    (" to you and bob@x.io", P),
    (" to you and cc'd bob@x.io", P),
    (" to the owner and alice@x.io", P),
    (" to you and archived the draft", E),
    (" to you and then archived it", E),
    (" and cc'd the team", P),
    (" and cc'd Bob", P),
    (" and bcc'd legal", P),
    (" and copied in Bob", P),
    (" and copied the file", E),
    (" to you and Will", P),  # a capitalised word is a name, not "will"
    (" to the bot and Bob", P),
    (" to your DMs and Bob", P),
    (" to yourself and Bob", P),
    (" to Telegram and Bob", P),
    (" and copied README.md", E),
    (" and copied PDF", E),
    (" and copied Bob's notes", E),
    # "on" or "over" a topic or a time is no destination
    (" on the outage", E),
    (" on Q3 revenue", P),  # a capitalised word after on/over reads as a place
    (" on the project status", E),
    (" on it", E),
    (" on the 24th", E),
    (" on Oct 24", E),
    (" on startup", E),
    (" on my end", E),
    (" over lunch", E),
    (" over the past hour", E),
    (" on the website", P),
    (" on the team channel", P),
    (" on GitHub", P),
    (" on @bob", P),
    (" on schedule", E),
    (" on hold", E),
    (" via the Telegram group", P),
    (" via Slack-style formatting", P),  # as on main (main's rule finds it)
    (" and cc'd you", E),
    (" and bcc'd the owner", E),
    (" and copied bob@x.io", P),
    (" and copied @bob", P),
    (" and copied in the team", P),
]
SEND_CLAIMS = ["I sent the report{}.", "I forwarded the summary{}.", "Message sent{}.", "I sent the message{}."]


@pytest.mark.parametrize(("tail", "level"), SEND_TAILS, ids=[tail.strip() or "-" for tail, _ in SEND_TAILS])
@pytest.mark.parametrize("claim", SEND_CLAIMS)
def test_the_send_destination_matrix(claim, tail, level):
    result = _verify(claim.format(tail), [Evidence("send_file", {"file_path": "/tmp/r.pdf"})])
    assert [c.evidence for c in result.claims] == [level]
    assert result.verified is (level == E)


def test_a_send_that_landed_in_the_chat_is_no_first_person_claim():
    # the chat path's rule (_NOT_ELSEWHERE): the effect landed in the reply; the report's own form is a claim
    for tail, level in ((" in the chat", E), (" to your chat", E), (" with a link to the dashboard", P)):
        assert _verify(f"I sent the report{tail}.").claims == []
        result = _verify(f"Message sent{tail}.", [Evidence("send_file", {"file_path": "/tmp/r.pdf"})])
        assert [c.evidence for c in result.claims] == [level]  # "to the dashboard": main's rule finds it


@pytest.mark.parametrize("claim", SEND_CLAIMS)
def test_a_send_to_an_address_is_never_a_telegram_send(claim):
    result = _verify(claim.format(" to alice@example.com"), [Evidence("send_file", {"file_path": "/tmp/r.pdf"})])
    assert [c.evidence for c in result.claims] == ["none"]


@pytest.mark.parametrize(
    ("tail", "level"),
    [
        (" to the user's team", P),
        (" to the owner’s team", P),
        (" to your team", P),
        (" to the user's chat", E),
        (" to the user's DMs", E),
        (" to the user's Telegram", E),
        (" to the user's inbox", P),  # an inbox is mail: a send_file never lands in one
        (" to your inbox", P),
        # a Telegram group or channel is another chat, possessive or not
        (" to the user's Telegram group", P),
        (" to the owner's Telegram channel", P),
    ],
)
def test_a_possessive_of_the_owner_is_self_only_for_their_chat(tail, level):
    result = _verify(f"I sent the report{tail}.", [Evidence("send_file", {"file_path": "/tmp/r.pdf"})])
    assert [c.evidence for c in result.claims] == [level]


@pytest.mark.parametrize(
    ("tail", "level"),
    [
        # a listed word followed by the noun it modifies is no time, format, self or sent thing: the noun is
        # the recipient
        (" to the next reviewer", P),
        (" to the last person", P),
        (" to the end user", P),
        (" to the end-user", P),
        (" to the same team", P),
        (" to the previous owner", P),
        (" to the latest hire", P),
        (" to the top reviewer", P),
        (" to the following people", P),
        (" to today's attendees", P),
        (" to Friday's speakers", P),
        (" to Jan", P),  # a month abbreviation is a time only before a day number
        (" to French colleagues", P),
        (" to the user group", P),
        (" to the bot team", P),
        (" to the bot's owner", P),
        (" to the user's Telegram DMs", E),
        (" via the chat admins", P),
        (" to you and the user group", P),
        (" and cc'd the owner team", P),
        (" with a link to the report team", P),
        # ... and alone, before a time noun, before "of" or before a function word, it still is
        (" by the end of the day", E),
        (" at the end of the week", E),
        (" the next morning", E),
        (" next week", E),
        (" in the last hour", E),
        (" over the last hour", E),
        (" and moved the sync to the next day", P),  # as on main (main's rule finds it)
        (" and moved the sync to the end of the month", P),  # as on main (main's rule finds it)
        (" and moved the sync to next week", P),  # as on main (main's rule finds it)
        (" and moved the sync to the same time", P),  # as on main (main's rule finds it)
        (" and moved the sync to Monday morning", P),  # as on main (main's rule finds it)
        (" and moved the sync to Oct 24", P),  # as on main (main's rule finds it)
        (" and moved the sync to 5pm today", P),  # as on main (main's rule finds it)
        (" to the end of the thread", P),  # as on main (main's rule finds it)
        (" to you directly", E),
        (" to the user as a PDF", E),
        (" via the Telegram bot", E),
        (" to your Telegram DMs", E),
        (" so we are on the same page", E),
    ],
)
def test_a_listed_word_excludes_only_the_phrase_it_heads(tail, level):
    result = _verify(f"I sent the report{tail}.", [Evidence("send_file", {"file_path": "/tmp/r.pdf"})])
    assert [c.evidence for c in result.claims] == [level]


@pytest.mark.parametrize(
    ("text", "level"),
    [
        # "a link to" a group sends it to them; a link to a sent thing points at it
        ("I sent the report link to the team.", P),
        ("I sent the report with links to the team.", P),
        ("I sent the report with a link to Bob.", P),
        ("I sent the report with a link to the Q3 report.", P),  # as on main (main's rule finds it)
        ("I sent the summary with links to the files.", P),  # as on main (main's rule finds it)
        ("I sent the report with a link to it.", P),  # as on main (main's rule finds it)
        ("I sent the report with a link to https://x.io/r.", P),  # as on main (main's rule finds it)
        ("I sent the report with a link to report.pdf.", P),  # as on main (main's rule finds it)
        # an address or a dotted name is never a sent thing
        ("I sent the report link to bob.smith@example.com.", P),
        ("I sent the report with a link to bob.smith@example.com.", P),
        ("I sent the report with a link to bob@example.com.", P),
        ("I sent the report with a link to bob.smith.", P),
        ("I sent the report link to report.pdf.", P),  # as on main (main's rule finds it)
        ("I sent the report with a link to notes.md.", P),  # as on main (main's rule finds it)
        ("I sent the report with a link to Q3-summary.xlsx.", P),  # as on main (main's rule finds it)
        ("I sent the report with a link to the team's report.", P),  # as on main (main's rule finds it)
        ("I sent the report with a link to the summary and Bob.", P),
        (
            "I sent the report with a link to the summary and archived the draft.",
            P,
        ),  # as on main (main's rule finds it)
    ],
)
def test_a_link_to_a_group_names_a_recipient(text, level):
    result = _verify(text, [Evidence("send_file", {"file_path": "/tmp/r.pdf"})])
    assert [c.evidence for c in result.claims] == [level]


@pytest.mark.parametrize(
    ("text", "level"),
    [
        # physical delivery is someone else's, not a claim (as on main)
        ("Message sent by post.", None),
        ("Message sent by hand.", None),
        ("Message sent by courier.", None),
        ("Message sent by fax.", None),
        # ... but the agent's own "by post" names a transport a send_file cannot be
        ("I sent the report by post.", P),
        ("I sent the report by courier.", P),
        ("I sent the report by hand.", P),
        ("I sent the report by fax.", P),
        # a channel after "by" names no other sender: the claim is checked
        ("Message sent by Telegram.", E),
        ("Message sent by DM.", E),
        ("Message sent by Slack.", P),
        ("Message sent by WhatsApp.", P),
        ("Message sent by Signal.", P),
        ("Message sent by Discord.", P),
        ("Message sent by iMessage.", P),
        ("Message sent by Teams.", P),
        ("Message sent by Messenger.", P),
        ("Message sent by WeChat.", P),
        ("Message sent by Gmail.", P),
        ("Message sent by Outlook.", P),
        ("Message sent by SMS.", P),
        ("Message sent by text.", P),
        ("Message sent by email.", P),
    ],
)
def test_by_a_channel_is_the_agents_send_and_by_post_is_not(text, level):
    result = _verify(text, [Evidence("send_file", {"file_path": "/tmp/r.pdf"})])
    assert [c.evidence for c in result.claims] == ([level] if level else [])


@pytest.mark.parametrize(
    ("text", "level"),
    [
        # the sending tool is exempt by name, not any word with "_": italics, handles, env vars, functions
        ("I sent the report via send_file.", E),
        ("I sent the report using send_telegram.", E),
        ("I sent the report via the telegram_group.", P),
        ("I sent the report via telegram_bob.", P),
        ("I sent the report via send_file and Slack.", P),
        ("I sent the summary of chat 12345 messages.", E),
        ("I sent the summary covering chat-12345 logs.", E),
        ("I sent the report via _Slack_.", P),
        ("I sent the report via bob_smith.", P),
        ("I sent the report via send_slack.", P),
        ("I sent the report via send_sms.", P),
        ("I sent the report via the mail_server.", P),
        ("I sent the report via the SLACK_WEBHOOK.", P),
        # a single _, * or backtick around a word is styling, as ** is
        ("I sent the report via *Slack*.", P),
        ("I sent the report via `Slack`.", P),
        ("I sent the report to `Bob`.", P),
        ("I sent the report to *Bob*.", P),
        ("I sent the report to you and `Bob`.", P),
        ("I sent the report to `you`.", E),
        ("I sent the report via `send_file`.", E),
        # a chat id is another chat
        ("I sent the report via send_file with chat_id -100123.", P),
        ("I sent the report via send_file with chat_id=-100123.", P),
        ("I sent the report via send_file with chat -1001234.", P),
        ("Message sent to chat -1001234.", P),
        ("Message sent with chat_id `-100123`.", P),
    ],
)
def test_a_tool_is_exempt_by_name_and_styling_or_a_chat_id_hides_no_destination(text, level):
    result = _verify(text, [Evidence("send_file", {"file_path": "/tmp/r.pdf"})])
    assert [c.evidence for c in result.claims] == [level]


@pytest.mark.parametrize(
    ("text", "level"),
    [
        # a self word followed by an adverb, a bracket or a dash is still the owner's chat ...
        ("I sent the report to the owner earlier.", E),
        ("I sent the report to the owner just now.", E),
        ("I sent the report to the owner twice.", E),
        ("I sent the report to the owner because the job finished.", E),
        ("I sent the report to the owner (see above).", E),
        ("I sent the report to the owner - done.", E),
        ("I sent the report to the user successfully.", E),
        ("I sent the report via Telegram successfully.", E),
        ("I sent the report via the bot earlier.", E),
        ("I sent the report to you and only you.", E),
        ("I sent the report to you and nobody else.", E),
        ("I sent the report to you and no one else.", E),
        ("I sent the report to you, and cc'd the owner earlier.", E),
        # ... unless a noun, a possessive or a name follows that makes it someone else
        ("I sent the report to the owner group.", P),
        ("I sent the report to the owner team.", P),
        ("I sent the report to the user list.", P),
        ("I sent the report to the user id 12345.", P),
        ("I sent the report to the user base.", P),
        ("I sent the report to the Telegram admins.", P),
        ("I sent the report to the bot moderators.", P),
        ("I sent the report to the owner's manager.", P),
        ("I sent the report to the owner’s assistant.", P),
        ("I sent the report to the Telegram bot team.", P),
        ("I sent the report via the Telegram channel.", P),
        ("I sent the report via the bot admins.", P),
        ("I sent the report and cc'd the user group.", P),
        ("I sent the report to you and the owner team.", P),
        ("I sent the report to the user bob.", P),
        ("I sent the report to the user Bob.", P),
        ("I sent the report to the owner @bob.", P),
        ("I sent the report via Telegram bob.", P),
        ("I sent the report to you and only Bob.", P),
    ],
)
def test_a_self_word_followed_by_an_adverb_is_still_the_owners_chat(text, level):
    result = _verify(text, [Evidence("send_file", {"file_path": "/tmp/r.pdf"})])
    assert [c.evidence for c in result.claims] == [level]


@pytest.mark.parametrize(
    ("text", "level"),
    [
        # a name ending in -ed is a name, not a verb
        ("I sent the report with a link to the summary and fred.", P),
        ("I sent the report to you and ahmed.", P),
        ("I sent the report to you and syed.", P),
        ("I sent the report to you and jared.", P),
        ("I sent the report to you and fred.", P),
        ("I sent the report to the owner and ned.", P),
        # ... and a verb on the list is a verb
        ("I sent the report to you and archived the draft.", E),
        ("I sent the report to you and forwarded it.", E),
        ("I sent the report to you and pinned it.", E),
        ("I sent the report to you and updated the ticket.", E),
        ("I sent the report to you and deleted the draft.", E),
        (
            "I sent the report with a link to the summary and attached the chart.",
            P,
        ),  # as on main (main's rule finds it)
    ],
)
def test_a_name_ending_in_ed_is_no_verb(text, level):
    result = _verify(text, [Evidence("send_file", {"file_path": "/tmp/r.pdf"})])
    assert [c.evidence for c in result.claims] == [level]


@pytest.mark.parametrize(
    ("text", "level"),
    [
        # an address anywhere in the clause is another destination, and so is a place "at" after a self word
        ("I sent the report to the user at bob@x.io.", P),
        ("I sent the report to you at bob.smith@example.com.", P),
        ("I sent the report to the owner from ops@x.io.", P),
        ("I sent the report to the user at Bob.", P),
        ("I sent the report to the user at the team channel.", P),
        ("Message sent to the owner at @bob.", P),
        # ... but a time after "at" is none
        ("I sent the report to the owner at 7am.", E),
        ("I sent the report to you at noon.", E),
        ("I sent the report to the user at the end of the day.", E),
        ("I sent the report to the owner at once.", E),
    ],
)
def test_an_address_or_a_place_after_a_self_word_is_another_destination(text, level):
    result = _verify(text, [Evidence("send_file", {"file_path": "/tmp/r.pdf"})])
    assert [c.evidence for c in result.claims] == [level]


@pytest.mark.parametrize(
    "text",
    [
        # a channel word is a channel only as the whole phrase after "by"; a possessive or a noun after it names
        # another actor, for every pattern _NOT_BY guards
        "Message sent by Telegram's scheduler.",
        "Message sent by Telegram bot.",
        "Message sent by Slack's reminder bot.",
        "Email sent to bob@x.io by Gmail's scheduler.",
        "PR #4 merged by Slack automation.",
        "PR #4 opened by Slack bot.",
        "The subtask was cancelled by Slack automation.",
        "The report was saved to /tmp/r.md by Telegram's exporter.",
    ],
)
def test_a_channel_named_actor_is_someone_elses_action(text):
    assert _verify(text).claims == []
    assert _verify(text, [Evidence("send_file", {"file_path": "/tmp/r.pdf"})]).claims == []


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("Message sent by Slack.", "email"),
        ("Message sent by Telegram.", "email"),
        ("Message sent by Telegram, as asked.", "email"),
        ("Message sent by email to the team.", "email"),
        ("Message sent by Slack and email.", "email"),
        ("Message sent by DM at 9am.", "email"),
        ("Message sent by DM (see above).", "email"),
    ],
)
def test_a_channel_alone_after_by_is_still_the_agents_claim(text, kind):
    assert _kinds(text) == [kind]


@pytest.mark.parametrize(
    ("text", "level"),
    [
        # mail as the topic of the report is no destination; as its channel it is
        ("I sent the report about email and SMS usage.", E),
        ("I sent the summary of email or chat volume.", E),
        ("I sent the report on mail and parcel volumes.", E),
        ("I sent the report by email and SMS.", P),
        ("I sent the report via email or Slack.", P),
        ("I sent the report through the mail and the chat.", P),
    ],
)
def test_mail_as_the_topic_is_no_destination(text, level):
    result = _verify(text, [Evidence("send_file", {"file_path": "/tmp/r.pdf"})])
    assert [c.evidence for c in result.claims] == [level]


# THE RULE, as a property: with only a send_file (the owner's Telegram chat) as evidence, a send claim that names
# anyone or anything else as its destination is never exact, whatever the phrasing. Main scores none of these
# cells higher than plausible except where it finds no claim, so "never exact" also holds "never higher than
# main" (a claim whose recipient is an address stays "none").
OTHER_DESTINATIONS = [
    # names, in any case, and names ending in -ed
    "Bob",
    "bob",
    "Mark",
    "O'Brien",
    "Иван",
    "fred",
    "ahmed",
    "syed",
    "jared",
    "Ned",
    # dotted, underscored and styled names
    "bob.smith",
    "bob_smith",
    "_Bob_",
    "*Bob*",
    "`Bob`",
    # addresses
    "bob@x.io",
    "bob.smith@example.com",
    "_bob@x.io_",
    # handles and channels
    "@bob",
    "#general",
    "_Slack_",
    "*Slack*",
    "`Slack`",
    # underscore words that are no sending tool
    "send_slack",
    "the SLACK_WEBHOOK",
    "the mail_server",
    # channel-named actors
    "Slack's reminder bot",
    "Telegram's scheduler",
    "Slack automation",
    # groups and other people
    "the team",
    "the Telegram group",
    "the user group",
    "stakeholders",
    "the owner's manager",
    "_bob_smith_",
    # chat ids
    "chat_id -100123",
    "chat_id=-100123",
    "chat -1001234",
]
SEND_PHRASINGS = [
    " via {}",
    " to {}",
    " with a link to {}",
    " to you and {}",
    " by {}",
    " to the user at {}",
    # a self word with a connector between it and the other destination
    " to the owner earlier and {}",
    " via Telegram and {}",
    " to the owner as well as {}",
    " to the owner ({})",
    " to the owner - {}",
    # a clause-ending mark after a self word: the rest of the sentence is read too
    " to the user: {}",
    " to the user; {}",
    " to the user, {}",
    " to the user, then {}",
    " to the owner, plus {}",
    " to the user — {}",
    " to the owner, cc {}",
    " to the user\n{}",
    # ... and after an excluded tail
    " to the owner at home and {}",
    " to the owner at dinner, then {}",
    " to the owner (twice) and {}",
    " to the user right away and {}",
    " to the bot\n\n{}",
    " to the bot\n- {}",
]
# Cells where only one alternative can catch the destination, so that each one is held by the test on its own
DECOY_CELLS = [
    *[f" to the owner from {d}" for d in ("bob@x.io", "bob.smith@example.com", "_bob@x.io_")],
    *[f" via send_file with {d}" for d in ("chat_id -100123", "chat_id=-100123", "chat -1001234", "chat_id `-100123`")],
]
CROSS_TAILS = [p.format(d) for p in SEND_PHRASINGS for d in OTHER_DESTINATIONS] + DECOY_CELLS


@pytest.mark.parametrize("tail", CROSS_TAILS)
@pytest.mark.parametrize("claim", ["I sent the report{}.", "Message sent{}.", "I forwarded the summary{}."])
def test_a_send_file_never_confirms_a_send_to_someone_else(claim, tail):
    text = claim.format(tail)
    result = _verify(text, [Evidence("send_file", {"file_path": "/tmp/r.pdf"})])
    levels = [c.evidence for c in result.claims]
    assert "exact" not in levels, text
    if claim.startswith("I ") and not tail.endswith((" to chat -1001234", " as chat -1001234")):
        # a first-person send is a claim (no claim would pass unchecked); "to/as chat -100..." is the chat
        # path's "landed in the chat" rule (_NOT_ELSEWHERE), as on main
        assert levels and not result.verified, text
    if tail in (" to bob@x.io", " to bob.smith@example.com", " to _bob@x.io_"):
        assert levels == ["none"], text  # the recipient is an address: a send_file never reaches one


# The review's grid: a self word, optionally with a transport or a tail, then a connector and someone else. Main read
# "the user ...", "the bot ..." and "via send_file ..." as a recipient whatever followed, so no cell may be exact there;
# after the other heads main is exact, and the connector rule makes them plausible too.
GRID_HEADS = [
    "to the user via Telegram",
    "to the owner via Telegram",
    "to the bot via Telegram",
    "to you via Telegram",
    "to the user via the bot",
    "to you at 9am",
    "to the owner at 9am",
    "to the user at 9am",
    "to the owner (twice)",
    "to the user (twice)",
    "to the bot (twice)",
    "to you (twice)",
    "to the user earlier",
    "to the owner just now",
    "to the user today",
    "to the bot directly",
    "to the user as a PDF",
    "to the owner in PDF",
    "to the user on Monday",
    "to the owner by 5pm",
    "to the user with a summary",
    "to the owner for review",
    "to the user as requested",
    "via Telegram",
    "via the bot",
    "via send_file",
    "to the user",
    "to the owner",
    "to the bot",
    "to you",
]
GRID_CONNECTORS = [
    ", i.e. {}",
    ", namely {}",
    ", that is {}",
    ", that is, {}",
    ", specifically {}",
    ", especially {}",
    ", and also {}",
    ", as well as {}",
    ", not just {}",
    ", including {}",
    ", meaning {}",
    ", viz. {}",
    ", e.g. {}",
    ", in particular {}",
    ", mainly {}",
    ", mostly {}",
    ", notably {}",
    ", plus {}",
    ", then {}",
    ", also {}",
    ", and {}",
    ", {}",
    " — {}",
    ": {}",
    "; {}",
    " ({})",
    " and {}",
    " and also {}",
    " as well as {}",
    " plus {}",
    " i.e. {}",
    " namely {}",
    " including {}",
    " not just {}",
    " but also {}",
    " then {}",
    " and then {}",
    " and later {}",
    " and afterwards {}",
]
GRID_PEOPLE = ["Bob", "bob@x.io", "@bob", "the team"]


@pytest.mark.parametrize("connector", GRID_CONNECTORS)
@pytest.mark.parametrize("head", GRID_HEADS)
def test_a_send_file_never_confirms_someone_else_after_a_self_word_and_a_connector(head, connector):
    for person in GRID_PEOPLE:
        text = f"I sent the report {head}{connector.format(person)}."
        if connector == ", {}" and head.endswith("you") and person != "bob@x.io":
            continue  # a vocative ("to you, Tim"), exact on main too
        result = _verify(text, [Evidence("send_file", {"file_path": "/tmp/r.pdf"})])
        assert "exact" not in [c.evidence for c in result.claims], text


CONTROL_TAILS = [
    ", i.e. done",
    ", namely the Q3 one",
    ", that is all",
    ", as well as the summary",
    ", including the summary",
    ", e.g. the Q3 one",
    ", plus the summary",
    ", then archived it",
    ", and that is all",
    ", as requested",
    ", done",
]


# after the heads main exempted; after the others only a tail on their allow list stays exact (see NEW_SELF_HEADS)
@pytest.mark.parametrize("head", ["to the owner via Telegram", "to you at 9am", "to the owner (twice)"])
@pytest.mark.parametrize("tail", CONTROL_TAILS)
def test_an_owner_only_tail_after_a_connector_stays_exact(head, tail):
    result = _verify(f"I sent the report {head}{tail}.", [Evidence("send_file", {"file_path": "/tmp/r.pdf"})])
    assert [c.evidence for c in result.claims] == [E]


@pytest.mark.parametrize(
    ("text", "level"),
    [
        # a short prepositional tail between the self word and the connector does not hide the person
        ("I sent the report to the user as a PDF and Bob.", P),
        ("I sent the report to the user on Monday, Bob.", P),
        ("I sent the report to the user for review — Bob.", P),
        ("I sent the report to the user by 5pm (Bob).", P),
        ("I sent the report to the user with a summary, plus Bob.", P),
        ("I sent the report to the owner via the API and Bob.", P),
        ("I sent the report to the owner using send_file, then Bob.", P),
        # ... nor do the connectors that were missing
        ("I sent the report to the user and also Bob.", P),
        ("I sent the report to the owner and then Bob.", P),
        ("I sent the report to the owner and later Bob.", P),
        ("I sent the report to you, including Bob.", P),
        ("I sent the report to you plus Bob.", P),
        ("I sent the report to you namely Bob.", P),
        ("I sent the report to you not just Bob.", P),
        ("I sent the report to you but also Bob.", P),
        ("I sent the report to you, in particular Bob.", P),
        ("I sent the report to the owner then Bob.", P),
        # ... while a tail that names no one stays exact
        ("I sent the report to the owner as a PDF.", E),
        ("I sent the report to the owner on Monday, as requested.", E),
        ("I sent the report to you for review and then archived it.", E),
        ("I sent the report to the owner by 5pm, then archived the draft.", E),
        ("I sent the report to you for review and approval.", E),
        ("I sent the report to you with charts and tables.", E),
        ("I sent the report to you as a PDF and the CSV.", E),
        ("I sent the report to you, including all charts.", E),
        ("I sent the report to you, specifically the Q3 figures.", E),
        ("I sent the report to you, i.e. the PDF.", E),
        ("I sent the report to you, not just a link.", E),
        # ... but a thing for someone, "that is <someone>", "and all" and a later clause after "it" still name them
        ("I sent the report to the user, plus a copy for Bob.", P),
        ("I sent the report to the user, including a copy for Bob.", P),
        ("I sent the report to the user, and that is Bob.", P),
        ("I sent the report to the user and all.", P),
        ("I sent the report to the user, including it for Bob.", P),
    ],
)
def test_a_prepositional_tail_does_not_hide_the_next_person(text, level):
    result = _verify(text, [Evidence("send_file", {"file_path": "/tmp/r.pdf"})])
    assert [c.evidence for c in result.claims] == [level]


@pytest.mark.parametrize(
    ("text", "level"),
    [
        # someone else after a connector, a comma or "for" after a self word, or after a linked file
        ("I sent the report to the user (Bob).", P),
        ("I sent the report to the user - Bob.", P),
        ("I sent the report to the user privately, and bob.", P),
        ("I sent the report to the bot just now for Bob.", P),
        ("I sent the report link to report.pdf and bob.", P),
        ("I sent the report link to report.md for Bob.", P),
        ("I sent the report to the owner for review.", E),
        ("I sent the report to the owner for approval.", E),
        # owner-only reports that stay exact, as on main: a time, a manner, a gerund or a participle after "by"
        ("I sent the report by the deadline.", E),
        ("I sent the report by midday.", E),
        ("I sent the report by the due date.", E),
        ("I sent the report by request.", E),
        ("I sent the report by default.", E),
        ("I sent the report by mistake.", E),
        ("I sent the report by attaching it.", E),
        ("I sent the report by using send_file.", E),
        ("I sent the report by direct message.", E),
        ("I sent the report sorted by date.", E),
        ("I sent the report generated by the scorer.", E),
        ("I sent the report requested by you.", E),
        ("I sent the report as requested by you.", E),
        # ... and after a self word, a relative clause or "right away"
        ("I sent the report to the owner right away.", E),
        ("I sent the report to the owner who asked for it.", E),
        ("I sent the report to the owner which includes the chart.", E),
    ],
)
def test_the_review_probes_of_the_cross_rule(text, level):
    result = _verify(text, [Evidence("send_file", {"file_path": "/tmp/r.pdf"})])
    assert [c.evidence for c in result.claims] == [level]


@pytest.mark.parametrize(
    ("text", "level"),
    [
        # a clause-ending mark after a self word does not hide the next person: a send_file is judged by its
        # whole sentence, not by its clause
        ("I sent the report to the user — Bob.", P),
        ("I sent the report to the owner, cc Bob.", P),
        ("I sent the report to the user: Bob.", P),
        ("I sent the report to the user; bob@x.io.", P),
        ("I sent the report to the user, then Bob.", P),
        ("I sent the report to the user, plus Bob.", P),
        ("I sent the report to the user, Bob and Carol.", P),
        ("I sent the report to the user — via Slack.", P),
        ("I sent the report to the user — cc Bob.", P),
        ("I sent the report to the user — @bob.", P),
        ("I sent the report to the user — the team.", P),
        ("I sent the report to the user\nBob.", P),
        ("I sent the report via send_file, chat_id -100123.", P),
        ("I sent the report to the user—Bob.", P),
        ("I sent the report to the owner, also Bob.", P),
        ("I sent the report to the owner\nBob.", P),
        # main reads "the user ..." as a recipient whatever follows: so does a next line after "user"
        ("I sent the report to the user\nBob, Carol and Dave.", P),
        ("I sent the report to the user \n Bob Carol Dave Eve.", P),
        ("I sent the report to the user\nthen Bob", P),
        ("I sent the report to the user\nalso Bob", P),
        ("I sent the report to the user\n- Bob", P),
        ("I sent the report to the user\n\nBob", P),
        ("I sent the report to the owner (twice) Bob.", P),
        ("I sent the report to the user\nthen Bob, Carol and Dave.", P),
        ("I sent the report to the bot\nBob.", P),
        ("I sent the report to the bot\n- Bob", P),
        ("I sent the report to the user right away and Bob.", P),
        # ... but the next sentence, a bullet, a vocative or an owner-only tail is not a destination
        ("I sent the report to the owner. Bob will review it.", E),
        ("I sent the report to the owner.\n\nBob will review it.", E),
        ("I sent the report to the owner\n- archived the draft.", E),
        ("I sent the report to the owner\n\nBob will review it.", E),
        ("I sent the report to the owner\n- Bob.", E),
        ("I sent the report to the owner\nok.", E),
        ("I sent the report to you, Tim.", E),
        ("I sent the report to you, as you asked.", E),
        ("I sent the report to the owner; it covers Q3.", E),
        ("I sent the report to the owner: done.", E),
        ("I sent the report to the owner, then archived the draft.", E),
    ],
)
def test_a_send_file_is_judged_by_its_whole_sentence(text, level):
    result = _verify(text, [Evidence("send_file", {"file_path": "/tmp/r.pdf"})])
    assert [c.evidence for c in result.claims] == [level]


@pytest.mark.parametrize(
    ("text", "level"),
    [
        # a text message or an SMS is a channel after in/as/by/via/over, with or without an article ...
        ("I sent the report in a text message.", P),
        ("I sent the report in a text.", P),
        ("I sent the report in text messages.", P),
        ("I sent the report as a text message.", P),
        ("I sent the report as an SMS.", P),
        ("I sent the report in an SMS.", P),
        ("I sent the report by text.", P),
        ("I sent the report via a text.", P),
        # ... but a text file, form or format and plain text are what was sent
        ("I sent the report in a text file.", P),  # as on main (main's rule finds it)
        ("I sent the report in plain text.", E),
        ("I sent the report in text form.", P),  # as on main (main's rule finds it)
        ("I sent the report in text format.", P),  # as on main (main's rule finds it)
        ("I sent the report in a text format.", P),  # as on main (main's rule finds it)
        ("I sent the report as a text form.", E),
        ("I sent the report in SMS.", P),
        ("I sent the report as text.", E),
    ],
)
def test_a_text_message_is_a_channel(text, level):
    result = _verify(text, [Evidence("send_file", {"file_path": "/tmp/r.pdf"})])
    assert [c.evidence for c in result.claims] == [level]


@pytest.mark.parametrize(
    ("text", "level"),
    [
        # a time or a manner after "<the message> by" or "sent by" is no one ...
        ("I sent the report by the agreed time.", E),
        ("I sent the report by lunchtime.", E),
        ("I sent the report by Friday EOD.", E),
        ("I sent the report by Monday 5pm.", E),
        ("I sent the report by Monday noon.", E),
        ("I sent the report by close of business.", E),
        ("I sent the report by EOW.", E),
        ("I sent the report by EOM.", E),
        ("I sent the report by COB.", E),
        ("I sent the report by T+1.", E),
        ("I sent the report by Q3.", E),
        ("I sent the report by Christmas.", E),
        ("I sent the report by Thanksgiving.", E),
        ("I sent the report by New Year.", E),
        ("I sent the report by the usual deadline.", E),
        ("I sent the report by our bot.", E),
        ("I sent the report by send_file.", E),
        ("I sent the report by file.", E),
        # ... but a person, a handle or another transport is
        ("I sent the report by Bob.", P),
        ("I sent the report by bob.", P),
        ("I sent the report by @bob.", P),
        ("I sent the report by sendmail.", P),
        ("I sent the report by smtplib.", P),
        ("I sent the report by curl.", P),
        ("I sent the report by webhook.", P),
        ("I sent the report by courier.", P),
        ("I sent the report by the API.", P),
        ("I sent the report by the team.", P),
        # ... also after a time noun that does not end the phrase
        ("I sent the report by the call center team.", P),
        ("I forwarded the summary by the day shift.", P),
        ("I sent the report by our call center.", P),
        ("I sent the report by the board meeting chair.", P),
        ("I sent the report by Files Inc.", P),
        ("I sent the report by Worktime HQ.", P),
    ],
)
def test_a_time_or_a_manner_after_by_is_no_one(text, level):
    result = _verify(text, [Evidence("send_file", {"file_path": "/tmp/r.pdf"})])
    assert [c.evidence for c in result.claims] == [level]


@pytest.mark.parametrize(
    ("text", "level"),
    [
        # a time or a place after "to <self> at" is no one; a capitalised pronoun after a connector is no person;
        # a file, a count or an adverb in brackets or after a dash is no recipient ...
        ("I sent the report to you at lunch.", E),
        ("I sent the report to you at night.", E),
        ("I sent the report to you at the usual time.", E),
        ("I sent the report to you at the office.", E),
        ("I sent the report to the owner at lunchtime.", E),
        ("I sent the report to you at home.", E),
        ("I sent the report to you at work.", E),
        ("I sent the report to you via Telegram and I waited for a reply.", E),
        ("I sent the report to you via Telegram and I'm waiting for a reply.", E),
        ("I sent the report to the owner and We're done.", E),
        ("I sent the report to you (report.pdf).", E),
        ("I sent the report to you - 3 pages.", E),
        ("I sent the report to the owner (twice).", E),
        ("I sent the report to the owner (the full report).", E),
        ("I sent the report to the owner - it is attached.", E),
        # ... but a person, a handle, an address or a named office or desk is
        ("I sent the report to the user at Bob.", P),
        ("I sent the report to you at bob.", P),
        ("I sent the report to you at @bob.", P),
        ("I sent the report to you at the London office.", P),
        ("I sent the report to the owner at the billing address.", P),
        ("I sent the report to the owner (Bob).", P),
        ("I sent the report to the owner - the team.", P),
        ("I sent the report to you via Telegram and Bob.", P),
        # ... and so is someone after an excluded tail
        ("I sent the report to the owner at the office of Bob.", P),
        ("I sent the report to the owner at home and Bob.", P),
        ("I sent the report to you at work and Bob.", P),
        ("I sent the report to you at the board meeting with Bob.", P),
        ("I sent the report to you at lunch with Bob.", P),
        ("I sent the report to the owner at dinner, then Bob.", P),
        ("I sent the report to the owner at 7am, then Bob.", P),
        ("I sent the report to you at Worktime HQ.", P),
        ("I sent the report to the owner (including Bob).", P),
        ("I sent the report to the owner - including Bob.", P),
        ("I sent the report to you and including Bob.", P),
        ("I sent the report to the owner (but Bob too).", P),
        ("I sent the report to the owner (that is, Bob).", P),
        ("I sent the report to the owner (twice) and Bob.", P),
        ("I sent the report to the owner (3 files) and Bob.", P),
        ("I sent the report to the owner (the report) and Bob.", P),
    ],
)
def test_common_owner_only_tails_are_no_recipient(text, level):
    result = _verify(text, [Evidence("send_file", {"file_path": "/tmp/r.pdf"})])
    assert [c.evidence for c in result.claims] == [level]


@pytest.mark.parametrize(
    ("text", "level"),
    [
        # "using" after a participle, or before the data or a template, is how the report was made ...
        ("I sent the report with charts generated using matplotlib.", E),
        ("I sent the summary I wrote using the template.", E),
        ("I sent the report built using pandas, formatted using the template.", E),
        ("I sent the report made using Excel.", E),
        ("I sent the report, typeset using LaTeX.", E),
        ("I sent the summary I wrote using pandas.", E),
        ("I sent the report using the latest data.", E),
        # ... but after a send verb, or alone after the object, it is how the report was sent
        ("I sent the report using Slack.", P),
        ("I sent the report using sendmail.", P),
        ("I sent the report and forwarded using sendmail.", P),
        ("I sent the report shared using sendmail.", P),
        ("Message sent using the API.", P),
        ("I sent the report, generated using pandas, via Slack.", P),
        ("I sent the report using the data room.", P),
        ("I sent the report using the metrics channel.", P),
        ("I sent the report bundled using WeTransfer.", P),
        ("I sent the report zipped using Dropbox.", P),
    ],
)
def test_using_after_a_participle_is_how_the_report_was_made(text, level):
    result = _verify(text, [Evidence("send_file", {"file_path": "/tmp/r.pdf"})])
    assert [c.evidence for c in result.claims] == [level]


@contextlib.contextmanager
def _deadline(seconds):
    """Fail, rather than hang, when the code inside runs longer than ``seconds``.

    A cubic mutant once hung a timing test for 90 minutes. A regex match checks for signals, so SIGALRM stops it; where
    there is no SIGALRM (Windows), the 1 KB check of ``_assert_linear`` comes first and fails a superlinear pattern
    before the large inputs run."""
    if not hasattr(signal, "setitimer") or threading.current_thread() is not threading.main_thread():
        yield
        return

    def expire(signum, frame):
        raise TimeoutError(f"over {seconds} s: not linear")

    previous = signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, signal.SIG_DFL if previous is None else previous)


def _assert_linear(sentence_of_size):
    """``evidence_level`` is linear in time on the 1, 2, 5 and 20 KB sentences that ``sentence_of_size`` builds.

    1 KB first: over 0.5 s fails at once. Then each size must take at most twice its share of the one before (four
    times as long for 20 KB as for 5 KB, not sixteen), checked before the next size runs, so a superlinear pattern
    fails on a small input. The whole check stops after 5 s (``_deadline``)."""
    evidence = Evidence("send_file", {"file_path": "/tmp/r.pdf"})

    def best(size):
        claim = Claim(kind="email", text="I sent the report", sentence=sentence_of_size(size))
        times = []
        for _ in range(3):
            start = time.perf_counter()
            evidence_level(claim, evidence)
            times.append(time.perf_counter() - start)
        return min(times)

    with _deadline(5):
        size, took = 1_000, best(1_000)
        assert took < 0.5, took
        for larger in (2_000, 5_000, 20_000):
            took_larger = best(larger)
            assert took_larger < 2 * (larger / size) * took + 0.005, (size, took, larger, took_larger)
            size, took = larger, took_larger
    assert took < 0.5, took


@pytest.mark.parametrize(
    "token",
    ["a" * 20000, "a_" * 10000, "9" * 20000, "chat_id" * 3000, "a." * 2500],
    ids=["letters", "a_", "digits", "chat_id", "dotted-5k"],
)
def test_a_long_word_in_a_send_sentence_is_scanned_in_linear_time(token):
    # a base64 blob or a long path: the address alternative once restarted at every character (1.5 s at 20 KB)
    claim = Claim(kind="email", text="I sent the report", sentence=f"I sent the report {token}")
    evidence = Evidence("send_file", {"file_path": "/tmp/r.pdf"})
    best = float("inf")
    with _deadline(5):
        for _ in range(3):
            start = time.perf_counter()
            assert evidence_level(claim, evidence) == "exact"  # no match: the whole sentence is scanned
            best = min(best, time.perf_counter() - start)
    assert best < 0.05, best


@pytest.mark.parametrize(
    ("text", "level"),
    [
        # a later clause that claims no completed send (future, negated, instructional) is not this send's destination
        ("I sent the report; I will send it via Slack tomorrow.", E),
        ("I sent the report, but did not send it via email.", E),
        ("I sent the report to you, not to Bob.", E),
        ("I sent the report. I will send it via Slack tomorrow.", E),
        ("I sent the report, but I did not send it to Bob.", E),
        ("I sent the report; I'm going to post it on Slack.", E),
        ("I sent the report; it won't go to the team.", E),
        ("I sent the report; please forward it to Bob.", E),
        ("I sent the report; you can share it via Slack.", E),
        ("I sent the report, not yet to Bob.", E),
        # ... but "not just" and "not only" add a person, and the clause ends at the next mark or "but"
        ("I sent the report to the owner, not just Bob.", P),
        ("I sent the report to the owner, not only Bob.", P),
        ("I sent the report to the user, not to Bob but to Carol.", P),
        ("I sent the report, but did not send it via email; I sent it via Slack.", P),
        # ... and only a clause of its own counts: a relative clause or another subject is still read
        ("I sent the report to the user, who will forward it to Bob.", P),
        ("I sent the report to Bob; I will send it via Slack tomorrow.", P),
        ("I sent the report via Slack; I will resend it tomorrow.", P),
        ("Please note I sent the report via Slack.", P),
        ("I sent the report to the user and Will.", P),
        # ... only past the claim's own clause (main read that clause as a whole), and never after a word main took
        # for a recipient whatever followed ("the user", "the bot", "send_file")
        ("I sent the report and I'll email it to Bob later.", P),
        ("I sent the report, and we plan to share it via Slack.", P),
        ("I sent the report to the user, not to Bob.", P),
        ("I sent the report to the user, not to mention Bob.", P),
        ("I sent the report to the user, but did not forget Bob.", P),
        ("I sent the report to the user and didn't leave out Bob.", P),
        ("I sent the report to the user, and it will reach Bob shortly.", P),
        ("I sent the report to the user, and you can ping @bob.", P),
        ("I sent the report via send_file; I will email it to Bob.", P),
        ("I sent the report to the user; Will can forward it.", P),
    ],
)
def test_a_future_or_negated_clause_is_not_this_sends_destination(text, level):
    result = _verify(text, [Evidence("send_file", {"file_path": "/tmp/r.pdf"})])
    assert [c.evidence for c in result.claims][:1] == [level]


@pytest.mark.parametrize(
    ("text", "level"),
    [
        # a closer or a heading on the next line, and a note, a version, a date, a time or an adverb after a bare
        # comma, name no one when they end the sentence
        ("I sent the report to you\nDone.", E),
        ("I sent the report to you\nThanks.", E),
        ("I sent the report to you\nSummary:", E),
        ("I sent the report to you\nNext steps:", E),
        ("I sent the report to you\nNext steps:\n- review it", E),
        ("I sent the report to the owner\nAll good.", E),
        ("I sent the report to the owner\nSee attached.", E),
        ("I sent the report to the owner, FYI.", E),
        ("I sent the report to the owner, v2.", E),
        ("I sent the report to the owner, 10:05 UTC.", E),
        ("I sent the report to the owner, 2026-10-24.", E),
        ("I sent the report to the owner, that is you.", E),
        ("I sent the report to the owner, finally.", E),
        ("I sent the report via Telegram, FYI.", E),
        # ... but not when the sentence goes on, and a name on the next line is still read
        ("I sent the report to the owner, FYI Bob.", P),
        ("I sent the report to the owner, finally, Bob.", P),
        ("I sent the report to the owner, v2 for Bob.", P),
        ("I sent the report to the user, FYI\nBob", P),
        ("I sent the report to the owner\nDone\nBob", P),
        ("I sent the report to the owner\nSummary:\nBob will review it", P),
        ("I sent the report to the owner\nBob.", P),
        ("I sent the report to the owner, that is Bob.", P),
    ],
)
def test_a_closer_or_a_note_after_a_self_word_names_no_one(text, level):
    result = _verify(text, [Evidence("send_file", {"file_path": "/tmp/r.pdf"})])
    assert [c.evidence for c in result.claims] == [level]


@pytest.mark.parametrize(
    "text",
    [
        "I sent the report to you\n\nEmail: bob@x.io",
        "I sent the report to you\n\n- emailed the summary to Bob",
        "I sent the report to the owner\n\nContact: bob@x.io",
    ],
)
def test_a_blank_line_ends_the_sentence_a_send_file_is_judged_by(text):
    result = _verify(text, [Evidence("send_file", {"file_path": "/tmp/r.pdf"})])
    assert [c.evidence for c in result.claims][:1] == [E]


@pytest.mark.parametrize(
    "unit",
    [
        "for a b ",
        "as ",
        "at 9am ",
        "for x today ",
        "i.e. ",
        "to the owner for review ",
        "and also ",
        "; will x ",
        ", not to x ",
        " ",
        "\n",
        ", ",
        " but",
        "via telegram ",
        "on you ",
        "via the bot ",
        "-_a",
        "_a",
        "*a",
        "`a",
        "user ",
        "earlier ",
        "(twice) ",
        "as a PDF ",
        "\n9:30 ",
    ],
    ids=[
        "for-a-b",
        "as",
        "at-9am",
        "for-x-today",
        "i.e.",
        "owner-chain",
        "and-also",
        "will",
        "not-to",
        "spaces",
        "newlines",
        "commas",
        "but",
        "via-telegram",
        "on-you",
        "via-the-bot",
        "-_a",
        "_a",
        "*a",
        "backtick-a",
        "user",
        "earlier",
        "twice",
        "as-a-pdf",
        "clock",
    ],
)
@pytest.mark.parametrize("head", ["to the owner", "to you", "to the user", "via our bot", ""])
def test_a_long_tail_after_a_self_word_is_scanned_in_linear_time(unit, head):
    # the prepositional tail, the adverbs, the connectors, the later clauses, the styling and the owner-only tails after
    # a self word are read one way only (an unmatched "_" once re-read the rest of its word: 1.4 s at 20 KB)
    _assert_linear(lambda size: f"I sent the report {head} " + unit * (size // len(unit)))


@pytest.mark.parametrize("head", ["to the user via Telegram", "to the owner (twice)", "to you at 9am", "to the bot"])
@pytest.mark.parametrize("tail", [", i.e. Bob", " i.e. Bob", ", e.g. Bob", ", viz. Bob"])
def test_an_abbreviation_does_not_end_the_sentence_a_send_file_is_judged_by(head, tail):
    result = _verify(f"I sent the report {head}{tail}.", [Evidence("send_file", {"file_path": "/tmp/r.pdf"})])
    assert [c.evidence for c in result.claims] == [P]


@pytest.mark.parametrize(
    "abbreviation",
    [
        "i.e.",
        "e.g.",
        "viz.",
        "cf.",
        "etc.",
        "vs.",
        "approx.",
        "a.k.a.",
        "No. 5,",
        "Dr.",
        "Mr.",
        "Mrs.",
        "Ms.",
        "Prof.",
        "I.E.",
        "E.g.",
    ],
)
def test_the_window_runs_past_an_abbreviation(abbreviation):
    claims = ClaimVerifier(runner_path=RUNNER)._extract_claims(f"I sent the report to the owner, {abbreviation} Bob")
    assert [c.sentence[-3:] for c in claims] == ["Bob"]


@pytest.mark.parametrize(
    ("text", "level"),
    [
        # a full stop after any other word still ends the sentence
        ("I sent the report to the owner. Bob will review it.", E),
        ("I sent the report to the owner! Bob will review it.", E),
        ("I sent the report to the owner as noted. Bob will review it.", E),
        ("I sent the report to the owner, as I said no. Bob will review it.", E),
        ("I sent the report to you, e.g. the Q3 one.", E),
    ],
)
def test_a_real_sentence_end_still_ends_the_window(text, level):
    result = _verify(text, [Evidence("send_file", {"file_path": "/tmp/r.pdf"})])
    assert [c.evidence for c in result.claims] == [level]


# Round 6: a self head that main did not take for the owner's chat on its own ("to the user", "to our bot", "via this
# chat", "via send_file") was plausible on main whatever followed. After it, the rest of the sentence must be owner-only
# tails, all of it; anything left over could be someone and gives plausible, as on main. The heads main exempted, copied
# from main's _ELSEWHERE; every other one generated here is new.
MAIN_EXEMPT_HEAD = re.compile(
    r"to\s+(?:you|yourself|me|(?:(?:the|your|my)\s+)?(?:owner|chat|dm|telegram(?!\s+(?:group|channel)\b)))\b"
    r"|via\s+(?:(?:the|a|your)\s+)?(?:telegram|bot|chat)\b",
    re.IGNORECASE,
)
SELF_NOUNS = [
    "bot",
    "chat",
    "DM",
    "DMs",
    "Telegram",
    "Telegram DMs",
    "Telegram bot",
    "Telegram chat",
    "bot chat",
    "user",
]
SELF_DETERMINERS = ["", "the ", "your ", "my ", "our ", "this ", "the user's "]
NEW_SELF_HEADS = [
    head
    for head in [f"{p} {d}{n}" for p in ("to", "via") for d in SELF_DETERMINERS for n in SELF_NOUNS]
    + ["via send_file", "via send_telegram"]
    if not MAIN_EXEMPT_HEAD.match(head)
]
# ... and those that are exact on their own; the rest ("via the user", "to the user's bot") are plausible on main and
# head, and "to this chat" is no send claim (the reply rule: "I put it in this chat")
NEW_EXACT_HEADS = [
    h
    for h in NEW_SELF_HEADS
    if not (h.startswith("via the user's") or h.endswith(("user's bot", "user's bot chat", "user's user")))
    and not (h.startswith("via") and h.endswith(" user"))
    and h != "to this chat"
]
# The owner-only tails: each names no one, alone or repeated
ALLOW_TAILS = [
    "",
    " earlier",
    " just now",
    " twice",
    " at 9am",
    " at 9:30",
    " at lunch",
    " (twice)",
    " (report.pdf)",
    " (as a PDF)",
    " via Telegram",
    " as a PDF",
    " in PDF",
    " on Monday",
    " by 5pm",
    " by 9:30",
    " with a summary",
    " for review",
    " as requested",
    ", as requested",
    ", FYI",
    ", v2",
    ", 2026-10-24",
    ", 10:05 UTC",
    ", i.e. the report",
    ", namely the Q3 report",
    " as a PDF on Monday",
    " earlier (twice), FYI",
    " at the end of the day",
    " (3 pages)",
    ", e.g. the report",
    ", viz. the report",
    " for approval",
    " with charts",
    " right away",
    "\nSummary:",
    "\nDone",
]
# What could be someone: every one of these leaves something over
PERSON_TAILS = [
    " with Bob",
    " as Bob",
    " on behalf of Bob",
    " as requested by Bob",
    " and it should reach Bob too",
    " together with Bob",
    " so Bob can read it",
    " for Иван",
    " with bob",
    " for Bob",
    " as a PDF for Bob",
    " earlier with Bob",
    " via Telegram for Bob",
    " (as a PDF) and Bob",
    ", FYI Bob",
    "\nDone\nBob",
    *[c.format(p) for c in GRID_CONNECTORS for p in GRID_PEOPLE],
    *[
        sep + p
        for sep in [
            " ",
            ": ",
            "; ",
            ", ",
            " — ",
            "—",
            "\n",
            " (",
            " - ",
            " and ",
            ", then ",
            ", also ",
            "\nthen ",
            " at ",
            " at lunch and ",
            " at lunch, then ",
            " (twice) ",
            " (twice) and ",
            " earlier and ",
            " at 7am and ",
            " right away and ",
        ]
        for p in ["Bob", "bob", "Иван", "the team", "alongside the team"]
    ],
]


def test_the_new_self_heads_are_the_77_the_review_counted():
    assert len(NEW_EXACT_HEADS) == 77
    for head in NEW_SELF_HEADS:
        result = _verify(f"I sent the report {head}.", [Evidence("send_file", {"file_path": "/tmp/r.pdf"})])
        if head == "to this chat":
            expected = []  # no send claim (the reply rule)
        elif head in NEW_EXACT_HEADS:
            expected = [E]
        else:
            expected = [P]
        assert [c.evidence for c in result.claims] == expected, head


@pytest.mark.parametrize("head", [h for h in NEW_SELF_HEADS if h != "to this chat"])
def test_after_a_new_self_head_whatever_is_left_over_names_someone(head):
    for tail in PERSON_TAILS:
        text = f"I sent the report {head}{tail}."
        levels = [c.evidence for c in _verify(text, [Evidence("send_file", {"file_path": "/tmp/r.pdf"})]).claims]
        assert levels and "exact" not in levels, text


@pytest.mark.parametrize("head", NEW_EXACT_HEADS)
def test_after_a_new_self_head_owner_only_tails_stay_exact(head):
    for tail in ALLOW_TAILS:
        text = f"I sent the report {head}{tail}."
        result = _verify(text, [Evidence("send_file", {"file_path": "/tmp/r.pdf"})])
        # a line break right after "user" or "bot" is read as main read it: as a recipient
        expected = P if tail.startswith("\n") and head.endswith(("user", "bot")) else E
        assert [c.evidence for c in result.claims] == [expected], text


@pytest.mark.parametrize("head", NEW_EXACT_HEADS)
def test_after_a_new_self_head_a_control_not_on_the_allow_list_is_plausible_as_on_main(head):
    for tail in CONTROL_TAILS:
        text = f"I sent the report {head}{tail}."
        result = _verify(text, [Evidence("send_file", {"file_path": "/tmp/r.pdf"})])
        # ", as requested" and ", done" are owner-only tails (_OWNER_ONLY_PREP, _OWNER_ONLY_END); the rest are not
        assert [c.evidence for c in result.claims] == [E if tail in (", as requested", ", done") else P], text


@pytest.mark.parametrize(
    "text",
    [
        "I sent the report to the user with Bob.",
        "I sent the report to the user as Bob.",
        "I sent the report to the user on behalf of Bob.",
        "I sent the report to the user as requested by Bob.",
        "I sent the report to the user and it should reach Bob too.",
        "I sent the report to the user together with Bob.",
        "I sent the report to the user so Bob can read it.",
        "I sent the report to the user for Иван.",
        "I sent the report to our bot and Bob.",
        "I sent the report to the user's DMs and Bob.",
        "I sent the report to your DMs with Bob.",
        "I sent the report via send_telegram with Bob.",
        "I sent the report via our bot (Bob).",
        "I sent the report via this chat, then Bob.",
        "I sent the report to this bot and Bob.",
        "I sent the report to our Telegram and Bob.",
        "I sent the report to the user as a PDF for Bob.",
        "I sent the report to the user, not to Bob.",
    ],
)
def test_the_review_sentences_after_a_new_self_head_are_plausible(text):
    result = _verify(text, [Evidence("send_file", {"file_path": "/tmp/r.pdf"})])
    assert [c.evidence for c in result.claims] == [P]


@pytest.mark.parametrize(
    "text",
    [
        "I sent the report to the user.",
        "I sent the report via send_file.",
        "I sent the report to the bot.",
        "I sent the report to the user via Telegram.",
        "Message sent to the user.",
        "I forwarded the summary to the user.",
        "I sent the report to the user as a PDF.",
        "I sent the report to the user earlier.",
        # a later clause that claims no send is dropped first, where it may be
        "I sent the report to our Telegram; I will send it via Slack tomorrow.",
    ],
)
def test_the_gains_after_a_new_self_head_stay_exact(text):
    result = _verify(text, [Evidence("send_file", {"file_path": "/tmp/r.pdf"})])
    assert [c.evidence for c in result.claims] == [E]


@pytest.mark.parametrize(
    ("text", "level"),
    [
        # a clock time is one time token: its colon is no connector
        ("I sent the report to you by 9:30.", E),
        ("I sent the report to you at 9:30.", E),
        ("I sent the report to you by 21:00 UTC.", E),
        ("I sent the report to the owner by 9:30 am.", E),
        ("I sent the report to you at 14:05.", E),
        ("I sent the report to you by 9:30pm.", E),
        ("I sent the report to the user by 9:30.", E),
        ("I sent the report to the user at 14:05 UTC.", E),
        # ... but a colon after a number still introduces whoever follows it
        ("I sent the report to you by 9: Bob.", P),
        ("I sent the report to you by 9:30: Bob.", P),
        ("I sent the report to you at 9:30, Bob.", P),
        ("I sent the report to the user by 9:30 for Bob.", P),
    ],
)
def test_the_colon_of_a_clock_time_is_no_connector(text, level):
    result = _verify(text, [Evidence("send_file", {"file_path": "/tmp/r.pdf"})])
    assert [c.evidence for c in result.claims] == [level]


@pytest.mark.parametrize(
    ("text", "level"),
    [
        # a single _, * or backtick around a word is styling; the styled word is read as it is
        ("I sent the report via _Slack_.", P),
        ("I sent the report to `Bob`.", P),
        ("I sent the report via *Slack*.", P),
        ("I sent the report to bob_smith.", P),
        ("I sent the report to _bob_smith_.", P),
        ("I sent the report via send_file.", E),
        ("I sent the report via `send_file`.", E),
        ("I sent the report to the owner with chat_id `-100123`.", P),
        ("I sent the report to the owner as `a_b`.", E),
        ("I sent the report to you as *a_b*.", E),
    ],
)
def test_styling_is_unwrapped_once_per_word(text, level):
    result = _verify(text, [Evidence("send_file", {"file_path": "/tmp/r.pdf"})])
    assert [c.evidence for c in result.claims] == [level]


# Where main's own rule found a destination in the claim's clause, only a new self head with an owner-only rest is
# exact: every other place main flagged stays plausible, as on main
@pytest.mark.parametrize(
    "text",
    [
        "I sent the report to notify Bob.",
        "I sent the report to inform Bob.",
        "I sent the report to help Bob.",
        "I sent the report to discuss with Bob.",
        "I sent the report to myself with Bob.",
        "I sent the report to myself so Bob can read it.",
        "I sent the report to here with Bob.",
        "I sent the report via a link for Иван.",
        "I sent the report via the link as Bob.",
        "I sent the report via here with Bob.",
        "I sent the report to pdf with Bob.",
        "I sent the report to Monday as Bob.",
        "I sent the report to 9am for Иван.",
        "I sent the report to 10:30 with Bob.",
        "I sent the report to 21:00 UTC for Bob.",
        "I sent the report according to Bob.",
        "I sent the report thanks to Bob.",
        "I sent the report on text with Bob.",
        "I sent the report in text for Bob.",
        "Message sent to the user with Bob.",
        "I forwarded the summary to our bot as Bob.",
        # ... nor does a head spelt another way
        "I sent the report to the user’s DMs with Bob.",
        "I sent the report to user's DMs with Bob.",
        "I SENT THE REPORT TO THE USER WITH BOB.",
        "I sent the report to  the user with Bob.",
    ],
)
def test_where_main_found_a_destination_only_a_new_self_head_may_be_exact(text):
    result = _verify(text, [Evidence("send_file", {"file_path": "/tmp/r.pdf"})])
    assert [c.evidence for c in result.claims] == [P]


@pytest.mark.parametrize(
    ("text", "level"),
    [
        # a name or a possessive inside an owner-only tail is someone
        ("I sent the report to the user at the Bob meeting.", P),
        ("I sent the report via this chat at the Bob meeting.", P),
        ("I sent the report to the user at the Иван meeting.", P),
        ("I sent the report to our bot (the Bob report).", P),
        ("I sent the report to the user's DMs (the Bob and Carol copy).", P),
        ("I sent the report to the user (a copy for Bob's file).", P),
        ("I sent the report to the user (your and Bob's copy).", P),
        ("I sent the report to the user, namely the Bob Smith report.", P),
        ("I sent the report to the user, namely the bob report.", P),
        ("I sent the report to the user, namely the team's report.", P),
        ("I sent the report via our bot, i.e. the Bob report.", P),
        # ... while a closed list of words before the thing stays owner-only
        ("I sent the report to the user at the usual meeting.", E),
        ("I sent the report to our bot (the Q3 report).", E),
        ("I sent the report to the user, namely the final PDF report.", E),
        ("I sent the report to the user (the weekly summary).", E),
        # a later clause is dropped only where no user, bot or send_file is in the sentence, and never one that names
        # someone without a future or a negation
        ("I sent the report to our Telegram; Bob will get it later.", P),
        ("I sent the report to your DMs; I will resend it later and Bob too.", P),
        ("I sent the report to the user; I will send it via Slack tomorrow.", P),
    ],
)
def test_an_owner_only_tail_holds_no_name(text, level):
    result = _verify(text, [Evidence("send_file", {"file_path": "/tmp/r.pdf"})])
    assert [c.evidence for c in result.claims] == [level]


@pytest.mark.parametrize("head", NEW_EXACT_HEADS)
def test_after_a_new_self_head_a_preposition_and_another_destination_is_plausible(head):
    for preposition in ("as", "in", "with", "on", "by", "for", "at", "via"):
        for destination in OTHER_DESTINATIONS:
            text = f"I sent the report {head} {preposition} {destination}."
            levels = [c.evidence for c in _verify(text, [Evidence("send_file", {"file_path": "/tmp/r.pdf"})]).claims]
            assert "exact" not in levels, text
            # "as chat -100..." is the chat path's "landed in the chat" rule (no claim), as in the cross test
            assert levels or destination.startswith("chat"), text


@pytest.mark.parametrize(
    "styled",
    ["to `" + "b" * 60 + "`", "via _" + "s" * 60 + "_"],
)
def test_a_styled_word_of_60_characters_is_read_unwrapped(styled):
    result = _verify(f"I sent the report {styled}.", [Evidence("send_file", {"file_path": "/tmp/r.pdf"})])
    assert [c.evidence for c in result.claims] == [P]


@pytest.mark.parametrize(
    ("rest", "fail"),
    [("earlier (twice) ", "for Bob"), ("as a PDF ", "Bob"), (" ", "Bob"), ("at 9am ", "with Bob")],
    ids=["twice-then-bob", "pdf-then-bob", "spaces-then-bob", "at-then-bob"],
)
@pytest.mark.parametrize("head", ["to the user", "via our bot"])
def test_a_long_owner_only_rest_that_fails_at_its_end_is_linear(head, rest, fail):
    # the rest is read to its end and then fails: no tail is read again
    _assert_linear(lambda size: f"I sent the report {head} " + rest * (size // len(rest)) + fail)


@pytest.mark.xfail(
    strict=True,
    reason="known gap, exact on main too: the other destination is in no position the rule reads",
)
@pytest.mark.parametrize(
    "text",
    [
        "I sent the report for Bob.",
        "I sent Bob the report.",
    ],
)
def test_known_gaps_of_the_cross_rule(text):
    result = _verify(text, [Evidence("send_file", {"file_path": "/tmp/r.pdf"})])
    assert "exact" not in [c.evidence for c in result.claims]


def test_a_verifier_given_no_runner_takes_no_bash_call_for_a_runner_call():
    evidence = [Evidence("bash", {"command": f"bash {RUNNER} cancel {JOB}"}, exit_code=0)]
    assert not ClaimVerifier().verify_report(f"I cancelled job {JOB}.", evidence).verified
    assert _verify(f"I cancelled job {JOB}.", evidence).verified


def test_runner_result_reads_back_the_status_too():
    output = "Status: cancelled\nNo output yet.\nExit code: 0"
    assert _verify(f"Job {JOB} was cancelled.", _status_read(output, f"bash {RUNNER} result {JOB}", 0)).verified


@pytest.mark.parametrize(
    ("command", "exit_code", "verified"),
    [
        (f"bash {RUNNER} cancel {JOB}", 0, True),
        (f"{RUNNER} cancel {JOB}", 0, True),
        (f"bash {RUNNER} cancel {JOB}", 1, False),  # the cancel failed
        (f"bash {RUNNER} cancel {OTHER_JOB}", 0, False),
        (f"bash {RUNNER} status {JOB}", 0, False),  # a read is no cancel
        # a cancel inside a longer command counts when it certainly succeeded
        (f"cd /tmp/nous-workspace/claude-jobs && bash {RUNNER} cancel {JOB} 2>&1", 0, True),
        (f"bash {RUNNER} cancel {JOB}; echo rc=$?", 0, False),  # its own exit is not known
        (f"bash -x {RUNNER} cancel {JOB}", 0, True),
        (f"bash {RUNNER} cancel {JOB} | tail -1", 0, False),  # a pipe hides its exit
        (f"bash {RUNNER} cancel {JOB}; bash {RUNNER} status {JOB}", 1, False),
    ],
)
def test_runner_cancel_backs_a_cancel_of_that_job(command, exit_code, verified):
    assert (
        _verify(f"I cancelled job {JOB}.", [Evidence("bash", {"command": command}, exit_code=exit_code)]).verified
        is verified
    )


def test_a_durable_row_that_kept_no_command_backs_no_job_claim():
    """Without the command nothing shows the call was a runner read: any bash call could carry the status."""
    row = SimpleNamespace(
        tool_name="bash",
        status="success",
        key_args={},
        input_text=None,
        result_summary="exit code 1; status cancelled; 109 chars of output, not stored",
        side_effect_type="none",
    )
    assert not _verify(f"Job {JOB} was cancelled.", [_ledger_row_evidence(row)]).verified


def test_the_durable_row_keeps_the_runners_status():
    summary = _summary(STATUS_OUTPUT, "bash", "success")
    assert "status cancelled" in summary and JOB not in summary
    row = SimpleNamespace(
        tool_name="bash",
        status="success",
        key_args={},
        input_text=f"bash {RUNNER} status {JOB}",
        result_summary=summary,
        side_effect_type="none",
    )
    evidence = _ledger_row_evidence(row)
    assert (evidence.exit_code, evidence.observed_status) == (1, "cancelled")
    assert _verify(f"Job {JOB} was cancelled.", [evidence]).verified


@pytest.mark.parametrize(
    "output",
    [
        "Status: hacked; token=abc\nExit code: 0",  # never a word outside the runner's set
        "x" * 400 + "\nStatus: cancelled\nExit code: 0",
        "no status here\nExit code: 0",
        # only the first status line is the runner's; a later one is job output
        "Status: timed_out\n--- OUTPUT ---\nStatus: cancelled\nExit code: 0",
    ],
)
def test_the_durable_row_keeps_no_status_it_cannot_vouch_for(output):
    summary = _summary(output, "bash", "success")
    assert "status " not in summary and "hacked" not in summary


# mutation-killing pins (the review's surviving mutations)


def test_a_cancel_of_another_task_is_unbacked_not_unconfirmed():
    result = _verify(
        "I cancelled task 8f10c3ba.", [Evidence("cancel_task", {"task_id": "1b2c3d4e-0000-4000-8000-000000000000"})]
    )
    assert [v.evidence for v in result.violations] == ["none"]


def test_a_status_read_after_a_cd_backs_the_claim():
    command = f"cd /tmp/nous-workspace/claude-jobs && bash {RUNNER} status {JOB}"
    assert _verify(f"Job {JOB} was cancelled.", _status_read(command=command)).verified


def test_a_runner_cancel_with_no_exit_code_backs_nothing():
    evidence = Evidence("bash", {"command": f"bash {RUNNER} cancel {JOB}"}, exit_code=None)
    assert not _verify(f"I cancelled job {JOB}.", [evidence]).verified


def test_a_verdict_naming_an_id_is_backed_by_an_unconfirmed_try():
    assert _verify("The cancellation failed for task 8f10c3ba.", [Evidence("dag_manage", {})]).verified


def test_a_hex_id_matches_a_job_ids_tail():
    assert _verify("Job 2b31b663 was cancelled.", _status_read()).verified


def test_a_dag_call_whose_action_was_not_kept_cannot_confirm_a_named_cancel():
    result = _verify("I cancelled task 8f10c3ba.", [Evidence("dag_manage", {"dag_id": DAG_ID})])
    assert [c.evidence for c in result.claims] == ["plausible"] and not result.verified


@pytest.mark.parametrize("word", ["decade", "facade", "20261021"])
def test_an_english_word_or_a_date_is_no_work_id(word):
    # named no id: any cancel backs it
    assert _verify(f"I cancelled the {word} task.", [Evidence("cancel_task", {"task_id": TASK})]).verified


@pytest.mark.parametrize("output", ["Status: hacked\nExit code: 0", "Status: sk_live_abc123\nExit code: 0"])
def test_only_a_runner_status_word_is_ever_kept(output):
    assert "status " not in _summary(output, "bash", "success")


def test_a_durable_summary_word_outside_the_runner_set_is_not_read():
    row = SimpleNamespace(
        tool_name="bash",
        status="success",
        key_args={},
        input_text=f"bash {RUNNER} status {JOB}",
        result_summary="exit code 1; status hacked; 40 chars of output, not stored",
        side_effect_type="none",
    )
    assert _ledger_row_evidence(row).observed_status is None


# owner review of #796


@pytest.mark.parametrize(
    ("text", "verified"),
    [
        (f'I cancelled job "{JOB}".', True),
        (f'I cancelled job "{OTHER_JOB}".', False),  # a quoted id is still the one named
        (f"I cancelled job “{OTHER_JOB}”.", False),
        ('I cancelled task "1b2c3d4e".', False),
        ('I cancelled the job "as asked".', True),  # a quotation names no id: any cancel backs it
    ],
)
def test_a_quoted_work_id_is_the_id_named(text, verified):
    evidence = [Evidence("bash", {"command": f"bash {RUNNER} cancel {JOB}"}, exit_code=0)]
    assert _verify(text, evidence).verified is verified


@pytest.mark.parametrize(
    ("text", "verified"),
    [
        (f"Job {OTHER_JOB} was cancelled.", False),  # the owner cancels the root's work, never a runner job
        (f"I cancelled job {JOB}.", False),
        ("The subtask was cancelled.", True),
        (f"Task {TASK} was cancelled.", True),
    ],
)
def test_the_owners_cancel_backs_no_claim_naming_a_job_id(text, verified):
    assert _verify(text, [], cancelled=True).verified is verified


@pytest.mark.parametrize(
    ("claimed", "kept"),
    [
        (DAG_ID, DAG_ID[:8]),  # the durable row kept the short id dag_manage was given
        (DAG_ID[:8], DAG_ID),
        (DAG_ID[:8], DAG_ID[:6]),
    ],
)
def test_a_hex_id_matches_a_prefix_either_way(claimed, kept):
    def cancel(dag_id):
        return [Evidence("dag_manage", {"action": "cancel", "dag_id": dag_id})]

    assert _verify(f"I cancelled DAG {claimed}.", cancel(kept)).verified
    assert not _verify(f"I cancelled DAG {claimed}.", cancel("9" + kept[1:])).verified


# Codex P2 4365228440: the digit-and-letter rule keeps a word ("decade") from reading as an id in a
# claim's TEXT; a recorded argument is no prose, so any hex prefix of 6 or more, any hyphenated UUID
# prefix and any spelling of a UUID is an id there.
DIGITS_UUID = "12345678-abcd-4321-aaaa-0123456789ab"
LETTERS_UUID = "abcdefab-1234-4321-aaaa-0123456789ab"
ALL_DIGIT_UUID = "12345678-1234-4321-1111-012345678912"


@pytest.mark.parametrize(
    ("claimed", "dag_id", "level"),
    [
        # an all-digit prefix
        (DIGITS_UUID, "12345678", "exact"),
        (DIGITS_UUID, "1234567", "exact"),
        (DIGITS_UUID, "123456", "exact"),
        (DIGITS_UUID, "87654321", "none"),
        (DIGITS_UUID, "12345", "plausible"),  # too short to be an id: it shows nothing
        (ALL_DIGIT_UUID, "12345678", "exact"),
        (ALL_DIGIT_UUID, "87654321", "none"),
        # an all-letter prefix
        (LETTERS_UUID, "abcdefab", "exact"),
        (LETTERS_UUID, "abcdefa", "exact"),
        (LETTERS_UUID, "abcdef", "exact"),
        (LETTERS_UUID, "ABCDEFAB", "exact"),
        (LETTERS_UUID, "abcdefac", "none"),
        (LETTERS_UUID, "12345678", "none"),
        (DIGITS_UUID, "abcdefab", "none"),
        # a mixed prefix
        (DIGITS_UUID, "1234567a", "none"),
        ("1234ab5c", "1234ab", "exact"),
        ("1234ab5c", "1234AB5C", "exact"),
        ("1234ab5c", "1234ab5d", "none"),
        ("1234ab5c", "12345678", "none"),
        # a whole UUID, however it is spelled
        (DIGITS_UUID, DIGITS_UUID, "exact"),
        (DIGITS_UUID, DIGITS_UUID.upper(), "exact"),
        (DIGITS_UUID, DIGITS_UUID.replace("-", ""), "exact"),
        (DIGITS_UUID, "{" + DIGITS_UUID + "}", "exact"),
        (DIGITS_UUID, LETTERS_UUID, "none"),
        ("12345678", DIGITS_UUID, "plausible"),  # an all-digit id in the claim's text is no id: any cancel backs it
        ("1234ab5c", "1234ab5c-0000-4000-8000-000000000000", "exact"),
        ("1234ab5c", DIGITS_UUID, "none"),
        # a hyphenated UUID prefix (dag_manage takes one)
        (DIGITS_UUID, "12345678-abcd", "exact"),
        (DIGITS_UUID, "12345678-ABCD-4321", "exact"),
        (DIGITS_UUID, "12345678-ffff", "none"),
        # a claim naming the UUID without its hyphens
        (DIGITS_UUID.replace("-", ""), "12345678", "exact"),
        (DIGITS_UUID.replace("-", ""), DIGITS_UUID, "exact"),
        # no id
        (DIGITS_UUID, "nightly-sync", "plausible"),
        (DIGITS_UUID, "12345678-abcd-x", "plausible"),
    ],
)
def test_a_recorded_dag_id_is_an_id_whatever_its_digits(claimed, dag_id, level):
    result = _verify(f"I cancelled DAG {claimed}.", [Evidence("dag_manage", {"action": "cancel", "dag_id": dag_id})])
    assert [c.evidence for c in result.claims] == [level]
    # an id the claim names needs an exact match; a claim that names none is backed by any cancel
    assert result.verified is (level == "exact" or claimed == "12345678")


@pytest.mark.parametrize(
    ("claimed", "task_id", "level"),
    [
        (DIGITS_UUID, DIGITS_UUID.upper(), "exact"),
        (DIGITS_UUID, DIGITS_UUID.replace("-", ""), "exact"),
        ("12345678-abcd", DIGITS_UUID, "plausible"),  # a hyphenated prefix in the claim's text is no id
        (LETTERS_UUID, LETTERS_UUID, "exact"),
        (LETTERS_UUID, DIGITS_UUID, "none"),
    ],
)
def test_a_recorded_task_id_is_compared_in_any_uuid_spelling(claimed, task_id, level):
    result = _verify(f"I cancelled task {claimed}.", [Evidence("cancel_task", {"task_id": task_id})])
    assert [c.evidence for c in result.claims] == [level]


@pytest.mark.parametrize(("dag_id", "verified"), [("12345678", True), ("abcdefab", False), ("87654321", False)])
def test_a_durable_all_digit_dag_id_backs_the_full_uuid(dag_id, verified):
    from nous.cognitive.ledger_store import durable_key_args

    row = SimpleNamespace(
        tool_name="dag_manage",
        status="success",
        key_args=durable_key_args("dag_manage", {"action": "cancel", "dag_id": dag_id}),
        input_text=None,
        result_summary=None,
        side_effect_type="write",
    )
    assert _verify(f"I cancelled DAG {DIGITS_UUID}.", [_ledger_row_evidence(row)]).verified is verified


@pytest.mark.parametrize(
    "text",
    [
        "I cancelled the cafe2024 job.",  # a name before the noun is no id
        "I cancelled task facade-1234.",  # nor a hyphenated word that is no UUID
    ],
)
def test_a_hex_looking_name_is_no_work_id(text):
    # named no id: any cancel backs it
    assert _verify(text, [Evidence("cancel_task", {"task_id": TASK})]).verified


def test_a_status_line_with_a_carriage_return_is_read():
    output = STATUS_OUTPUT.replace("\n", "\r\n")
    assert _verify(f"Job {JOB} was cancelled.", _status_read(output)).verified


def test_an_unreadable_bash_command_cannot_disprove_an_id_less_cancel():
    unreadable = Evidence("bash", {"command": "echo 'unbalanced"}, exit_code=0)
    assert unreadable.runs is None
    result = _verify("The job was cancelled.", [unreadable])
    assert result.verified and [c.evidence for c in result.claims] == ["plausible"]
    named = _verify(f"I cancelled job {JOB}.", [unreadable])
    assert [v.evidence for v in named.violations] == ["plausible"]  # could not confirm, not refuted


def test_the_runner_path_is_the_workspaces_claude_jobs_runner():
    assert runner_path("/w") == "/w/claude-jobs/runner.sh"


def test_the_continuation_runner_checks_a_note_against_its_workspaces_runner(tmp_path):
    from nous.handlers.continuation_runner import LineageCalls

    settings = Settings(workspace_dir=str(tmp_path))
    cont = ContinuationRunner(database=None, settings=settings, runner=None, heart=None, brain=None)
    claim = SimpleNamespace(root_id=uuid.uuid4())

    def vet(runner):
        calls = [Evidence("bash", {"command": f"bash {runner} cancel {JOB}"}, exit_code=0)]
        return cont._vet_note(claim, f"I cancelled job {JOB}.", LineageCalls(), calls, "warn")[1]

    assert vet(f"{tmp_path}/claude-jobs/runner.sh") is False  # backed
    assert vet(RUNNER) is True  # another workspace's runner


@pytest.mark.parametrize(
    ("command", "verified"),
    [
        (f"bash -- {RUNNER} cancel {JOB}", True),
        (f"bash -x -- {RUNNER} cancel {JOB}", True),
        (f"bash -- -x {RUNNER} cancel {JOB}", False),  # after --, -x is the script bash runs
    ],
)
def test_the_options_end_at_a_double_dash(command, verified):
    evidence = [Evidence("bash", {"command": command}, exit_code=0)]
    assert _verify(f"I cancelled job {JOB}.", evidence).verified is verified


def test_the_owners_cancel_backs_no_claim_naming_a_jobs_hex_tail():
    assert not _verify("I cancelled job abcdef12.", [], cancelled=True).verified
    assert _verify("I cancelled task abcdef12.", [], cancelled=True).verified


def test_a_verifier_given_no_runner_takes_no_unreadable_command_for_a_cancel():
    unreadable = Evidence("bash", {"command": "echo 'unbalanced"}, exit_code=0)
    assert not ClaimVerifier().verify_report("I cancelled the job.", [unreadable]).verified


@pytest.mark.parametrize(
    "text",
    [
        f'I cancelled job "please stop {OTHER_JOB} too".',  # an id inside a quotation is quoted
        f"I cancelled job\n>{OTHER_JOB}",  # a block quote is quoted, whatever it holds
        "I cancelled task decade.",  # a hex-looking word with no digit is no id
    ],
)
def test_a_cancel_naming_no_id_of_its_own_is_backed_by_any_cancel(text):
    assert ClaimVerifier().verify_report(text, []).claims  # a cancel claim is read
    assert _verify(text, [Evidence("bash", {"command": f"bash {RUNNER} cancel {JOB}"}, exit_code=0)]).verified
