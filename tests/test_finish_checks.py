"""F113 fixed finish checks: the two pure checks, the verdict and the record (no database).

Evidence is built the way the runner builds it: calls recorded on a real
``ExecutionLedger`` and read back with ``ledger_evidence``, so a bash
command's programs come from the real command parser. File existence is a
fake ``exists`` so the tests do not depend on the platform.
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
    """Each call is ``(tool, input)`` or ``(tool, input, status)``; bash results carry an exit 0 trailer."""
    ledger = ExecutionLedger("subtask-test")
    for call in calls:
        tool, tool_input, status = (*call, "success") if len(call) == 2 else call
        ledger.record(tool, tool_input, "done\nExit code: 0" if tool == "bash" else "done", status)
    return ledger_evidence(ledger.actions)


def _bash(command: str, status: str = "success") -> tuple:
    return ("bash", {"command": command}, status)


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


# ---------------------------------------------------------------------------
# named_file_missing
# ---------------------------------------------------------------------------


def test_a_missing_absolute_file_the_report_says_was_created_is_a_finding():
    exists = _missing("/ws/reports/summary.md")
    found = fc.named_file_missing("I created /ws/reports/summary.md with the table.", _evidence(WRITE), WS, exists)
    assert found == [
        fc.Finding("named_file_missing", "the report says /ws/reports/summary.md was written; it does not exist")
    ]


def test_a_backticked_path_is_read():
    exists = _missing("/ws/reports/summary.md")
    found = fc.named_file_missing("Saved the table to `/ws/reports/summary.md`.", _evidence(WRITE), WS, exists)
    assert [f.check for f in found] == ["named_file_missing"]


def test_a_file_that_exists_is_no_finding():
    assert fc.named_file_missing("I created /ws/reports/summary.md.", _evidence(WRITE), WS, _missing()) == []


def test_an_unknown_existence_is_no_finding():
    assert fc.named_file_missing("I created /ws/reports/summary.md.", _evidence(WRITE), WS, lambda _p: None) == []


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
def test_a_call_that_could_have_removed_the_file_silences_the_check(call):
    exists = _missing("/ws/reports/summary.md")
    found = fc.named_file_missing("I created /ws/reports/summary.md.", _evidence(WRITE, call), WS, exists)
    assert found == []
    assert exists.asked == []  # decided before any file is looked at


def test_an_unreadable_bash_command_silences_the_check():
    unreadable = Evidence("bash", {"command": "echo 'unclosed"}, 0, "write", None)
    assert unreadable.runs is None
    found = fc.named_file_missing(
        "I created /ws/reports/summary.md.", [*_evidence(WRITE), unreadable], WS, _missing("/ws/reports/summary.md")
    )
    assert found == []


def test_a_cut_bash_command_silences_the_check():
    cut = Evidence("bash", {"command": f"ls /ws{EVIDENCE_TRUNCATED}ls /ws"}, 0, "none", (("ls", ("/ws",), True),))
    found = fc.named_file_missing(
        "I created /ws/reports/summary.md.", [*_evidence(WRITE), cut], WS, _missing("/ws/reports/summary.md")
    )
    assert found == []


@pytest.mark.parametrize("command", ["ls /ws", "cat /ws/reports/summary.md", "echo hi > /ws/out.md", "python3 x.py"])
def test_other_shell_commands_do_not_silence_the_check(command):
    exists = _missing("/ws/reports/summary.md")
    found = fc.named_file_missing("I created /ws/reports/summary.md.", _evidence(WRITE, _bash(command)), WS, exists)
    assert [f.check for f in found] == ["named_file_missing"]


def test_a_relative_path_without_a_matching_write_is_not_checked():
    exists = _missing("/ws/reports/summary.md")
    assert fc.named_file_missing("I created reports/summary.md.", _evidence(READ), WS, exists) == []
    assert exists.asked == []


def test_a_relative_path_equal_to_a_write_file_path_is_resolved_against_the_workspace():
    write = ("write_file", {"path": "reports/./summary.md", "content": "x"})
    exists = _missing("/ws/reports/summary.md")
    found = fc.named_file_missing("I wrote reports/summary.md.", _evidence(write), WS, exists)
    assert exists.asked == ["/ws/reports/summary.md"]
    assert [f.detail for f in found] == ["the report says reports/summary.md was written; it does not exist"]


def test_a_write_file_path_given_as_file_path_counts_too():
    write = ("write_file", {"file_path": "out/data.csv", "content": "x"})
    found = fc.named_file_missing("Exported out/data.csv.", _evidence(write), WS, _missing("/ws/out/data.csv"))
    assert [f.check for f in found] == ["named_file_missing"]


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
    exists = _missing()
    exists_all_missing = lambda _p: False  # noqa: E731
    assert fc.named_file_missing(text, _evidence(WRITE), WS, exists_all_missing) == []
    assert fc.named_file_missing(text, _evidence(WRITE), WS, exists) == []
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
    ],
)
def test_hedged_sentences_are_skipped(text):
    exists = _missing("/ws/reports/summary.md")
    assert fc.named_file_missing(text, _evidence(WRITE), WS, exists) == []


@pytest.mark.parametrize("verb", ["updated", "added", "modified", "edited", "appended"])
def test_other_verbs_are_not_write_claims(verb):
    exists = _missing("/ws/reports/summary.md")
    assert fc.named_file_missing(f"I {verb} /ws/reports/summary.md.", _evidence(WRITE), WS, exists) == []


def test_words_inside_a_path_do_not_hedge_or_claim():
    exists = _missing("/ws/notes/if-needed/summary.md", "/ws/created/a.md")
    found = fc.named_file_missing("I created /ws/notes/if-needed/summary.md.", _evidence(WRITE), WS, exists)
    assert [f.check for f in found] == ["named_file_missing"]
    assert fc.named_file_missing("I read /ws/created/a.md.", _evidence(WRITE), WS, exists) == []


def test_hedge_words_match_whole_words_only():
    exists = _missing("/ws/reports/summary.md")
    text = "Nothing else: I created /ws/reports/summary.md for the notes and the iffy cases."
    assert [f.check for f in fc.named_file_missing(text, _evidence(WRITE), WS, exists)] == ["named_file_missing"]


def test_a_hedge_in_another_sentence_does_not_hide_a_claim():
    exists = _missing("/ws/reports/summary.md")
    text = "I could not reach the second site. I created /ws/reports/summary.md from the first."
    assert [f.check for f in fc.named_file_missing(text, _evidence(WRITE), WS, exists)] == ["named_file_missing"]


def test_sentences_split_on_newlines_and_bullets():
    exists = _missing("/ws/b.md")
    text = "Plan: I will create /ws/a.md later\n- Saved /ws/b.md"
    found = fc.named_file_missing(text, _evidence(WRITE), WS, exists)
    assert [f.detail for f in found] == ["the report says /ws/b.md was written; it does not exist"]


@pytest.mark.parametrize("wrapped", ["/ws/r.md.", "/ws/r.md,", "/ws/r.md;", "/ws/r.md:", "(/ws/r.md)", '"/ws/r.md"'])
def test_punctuation_around_a_path_is_stripped(wrapped):
    exists = _missing("/ws/r.md")
    found = fc.named_file_missing(f"Saved {wrapped} for review", _evidence(WRITE), WS, exists)
    assert exists.asked == ["/ws/r.md"]
    assert [f.check for f in found] == ["named_file_missing"]


def test_only_the_first_ten_paths_are_examined():
    paths = [f"/ws/out/file{i:02d}.md" for i in range(12)]
    exists = _missing(*paths)
    found = fc.named_file_missing("I created " + ", ".join(paths) + ".", _evidence(WRITE), WS, exists)
    assert exists.asked == paths[:10]
    assert len(found) == 10


def test_a_path_named_twice_is_examined_once():
    exists = _missing("/ws/a.md")
    found = fc.named_file_missing("I created /ws/a.md. I saved /ws/a.md again.", _evidence(WRITE), WS, exists)
    assert exists.asked == ["/ws/a.md"]
    assert len(found) == 1


def test_a_file_written_by_a_shell_command_and_present_passes():
    text = "I generated /ws/out/report.html with the build script."
    evidence = _evidence(_bash("echo '<html>' > /ws/out/report.html"))
    assert fc.named_file_missing(text, evidence, WS, _missing()) == []


def test_the_detail_is_at_most_200_characters():
    path = "/ws/" + "d" * 300 + "/a.md"
    found = fc.named_file_missing(f"I created {path}.", _evidence(WRITE), WS, _missing(path))
    assert len(found) == 1 and len(found[0].detail) <= 200


# ---------------------------------------------------------------------------
# effects_all_failed
# ---------------------------------------------------------------------------

SILENT = "Done. Everything is in place and the summary is ready for the owner."


def test_one_failed_write_with_a_silent_report_is_a_finding():
    found = fc.effects_all_failed(SILENT, _evidence(READ, ("write_file", {"path": "/ws/a.md"}, "error")))
    assert found == [
        fc.Finding(
            "effects_all_failed",
            "1 attempts to change something all failed or were refused; the report does not say so",
        )
    ]


def test_three_failed_calls_are_counted():
    evidence = _evidence(
        ("write_file", {"path": "/ws/a.md"}, "error"),
        ("send_email", {"to": "a@example.com", "subject": "s"}, "blocked"),
        _bash("rm /ws/x", "timeout"),
    )
    [finding] = fc.effects_all_failed(SILENT, evidence)
    assert finding.detail.startswith("3 attempts ")


@pytest.mark.parametrize(
    "words",
    [
        "failed",
        "unable",
        "could not",
        "couldn't",
        "couldn’t",
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
    assert fc.effects_all_failed(text, _evidence(("write_file", {"path": "/ws/a.md"}, "error"))) == []


def test_a_later_successful_effect_call_is_no_finding():
    evidence = _evidence(("write_file", {"path": "/ws/a.md"}, "error"), ("write_file", {"path": "/ws/a.md"}))
    assert fc.effects_all_failed(SILENT, evidence) == []


def test_memory_tools_are_not_effect_calls():
    evidence = _evidence(READ, ("learn_fact", {"content": "x"}), ("write_file", {"path": "/ws/a.md"}, "error"))
    assert [f.check for f in fc.effects_all_failed(SILENT, evidence)] == ["effects_all_failed"]


def test_no_effect_call_at_all_is_no_finding():
    assert fc.effects_all_failed(SILENT, _evidence(READ, ("learn_fact", {"content": "x"}, "error"))) == []


def test_a_read_only_shell_command_is_not_an_effect_call():
    evidence = _evidence(_bash("ls /ws", "error"))
    assert evidence[0].side_effect == "none"
    assert fc.effects_all_failed(SILENT, evidence) == []


def test_a_failed_mcp_call_is_an_effect_call():
    evidence = _evidence(("mcp__desktop__click", {"x": 1}, "error"))
    assert [f.check for f in fc.effects_all_failed(SILENT, evidence)] == ["effects_all_failed"]


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


# ---------------------------------------------------------------------------
# evaluate and record_of
# ---------------------------------------------------------------------------


def test_no_findings_is_clean():
    verdict = fc.evaluate("I created /ws/reports/summary.md.", _evidence(WRITE), WS, _missing())
    assert verdict == fc.FinishVerdict("clean")


def test_findings_are_flagged_with_check_names_in_a_fixed_order():
    evidence = _evidence(("write_file", {"path": "/ws/a.md"}, "error"), ("write_file", {"path": "/ws/b.md"}, "error"))
    text = "I created /ws/a.md. I saved /ws/b.md."
    verdict = fc.evaluate(text, evidence, WS, _missing("/ws/a.md", "/ws/b.md"))
    assert verdict.verdict == "flagged"
    assert verdict.failed == ("named_file_missing", "effects_all_failed")
    assert verdict.why is None
    assert len(verdict.findings) == 3


def test_evaluate_reads_the_first_4000_characters():
    text = "x" * 4000 + " I created /ws/a.md."
    assert fc.evaluate(text, _evidence(WRITE), WS, _missing("/ws/a.md")) == fc.FinishVerdict("clean")


def test_a_check_that_raises_gives_not_checked(monkeypatch, caplog):
    def boom(*_args):
        raise RuntimeError("secret text from the report")

    monkeypatch.setattr(fc, "effects_all_failed", boom)
    with caplog.at_level(logging.WARNING):
        verdict = fc.evaluate(SILENT, _evidence(WRITE), WS, _missing())
    assert verdict == fc.FinishVerdict("not_checked", why="check_error")
    assert "RuntimeError" in caplog.text
    assert "secret text" not in caplog.text


def test_the_record_has_the_agreed_shape():
    verdict = fc.FinishVerdict(
        "flagged", failed=("named_file_missing",), findings=(fc.Finding("named_file_missing", "d"),)
    )
    assert fc.record_of(verdict, "shadow") == {
        "v": 1,
        "mode": "shadow",
        "verdict": "flagged",
        "failed": ["named_file_missing"],
        "why": None,
    }
    assert fc.record_of(fc.FinishVerdict("not_checked", why="no_calls"), "shadow") == {
        "v": 1,
        "mode": "shadow",
        "verdict": "not_checked",
        "failed": [],
        "why": "no_calls",
    }


def test_the_record_carries_no_text_of_the_report_or_the_calls():
    markers = ["ZEBRA-MARKER", "/ws/zebra-dir/ZEBRA-FILE.md", "QUAGGA-MARKER"]
    text = f"{markers[0]}: I created {markers[1]}. {markers[2]}."
    evidence = _evidence(("write_file", {"path": markers[1]}, "error"))
    verdict = fc.evaluate(text, evidence, WS, _missing(markers[1]))
    assert verdict.verdict == "flagged" and verdict.findings
    dumped = json.dumps(fc.record_of(verdict, "shadow"))
    assert "/" not in dumped
    for marker in [*markers, "ZEBRA", "zebra", "QUAGGA"]:
        assert marker not in dumped


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


def test_counts_of_records():
    records = [
        {"v": 1, "mode": "shadow", "verdict": "clean", "failed": [], "why": None},
        {"v": 1, "mode": "shadow", "verdict": "flagged", "failed": ["effects_all_failed"], "why": None},
        {"v": 1, "mode": "shadow", "verdict": "flagged", "failed": ["named_file_missing"], "why": None},
        {"v": 1, "mode": "shadow", "verdict": "not_checked", "failed": [], "why": "no_calls"},
        None,  # a row written before the checks existed
        "not a record",
        {"verdict": "something else"},
    ]
    assert fc.finish_check_counts(records, "shadow") == {"checked": 3, "flagged": 2, "not_checked": 1, "mode": "shadow"}


def test_counts_of_no_records():
    assert fc.finish_check_counts([], "off") == {"checked": 0, "flagged": 0, "not_checked": 0, "mode": "off"}
