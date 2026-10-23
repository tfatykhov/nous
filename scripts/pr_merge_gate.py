#!/usr/bin/env python3
"""Decide whether a pull request may be merged: green CI and no open Codex P1 on the current head.

PRs #753 and #754 were merged with inline Codex P1 comments open on their head, because the gate read only
the Codex review bodies, and those never carry the findings. This gate reads the inline comments too.

Rules (all on the PR's current head sha):

* CI: the latest run of every check on the head is completed with conclusion success, neutral or skipped,
  and a ``Tests`` run is among them. Pending or missing checks fail.
* Codex has reviewed the head: a Codex review whose ``commit_id`` is the head, or the Codex summary comment
  whose ``codex-review-state`` marker says the code review of the head completed (a clean review posts no
  review object). A review still running on the head fails ("wait"). If Codex hit its usage limit, the
  documented fallback (procedure "Gate PR merges on Codex review with no P1 findings (with quota-exhaustion
  fallback)") is to stop and get the owner's explicit approval to merge without Codex. The gate says so and
  fails; it never passes on that fallback by itself.
* A P1 badge in the body of a Codex review of the head fails.
* Inline Codex P1 comments (thread roots by the Codex bot whose body carries the P1 badge):

  1. posted on the head (``original_commit_id`` is the head): fails, whatever the replies say. Only a new
     commit can address it, and that commit moves the head.
  2. posted on an earlier commit, thread resolved: passes.
  3. posted on an earlier commit and still anchored on the head (thread not outdated, i.e. the anchored
     lines are unchanged, so GitHub moved its ``commit_id`` to the head): fails until the thread is
     resolved. A "Fixed in ..." reply is not evidence when the anchored code did not change.
  4. posted on an earlier commit, thread outdated: passes only if someone replied AND the comment's file
     changed between the commit it was posted on and the head. A renamed file counts as unchanged.

  Thread state (resolved / outdated) comes from GraphQL. Without it every thread counts as unresolved and
  outdatedness falls back to the REST fields (``commit_id`` is the head and ``line`` is set means anchored).
* Open Codex P2 comments never fail the gate; they are counted in a note.

Output: one line ``GATE: PASS|FAIL head=<sha> <reasons>``; exit status 0 for PASS, 1 for FAIL. The CLI only
reads from GitHub (``gh api`` GETs and a GraphQL query). Merge with ``gh pr merge --match-head-commit <sha>``
using the printed sha, so a push after the gate ran cannot slip in.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

CODEX_LOGIN = "chatgpt-codex-connector[bot]"
TESTS_CHECK = "Tests"
OK_CONCLUSIONS = {"success", "neutral", "skipped"}
_P1 = re.compile(r"!\[P1 Badge\]")
_P2 = re.compile(r"!\[P2 Badge\]")
_STATE = re.compile(r"<!-- codex-review-state:v1 (\{.*?\}) -->", re.S)
_QUOTA = "reached your Codex usage limits"
_COMPARE_FILE_CAP = 300  # GitHub's compare endpoint lists at most this many files
QUOTA_FALLBACK = (
    "quota exhausted; documented fallback (procedure 'Gate PR merges on Codex review with no P1 findings "
    "(with quota-exhaustion fallback)'): stop and get explicit owner approval to merge without Codex"
)


@dataclass
class Verdict:
    """The gate's result: failure reasons (empty means PASS) and non-blocking notes."""

    head_sha: str
    reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.reasons

    def line(self) -> str:
        """The single verdict line the CLI prints."""
        parts = self.reasons + [f"note: {n}" for n in self.notes]
        word = "PASS" if self.passed else "FAIL"
        return f"GATE: {word} head={self.head_sha} " + ("; ".join(parts) or "CI green, Codex reviewed head, no P1")


def _is_codex(item: Mapping) -> bool:
    return (item.get("user") or {}).get("login") == CODEX_LOGIN


def _title(body: str) -> str:
    """The finding's headline: the badge's line with the badge markup removed."""
    first = body.strip().splitlines()[0] if body.strip() else ""
    return re.sub(r"\*\*<sub><sub>!\[P\d Badge\]\([^)]*\)</sub></sub>\s*", "", first).strip(" *")[:80]


