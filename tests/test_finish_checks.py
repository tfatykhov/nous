"""F113 fixed finish checks: the two pure checks, the verdict and the record (no database).

Evidence is built the way the runner builds it: calls recorded on a real
``ExecutionLedger`` and read back with ``ledger_evidence``, so a bash
command's programs, side-effect class and exit code come from the real
ledger. File existence is a fake ``exists`` so the tests do not depend on
the platform.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

from nous.cognitive import finish_checks as fc
from nous.cognitive.claim_verifier import Evidence, ledger_evidence
from nous.cognitive.execution_ledger import EVIDENCE_TRUNCATED, ExecutionLedger

WS = "/ws"


def _evidence(*calls: tuple) -> list[Evidence]:
    """Each call is ``(tool, input[, status[, exit code]])``; a bash result ends with the bash tool's exit trailer."""
    ledger = ExecutionLedger("subtask-test")
    for call in calls:
        tool, tool_input, status, exit_code = (*call, "success", 0, 0)[:4] if len(call) == 2 else (*call, 0)[:4]
        ledger.record(tool, tool_input, f"done\nExit code: {exit_code}" if tool == "bash" else "done", status)
    return ledger_evidence(ledger.actions)


def _bash(command: str, status: str = "success", exit_code: int = 0) -> tuple:
    return ("bash", {"command": command}, status, exit_code)


def _missing(*missing: str):
    """An ``exists`` where only the named paths are missing; records what it was asked."""
    asked: list[str] = []

    def exists(path: str) -> bool | None:
        asked.append(path)
        return path not in missing

    exists.asked = asked  # type: ignore[attr-defined]
    return exists


WRITE = ("write_file", {"path": "/ws/reports/summary.md", "content": "x"})
READ = ("web_search", {"query": "prices"})
FAILED_WRITE = ("write_file", {"path": "/ws/a.md"}, "error")


def _named(text: str, *missing: str, evidence: list[Evidence] | None = None):
    exists = _missing(*missing)
    return fc.named_file_missing(text, _evidence(WRITE) if evidence is None else evidence, WS, exists), exists


# ---------------------------------------------------------------------------
# named_file_missing
# ---------------------------------------------------------------------------


def test_a_missing_absolute_file_the_report_says_was_created_is_a_finding():
    found, exists = _named("I created /ws/reports/summary.md with the table.", "/ws/reports/summary.md")
    assert found == 1
    assert exists.asked == ["/ws/reports/summary.md"]


def test_a_backticked_path_is_read():
    assert _named("Saved the table to `/ws/reports/summary.md`.", "/ws/reports/summary.md")[0] == 1


def test_a_file_that_exists_is_no_finding():
    assert _named("I created /ws/reports/summary.md.")[0] == 0


def test_an_unknown_existence_is_no_finding():
    assert fc.named_file_missing("I created /ws/reports/summary.md.", _evidence(WRITE), WS, lambda _p: None) == 0


@pytest.mark.parametrize(
    "call",
    [
        _bash("rm /ws/tmp/old.txt"),
        _bash("/bin/rm -f /ws/tmp/old.txt"),
        _bash("sudo rm /ws/tmp/old.txt"),
        _bash("find /ws/tmp -name '*.log' | xargs rm"),
        _bash("bash -c 'rm /ws/tmp/old.txt'"),
        _bash("bash cleanup.sh"),
        _bash("mv /ws/a.md /ws/b.md"),
        _bash("git status"),
        _bash("git checkout -- ."),
        _bash("tar -czf /ws/out.tgz /ws/reports"),
        _bash("rsync -a /ws/a/ /ws/b/"),
        _bash("docker compose down"),
        _bash("./runner.sh launch job-1"),
        _bash("/ws/claude-jobs/runner.sh launch job-1"),
        _bash("cd /ws/claude-jobs && ./runner.sh status job-1"),
        _bash('eval "$CMD"'),
        _bash('echo "`rm /ws/a`"'),
        _bash("rm /ws/tmp/old.txt", "error"),
        ("run_python", {"code": "print(1)"}),
    ],
    ids=lambda c: c[0] + ":" + str(c[1])[:40],
)
def test_a_call_that_could_have_removed_the_file_skips_the_check(call):
    found, exists = _named(
        "I created /ws/reports/summary.md.", "/ws/reports/summary.md", evidence=_evidence(WRITE, call)
    )
    assert found is None
    assert exists.asked == []  # decided before any file is looked at


