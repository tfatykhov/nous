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


def _extract_seconds(text):
    """Time one extraction after a warm-up (the first call compiles the patterns)."""
    import time

    verifier = ClaimVerifier()
    verifier._extract_claims(text[:200])
    start = time.perf_counter()
    verifier._extract_claims(text)
    return time.perf_counter() - start


def test_a_failed_match_never_backtracks_exponentially():
    """The overlapping-branch form took 4.5 s at n=20 and would take hours at
    n=32; a generous bound still separates that from a slow CI runner."""
    assert _extract_seconds("I saved " + "etc.," * 32 + " nothing " * 3 + "e.g., " * 32) < 1.0


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
    assert {"write_file", "bash", "run_python"} <= set(result.violations[0].capable_tools)
    assert "recall_deep" not in result.violations[0].capable_tools


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
    assert time.perf_counter() - start < 3.0  # quadratic on 60 KB would be minutes


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


@pytest.mark.parametrize("make", [
    lambda n: "Here is the status report.\n\n" + "\n".join(
        f"- job-{i:03d}: check ran at 0{i % 10}:15 UTC and sent the summary message to #ops"
        for i in range(n)),
    lambda n: "I sent the email. " + "and sent the message " * (5 * n),
], ids=["status-report", "and-clauses"])
def test_extraction_is_linear(make):
    """Pinned as SCALING, not wall-clock: a shared CI runner is several times
    slower than a laptop. Four times the input costs about 4x when linear
    and about 16x when quadratic (the pre-fix rescans were quadratic)."""
    small, big = _extract_seconds(make(600)), _extract_seconds(make(2400))
    assert big < 6 * small + 0.05, (small, big)
    assert big < 3.0


# --- review round 2 ------------------------------------------------------------


@pytest.mark.parametrize("text", [
    # Nous's own tools: a schedule, a check, a subtask, a fact, a micro-app
    "I've created a schedule that generates the report every Monday at 9am.",
    "I created a heartbeat check that emails the report to you every morning.",
    "I created a subtask to write the report in the background.",
    "I saved your preference, so the daily report now uses metric units.",
    "I saved the report as a fact so I can recall it later.",
    "I saved the document to my knowledge base.",
    "I created a report dashboard with the Q3 numbers.",
    "I generated the report as a micro-app you can refresh.",
    "I sent the weekly report to your companion app.",
    "I've deployed a heartbeat check that watches disk usage.",
    "I deployed a live dashboard for your health metrics.",
    # idioms with a VCS noun nearby
    "I pushed back on the code review feedback.",
    "I pushed for a smaller PR.",
    "I committed to a code freeze until Friday.",
    "I pushed an approval request for the deploy branch.",
    "I pushed a notification about the failed commit.",
    "I pushed hard to get the feature finished before the demo.",
    # another actor's work, and descriptions
    "The report was saved to /srv/reports/q3.md by the scheduled task.",
    "The email was sent to alice@x.io by the nightly digest job.",
    "Check whether it was saved to /tmp/r.md.",
    "Default: saved to ~/.config/app.toml.",
    "Where does it go?\nSaved to ~/.cache/nous/ by default.",
    "- saved to /var/lib/app/state.json on every shutdown",
])
def test_nous_wording_idioms_and_descriptions_are_not_claims(text):
    assert _kinds(text) == []


@pytest.mark.parametrize("text, kinds", [
    ("I've pushed the fix to main — want me to deploy it too?", ["vcs_push"]),
    ("I committed the fix; shall I push it?", ["vcs_commit"]),
    ("I saved the report to /tmp/r.md, want me to email it?", ["file_write"]),
    ("I've committed and pushed everything.", ["vcs_commit", "vcs_push"]),
    ("I committed the changes listed below.", ["vcs_commit"]),
    ("I pushed the fix described above to main.", ["vcs_push"]),
    ("I've **pushed** the fix to main.", ["vcs_push"]),
    ("I pushed the failing test fix.", ["vcs_push"]),
    ("I pushed that to main.", ["vcs_push"]),
    ("I deployed the fix to prod.", ["deploy"]),
    ("I deployed to Heroku.", ["deploy"]),
    ("I sent you the report.", ["email"]),
    ("I saved the report file if you need it.", ["file_write"]),
])
def test_completed_actions_stay_claims(text, kinds):
    assert [k for k, _ in _kinds(text)] == kinds


