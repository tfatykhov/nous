"""scripts/pr_merge_gate.py: the merge gate reads inline Codex comments, not only review bodies.

The two fixtures are trimmed copies of the real GitHub API payloads of PRs #753 and #754, both merged with
inline Codex P1 comments open on their head.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
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
AUTHOR = "cognition-engines-nous-agent"  # the PR author, also an agent login
OWNER = {"login": "tfatykhov"}  # a resolvedBy that counts
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
    app = {"id": gate.ACTIONS_APP_ID}
    return {"id": rid, "name": name, "head_sha": head_sha, "status": status, "conclusion": conclusion, "app": app}


GREEN = [run(), run("Lint & Format", rid=2), run("DB roles", rid=3), run("Protected paths", rid=4)]
# PRs #753/#754 predate the DB roles and Protected paths checks (#770), so their heads lack them
LATER_CHECKS = {"no DB roles check run on head", "no Protected paths check run on head"}


def load(n):
    data = json.loads((FIXTURES / f"pr{n}.json").read_text(encoding="utf-8"))
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
        [review()], comments, HEAD, GREEN, threads={10: {"isResolved": True, "isOutdated": False, "resolvedBy": OWNER}}
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
    """A failing Tests run on another commit, listed last, neither fails nor replaces the head's run."""
    runs = GREEN + [run(conclusion="failure", rid=9, head_sha=OLD)]
    assert gate.evaluate_gate([review()], [], HEAD, runs, threads={}).passed
    verdict = gate.evaluate_gate([review()], [], HEAD, GREEN[1:] + [run(rid=9, head_sha=OLD)], threads={})
    assert not verdict.passed
    assert verdict.reasons == ["no Tests check run on head"]


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
    verdict = gate.evaluate_gate(
        [review()], [inline(10)], HEAD, GREEN, threads={10: {"isResolved": True, "resolvedBy": OWNER}}
    )
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
    verdict = gate.evaluate_gate([review()], [], HEAD, [run(conclusion="skipped")] + GREEN[1:], threads={})
    assert not verdict.passed
    assert verdict.reasons == ["check Tests skipped"]


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


def fake_gh(n, compare_files, author="tfatykhov", failing=None, data=None):
    """``gh api`` replaced by fixture ``n`` (or ``data``); a call whose path contains ``failing`` fails."""
    data = data or load(n)[0]
    graphql = [
        {"isResolved": t["isResolved"], "isOutdated": t["isOutdated"], "comments": {"nodes": [{"databaseId": t["id"]}]}}
        for t in data["threads"]
    ]

    def gh_lines(*args):
        path = next(a for a in args if a.startswith("repos/") or a == "graphql")
        if failing and failing in path:
            raise subprocess.CalledProcessError(1, ["gh", "api", path], stderr="HTTP 404")
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
        if path.endswith("/actions/variables/NOUS_PR_LOGINS"):
            return [AUTHOR]
        assert path.endswith(f"/pulls/{n}"), path
        return [{"head": data["head_sha"], "author": author, "base": "main"}]

    return gh_lines


def test_the_cli_fails_pr753_on_the_head_p1_alone_once_older_p1s_were_fixed(monkeypatch, capsys):
    """The older P1s' replies are tfatykhov's, which count once tfatykhov is not the PR author."""
    paths = {c["path"] for c in load(753)[0]["inline_comments"]}
    monkeypatch.delenv("NOUS_PR_LOGINS", raising=False)
    monkeypatch.setattr(gate, "_gh_lines", fake_gh(753, paths, author=AUTHOR))
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
    ruleset = json.loads((ROOT / "scripts" / "ops" / "main-ruleset.json").read_text(encoding="utf-8"))
    required = [
        c
        for rule in ruleset["rules"]
        if rule["type"] == "required_status_checks"
        for c in rule["parameters"]["required_status_checks"]
    ]
    assert sorted(gate.REQUIRED_CHECKS) == sorted(c["context"] for c in required)
    assert {c["integration_id"] for c in required} == {gate.ACTIONS_APP_ID}


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
        [review()],
        [inline(10, body="Something looks off")],
        HEAD,
        GREEN,
        threads={10: {"isResolved": True, "resolvedBy": OWNER}},
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