def test_the_check_is_skipped_when_no_path_is_named_too():
    """Skipped says the session had a call that may have removed files, whatever the report names."""
    found, _exists = _named("Compared the three providers.", evidence=_evidence(_bash("git log -1")))
    assert found is None


def test_an_unreadable_bash_command_skips_the_check():
    unreadable = Evidence("bash", {"command": "echo 'unclosed"}, 0, "write", None)
    assert unreadable.runs is None
    found, _exists = _named(
        "I created /ws/reports/summary.md.", "/ws/reports/summary.md", evidence=[*_evidence(WRITE), unreadable]
    )
    assert found is None


def test_a_cut_bash_command_skips_the_check():
    cut = Evidence("bash", {"command": f"ls /ws{EVIDENCE_TRUNCATED}ls /ws"}, 0, "none", (("ls", ("/ws",), True),))
    found, _exists = _named(
        "I created /ws/reports/summary.md.", "/ws/reports/summary.md", evidence=[*_evidence(WRITE), cut]
    )
    assert found is None


@pytest.mark.parametrize("command", ["ls /ws", "cat /ws/reports/summary.md", "echo hi > /ws/out.md", "python3 x.py"])
def test_other_shell_commands_do_not_skip_the_check(command):
    found, _exists = _named(
        "I created /ws/reports/summary.md.", "/ws/reports/summary.md", evidence=_evidence(WRITE, _bash(command))
    )
    assert found == 1


def test_a_relative_path_without_a_matching_write_is_not_checked():
    found, exists = _named("I created reports/summary.md.", "/ws/reports/summary.md", evidence=_evidence(READ))
    assert found == 0
    assert exists.asked == []


def test_a_relative_path_equal_to_a_write_file_path_is_resolved_against_the_workspace():
    write = ("write_file", {"path": "reports/./summary.md", "content": "x"})
    found, exists = _named("I wrote reports/summary.md.", "/ws/reports/summary.md", evidence=_evidence(write))
    assert exists.asked == ["/ws/reports/summary.md"]
    assert found == 1


def test_a_write_file_path_given_as_file_path_counts_too():
    write = ("write_file", {"file_path": "out/data.csv", "content": "x"})
    assert _named("Exported out/data.csv.", "/ws/out/data.csv", evidence=_evidence(write))[0] == 1


@pytest.mark.parametrize(
    "text",
    [
        "I created /etc/nous/summary.md.",  # outside the workspace
        "I created /ws/../etc/summary.md.",  # leaves it
        "I created /ws-other/summary.md.",  # a prefix that is not on a segment boundary
        "I created https://example.com/a/summary.md.",
        "I created ~/reports/summary.md.",
        "I created /ws/reports/*.md.",
        "I created /ws/reports/summary-$DATE.md.",
        "I created summary.md.",  # a bare name
        "I created /ws/reports/summary.",  # no extension
        "I created /ws/reports/archive.verylongext.",  # an extension longer than 8
    ],
)
def test_paths_that_are_not_checkable_are_skipped(text):
    assert fc.named_file_missing(text, _evidence(WRITE), WS, lambda _p: False) == 0
    found, exists = _named(text)
    assert found == 0
    assert exists.asked == []