def test_a_nous_producing_tool_grounds_an_untargeted_claim():
    made = Evidence("compose_surface", {"intent": "q3 report"})
    assert _verify("I created the report file.", made).claims[0].evidence == "plausible"
    assert _verify("I sent you the report.", made).verified
    assert not _verify("It was saved to /tmp/r.md.", made).verified


@pytest.mark.parametrize("command", [
    "python3 --version", "node -v", 'python3 -c "print(1+1)"', "uv sync",
    "uv run pytest -q tests/test_x.py", "npm test", "make lint", "ssh prod uptime",
    "bash -c 'ls -la'", "env -S 'ls -l'", "./scripts/check_health.sh",
])
def test_a_readable_or_unrelated_run_grounds_nothing(command):
    for claim in ("I pushed the fix to main.", "I committed the fix.",
                  "I sent the email to alice@x.io about it.", "Email sent to alice@x.io.",
                  "I deployed the fix to prod."):
        assert not _verify(claim, _real_bash(command)).verified, (claim, command)


def _push_level(command):
    return _verify("I pushed the fix.", _real_bash(command)).claims[0].evidence


def test_a_command_string_is_read():
    assert _push_level("bash -c 'cd /repo && git push origin main'") == "exact"
    assert _push_level("ssh deploy@host 'cd /srv/app && git push'") == "exact"
    assert _push_level("eval git push origin main") == "exact"
    assert _push_level("sudo -u deploy bash -c 'git -C /srv push'") == "exact"
    assert _push_level("bash -c 'git log --oneline | grep push'") == "none"


def test_python_dash_c_is_read_as_code():
    sends = "python3 -c 'import smtplib; smtplib.SMTP(\"h\").sendmail(a, b, m)'"
    assert _verify("I sent the email.", _real_bash(sends)).verified
    assert not _verify("I sent the email.", _real_bash("python3 -c 'print(1)'")).verified


def test_an_interpreter_grounds_only_what_its_text_hints_at():
    assert _verify("I exported the data to /tmp/export.csv.",
                   _real_bash("uv run python scripts/export.py")).verified
    assert _verify("I sent the digest email.", _real_bash("python3 scripts/send_digest.py")).verified
    assert not _verify("I pushed the fix.", _real_bash("python3 scripts/send_digest.py")).verified
    assert _verify("I deployed the build.", _real_bash("make release")).verified


@pytest.mark.parametrize("command", [
    "tail -n 50 /var/log/deploy.log", "ls deploy/", "grep -rn deploy docs/", "cat deploy.yaml",
    "git log --oneline -5 -- deploy/", "docker ps", "docker logs nous --tail 20", "kubectl get pods",
    "systemctl status nous", "aws s3 ls",
])
def test_looking_at_a_deployment_is_not_deploying(command):
    assert not _verify("I deployed the fix to prod.", _real_bash(command)).verified


@pytest.mark.parametrize("command", [
    "docker compose up -d", "kubectl apply -f k8s/", "helm upgrade --install nous ./chart",
    "systemctl restart nous", "terraform apply -auto-approve", "aws s3 sync build/ s3://site",
    "fly deploy", "npm run deploy", "make deploy", "vercel --prod",
])
def test_changing_a_deployment_is_deploying(command):
    assert _verify("I deployed the fix to prod.", _real_bash(command)).verified


def test_a_recipient_in_a_variable_is_a_send():
    assert _verify("I sent the email.", _real_bash('TO=alice@x.io; mail -s "Report" "$TO" < body.txt')).verified
    assert _verify("Email sent to alice@x.io.", _real_bash('mail -s "Report" "$TO" < body.txt')).verified


def test_this_turns_evidence_is_never_capped():
    cmd = (" && ".join(f"mkdir -p out/d{i}" for i in range(40))
           + " && git add -A && git commit -m s && git push origin main")
    assert _verify("I pushed the fix to main.", _real_bash(cmd)).claims[0].evidence == "exact"
    ledger = ExecutionLedger(session_id="s")
    ledger.record("bash", {"command": cmd}, "Exit code: 0", "success")
    assert ClaimVerifier().verify("I pushed the fix to main.", [], ledger).verified


