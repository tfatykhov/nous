"""Harness Phase 2c: first-person claims by kind, grounded in the evidence of any capable call."""

import pytest

from nous.cognitive.claim_verifier import ClaimVerifier, Evidence
from nous.cognitive.execution_ledger import ExecutionLedger


def _kinds(text):
    return [(c.kind, c.target) for c in ClaimVerifier()._extract_claims(text)]


def test_a_word_ending_in_i_is_not_a_subject():
    assert _kinds("The API sent the message to the queue.") == []


def test_a_claim_never_spans_sentences():
    assert _kinds("I created a helper. It reads data.\n\nLater the report file was large.") == []


def test_two_claims_in_one_paragraph_stay_two():
    text = "I saved the summary file. I sent the email to the team."
    assert [k for k, _ in _kinds(text)] == ["file_write", "email"]


def test_a_first_person_compound_is_two_claims():
    assert [k for k, _ in _kinds("I saved the file and sent the email.")] == ["file_write", "email"]


@pytest.mark.parametrize("narration", [
    "The send node completed and sent the premarket email to Tim.",
    "The subtask finished and pushed the branch.",
    "The heartbeat check ran and sent a message to the channel.",
    "Bob reviewed it and sent the email to the team.",
    "I checked: the DAG finished and sent the summary email to Tim.",
    "I see the subtask completed and sent the email to the team.",
    "I confirmed the scheduled task ran and pushed the branch.",
    "As I expected, the node ran and deployed the build.",
    "i sent the email",  # lowercase i is not the first person
])
def test_third_person_narration_is_not_a_claim(narration):
    assert _kinds(narration) == []


def test_a_failed_match_never_backtracks_exponentially():
    import time

    start = time.perf_counter()
    ClaimVerifier()._extract_claims("I saved " + "etc.," * 32 + " nothing " * 3 + "e.g., " * 32)
    assert time.perf_counter() - start < 0.05


@pytest.mark.parametrize("claim", [
    "I wrote the config.yaml file.",
    "I generated the v1.2 report for the board.",
    "I saved the notes (see e.g. section 2) in a file.",
])
def test_dots_inside_words_do_not_end_a_sentence(claim):
    assert [k for k, _ in _kinds(claim)] == ["file_write"]


def test_saved_to_captures_the_target():
    assert _kinds("The output was saved to /tmp/report.txt.") == [("file_write", "/tmp/report.txt")]


def test_i_saved_x_to_path_captures_the_target():
    assert _kinds("I saved the report to /tmp/x.md.") == [("file_write", "/tmp/x.md")]


def test_email_sent_to_captures_the_recipient():
    assert _kinds("Email sent to Alice@Example.com.") == [("email", "alice@example.com")]


def test_push_commit_deploy_are_distinct_kinds():
    assert [k for k, _ in _kinds("I committed the fix. I pushed it. I deployed the build.")] == [
        "vcs_commit", "vcs_push", "deploy"]


def test_plans_are_not_claims():
    assert _kinds("I will save the file and then I'll send the email.") == []


def test_a_curly_apostrophe_subject_is_a_claim():
    assert [k for k, _ in _kinds("I’ve saved the report file.")] == ["file_write"]


# --- evidence levels ---------------------------------------------------------


def _verify(text, *evidence, ledger=None):
    return ClaimVerifier().verify(text, [], ledger or ExecutionLedger(session_id="s"),
                                  turn_evidence=list(evidence))


def _bash(command, exit_code=0, side_effect="write"):
    return Evidence("bash", {"command": command}, exit_code=exit_code, side_effect=side_effect)


def test_a_bash_backup_grounds_a_file_claim():
    assert _verify("I created a backup file.", _bash("cp db.sqlite db.bak")).verified


def test_a_read_only_bash_never_grounds_a_file_claim():
    assert not _verify("It was saved to /tmp/report.md.", _bash("cat /tmp/report.md", side_effect="none")).verified


def test_any_bash_no_longer_grounds_a_push():
    assert not _verify("I pushed the changes.", _bash("ls -la", side_effect="none")).verified