@pytest.mark.parametrize(
    "text",
    [
        "I will create /ws/reports/summary.md next.",
        "The file was not saved to /ws/reports/summary.md.",
        "It should be written to /ws/reports/summary.md.",
        "The table wasn't saved to /ws/reports/summary.md.",
        "No file was created at /ws/reports/summary.md.",
        "If needed, the data is exported to /ws/reports/summary.md.",
        "I created /ws/reports/summary.md and then deleted it.",
        "The draft was saved to /ws/reports/summary.md, then moved.",
        "A file may be generated at /ws/reports/summary.md.",
        # sentences that report or describe, not claim
        "I checked whether /ws/reports/summary.md was created; it is missing.",
        "The step that saved /ws/reports/summary.md failed.",
        "Expected /ws/reports/summary.md to have been generated by the job, but found nothing there.",
        "The file /ws/reports/summary.md was supposed to be written by the job.",
        "The summary /ws/reports/summary.md created by the export is absent.",
        "Writing /ws/reports/summary.md gave an error, so nothing was saved.",
        "The page /ws/reports/summary.md is generated by running make docs.",
        "The cache /ws/reports/summary.md is created on first run.",
        "Results are written to /ws/reports/summary.md each night.",
        "The file /ws/reports/summary.md is automatically generated when the job runs.",
    ],
)
def test_hedged_or_descriptive_sentences_are_not_claims(text):
    found, exists = _named(text, "/ws/reports/summary.md")
    assert found == 0
    assert exists.asked == []


@pytest.mark.parametrize(
    "text",
    [
        "The report was generated at /ws/reports/summary.md.",
        "I have saved /ws/reports/summary.md for the owner.",
        "/ws/reports/summary.md has been written.",
        "Created /ws/reports/summary.md; it is generated from the three sources.",
    ],
)
def test_past_and_perfect_forms_are_claims(text):
    assert _named(text, "/ws/reports/summary.md")[0] == 1


def test_a_fenced_code_block_is_not_read():
    text = "Steps I ran:\n```bash\n# wrote /ws/reports/summary.md\ncp a b\n```\nThe owner can rerun them."
    found, exists = _named(text, "/ws/reports/summary.md")
    assert found == 0
    assert exists.asked == []


def test_an_unclosed_fence_hides_the_rest():
    found, _exists = _named("Notes:\n~~~\nsaved /ws/reports/summary.md", "/ws/reports/summary.md")
    assert found == 0


def test_a_claim_after_a_closed_fence_is_read():
    text = "```\nls /ws\n```\nI saved /ws/reports/summary.md."
    assert _named(text, "/ws/reports/summary.md")[0] == 1


@pytest.mark.parametrize("verb", ["updated", "added", "modified", "edited", "appended"])
def test_other_verbs_are_not_write_claims(verb):
    assert _named(f"I {verb} /ws/reports/summary.md.", "/ws/reports/summary.md")[0] == 0


def test_words_inside_a_path_do_not_hedge_or_claim():
    assert _named("I created /ws/notes/if-missing/summary.md.", "/ws/notes/if-missing/summary.md")[0] == 1
    assert _named("I read /ws/created/a.md.", "/ws/created/a.md")[0] == 0


def test_hedge_words_match_whole_words_only():
    text = "I created /ws/reports/summary.md for the notes, the iffy cases, the notable ones and the nowhere-near list."
    assert _named(text, "/ws/reports/summary.md")[0] == 1


def test_a_hedge_in_another_sentence_does_not_hide_a_claim():
    text = "I could not reach the second site. I created /ws/reports/summary.md from the first."
    assert _named(text, "/ws/reports/summary.md")[0] == 1


def test_sentences_split_on_newlines_and_bullets():
    found, exists = _named("Plan: I will create /ws/a.md later\n- Saved /ws/b.md \u2022 next", "/ws/a.md", "/ws/b.md")
    assert exists.asked == ["/ws/b.md"]
    assert found == 1


@pytest.mark.parametrize("wrapped", ["/ws/r.md.", "/ws/r.md,", "/ws/r.md;", "/ws/r.md:", "(/ws/r.md)", '"/ws/r.md"'])
def test_punctuation_around_a_path_is_stripped(wrapped):
    found, exists = _named(f"Saved {wrapped} for review", "/ws/r.md")
    assert exists.asked == ["/ws/r.md"]
    assert found == 1


def test_only_the_first_ten_paths_are_examined():
    paths = [f"/ws/out/file{i:02d}.md" for i in range(12)]
    found, exists = _named("I created " + ", ".join(paths) + ".", *paths)
    assert exists.asked == paths[:10]
    assert found == 10


def test_a_path_named_twice_is_examined_once():
    found, exists = _named("I created /ws/a.md. I saved /ws/a.md again.", "/ws/a.md")
    assert exists.asked == ["/ws/a.md"]
    assert found == 1