# Evidence the PR author or an agent produces itself does not clear a P1 (owner review of #795)


def test_a_thread_the_author_resolved_still_anchored_on_head_fails():
    """Codex flags A; the author pushes an unrelated B, Codex finds nothing new, the author resolves the thread."""
    comments = [inline(10, commit_id=HEAD, original=OLD)]
    threads = {10: {"isResolved": True, "isOutdated": False, "resolvedBy": {"login": AUTHOR}}}
    verdict = gate.evaluate_gate([review()], comments, HEAD, GREEN, threads=threads, untrusted_logins={AUTHOR})
    assert not verdict.passed
    assert "still anchored on head, thread unresolved" in verdict.line()


def test_an_outdated_p1_with_only_the_authors_reply_and_a_touched_file_fails():
    """B makes a trivial edit to the flagged lines, which outdates the thread, and the author replies."""
    comments = [
        inline(10, commit_id=OLD, original=OLD, line=None),
        inline(11, body="Fixed in hhhh", login=AUTHOR, reply_to=10, commit_id=OLD, original=OLD, line=None),
    ]
    verdict = gate.evaluate_gate(
        [review()],
        comments,
        HEAD,
        GREEN,
        threads={10: {"isResolved": False, "isOutdated": True}},
        touched_paths={OLD: {"nous/x.py"}},
        untrusted_logins={AUTHOR},
    )
    assert not verdict.passed
    assert "outdated, no reply" in verdict.line()


def test_a_thread_resolved_or_replied_to_by_someone_else_passes():
    anchored = [inline(10, commit_id=HEAD, original=OLD)]
    threads = {10: {"isResolved": True, "isOutdated": False, "resolvedBy": OWNER}}
    resolved = gate.evaluate_gate([review()], anchored, HEAD, GREEN, threads=threads, untrusted_logins={AUTHOR})
    assert resolved.passed, resolved.line()
    outdated = [
        inline(10, commit_id=OLD, original=OLD, line=None),
        inline(11, body="Fixed in hhhh", login="tfatykhov", reply_to=10, commit_id=OLD, original=OLD, line=None),
    ]
    replied = gate.evaluate_gate(
        [review()],
        outdated,
        HEAD,
        GREEN,
        threads={10: {"isResolved": False, "isOutdated": True}},
        touched_paths={OLD: {"nous/x.py"}},
        untrusted_logins={AUTHOR},
    )
    assert replied.passed, replied.line()


def test_a_resolution_by_an_unknown_login_does_not_count():
    """GraphQL gives resolvedBy null for a deleted account: unknown counts as untrusted."""
    comments = [inline(10, commit_id=HEAD, original=OLD)]
    for thread in ({"isResolved": True, "isOutdated": False}, {"isResolved": True, "resolvedBy": None}):
        assert not gate.evaluate_gate([review()], comments, HEAD, GREEN, threads={10: thread}).passed


# Forged or failed reviews do not count as a review of the head


def summary_comment(state, login=CODEX, updated_at="2026-10-22T12:48:54Z"):
    body = f"<!-- codex-review-state:v1 {json.dumps({'code_review': state})} -->"
    return {"id": 5, "user": {"login": login}, "updated_at": updated_at, "body": body}


def test_a_failed_review_state_on_head_is_not_a_review():
    """#792's marker reads status "failed": Codex did not finish the review."""
    failed = summary_comment({"commit_sha": HEAD, "status": "failed"})
    verdict = gate.evaluate_gate([], [], HEAD, GREEN, threads={}, issue_comments=[failed])
    assert not verdict.passed
    assert "has not reviewed head" in verdict.line()


def test_a_review_state_marker_posted_by_someone_else_is_ignored():
    forged = summary_comment({"commit_sha": HEAD, "status": "completed"}, login=AUTHOR)
    verdict = gate.evaluate_gate([], [], HEAD, GREEN, threads={}, issue_comments=[forged])
    assert not verdict.passed
    assert "has not reviewed head" in verdict.line()