def _check_ci(head_sha: str, check_runs: Iterable[Mapping], verdict: Verdict) -> None:
    latest: dict[str, Mapping] = {}
    for run in check_runs:
        if run.get("head_sha", head_sha) != head_sha:
            continue
        name = run["name"]
        if name not in latest or run["id"] > latest[name]["id"]:
            latest[name] = run
    if TESTS_CHECK not in latest:
        verdict.reasons.append(f"no {TESTS_CHECK} check run on head")
    for name, run in sorted(latest.items()):
        if run.get("status") != "completed":
            verdict.reasons.append(f"check {name} pending ({run.get('status')})")
        elif run.get("conclusion") not in OK_CONCLUSIONS:
            verdict.reasons.append(f"check {name} {run.get('conclusion')}")


def _check_codex_review(
    head_sha: str, reviews: Iterable[Mapping], issue_comments: Iterable[Mapping], verdict: Verdict
) -> None:
    head_reviews = [r for r in reviews if _is_codex(r) and r.get("commit_id") == head_sha]
    for r in head_reviews:
        if _P1.search(r.get("body") or ""):
            verdict.reasons.append(f"P1 in the body of Codex review {r.get('id')} of head")
    codex_comments = sorted(
        (c for c in issue_comments if _is_codex(c)), key=lambda c: c.get("updated_at") or c.get("created_at") or ""
    )
    state = None
    for c in codex_comments:
        m = _STATE.search(c.get("body") or "")
        if m:
            state = json.loads(m.group(1)).get("code_review") or {}
    head_state = state if state and state.get("commit_sha") == head_sha else None
    if head_reviews or (head_state and head_state.get("status") == "completed"):
        return
    if head_state and head_state.get("status") in {"in_progress", "queued", "pending", "running"}:
        verdict.reasons.append(f"Codex review of head in progress ({head_state['status']}); wait")
    elif any(_QUOTA in (c.get("body") or "") for c in codex_comments):
        verdict.reasons.append(f"Codex has not reviewed head: {QUOTA_FALLBACK}")
    else:
        verdict.reasons.append("Codex has not reviewed head")


def _check_inline(
    head_sha: str,
    inline_comments: list[Mapping],
    threads: Mapping[int, Mapping] | None,
    touched_paths: Mapping[str, set[str]],
    verdict: Verdict,
) -> None:
    replied = {c["in_reply_to_id"] for c in inline_comments if c.get("in_reply_to_id") and not _is_codex(c)}
    roots = [c for c in inline_comments if _is_codex(c) and not c.get("in_reply_to_id")]
    if threads is None and roots:
        verdict.notes.append("thread data unavailable, every thread counted as unresolved")
    open_p2 = 0
    for c in roots:
        body = c.get("body") or ""
        thread = (threads or {}).get(c["id"]) or {}
        resolved = bool(thread.get("isResolved"))
        if not _P1.search(body):
            if _P2.search(body) and not resolved:
                open_p2 += 1
            continue
        where = f"{c.get('path')}:{c.get('line') or c.get('original_line')} (comment {c['id']}): {_title(body)}"
        original = c.get("original_commit_id")
        if original == head_sha:
            verdict.reasons.append(f"P1 posted on head {where}")
            continue
        if resolved:
            continue
        if "isOutdated" in thread:
            anchored = not thread["isOutdated"]
        else:
            anchored = c.get("commit_id") == head_sha and c.get("line") is not None
        since = f"P1 from {str(original)[:8]}"
        if anchored:
            verdict.reasons.append(f"{since} still anchored on head, thread unresolved {where}")
        elif c["id"] not in replied:
            verdict.reasons.append(f"{since} outdated, no reply {where}")
        elif c.get("path") not in touched_paths.get(original, set()):
            verdict.reasons.append(f"{since} outdated, file unchanged since {where}")
    if open_p2:
        verdict.notes.append(f"{open_p2} open P2 inline comment(s), non-blocking")