def test_a_file_a_shell_command_wrote_below_a_repository_is_not_checked():
    """bash runs in the workspace root, and a report names a repository's file relative to the repository."""
    evidence = _evidence(_bash("cd /ws/repo && echo '<html>' > out/report.html"))
    found, exists = _named(
        "I generated out/report.html with the build script.",
        "/ws/out/report.html",
        "/ws/repo/out/report.html",
        evidence=evidence,
    )
    assert found == 0
    assert exists.asked == []


def test_an_absolute_file_a_shell_command_wrote_is_found():
    asked: list[str] = []

    def exists(path: str) -> bool:
        asked.append(path)
        return path == "/ws/out/report.html"

    text = "I generated /ws/out/report.html with the build script."
    evidence = _evidence(_bash("echo '<html>' > /ws/out/report.html"))
    assert fc.named_file_missing(text, evidence, WS, exists) == 0
    assert asked == ["/ws/out/report.html"]


# ---------------------------------------------------------------------------
# effects_all_failed
# ---------------------------------------------------------------------------

SILENT = "Done. Everything is in place and the summary is ready for the owner."


def test_one_failed_write_with_a_silent_report_is_a_finding():
    assert fc.effects_all_failed(SILENT, _evidence(READ, FAILED_WRITE)) == 1


def test_failed_writes_and_sends_are_counted():
    evidence = _evidence(
        FAILED_WRITE,
        ("send_email", {"to": "a@example.com", "subject": "s"}, "blocked"),
        ("send_file", {"file_path": "/ws/a.md"}, "error"),
        _bash("rm /ws/x", "timeout"),
    )
    assert fc.effects_all_failed(SILENT, evidence) == 4


@pytest.mark.parametrize(
    "words",
    [
        "failed",
        "unable",
        "could not",
        "couldn't",
        "couldn\u2019t",
        "cannot",
        "can't",
        "can not",
        "blocked",
        "refused",
        "rejected",
        "denied",
        "deny",
        "not allowed",
        "not permitted",
        "permission",
        "approval",
        "approve",
        "error",
        "did not",
        "didn't",
        "was not",
        "wasn't",
        "not able",
        "unsuccessful",
        "timed out",
        "timeout",
        "no access",
    ],
)
def test_a_report_that_mentions_the_failure_is_no_finding(words):
    text = f"The summary is ready. The write {words.upper() if words == 'error' else words} along the way."
    assert fc.effects_all_failed(text, _evidence(FAILED_WRITE)) == 0


@pytest.mark.parametrize(
    "text",
    [
        "All done with no errors.",
        "Finished without errors.",
        "Finished without any errors.",
        "The run ended with 0 errors.",
        "Zero errors were reported.",
        "No failures. Everything is in place.",
        "Done, 12 passed and 0 failed.",
    ],
)
def test_a_report_that_denies_a_failure_does_not_mention_one(text):
    assert fc.effects_all_failed(text, _evidence(FAILED_WRITE)) == 1


def test_a_report_that_counts_errors_mentions_them():
    assert fc.effects_all_failed("Finished with 2 errors.", _evidence(FAILED_WRITE)) == 0


def test_a_later_successful_effect_call_is_no_finding():
    assert fc.effects_all_failed(SILENT, _evidence(FAILED_WRITE, ("write_file", {"path": "/ws/a.md"}))) == 0


def test_memory_tools_are_not_effect_calls():
    evidence = _evidence(READ, ("learn_fact", {"content": "x"}), FAILED_WRITE)
    assert fc.effects_all_failed(SILENT, evidence) == 1


def test_no_effect_call_at_all_is_no_finding():
    assert fc.effects_all_failed(SILENT, _evidence(READ, ("learn_fact", {"content": "x"}, "error"))) == 0