def test_thread_outdatedness_comes_from_graphql_over_the_rest_fields():
    """REST says the comment is off the head; GraphQL says the thread is not outdated, so it is anchored."""
    comments = [
        inline(10, commit_id=OLD, original=OLD, line=None),
        inline(11, body="Fixed", login="tfatykhov", reply_to=10, commit_id=OLD, original=OLD, line=None),
    ]
    verdict = gate.evaluate_gate(
        [review()],
        comments,
        HEAD,
        GREEN,
        threads={10: {"isResolved": False, "isOutdated": False}},
        touched_paths={OLD: {"nous/x.py"}},
    )
    assert not verdict.passed
    assert "still anchored on head" in verdict.line()


def test_codex_comments_are_read_in_time_order_not_list_order():
    quota = {"id": 6, "user": {"login": CODEX}, "updated_at": "2026-10-22T04:00:00Z", "body": gate._QUOTA}
    later = summary_comment({"commit_sha": OLD, "status": "completed"}, updated_at="2026-10-22T05:00:00Z")
    verdict = gate.evaluate_gate([], [], HEAD, GREEN, threads={}, issue_comments=[later, quota])
    assert not verdict.passed
    assert "quota" not in verdict.line()


def test_the_cli_does_not_count_the_pr_authors_own_replies(monkeypatch, capsys):
    """#753's author is tfatykhov, who also wrote the replies on its older P1s."""
    paths = {c["path"] for c in load(753)[0]["inline_comments"]}
    monkeypatch.delenv("NOUS_PR_LOGINS", raising=False)
    monkeypatch.setattr(gate, "_gh_lines", fake_gh(753, paths))
    assert gate.main(["753"]) == 1
    out = capsys.readouterr().out
    assert "P1 posted on head nous/cognitive/execution_ledger.py:179" in out
    assert "outdated, no reply" in out


# Codex's own finding flags, a second source for findings whose inline comment is gone


def test_findings_flagged_on_head_without_a_visible_inline_comment_fail():
    flagged = summary_comment({"commit_sha": HEAD, "status": "completed", "has_any_findings": True})
    verdict = gate.evaluate_gate([review()], [], HEAD, GREEN, threads={}, issue_comments=[flagged])
    assert not verdict.passed
    assert "no Codex inline comment on head" in verdict.line()
    unresolved = summary_comment({"commit_sha": HEAD, "status": "completed", "has_unresolved_findings": True})
    assert not gate.evaluate_gate([review()], [], HEAD, GREEN, threads={}, issue_comments=[unresolved]).passed
    older = [inline(10, body=P2, commit_id=OLD, original=OLD, line=None)]
    verdict = gate.evaluate_gate([review()], older, HEAD, GREEN, threads={}, issue_comments=[flagged])
    assert "no Codex inline comment on head" in verdict.line()


def test_findings_flagged_on_head_with_only_a_p2_inline_comment_on_head_pass():
    """#768 and #790: the flags carry no severity, so a visible P2 on head explains them."""
    state = {"commit_sha": HEAD, "status": "completed", "has_any_findings": True, "has_unresolved_findings": True}
    verdict = gate.evaluate_gate(
        [review()], [inline(10, body=P2)], HEAD, GREEN, threads={}, issue_comments=[summary_comment(state)]
    )
    assert verdict.passed, verdict.line()


def test_findings_flagged_on_an_older_commit_do_not_fail():
    flagged = summary_comment({"commit_sha": OLD, "status": "completed", "has_any_findings": True})
    assert gate.evaluate_gate([review()], [], HEAD, GREEN, threads={}, issue_comments=[flagged]).passed


# Required checks come from GitHub Actions, as the ruleset pins them


def test_a_required_check_from_another_app_does_not_count():
    forged = dict(run(rid=9), app={"id": 1})
    verdict = gate.evaluate_gate([review()], [], HEAD, [forged] + GREEN[1:], threads={})
    assert not verdict.passed
    assert "no Tests check run on head" in verdict.line()
    assert gate.evaluate_gate([review()], [], HEAD, GREEN + [forged], threads={}).passed


# The CLI


def test_the_cli_prints_repo_and_base(monkeypatch, capsys):
    paths = {c["path"] for c in load(753)[0]["inline_comments"]}
    monkeypatch.setenv("NOUS_PR_LOGINS", AUTHOR)
    monkeypatch.setattr(gate, "_gh_lines", fake_gh(753, paths, author=AUTHOR))
    gate.main(["753"])
    out = capsys.readouterr().out
    assert out.startswith("GATE: FAIL head=75943279323d10d0b7df569b9e0027692048824b repo=tfatykhov/nous base=main ")