@pytest.mark.parametrize("command, side_effect", [
    ("git log --oneline | grep push", "none"),
    ("git push --dry-run origin main", "external"),
])
def test_things_that_mention_push_are_not_a_push(command, side_effect):
    assert not _verify("I pushed the changes.", _bash(command, side_effect=side_effect)).verified


def test_git_push_grounds_a_push_but_a_failed_one_does_not():
    assert _verify("I pushed the changes.", _bash("cd r && git push origin main", side_effect="external")).verified
    assert not _verify("I pushed the changes.",
                       _bash("git push origin main", exit_code=1, side_effect="external")).verified


def test_a_named_target_must_match():
    assert not _verify("It was saved to /tmp/report.md.", Evidence("write_file", {"path": "/tmp/other.md"})).verified
    assert not _verify("It was saved to /tmp/report.md.", _bash("touch /tmp/x")).verified
    assert _verify("It was saved to /tmp/report.md.", Evidence("write_file", {"path": "/tmp/report.md"})).verified


def test_a_script_that_only_reads_grounds_nothing():
    script = Evidence("run_python", {"code": "print(recall_deep('premarket'))"})
    for claim in ("I saved the report file.", "I sent the email to the team.", "I deployed the build."):
        assert not _verify(claim, script).verified, claim


def test_a_web_request_is_not_an_email():
    assert not _verify("I sent the email.", _bash("curl -s https://wttr.in", side_effect="external")).verified


@pytest.mark.parametrize("claim, evidence", [
    ("I sent the email.", _bash("cat /var/log/mail.log", side_effect="none")),
    ("I committed the fix.", _bash("git log --grep commit", side_effect="none")),
    ("I pushed the fix.", _bash("git push -n origin main", side_effect="external")),
    ("I deployed the build.", _bash("curl -s https://api.example.com/status", side_effect="external")),
])
def test_reads_and_no_ops_ground_nothing(claim, evidence):
    assert not _verify(claim, evidence).verified


def test_an_rsync_grounds_a_deploy():
    assert _verify("I deployed the build.", _bash("rsync -a build/ host:/srv", side_effect="external")).verified


@pytest.mark.parametrize("evidence", [
    _bash("sendmail a@x.io < m", side_effect="external"),
    Evidence("run_python", {"code": "import smtplib\ns = smtplib.SMTP('h')"}),
    Evidence("send_file", {"file_path": "/tmp/r.png"}),
])
def test_other_ways_to_send_are_plausible(evidence):
    result = _verify("I sent the email.", evidence)
    assert result.verified and result.claims[0].evidence == "plausible"


def test_a_script_that_writes_grounds_a_file_claim():
    assert _verify("I saved the report file.", Evidence("run_python", {"code": "df.to_csv('r.csv')"})).verified


def test_email_recipient_must_match_when_named():
    other = Evidence("send_email", {"to": "['bob@x.io']", "subject": "s"})
    assert not _verify("Email sent to alice@x.io.", other).verified


def test_docker_compose_grounds_a_deploy():
    assert _verify("I deployed the build.", _bash("docker compose up -d")).verified


def test_no_capable_tool_is_still_a_violation():
    result = _verify("I saved the report file.", Evidence("recall_deep", {}))
    assert not result.verified
    assert result.violations[0].expected_tool == "write_file"
    assert set(result.violations[0].capable_tools) == {"write_file", "bash", "run_python"}


def test_ledger_history_with_evidence_args_counts():
    ledger = ExecutionLedger(session_id="s")
    ledger.record("bash", {"command": "git push"}, "Exit code: 0", "success")
    assert ClaimVerifier().verify("I pushed the fix.", [], ledger).verified


def test_names_only_callers_keep_working():
    assert ClaimVerifier().verify("I saved the report file.", ["write_file"],
                                  ExecutionLedger(session_id="s")).verified


def test_claims_are_reported_even_when_verified():
    result = _verify("I saved the report file.", Evidence("write_file", {"path": "/tmp/r.md"}))
    assert [(c.kind, c.evidence) for c in result.claims] == [("file_write", "exact")]


# --- evidence reads the command the way bash does ---------------------------