@pytest.mark.parametrize(
    "call",
    [
        _bash("ls /ws", "error"),
        _bash("curl -s https://example.com/data.json", "error"),
        _bash("curl -s https://example.com/data.json", "success", 6),
        _bash("git fetch origin", "error"),
        ("run_python", {"code": "import json\nprint(json.load(open('/ws/a.json')))"}, "error"),
        ("run_python", {"code": "import httpx\nhttpx.get('https://example.com')"}, "error"),
        ("mcp__desktop__screenshot", {"x": 1}, "error"),
        ("mcp__desktop__click", {"x": 1}, "error"),
    ],
    ids=lambda c: c[0] + ":" + str(c[1])[:40],
)
def test_reads_and_calls_that_cannot_be_told_from_reads_are_not_effect_calls(call):
    assert fc.effects_all_failed(SILENT, _evidence(call)) == 0


@pytest.mark.parametrize("command", ["cp /ws/a.md /ws/b.md", "mkdir -p /ws/out", "echo x > /ws/out.md"])
def test_a_shell_command_that_writes_is_an_effect_call(command):
    evidence = _evidence(_bash(command, "error"))
    assert evidence[0].side_effect == "write"
    assert fc.effects_all_failed(SILENT, evidence) == 1


def test_a_shell_command_with_a_non_zero_exit_failed():
    evidence = _evidence(_bash("cp /ws/a.md /ws/b.md", "success", 1))
    assert evidence[0].ok is True and evidence[0].exit_code == 1
    assert fc.effects_all_failed(SILENT, evidence) == 1


def test_a_shell_command_with_a_zero_exit_succeeded():
    evidence = _evidence(_bash("cp /ws/a.md /ws/b.md", "success", 0), FAILED_WRITE)
    assert fc.effects_all_failed(SILENT, evidence) == 0


def test_a_shell_success_after_a_non_zero_exit_is_no_finding():
    evidence = _evidence(_bash("cp /ws/a.md /ws/b.md", "success", 1), _bash("cp /ws/a.md /ws/c.md"))
    assert fc.effects_all_failed(SILENT, evidence) == 0


# ---------------------------------------------------------------------------
# The tasks that must come out clean
# ---------------------------------------------------------------------------


def test_a_read_only_research_task_is_clean():
    text = "Compared three providers. The cheapest is B.\nSources read: /ws/notes/providers.md and two web pages."
    evidence = _evidence(READ, ("web_fetch", {"url": "https://example.com"}), _bash("cat /ws/notes/providers.md"))
    assert fc.evaluate(text, evidence, WS, lambda _p: False) == fc.FinishVerdict("clean")


def test_a_brief_that_asked_for_no_change_is_clean():
    text = "As asked, nothing was changed and no file was created. The config at /ws/app/config.yaml looks right."
    evidence = _evidence(_bash("cat /ws/app/config.yaml"), READ)
    assert fc.evaluate(text, evidence, WS, lambda _p: False) == fc.FinishVerdict("clean")


def test_a_research_task_whose_fetches_failed_is_clean():
    text = "Compared three providers from their public price pages. The cheapest is B."
    evidence = _evidence(
        _bash("curl -s https://example.com/prices", "success", 22), ("mcp__web__fetch", {"u": 1}, "error")
    )
    assert fc.evaluate(text, evidence, WS, lambda _p: False) == fc.FinishVerdict("clean")


# ---------------------------------------------------------------------------
# evaluate
# ---------------------------------------------------------------------------


def test_no_findings_is_clean():
    assert fc.evaluate("I created /ws/reports/summary.md.", _evidence(WRITE), WS, _missing()) == fc.FinishVerdict(
        "clean"
    )


def test_findings_are_flagged_with_check_names_in_a_fixed_order():
    evidence = _evidence(FAILED_WRITE, ("write_file", {"path": "/ws/b.md"}, "error"))
    verdict = fc.evaluate("I created /ws/a.md. I saved /ws/b.md.", evidence, WS, _missing("/ws/a.md", "/ws/b.md"))
    assert verdict == fc.FinishVerdict("flagged", failed=("named_file_missing", "effects_all_failed"))


def test_a_skipped_file_check_is_named():
    evidence = _evidence(WRITE, ("run_python", {"code": "print(1)"}))
    verdict = fc.evaluate("I created /ws/reports/summary.md.", evidence, WS, _missing("/ws/reports/summary.md"))
    assert verdict == fc.FinishVerdict("clean", skipped=("named_file_missing",))


