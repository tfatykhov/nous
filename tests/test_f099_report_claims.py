"""F099 gate hardening B: a subtask's final report and a continuation note are checked against their tool log.

The soak failed gate criterion 5: on 2026-10-07 a continuation turn wrote "F099 test 6 FAILED. The cancel did not
take effect." when nobody had issued a cancel, stored it as a fact, and the ``learn_fact`` earned the arrival its
progress credit. ``ClaimVerifier.verify_report`` reads the verdicts and passive forms a report uses and checks them
against the calls the work recorded; an unbacked claim is annotated and, in enforce, not credited. Nothing is blocked.
"""

from __future__ import annotations

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
from nous.cognitive.claim_verifier import UNVERIFIED_MARKER, ClaimVerifier, Evidence
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


def _verify(text, evidence=(), **kwargs):
    return ClaimVerifier().verify_report(text, list(evidence), **kwargs)


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
    ran = "import subprocess\nsubprocess.run(['gh', 'pr', 'merge', '4'])"
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
    """The durable ledger hashes a command: names-only evidence, never a false flag."""
    assert _verify("PR #4 merged.", [Evidence("bash", {})]).verified


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