def evaluate_gate(
    reviews: Iterable[Mapping],
    inline_comments: Iterable[Mapping],
    head_sha: str,
    check_runs: Iterable[Mapping],
    *,
    threads: Mapping[int, Mapping] | None = None,
    issue_comments: Iterable[Mapping] = (),
    touched_paths: Mapping[str, set[str]] | None = None,
) -> Verdict:
    """Apply the module docstring's rules to the PR data and return the verdict.

    ``reviews``, ``inline_comments``, ``issue_comments`` and ``check_runs`` are the GitHub REST objects.
    ``threads`` maps a thread's first comment id to its GraphQL ``isResolved`` / ``isOutdated``; ``None``
    means the thread data could not be read. ``touched_paths`` maps a commit sha to the files changed
    between it and the head; a missing sha means unknown, which counts as unchanged.
    """
    verdict = Verdict(head_sha)
    _check_ci(head_sha, check_runs, verdict)
    _check_codex_review(head_sha, list(reviews), list(issue_comments), verdict)
    _check_inline(head_sha, list(inline_comments), threads, touched_paths or {}, verdict)
    return verdict


# --- CLI: read-only GitHub reads -------------------------------------------------------------------------

_THREADS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $endCursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $endCursor) {
        nodes { isResolved isOutdated comments(first: 1) { nodes { databaseId } } }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""


def _gh_lines(*args: str) -> list:
    """Run ``gh api`` and parse its one-JSON-value-per-line output (from a ``--jq`` filter)."""
    out = subprocess.run(["gh", "api", *args], check=True, capture_output=True, text=True).stdout
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def fetch_and_evaluate(repo: str, number: int) -> Verdict:
    """Read the PR's data from GitHub with ``gh api`` (GETs and one GraphQL query) and evaluate it."""
    base = f"repos/{repo}"
    (head_sha,) = _gh_lines(f"{base}/pulls/{number}", "--jq", ".head.sha | tojson")
    reviews = _gh_lines("--paginate", f"{base}/pulls/{number}/reviews?per_page=100", "--jq", ".[]")
    inline = _gh_lines("--paginate", f"{base}/pulls/{number}/comments?per_page=100", "--jq", ".[]")
    issue = _gh_lines("--paginate", f"{base}/issues/{number}/comments?per_page=100", "--jq", ".[]")
    runs = _gh_lines("--paginate", f"{base}/commits/{head_sha}/check-runs?per_page=100", "--jq", ".check_runs[]")
    owner, name = repo.split("/", 1)
    threads: dict[int, Mapping] | None
    try:
        nodes = _gh_lines(
            "graphql", "--paginate", "-f", f"query={_THREADS_QUERY}", "-F", f"owner={owner}", "-F",
            f"name={name}", "-F", f"number={number}", "--jq", ".data.repository.pullRequest.reviewThreads.nodes[]",
        )  # fmt: skip
        threads = {n["comments"]["nodes"][0]["databaseId"]: n for n in nodes if n["comments"]["nodes"]}
    except subprocess.CalledProcessError:
        threads = None
    touched: dict[str, set[str]] = {}
    for sha in {c["original_commit_id"] for c in inline if _is_codex(c) and c["original_commit_id"] != head_sha}:
        try:
            files = _gh_lines(f"{base}/compare/{sha}...{head_sha}", "--jq", ".files[].filename | tojson")
        except subprocess.CalledProcessError:
            continue  # unknown (e.g. the commit was force-pushed away) counts as unchanged
        if len(files) < _COMPARE_FILE_CAP:
            touched[sha] = set(files)
    return evaluate_gate(reviews, inline, head_sha, runs, threads=threads, issue_comments=issue, touched_paths=touched)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("pr", type=int, help="pull request number")
    parser.add_argument("--repo", default="tfatykhov/nous", help="owner/name (default: %(default)s)")
    args = parser.parse_args(argv)
    try:
        verdict = fetch_and_evaluate(args.repo, args.pr)
    except (subprocess.CalledProcessError, ValueError, KeyError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        print(f"GATE: FAIL head=unknown could not read PR #{args.pr}: {detail.strip()[:300]}")
        return 1
    print(verdict.line())
    return 0 if verdict.passed else 1


if __name__ == "__main__":
    sys.exit(main())