@pytest.mark.parametrize("command", [
    "git push origin main 2>&1 | tail -n 5",          # a -n elsewhere is not a dry run
    "cd /srv/app && sudo -u deploy git -C /srv/app push",
    "GIT_SSH_COMMAND='ssh -i k' timeout 60 git push --force-with-lease",
])
def test_a_real_push_is_grounded_however_it_is_wrapped(command):
    result = _verify("I pushed the fix.", _bash(command, side_effect="external"))
    assert result.verified and result.claims[0].evidence == "exact"


def test_commit_dash_n_is_no_verify_not_a_dry_run():
    assert _verify("I committed the fix.", _bash("git commit -n -m 'wip'")).verified
    assert not _verify("I committed the fix.", _bash("git commit --dry-run -m 'wip'")).verified


@pytest.mark.parametrize("claim, command", [
    ("I pushed the fix.", "echo 'git push' > notes.txt; curl -s https://x"),
    ("I committed the fix.", "echo git commit >> notes.txt"),
    ("I pushed the fix.", "git log --grep push && curl -s https://x"),
    ("I sent the email.", "grep sendmail /etc/aliases; curl -s https://x"),
])
def test_a_mention_is_not_a_run(claim, command):
    assert not _verify(claim, _bash(command, side_effect="external")).verified


def test_curl_to_an_smtp_server_is_a_send():
    command = "curl --url smtps://smtp.x.io:465 --mail-rcpt a@x.io -T mail.txt"
    assert _verify("I sent the email.", _bash(command, side_effect="external")).verified


def test_a_script_dry_run_push_is_not_a_push():
    code = 'subprocess.run(["git", "push", "-n", "origin", "main"])'
    assert not _verify("I pushed the fix.", Evidence("run_python", {"code": code})).verified
    code = 'subprocess.run(["git", "push", "origin", "main"])'
    assert _verify("I pushed the fix.", Evidence("run_python", {"code": code})).verified


def test_evidence_on_huge_arguments_is_linear():
    """This turn's evidence is untruncated and checked on the event loop."""
    import time

    command = "git " * 15_000 + "curl " * 1_000
    code = "open(" * 20_000 + "git " * 20_000
    start = time.perf_counter()
    for claim in ("I pushed the fix.", "I sent the email.", "I saved the report file."):
        _verify(claim, _bash(command, side_effect="external"), Evidence("run_python", {"code": code}))
    assert time.perf_counter() - start < 1.0


# --- runner wiring -------------------------------------------------------------


def _runner_with_capture():
    from types import SimpleNamespace

    from nous.api.runner import AgentRunner

    runner = AgentRunner.__new__(AgentRunner)
    runner._claim_verifier = ClaimVerifier()
    runner._intent_tracker = None
    runner._settings = SimpleNamespace(claim_verification_mode="enforce")
    runner._pending_corrections = {}
    events = []
    runner._log_f026_decision = lambda kind, data, session_id: events.append((kind, data))
    return runner, events


def test_runner_grounds_a_claim_in_this_turns_untruncated_arguments():
    from nous.cognitive.schemas import ToolResult

    runner, events = _runner_with_capture()
    long_cmd = "git commit -m '" + "x" * 5000 + "' && git push origin main"
    results = [ToolResult(tool_name="bash", arguments={"command": long_cmd}, result="Exit code: 0")]
    runner._verify_claims("s1", "I pushed the fix.", results, ExecutionLedger(session_id="s1"))
    (_, data), = events
    assert data["verified"] and data["claim_count"] == 1
    assert data["claims"] == [{"kind": "vcs_push", "evidence": "exact", "text": "I pushed"}]
    assert data["turn"] == 0
    assert runner._pending_corrections == {}


def test_runner_rejects_a_failed_push():
    from nous.cognitive.schemas import ToolResult

    runner, events = _runner_with_capture()
    results = [ToolResult(tool_name="bash", arguments={"command": "git push"}, result="rejected\nExit code: 1")]
    runner._verify_claims("s1", "I pushed the fix.", results, ExecutionLedger(session_id="s1"))
    assert not events[0][1]["verified"] and runner._pending_corrections["s1"]