def test_a_skipped_file_check_and_a_flag_from_the_other():
    evidence = _evidence(FAILED_WRITE, _bash("git status"))
    verdict = fc.evaluate(SILENT, evidence, WS, _missing())
    assert verdict == fc.FinishVerdict("flagged", failed=("effects_all_failed",), skipped=("named_file_missing",))


@pytest.mark.parametrize(
    "text",
    [
        "\u0413\u043e\u0442\u043e\u0432\u043e. "
        "\u0424\u0430\u0439\u043b \u0441\u043e\u0445\u0440\u0430\u043d\u0451\u043d.",
        "\u0413\u043e\u0442\u043e\u0432\u043e: \u0444\u0430\u0439\u043b /ws/out/report.md "
        "\u0441\u043e\u0445\u0440\u0430\u043d\u0451\u043d, PR #12 \u043e\u0442\u043a\u0440\u044b\u0442.",
        "\u5b8c\u6210\u3002`/ws/out/report.md`",
        "\u0413\u043e\u0442\u043e\u0432\u043e: /ws/reports/quarterly-summary-for-the-owner.md",
        "1234 / 5678 = 0.217",
    ],
)
def test_a_report_not_written_in_english_letters_is_not_checked(text):
    verdict = fc.evaluate(text, _evidence(FAILED_WRITE), WS, _missing("/ws/out/report.md"))
    assert verdict == fc.FinishVerdict("not_checked", why="non_ascii_text")


def test_an_english_report_with_a_foreign_name_is_checked():
    text = "Done. The summary for \u0418\u0432\u0430\u043d is ready for the owner."
    assert fc.evaluate(text, _evidence(FAILED_WRITE), WS, _missing()).failed == ("effects_all_failed",)


def test_evaluate_reads_the_first_4000_characters():
    text = "x" * 4000 + " I created /ws/a.md."
    assert fc.evaluate(text, _evidence(WRITE), WS, _missing("/ws/a.md")) == fc.FinishVerdict("clean")


