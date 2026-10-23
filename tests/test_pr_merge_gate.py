"""scripts/pr_merge_gate.py: the merge gate reads inline Codex comments, not only review bodies.

The two fixtures are trimmed copies of the real GitHub API payloads of PRs #753 and #754, both merged with
inline Codex P1 comments open on their head.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "pr_merge_gate.py"
FIXTURES = ROOT / "tests" / "fixtures" / "pr_merge_gate"
spec = importlib.util.spec_from_file_location("pr_merge_gate", SCRIPT)
gate = importlib.util.module_from_spec(spec)
sys.modules["pr_merge_gate"] = gate
spec.loader.exec_module(gate)

CODEX = "chatgpt-codex-connector[bot]"
HEAD = "h" * 40
OLD = "a" * 40
P1 = "**<sub><sub>![P1 Badge](https://img.shields.io/badge/P1-orange?style=flat)</sub></sub>  Fix the thing"
P2 = "**<sub><sub>![P2 Badge](https://img.shields.io/badge/P2-yellow?style=flat)</sub></sub>  Tidy the thing"


def review(commit_id=HEAD, body="Codex Review: Didn't find any major issues. Hooray!"):
    return {"id": 1, "user": {"login": CODEX}, "commit_id": commit_id, "state": "COMMENTED", "body": body}


def inline(cid, body=P1, commit_id=HEAD, original=HEAD, path="nous/x.py", line=10, reply_to=None, login=CODEX):
    return {
        "id": cid,
        "user": {"login": login},
        "body": body,
        "commit_id": commit_id,
        "original_commit_id": original,
        "path": path,
        "line": line,
        "original_line": line,
        "in_reply_to_id": reply_to,
    }


def run(name="Tests", conclusion="success", status="completed", rid=1, head_sha=HEAD):
    return {"id": rid, "name": name, "head_sha": head_sha, "status": status, "conclusion": conclusion}


GREEN = [run(), run("Lint & Format", rid=2), run("DB roles", rid=3), run("Protected paths", rid=4)]
# PRs #753/#754 predate the DB roles and Protected paths checks (#770), so their heads lack them
LATER_CHECKS = {"no DB roles check run on head", "no Protected paths check run on head"}


def load(n):
    data = json.loads((FIXTURES / f"pr{n}.json").read_text())
    threads = {t["id"]: t for t in data["threads"]}
    return data, threads


def evaluate_fixture(n):
    data, threads = load(n)
    return gate.evaluate_gate(
        data["reviews"],
        data["inline_comments"],
        data["head_sha"],
        data["check_runs"],
        threads=threads,
        issue_comments=data["issue_comments"],
    )


# (1) review bodies look clean, inline P1 on the current head: the #753 / #754 merges


def test_pr753_fails_on_the_inline_p1_posted_on_its_head():
    verdict = evaluate_fixture(753)
    assert not verdict.passed
    assert verdict.line().startswith("GATE: FAIL head=75943279323d10d0b7df569b9e0027692048824b ")
    assert any("nous/cognitive/execution_ledger.py:179" in r and "posted on head" in r for r in verdict.reasons)


def test_pr754_fails_on_its_three_inline_p1s_posted_on_its_head():
    verdict = evaluate_fixture(754)
    assert not verdict.passed
    on_head = [r for r in verdict.reasons if "posted on head" in r]
    assert sorted(r.split()[4] for r in on_head) == [
        "nous/cognitive/claim_verifier.py:1621",
        "nous/cognitive/claim_verifier.py:1692",
        "nous/cognitive/claim_verifier.py:1841",
    ]


def test_the_old_body_only_gate_would_have_passed_both_prs():
    """CI was green and Codex reviewed the head, so only the inline P1s can fail these PRs."""
    for n in (753, 754):
        data, _ = load(n)
        head_reviews = [
            r for r in data["reviews"] if r["user"]["login"] == CODEX and r["commit_id"] == data["head_sha"]
        ]
        assert head_reviews and not any("P1" in r["body"] for r in head_reviews)
        reasons = [r for r in evaluate_fixture(n).reasons if r not in LATER_CHECKS]
        assert reasons and all("P1" in r for r in reasons)


def test_a_clean_review_body_does_not_hide_an_inline_p1_on_head():
    verdict = gate.evaluate_gate([review()], [inline(10)], HEAD, GREEN, threads={})
    assert not verdict.passed
    assert "posted on head" in verdict.line()


def test_a_reply_does_not_clear_a_p1_posted_on_head():
    comments = [inline(10), inline(11, body="Fixed in abc", login="tfatykhov", reply_to=10)]
    verdict = gate.evaluate_gate([review()], comments, HEAD, GREEN, threads={10: {"isResolved": False}})
    assert not verdict.passed


# (2) older P1s that later commits fixed


def test_outdated_p1_with_a_reply_and_a_later_change_to_its_file_passes():
    comments = [
        inline(10, commit_id=OLD, original=OLD, line=None),
        inline(11, body="Fixed in hhhh", login="tfatykhov", reply_to=10, commit_id=OLD, original=OLD, line=None),
    ]
    verdict = gate.evaluate_gate(
        [review(OLD, P1), review()],
        comments,
        HEAD,
        GREEN,
        threads={10: {"isResolved": False, "isOutdated": True}},
        touched_paths={OLD: {"nous/x.py"}},
    )
    assert verdict.passed, verdict.line()
    assert verdict.line().startswith(f"GATE: PASS head={HEAD}")


def test_outdated_p1_with_a_reply_but_no_change_to_its_file_fails():
    comments = [
        inline(10, commit_id=OLD, original=OLD, line=None),
        inline(11, body="Fixed", login="tfatykhov", reply_to=10, commit_id=OLD, original=OLD, line=None),
    ]
    verdict = gate.evaluate_gate(
        [review()],
        comments,
        HEAD,
        GREEN,
        threads={10: {"isResolved": False, "isOutdated": True}},
        touched_paths={OLD: {"nous/other.py"}},
    )
    assert not verdict.passed


def test_outdated_p1_without_a_reply_fails():
    comments = [inline(10, commit_id=OLD, original=OLD, line=None)]
    verdict = gate.evaluate_gate(
        [review()],
        comments,
        HEAD,
        GREEN,
        threads={10: {"isResolved": False, "isOutdated": True}},
        touched_paths={OLD: {"nous/x.py"}},
    )
    assert not verdict.passed
    assert "no reply" in verdict.line()


def test_older_p1_still_anchored_on_head_fails_until_its_thread_is_resolved():
    """GitHub moves commit_id to the head when the anchored lines are unchanged; a reply is not enough."""
    comments = [
        inline(10, commit_id=HEAD, original=OLD),
        inline(11, body="Fixed in hhhh", login="tfatykhov", reply_to=10, commit_id=HEAD, original=OLD),
    ]
    unresolved = gate.evaluate_gate(
        [review()], comments, HEAD, GREEN, threads={10: {"isResolved": False, "isOutdated": False}}
    )
    assert not unresolved.passed
    assert "still anchored on head" in unresolved.line()
    resolved = gate.evaluate_gate(
        [review()], comments, HEAD, GREEN, threads={10: {"isResolved": True, "isOutdated": False}}
    )
    assert resolved.passed, resolved.line()


def test_missing_thread_data_fails_closed():
    comments = [
        inline(10, commit_id=HEAD, original=OLD),
        inline(11, body="Fixed", login="tfatykhov", reply_to=10, commit_id=HEAD, original=OLD),
    ]
    verdict = gate.evaluate_gate([review()], comments, HEAD, GREEN, threads=None)
    assert not verdict.passed
    assert "thread data unavailable" in verdict.line()


# (3) Codex reviewed an older head only


def test_review_on_an_older_head_only_fails():
    verdict = gate.evaluate_gate([review(OLD)], [], HEAD, GREEN, threads={})
    assert not verdict.passed
    assert "has not reviewed head" in verdict.line()


def test_a_completed_summary_state_on_head_counts_as_a_review():
    """A clean Codex review posts no review object, only the summary comment with its state marker."""
    state = {"code_review": {"commit_sha": HEAD, "has_any_findings": False, "status": "completed"}}
    summary = {
        "id": 5,
        "user": {"login": CODEX},
        "updated_at": "2026-10-22T12:48:54Z",
        "body": f"<!-- codex-pull-request-review-summary -->\n<!-- codex-review-state:v1 {json.dumps(state)} -->\n",
    }
    verdict = gate.evaluate_gate([review(OLD)], [], HEAD, GREEN, threads={}, issue_comments=[summary])
    assert verdict.passed, verdict.line()


def test_a_review_in_progress_on_head_fails_with_wait():
    state = {"code_review": {"commit_sha": HEAD, "status": "in_progress"}}
    summary = {
        "id": 5,
        "user": {"login": CODEX},
        "updated_at": "2026-10-22T12:48:54Z",
        "body": f"<!-- codex-review-state:v1 {json.dumps(state)} -->",
    }
    verdict = gate.evaluate_gate([], [], HEAD, GREEN, threads={}, issue_comments=[summary])
    assert not verdict.passed
    assert "in progress" in verdict.line()


def test_quota_exhaustion_states_the_fallback_and_still_fails():
    quota = {
        "id": 6,
        "user": {"login": CODEX},
        "updated_at": "2026-10-22T04:04:08Z",
        "body": "You have reached your Codex usage limits for code reviews.",
    }
    verdict = gate.evaluate_gate([review(OLD)], [], HEAD, GREEN, threads={}, issue_comments=[quota])
    assert not verdict.passed
    assert "quota exhausted" in verdict.line()
    assert "explicit owner approval" in verdict.line()


def test_a_p1_in_the_head_review_body_fails():
    verdict = gate.evaluate_gate([review(body=P1)], [], HEAD, GREEN, threads={})
    assert not verdict.passed


# (4) CI


def test_red_ci_fails():
    verdict = gate.evaluate_gate(
        [review()], [], HEAD, [run(conclusion="failure"), run("Lint & Format", rid=2)], threads={}
    )
    assert not verdict.passed
    assert "Tests failure" in verdict.line()


def test_pending_ci_fails():
    verdict = gate.evaluate_gate([review()], [], HEAD, [run(status="in_progress", conclusion=None)], threads={})
    assert not verdict.passed
    assert "pending" in verdict.line()


def test_missing_tests_check_fails():
    verdict = gate.evaluate_gate([review()], [], HEAD, [run("Lint & Format")], threads={})
    assert not verdict.passed
    assert "no Tests check" in verdict.line()


def test_only_the_latest_run_of_a_check_counts():
    runs = [run(conclusion="failure", rid=1), run(conclusion="success", rid=7)] + GREEN[1:]
    assert gate.evaluate_gate([review()], [], HEAD, runs, threads={}).passed


def test_check_runs_of_another_commit_are_ignored():
    runs = [run(head_sha=OLD)]
    verdict = gate.evaluate_gate([review()], [], HEAD, runs, threads={})
    assert not verdict.passed


# (5) P2 only


def test_p2_only_inline_passes_with_a_note():
    verdict = gate.evaluate_gate([review()], [inline(10, body=P2)], HEAD, GREEN, threads={})
    assert verdict.passed, verdict.line()
    assert "1 open P2" in verdict.line()


def test_inline_p1_from_a_human_is_not_a_codex_finding():
    verdict = gate.evaluate_gate([review()], [inline(10, login="someone")], HEAD, GREEN, threads={})
    assert verdict.passed, verdict.line()


# Rules a refactor could break without the tests above noticing


def test_resolving_the_thread_of_a_p1_posted_on_head_does_not_clear_it():
    verdict = gate.evaluate_gate([review()], [inline(10)], HEAD, GREEN, threads={10: {"isResolved": True}})
    assert not verdict.passed


def test_a_p0_inline_blocks_like_a_p1():
    p0 = P1.replace("P1 Badge", "P0 Badge")
    assert not gate.evaluate_gate([review()], [inline(10, body=p0)], HEAD, GREEN, threads={}).passed


def test_a_codex_inline_comment_without_a_severity_badge_fails():
    verdict = gate.evaluate_gate([review()], [inline(10, body="Something looks off")], HEAD, GREEN, threads={})
    assert not verdict.passed
    assert "without a severity badge" in verdict.line()


def test_a_completed_summary_state_for_an_older_commit_is_not_a_review_of_head():
    state = {"code_review": {"commit_sha": OLD, "status": "completed"}}
    summary = {"id": 5, "user": {"login": CODEX}, "body": f"<!-- codex-review-state:v1 {json.dumps(state)} -->"}
    verdict = gate.evaluate_gate([], [], HEAD, GREEN, threads={}, issue_comments=[summary])
    assert not verdict.passed
    assert "has not reviewed head" in verdict.line()


def test_a_review_of_head_by_someone_else_is_not_a_codex_review():
    other = dict(review(), user={"login": "tfatykhov"})
    assert not gate.evaluate_gate([other], [], HEAD, GREEN, threads={}).passed


def test_without_thread_data_a_p1_still_on_head_is_anchored_from_the_rest_fields():
    comments = [
        inline(10, commit_id=HEAD, original=OLD),
        inline(11, body="Fixed", login="tfatykhov", reply_to=10, commit_id=HEAD, original=OLD),
    ]
    verdict = gate.evaluate_gate([review()], comments, HEAD, GREEN, threads=None, touched_paths={OLD: {"nous/x.py"}})
    assert not verdict.passed
    assert "still anchored on head" in verdict.line()


def test_a_reply_by_codex_itself_is_not_a_reply():
    comments = [
        inline(10, commit_id=OLD, original=OLD, line=None),
        inline(11, body="More detail", reply_to=10, commit_id=OLD, original=OLD, line=None),
    ]
    verdict = gate.evaluate_gate(
        [review()],
        comments,
        HEAD,
        GREEN,
        threads={10: {"isResolved": False, "isOutdated": True}},
        touched_paths={OLD: {"nous/x.py"}},
    )
    assert not verdict.passed
    assert "no reply" in verdict.line()


def test_an_unknown_change_set_counts_as_unchanged():
    comments = [
        inline(10, commit_id=OLD, original=OLD, line=None),
        inline(11, body="Fixed", login="tfatykhov", reply_to=10, commit_id=OLD, original=OLD, line=None),
    ]
    verdict = gate.evaluate_gate(
        [review()], comments, HEAD, GREEN, threads={10: {"isResolved": False, "isOutdated": True}}, touched_paths={}
    )
    assert not verdict.passed
    assert "file unchanged since" in verdict.line()


def test_a_skipped_tests_run_fails():
    verdict = gate.evaluate_gate([review()], [], HEAD, [run(conclusion="skipped")], threads={})
    assert not verdict.passed


def test_a_failing_commit_status_fails():
    statuses = gate.statuses_as_check_runs([{"id": 3, "context": "ci/legacy", "state": "failure"}], HEAD)
    verdict = gate.evaluate_gate([review()], [], HEAD, GREEN + statuses, threads={})
    assert not verdict.passed
    assert "ci/legacy failure" in verdict.line()


def test_touched_since_is_unknown_once_the_commit_is_not_an_ancestor_of_head():
    files = [{"filename": "nous/x.py"}]
    assert gate.touched_since({"merge_base_commit": {"sha": OLD}, "files": files}, OLD) == {"nous/x.py"}
    assert gate.touched_since({"merge_base_commit": {"sha": "b" * 40}, "files": files}, OLD) is None
    capped = [{"filename": f"f{i}"} for i in range(300)]
    assert gate.touched_since({"merge_base_commit": {"sha": OLD}, "files": capped}, OLD) is None


# The CLI, with gh replaced by the fixture data


def fake_gh(n, compare_files):
    data, _ = load(n)
    graphql = [
        {"isResolved": t["isResolved"], "isOutdated": t["isOutdated"], "comments": {"nodes": [{"databaseId": t["id"]}]}}
        for t in data["threads"]
    ]

    def gh_lines(*args):
        path = next(a for a in args if a.startswith("repos/") or a == "graphql")
        if path == "graphql":
            return graphql
        if "/compare/" in path:
            sha = path.split("/compare/")[1].split("...")[0]
            return [{"merge_base_commit": {"sha": sha}, "files": [{"filename": f} for f in compare_files]}]
        for suffix, value in [
            ("/reviews?", data["reviews"]),
            (f"/pulls/{n}/comments?", data["inline_comments"]),
            ("/issues/", data["issue_comments"]),
            ("/check-runs?", data["check_runs"]),
            ("/status?", []),
        ]:
            if suffix in path:
                return value
        assert path.endswith(f"/pulls/{n}"), path
        return [data["head_sha"]]

    return gh_lines


def test_the_cli_fails_pr753_on_the_head_p1_alone_once_older_p1s_were_fixed(monkeypatch, capsys):
    paths = {c["path"] for c in load(753)[0]["inline_comments"]}
    monkeypatch.setattr(gate, "_gh_lines", fake_gh(753, paths))
    assert gate.main(["753"]) == 1
    out = capsys.readouterr().out.strip()
    assert out.startswith("GATE: FAIL head=75943279323d10d0b7df569b9e0027692048824b ")
    assert "P1 posted on head nous/cognitive/execution_ledger.py:179" in out
    assert out.count("P1 ") == 1


def test_the_cli_prints_fail_when_it_cannot_read_the_pr(monkeypatch, capsys):
    def broken(*args):
        raise FileNotFoundError("gh")

    monkeypatch.setattr(gate, "_gh_lines", broken)
    assert gate.main(["753"]) == 1
    assert capsys.readouterr().out.startswith("GATE: FAIL head=unknown could not read PR #753: FileNotFoundError")


def test_the_required_checks_are_the_rulesets():
    ruleset = json.loads((ROOT / "scripts" / "ops" / "main-ruleset.json").read_text())
    required = [
        c["context"]
        for rule in ruleset["rules"]
        if rule["type"] == "required_status_checks"
        for c in rule["parameters"]["required_status_checks"]
    ]
    assert sorted(gate.REQUIRED_CHECKS) == sorted(required)


def test_a_missing_required_check_fails():
    verdict = gate.evaluate_gate([review()], [], HEAD, GREEN[:3], threads={})
    assert not verdict.passed
    assert "no Protected paths check run on head" in verdict.line()


def test_a_commit_status_cannot_stand_in_for_a_check_run():
    statuses = gate.statuses_as_check_runs([{"id": 99, "context": "Tests", "state": "success"}], HEAD)
    verdict = gate.evaluate_gate([review()], [], HEAD, [run(conclusion="failure")] + GREEN[1:] + statuses, threads={})
    assert not verdict.passed
    assert "check Tests failure" in verdict.line()


def test_a_rerun_in_progress_on_head_waits_even_after_a_review_of_head():
    state = {"code_review": {"commit_sha": HEAD, "status": "in_progress"}}
    summary = {"id": 5, "user": {"login": CODEX}, "body": f"<!-- codex-review-state:v1 {json.dumps(state)} -->"}
    verdict = gate.evaluate_gate([review()], [], HEAD, GREEN, threads={}, issue_comments=[summary])
    assert not verdict.passed
    assert "in progress" in verdict.line()


def test_a_badgeless_codex_comment_on_head_fails_even_when_resolved():
    verdict = gate.evaluate_gate(
        [review()], [inline(10, body="Something looks off")], HEAD, GREEN, threads={10: {"isResolved": True}}
    )
    assert not verdict.passed


def test_an_old_quota_notice_followed_by_a_review_state_is_not_the_fallback():
    quota = {
        "id": 6,
        "user": {"login": CODEX},
        "updated_at": "2026-10-22T04:00:00Z",
        "body": "You have reached your Codex usage limits",
    }
    state = {"code_review": {"commit_sha": OLD, "status": "completed"}}
    summary = {
        "id": 5,
        "user": {"login": CODEX},
        "updated_at": "2026-10-22T05:00:00Z",
        "body": f"<!-- codex-review-state:v1 {json.dumps(state)} -->",
    }
    verdict = gate.evaluate_gate([], [], HEAD, GREEN, threads={}, issue_comments=[quota, summary])
    assert not verdict.passed
    assert "quota" not in verdict.line()