def test_runner_ignores_third_person_narration():
    runner, events = _runner_with_capture()
    runner._verify_claims("s1", "The send node completed and sent the email to Tim.", [],
                          ExecutionLedger(session_id="s1"))
    assert events[0][1]["verified"] and events[0][1]["claim_count"] == 0


# --- unreadable is not no-signal (review round 1) ------------------------------
# Under `enforce` a false violation misleads the agent, so evidence the reader
# cannot parse is plausible; only a command it READ, doing something else, is none.


def _real_bash(command, exit_code=0):
    from nous.cognitive.execution_ledger import classify_side_effect

    return Evidence("bash", {"command": command}, exit_code=exit_code,
                    side_effect=classify_side_effect("bash", {"command": command}))


_HEREDOC_PUSH = ("cd /repo && git add -A && git commit -F - <<'EOF'\n"
                 "fix: don't drop the header\nEOF\ngit push origin main")


@pytest.mark.parametrize("command", [
    _HEREDOC_PUSH,                                    # an apostrophe in a heredoc body
    "if git push origin main; then echo ok; fi",      # reserved words
    "{ git push origin main; } 2>&1 | tee push.log",
    "! git push origin main",
    "bash -c 'cd /repo && git push origin main'",     # an inner command string
    "ssh deploy@host 'cd /srv/app && git push'",
])
def test_a_push_the_reader_cannot_disprove_is_not_a_violation(command):
    assert _verify("I pushed the fix.", _real_bash(command)).verified


def test_a_push_past_the_ledgers_cut_still_grounds_a_later_claim():
    cmd = ('git commit -m "feat: ' + "x" * 1200 + '" && git tag -a v1.2 -m \''
           + "notes " * 200 + "' && git push --follow-tags origin main")
    ledger = ExecutionLedger(session_id="s")
    ledger.set_turn(1)
    ledger.record("bash", {"command": cmd}, "To github.com:x/y\nExit code: 0", "success")
    ledger.set_turn(2)
    result = ClaimVerifier().verify("As noted, I pushed the tag earlier.", [], ledger, turn_evidence=[])
    assert result.verified and result.claims[0].evidence == "exact"


def test_a_non_zero_exit_disproves_only_the_last_command():
    assert _verify("I pushed the fix.", _real_bash("git push origin main && gh pr create --fill", 1)).verified
    assert not _verify("I pushed the fix.", _real_bash("gh pr view 12 && git push origin main", 1)).verified
    assert _verify("I saved the report file.",
                   _real_bash("python3 gen.py > /tmp/report.md; grep -c TODO /tmp/report.md", 1)).verified


@pytest.mark.parametrize("command", [
    "msmtp -a default alice@x.io < /tmp/mail.txt",
    "swaks --to alice@x.io --server smtp.x.io --body @/tmp/m.txt",
    "mail -s 'Update' alice@x.io <<'EOF'\nIt's done.\nEOF",
    "curl -s -X POST https://api.telegram.org/bot$TOKEN/sendMessage -d chat_id=1 -d text=done",
])
def test_every_way_to_send_a_message_counts(command):
    assert _verify("I sent the alert message to the team.", _real_bash(command)).verified


def test_reading_the_mailbox_is_not_sending():
    assert not _verify("I sent the email.", _real_bash("mail -H")).verified


@pytest.mark.parametrize("command", [
    "git push heroku main",
    "vercel --prod",
    "npm run deploy",
    "./scripts/deploy.sh production",
])
def test_deploys_through_other_tools(command):
    assert _verify("I deployed the build.", _real_bash(command)).verified


def test_docker_push_grounds_a_tag_push():
    assert _verify("I pushed the v1.2 tag.", _real_bash("docker push ghcr.io/me/app:v1.2")).verified


@pytest.mark.parametrize("code", [
    "with open(os.path.join(out_dir, 'report.md'), 'w') as f: f.write(s)",
    "with open(Path(out_dir) / 'report.md', 'w', encoding='utf-8') as f: f.write(s)",
    "open(str(path), 'w').write(s)",
    "wb.save('/tmp/report.xlsx')",
    "fig.write_html('/tmp/report.html')",
    "pickle.dump(obj, fh)",
    "yaml.safe_dump(data, fh)",
])
def test_common_python_file_writes(code):
    assert _verify("I saved the report file.", Evidence("run_python", {"code": code})).verified