def test_the_cli_rejects_a_malformed_repo(monkeypatch, capsys):
    def never(*args):
        raise AssertionError(args)

    monkeypatch.setattr(gate, "_gh_lines", never)
    for repo in ("tfatykhov/nous/../x", "../nous", "tfatykhov", "a/b?x=1", "./.."):
        assert gate.main(["753", "--repo", repo]) == 1
        assert capsys.readouterr().out.startswith(f"GATE: FAIL head=unknown repo={repo} invalid --repo")


def test_the_cli_counts_a_file_unchanged_when_compare_fails(monkeypatch, capsys):
    """A compare that 404s (the commit is gone) is unknown, which counts as unchanged."""
    paths = {c["path"] for c in load(753)[0]["inline_comments"]}
    monkeypatch.setenv("NOUS_PR_LOGINS", AUTHOR)
    fake = fake_gh(753, paths, author=AUTHOR, failing="/compare/")
    monkeypatch.setattr(gate, "_gh_lines", fake)
    assert gate.main(["753"]) == 1
    assert "outdated, file unchanged since" in capsys.readouterr().out


def test_the_cli_fails_closed_when_graphql_fails(monkeypatch, capsys):
    paths = {c["path"] for c in load(753)[0]["inline_comments"]}
    monkeypatch.setenv("NOUS_PR_LOGINS", AUTHOR)
    monkeypatch.setattr(gate, "_gh_lines", fake_gh(753, paths, author=AUTHOR, failing="graphql"))
    assert gate.main(["753"]) == 1
    assert "thread data unavailable" in capsys.readouterr().out


def test_the_cli_reads_the_agent_logins_from_the_repository_variable(monkeypatch, capsys):
    """Without NOUS_PR_LOGINS in the environment the variable is read; replies by its login do not count."""
    data, _ = load(753)
    agent_replies = dict(
        data,
        inline_comments=[
            dict(c, user={"login": AUTHOR}) if c.get("in_reply_to_id") else c for c in data["inline_comments"]
        ],
    )
    paths = {c["path"] for c in data["inline_comments"]}
    monkeypatch.delenv("NOUS_PR_LOGINS", raising=False)
    monkeypatch.setattr(gate, "_gh_lines", fake_gh(753, paths, author="someone", data=agent_replies))
    assert gate.main(["753"]) == 1
    assert "outdated, no reply" in capsys.readouterr().out


def test_logins_match_case_insensitively():
    """GitHub logins are case-insensitive, as scripts/ci/protected_paths.py treats NOUS_PR_LOGINS."""
    anchored = [inline(10, commit_id=HEAD, original=OLD)]
    threads = {10: {"isResolved": True, "isOutdated": False, "resolvedBy": {"login": AUTHOR}}}
    untrusted = {AUTHOR.upper()}
    assert not gate.evaluate_gate([review()], anchored, HEAD, GREEN, threads=threads, untrusted_logins=untrusted).passed
    outdated = [
        inline(10, commit_id=OLD, original=OLD, line=None),
        inline(11, body="Fixed", login=AUTHOR.title(), reply_to=10, commit_id=OLD, original=OLD, line=None),
    ]
    verdict = gate.evaluate_gate(
        [review()],
        outdated,
        HEAD,
        GREEN,
        threads={10: {"isResolved": False, "isOutdated": True}},
        touched_paths={OLD: {"nous/x.py"}},
        untrusted_logins={AUTHOR},
    )
    assert "outdated, no reply" in verdict.line()


def test_the_cli_fails_when_the_agent_logins_cannot_be_read(monkeypatch, capsys):
    """Without them an agent's own resolution would count, so the gate fails closed."""
    monkeypatch.delenv("NOUS_PR_LOGINS", raising=False)
    monkeypatch.setattr(gate, "_gh_lines", fake_gh(753, set(), failing="/actions/variables/"))
    assert gate.main(["753"]) == 1
    out = capsys.readouterr().out
    assert "NOUS_PR_LOGINS unreadable" in out
    assert "note: NOUS_PR_LOGINS" not in out