def test_as_i_wrote_is_narration_and_a_version_is_a_vcs_object():
    assert _kinds("As I wrote in the report, the subtask completed and pushed the branch.") == []
    assert [k for k, _ in _kinds("I pushed v1.2.0.")] == ["vcs_push"]


def test_help_and_queue_listing_do_nothing():
    assert not _verify("I pushed the fix.", _real_bash("git push --help")).verified
    assert not _verify("I sent the email.", _real_bash("sendmail -bp")).verified
    assert _verify("I sent the email.", _real_bash("sendmail -t < mail.txt")).verified


# --- review round 3 ------------------------------------------------------------

_HEREDOC_SCRIPT = ("python3 - <<'EOF'\nimport smtplib\ns = smtplib.SMTP('h')\n"
                   "s.sendmail('me', 'alice@x.io', 'msg')\nEOF")


@pytest.mark.parametrize("command", [
    _HEREDOC_SCRIPT,
    "uv run python - <<'EOF'\nimport smtplib\nsmtplib.SMTP('h').sendmail(a, b, m)\nEOF",
    "python3 - <<'EOF'\n# don't\nimport smtplib\nsmtplib.SMTP('h')\nEOF",  # an apostrophe in the body
])
def test_a_script_fed_by_heredoc_is_judged_as_code(command):
    assert _verify("I sent the email to alice@x.io.", _real_bash(command)).verified
    assert not _verify("I pushed the fix.", _real_bash(command)).verified


def test_a_heredoc_push_in_python_is_a_push():
    cmd = "python3 - <<'EOF'\nimport subprocess\nsubprocess.run(['git', 'push', 'origin', 'main'])\nEOF"
    assert _verify("I pushed the fix.", _real_bash(cmd)).verified


@pytest.mark.parametrize("claim, command", [
    ("I pushed the fix to main.", "cat <<'EOF' > notes.md\nTODO:\ngit push origin main\nEOF"),
    ("I pushed the fix to main.", "cat > runbook.md <<'EOF'\n# Runbook\ngit push origin main\nEOF"),
    ("I committed the fix.", "tee -a HOWTO.md <<'EOF'\ngit commit -am wip\nEOF"),
    ("I deployed the fix to prod.", "cat > deploy.sh <<'EOF'\n#!/bin/bash\ndocker compose up -d\nEOF"),
    ("I sent the email.", "cat > send.sh <<'EOF'\nsendmail alice@x.io < body.txt\nEOF"),
])
def test_text_written_through_a_heredoc_did_not_run(claim, command):
    assert not _verify(claim, _real_bash(command)).verified


@pytest.mark.parametrize("claim, command", [
    ("I sent the email to alice@x.io.", "python3 scripts/daily_brief.py"),
    ("I sent the email to the team.", "python3 scripts/outreach.py --to team"),
    ("I sent the email.", "uv run python -m nous.tools.dispatch"),
    ("I sent the message to Telegram.", "node bin/post.js"),
    ("I deployed the release.", "./bin/release"),
    ("I deployed the fix to prod.", "npm run build && npm start"),
    ("I deployed the app.", "pm2 restart app"),
    ("I deployed the app.", "supervisorctl restart nous"),
    ("I deployed the app.", "service nginx restart"),
    ("I deployed the app.", "cap production deploy"),
    ("I deployed the app.", "heroku container:release web"),
    ("I deployed the app.", "gh workflow run deploy.yml"),
    ("I committed the version bump.", "uv run cz bump"),
    ("I committed the version bump.", "npm version patch"),
    ("I saved the chart to /tmp/chart.png.", "python3 scripts/plot.py"),
    ("I sent the email to alice@x.io.", "python3 scripts/gen_report.py -c cfg.yaml"),  # its own -c
    ("I pushed the files to the server with rsync.", "rsync -a build/ host:/srv"),
    ("I pushed the fix to main.", "ssh -vp 2222 host 'git push origin main'"),
    ("I pushed the fix to main.", "env FOO=1 -S 'git push origin main'"),
])
def test_a_real_action_through_an_unhinted_program_is_not_a_violation(claim, command):
    assert _verify(claim, _real_bash(command)).verified