def test_a_split_list_literal_push_in_python():
    code = "subprocess.run([\n    'git',\n    'push',\n    'origin', 'main',\n], check=True)"
    assert _verify("I pushed the fix.", Evidence("run_python", {"code": code})).verified


# --- extraction: what a claim is (review round 1) -------------------------------


@pytest.mark.parametrize("text", [
    # Nous's own tools prompt this wording: push_surface "pushes" a surface
    "I pushed an approval card to your companion app.",
    "I've pushed the update to your dashboard.",
    "I pushed the image to GHCR.",
    "I've committed that to memory.",
    "I committed the key changes to memory.",
    "I created the health report in your companion app.",
    "I saved the key facts from the report to memory.",
    "I've written a short report below:",
])
def test_non_vcs_and_in_chat_wording_is_not_a_file_or_vcs_claim(text):
    assert _kinds(text) == []


@pytest.mark.parametrize("text, kind", [
    ("I pushed it.", "vcs_push"),
    ("I pushed that to main.", "vcs_push"),
    ("I've pushed the changes to the repository.", "vcs_push"),
    ("I pushed and opened a PR.", "vcs_push"),
    ("I committed the fix.", "vcs_commit"),
    ("I committed them.", "vcs_commit"),
])
def test_vcs_claims_name_a_vcs_object(text, kind):
    assert [k for k, _ in _kinds(text)] == [kind]


@pytest.mark.parametrize("text", [
    "I saved the updated config, as described in README.md.",
    "I saved the summary document; the raw numbers are still in data.csv.",
    "I saved the summary document (the raw numbers are in data.csv).",
    "I saved the report in v1.2 format.",
    "I saved the file at 3.30pm.",
])
def test_a_target_is_only_where_the_object_was_saved(text):
    assert all(target is None for _, target in _kinds(text))
    assert _verify(text, Evidence("write_file", {"path": "/repo/config.yaml"})).verified


@pytest.mark.parametrize("narration", [
    "As I wrote earlier, the DAG finished and sent the email to Tim.",
    "I created a schedule for this; it ran at 9am and sent the report to Tim.",
    "I stored the credentials, then the CI job ran and deployed the build.",
])
def test_an_and_clause_needs_a_first_person_claim_in_the_same_clause(narration):
    assert _kinds(narration) == []


def test_a_comma_before_and_keeps_the_compound():
    assert [k for k, _ in _kinds("I saved the file, and sent the email.")] == ["file_write", "email"]


@pytest.mark.parametrize("text", [
    "Logs are written to /var/log/nous/app.log.",
    "Where is it saved? It's saved to ~/.nous/config.toml.",
    "The email sent to alice@x.io bounced.",
    "No email sent to alice@x.io was found in the outbox.",
    "Was the email sent to alice@x.io?",
    "Did I send the email to the team?",
    "If I pushed now, CI would break.",
    "Not sure whether I committed it.",
    'You said "I sent the email to Tim" earlier.',
    "```\nI pushed the fix\n```",
    "> I sent the email to the team.",
])
def test_descriptions_questions_and_quotes_are_not_claims(text):
    assert _kinds(text) == []


@pytest.mark.parametrize("text", [
    "The output was saved to /tmp/report.txt",
    "email sent to alice@example.com",
    "Done. Email sent to alice@example.com.",
    "The email was sent to alice@example.com.",
])
def test_completion_statements_without_an_actor_stay_claims(text):
    assert len(_kinds(text)) == 1


def test_extraction_is_linear_on_a_long_status_report():
    import time

    lines = [f"- job-{i:03d}: check ran at 0{i % 10}:15 UTC and sent the summary message to #ops"
             for i in range(600)]
    report = "Here is the status report.\n\n" + "\n".join(lines)
    wall = "I sent the email. " + "and sent the message " * 3000
    start = time.perf_counter()
    ClaimVerifier()._extract_claims(report)
    ClaimVerifier()._extract_claims(wall)
    assert time.perf_counter() - start < 0.2