def _filled_to_the_cut(tail: str, cut_after: str) -> str:
    """Whole filler sentences, then ``tail``, so that the first ``MAX_TEXT_CHARS`` end just after ``cut_after``."""
    keep = tail[: tail.index(cut_after) + len(cut_after)]
    room = fc.MAX_TEXT_CHARS - len(keep)
    filler = ("Filler sentence. " * (room // 17 + 1))[:room]
    filler = filler[: filler.rindex(". ") + 2].ljust(room)
    text = filler + tail
    assert text[: fc.MAX_TEXT_CHARS].endswith(cut_after)
    return text


def test_the_cut_does_not_shorten_a_path_into_another():
    text = _filled_to_the_cut("I created /ws/reports/summary.md for the owner.", "/ws/reports/summary.m")
    exists = _missing("/ws/reports/summary.m", "/ws/reports/summary.md")
    assert fc.evaluate(text, _evidence(WRITE), WS, exists) == fc.FinishVerdict("clean")
    assert exists.asked == []


def test_the_cut_does_not_drop_a_hedge_from_a_sentence():
    text = _filled_to_the_cut("I created /ws/a.md and then deleted it again.", "/ws/a.md and then ")
    assert fc.evaluate(text, _evidence(WRITE), WS, _missing("/ws/a.md")) == fc.FinishVerdict("clean")


def test_a_whole_sentence_before_the_cut_is_still_read():
    text = _filled_to_the_cut("I created /ws/a.md. More words follow here.", "/ws/a.md. More")
    assert fc.evaluate(text, _evidence(WRITE), WS, _missing("/ws/a.md")).failed == ("named_file_missing",)


def test_a_check_that_raises_gives_not_checked(monkeypatch, caplog):
    def boom(*_args):
        raise RuntimeError("secret text from the report")

    monkeypatch.setattr(fc, "effects_all_failed", boom)
    with caplog.at_level(logging.WARNING):
        verdict = fc.evaluate(SILENT, _evidence(WRITE), WS, _missing())
    assert verdict == fc.FinishVerdict("not_checked", why="check_error")
    assert "RuntimeError" in caplog.text
    assert "secret text" not in caplog.text


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------


def test_the_record_has_the_agreed_shape():
    verdict = fc.FinishVerdict("flagged", failed=("effects_all_failed",), skipped=("named_file_missing",))
    assert fc.record_of(verdict, "shadow") == {
        "v": 1,
        "mode": "shadow",
        "verdict": "flagged",
        "failed": ["effects_all_failed"],
        "skipped": ["named_file_missing"],
        "why": None,
    }
    assert fc.record_of(fc.FinishVerdict("not_checked", why="no_calls"), "shadow") == {
        "v": 1,
        "mode": "shadow",
        "verdict": "not_checked",
        "failed": [],
        "skipped": [],
        "why": "no_calls",
    }


def test_the_record_carries_no_text_of_the_report_or_the_calls():
    markers = ["ZEBRA-MARKER", "/ws/zebra-dir/ZEBRA-FILE.md", "QUAGGA-MARKER"]
    text = f"{markers[0]}: I created {markers[1]}. {markers[2]}."
    evidence = _evidence(("write_file", {"path": markers[1]}, "error"))
    verdict = fc.evaluate(text, evidence, WS, _missing(markers[1]))
    assert verdict.failed == ("named_file_missing", "effects_all_failed")
    dumped = json.dumps(fc.record_of(verdict, "shadow"))
    assert "/" not in dumped
    for marker in [*markers, "ZEBRA", "zebra", "QUAGGA"]:
        assert marker not in dumped


def test_a_verdict_holds_labels_only():
    assert [f.name for f in fc.FinishVerdict.__dataclass_fields__.values()] == ["verdict", "failed", "skipped", "why"]


# ---------------------------------------------------------------------------
# fs_exists and the mode
# ---------------------------------------------------------------------------


def test_fs_exists(tmp_path):
    present = tmp_path / "a.md"
    present.write_text("x", encoding="utf-8")
    assert fc.fs_exists(str(present)) is True
    assert fc.fs_exists(str(tmp_path / "missing.md")) is False
    assert fc.fs_exists(str(present / "below-a-file.md")) is False
    assert fc.fs_exists("/ws/a\0b.md") is None


def test_fs_exists_reports_a_dangling_link_as_present(tmp_path):
    link = tmp_path / "link.md"
    try:
        link.symlink_to(tmp_path / "nowhere.md")
    except OSError:
        pytest.skip("this platform cannot create a symbolic link here")
    assert fc.fs_exists(str(link)) is True


def test_the_mode_of_settings_without_the_field_is_off():
    assert fc.finish_checks_mode_of(SimpleNamespace()) == "off"
    assert fc.finish_checks_mode_of(SimpleNamespace(finish_checks_mode="shadow")) == "shadow"


# ---------------------------------------------------------------------------
# The counts a dashboard block reads
# ---------------------------------------------------------------------------


def _record(verdict, failed=(), skipped=(), why=None):
    return fc.record_of(fc.FinishVerdict(verdict, failed=tuple(failed), skipped=tuple(skipped), why=why), "shadow")


def test_counts_of_records_per_row_and_per_check():
    records = [
        _record("clean"),
        _record("clean", skipped=["named_file_missing"]),
        _record("flagged", failed=["effects_all_failed"], skipped=["named_file_missing"]),
        _record("flagged", failed=["named_file_missing"]),
        _record("flagged", failed=["named_file_missing", "effects_all_failed"]),
        _record("not_checked", why="no_calls"),
        _record("not_checked", why="non_ascii_text"),
        None,  # a row written before the checks existed
        "not a record",
        {"verdict": "something else"},
    ]
    assert fc.finish_check_counts(records, "shadow") == {
        "checked": 5,
        "flagged": 3,
        "not_checked": 2,
        "mode": "shadow",
        "by_check": {
            "named_file_missing": {"checked": 3, "flagged": 2},
            "effects_all_failed": {"checked": 5, "flagged": 2},
        },
    }


def test_counts_of_no_records():
    assert fc.finish_check_counts([], "off") == {
        "checked": 0,
        "flagged": 0,
        "not_checked": 0,
        "mode": "off",
        "by_check": {
            "named_file_missing": {"checked": 0, "flagged": 0},
            "effects_all_failed": {"checked": 0, "flagged": 0},
        },
    }