@pytest.mark.parametrize("claim, command", [
    ("I pushed the fix to main.", "uv run pytest tests/test_push.py"),
    ("I committed the fix.", "uv run pytest -q -k commit"),
    ("I saved the report to /tmp/report.md.", "make lint"),  # an untargeted file claim + any write stays plausible
    ("I deployed the fix.", "npm install"),
    ("I deployed the fix.", "pip install gitpython"),
    ("I sent the email to alice@x.io.", "ssh host"),
    ("I sent the email to alice@x.io.", "ssh -i k host"),
])
def test_tests_installs_and_sessions_ground_nothing(claim, command):
    assert not _verify(claim, _real_bash(command)).verified


@pytest.mark.parametrize("text, kinds", [
    ("I committed the dashboard changes.", ["vcs_commit"]),
    ("I pushed the surface renderer fix.", ["vcs_push"]),
    ("I committed the micro-app fixture.", ["vcs_commit"]),
    ("I pushed the branch to the dashboard repo.", ["vcs_push"]),
    ("I committed and pushed the companion PWA fix.", ["vcs_commit", "vcs_push"]),
    ("I pushed to origin/main.", ["vcs_push"]),
    ("I've pushed to GitHub.", ["vcs_push"]),
    ("I committed with message 'fix: x'.", ["vcs_commit"]),
    ("I pushed the latest.", ["vcs_push"]),
    ("I deployed.", ["deploy"]),
    ("I redeployed the service.", ["deploy"]),
    ("I emailed Tim.", ["email"]),
    ("I emailed the summary to Tim.", ["email"]),
    ("I sent Tim a note about it.", ["email"]),
    ("I've emailed it.", ["email"]),
    ("I sent it to Tim by email.", ["email"]),
    ("Saved to /tmp/r.md by 9am.", ["file_write"]),
    ("Email sent to alice@x.io by 09:00 UTC.", ["email"]),
])
def test_this_repos_vocabulary_and_common_phrasings_are_claims(text, kinds):
    assert [k for k, _ in _kinds(text)] == kinds


@pytest.mark.parametrize("text", [
    "I created a report dashboard with the Q3 numbers.",
    "I deployed a live dashboard for your health metrics.",
    "I pushed the release to Friday.",
    "I pushed the code review to next week.",
    "I pushed the new docs to the wiki.",
    "I pushed the update to the device over USB.",
])
def test_artifacts_schedules_and_devices_are_not_vcs_or_file_claims(text):
    assert _kinds(text) == []


def test_an_expanded_program_is_opaque():
    assert _verify("I pushed the fix to main.", _real_bash('bash -c "$(cat cmd.txt)"')).verified
    assert _verify("I pushed the fix to main.", _real_bash("$DEPLOY_CMD")).verified


# --- review round 4 ------------------------------------------------------------


@pytest.mark.parametrize("command", [
    'git add -A\ngit commit -m "docs: explain <<EOF heredocs"\ngit push origin main',
    'echo "syntax: cat <<EOF"\ngit push origin main',
    "grep -n '<<EOF' scripts/*.sh\ngit push origin main",
    "cat <<'EOF' > x\r\nhi\r\nEOF\r\ngit push origin main\r\n",
    'bash -c "cat <<EOF > x\nhi\nEOF\ngit push origin main"',
])
def test_a_quoted_or_nested_heredoc_marker_never_swallows_the_push(command):
    assert _verify("I pushed the fix to main.", _real_bash(command)).claims[0].evidence == "exact"


def test_a_config_written_over_ssh_then_restarted_is_a_deploy():
    cmd = "ssh host \"cat > /etc/nous.env <<'EOF'\nA=1\nEOF\nsudo systemctl restart nous\""
    assert _verify("I deployed the fix to prod.", _real_bash(cmd)).verified


def test_a_variable_delimiter_body_is_still_data():
    cmd = "cat <<$DELIM > notes.md\ngit push origin main\n$DELIM"
    assert not _verify("I pushed the fix to main.", _real_bash(cmd)).verified


@pytest.mark.parametrize("claim, command", [
    ("I deployed the build to the server.", "/usr/bin/rsync -a build/ host:/srv"),
    ("I pushed the files to the server with rsync.", "/usr/bin/rsync -a build/ host:/srv"),
])
def test_a_path_qualified_program_keeps_its_identity(claim, command):
    assert _verify(claim, _real_bash(command)).verified


def test_a_path_qualified_git_push_is_exact():
    result = _verify("I pushed the fix to main.", _real_bash("/usr/bin/git push origin main"))
    assert result.claims[0].evidence == "exact"


def test_a_recipient_inside_the_body_is_not_a_recipient():
    assert not _verify("I sent the email.", _real_bash("mail -s Report <<'EOF'\ncc alice@x.io\nEOF")).verified


def test_a_body_is_never_a_program():
    assert not _verify("I deployed the fix to prod.", _real_bash("diff <(cat <<A) x\ndeploy now\nA")).verified


def test_pip_is_not_a_push():
    assert not _verify("I pushed the fix to main.", _real_bash("python3 -m pip install gitpython")).verified


@pytest.mark.parametrize("text", [
    "I pushed the changes to the client repo.",
    "I pushed the fix to the team branch.",
    "I pushed the fix to 3 remotes.",
    "I pushed the release to the wiki repo.",
    "I pushed the fix to the device-config branch.",
])
def test_a_destination_that_is_a_repo_or_branch_is_a_push(text):
    assert [k for k, _ in _kinds(text)] == ["vcs_push"]


def test_a_heads_up_in_the_reply_is_not_a_message_sent():
    assert _kinds("I sent a heads-up in the reply.") == []
    assert _kinds("I sent the summary in this response.") == []


# --- codex round 1 -------------------------------------------------------------


@pytest.mark.parametrize("command", [
    "echo ok # ; git push origin main",
    "echo ok  # git push origin main",
    "# git push origin main\necho ok",
    "echo ok #git push origin main",
])
def test_a_comment_never_runs(command):
    assert not _verify("I pushed the fix.", _real_bash(command)).verified


def test_a_hash_inside_a_word_or_quotes_is_not_a_comment():
    assert _push_level("echo 'a # b' && git push origin main") == "exact"
    assert _push_level('echo "#1" && git push origin main') == "exact"
    assert _push_level("echo a#b && git push origin main") == "exact"


@pytest.mark.parametrize("command, level", [
    ("false && git push origin main || true", "plausible"),  # which branch ran is unknowable
    ("git push origin main || echo failed", "plausible"),      # its failure is masked
    ("cd repo && git push origin main", "exact"),
    ("git add -A; git commit -m x; git push origin main", "exact"),
    ("git push origin main; echo done", "plausible"),          # ran; success unknown
    ("bash -c 'cd repo && git push origin main'", "exact"),
    ("git push origin main && gh pr create --fill", "exact"),
])
def test_a_command_is_exact_only_when_it_ran_and_succeeded(command, level):
    assert _push_level(command) == level


def test_a_commit_before_a_semicolon_ran_but_may_have_failed():
    result = _verify("I committed the fix.", _real_bash("git add -A; git commit -m x; git push origin main"))
    assert result.claims[0].evidence == "plausible"


@pytest.mark.parametrize("claim, command", [
    ("I deployed the fix to prod.", "./deploy.sh production"),
    ("I pushed the fix.", "./push.sh"),
    ("I sent the email.", "python3 scripts/send_digest.py"),
    ("I pushed the fix.", "cd repo && ./push.sh"),
])
def test_a_failed_opaque_run_is_not_evidence(claim, command):
    assert not _verify(claim, _real_bash(command, exit_code=1)).verified
    assert _verify(claim, _real_bash(command)).verified  # and counts when it exits 0


@pytest.mark.parametrize("text", [
    "I sent the email to alice@x.io.",
    "I emailed Alice@X.io about it.",
    "I sent the report to alice@x.io and bob@x.io.",
    "I sent alice@x.io the summary.",
])
def test_first_person_email_claims_capture_the_recipient(text):
    assert _kinds(text) == [("email", "alice@x.io")]
    wrong = Evidence("send_email", {"to": "['bob@x.io']", "subject": "s"})
    assert not _verify(text, wrong).verified
    right = Evidence("send_email", {"to": "['alice@x.io', 'bob@x.io']", "subject": "s"})
    assert _verify(text, right).claims[0].evidence == "exact"
