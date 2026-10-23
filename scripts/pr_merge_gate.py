#!/usr/bin/env python3
"""Decide whether a pull request may be merged: green CI and no open Codex P1 on the current head.

PRs #753 and #754 were merged with inline Codex P1 comments open on their head, because the gate read only
the Codex review bodies, and those never carry the findings. This gate reads the inline comments too.

Rules (all on the PR's current head sha):

* CI: the latest run of every check on the head (check runs, plus commit statuses) is completed with
  conclusion success, neutral or skipped; every check the default-branch ruleset requires
  (``scripts/ops/main-ruleset.json``) is among them, run by the GitHub Actions app the ruleset pins them
  to, and ``Tests`` concluded success. Pending or missing required checks fail.
* Codex has reviewed the head: a Codex review whose ``commit_id`` is the head, or the Codex summary comment
  whose ``codex-review-state`` marker says the code review of the head completed (a clean review posts no
  review object). A review still running on the head fails ("wait"). If Codex's latest word on the PR is
  that it hit its usage limit, the
  documented fallback (procedure "Gate PR merges on Codex review with no P1 findings (with quota-exhaustion
  fallback)") is to stop and get the owner's explicit approval to merge without Codex. The gate says so and
  fails; it never passes on that fallback by itself. A quota notice after a completed review of the same
  head is ignored: the fallback is for a head Codex has not reviewed, and a new push needs a new review.
* A P0 or P1 badge in the body of a Codex review of the head fails.
* The head's ``codex-review-state`` marker reports unresolved findings (``has_unresolved_findings``) while
  no Codex inline comment was posted on the head: fails, since a deleted inline comment is otherwise
  invisible. ``has_any_findings`` alone does not fail: it stays true once earlier findings are resolved
  (#782). The flag carries no severity, so when inline comments on the head are visible their badges
  decide.
* Inline Codex P1 comments (thread roots by the Codex bot whose body carries a P0 or P1 badge, or no
  severity badge at all, so a change to the badge markup fails rather than hiding every finding):

  1. posted on the head (``original_commit_id`` is the head): fails, whatever the replies say. Only a new
     commit can address it, and that commit moves the head.
  2. posted on an earlier commit, thread resolved: passes.
  3. posted on an earlier commit and still anchored on the head (thread not outdated, i.e. the anchored
     lines are unchanged, so GitHub moved its ``commit_id`` to the head): fails until the thread is
     resolved. A "Fixed in ..." reply is not evidence when the anchored code did not change.
  4. posted on an earlier commit, thread outdated: passes only if someone other than Codex replied AND the
     comment's file changed between the commit it was posted on and the head. That is known only while
     that commit is an ancestor of the head: after a rebase the file counts as unchanged, and so does a
     renamed file. A merge of the base branch into the PR can make a file look changed by the base's own
     commits; that is an accepted limit.

  The PR author's pushes move the head past rule 1, so its own word must not also clear a finding: a
  thread resolved by the PR author, by an agent login (``NOUS_PR_LOGINS``) or by a login the gate cannot
  read counts as unresolved, and replies by the author or an agent login are not replies. A fixed P1 still
  anchored on the head therefore waits for the owner to resolve its thread. Logins compare
  case-insensitively; the CLI takes the union of ``NOUS_PR_LOGINS`` in the environment and the repository
  variable, and fails when it can read neither.

  Thread state (resolved / outdated) comes from GraphQL. Without it every thread counts as unresolved and
  outdatedness falls back to the REST fields (``commit_id`` is the head and ``line`` is set means anchored).
* Open Codex P2 and P3 comments never fail the gate; they are counted in a note.

Output: one line ``GATE: PASS|FAIL head=<sha> repo=<owner/name> base=<branch> <reasons>``; exit status 0
for PASS, 1 for FAIL. The CLI only reads from GitHub (``gh api`` GETs and a GraphQL query). Merge with
``gh pr merge --match-head-commit <sha>`` using the printed sha, so a push after the gate ran cannot slip in.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field

CODEX_LOGIN = "chatgpt-codex-connector[bot]"
TESTS_CHECK = "Tests"
ACTIONS_APP_ID = 15368  # GitHub Actions: the integration_id the ruleset pins its required checks to
# The checks the default-branch ruleset requires (scripts/ops/main-ruleset.json; a test keeps them in sync)
REQUIRED_CHECKS = (TESTS_CHECK, "DB roles", "Protected paths")
OK_CONCLUSIONS = {"success", "neutral", "skipped"}
_BADGE = re.compile(r"!\[(P\d) Badge\]")
_BLOCKING = {"P0", "P1"}
_REPO = re.compile(r"[\w.-]+/[\w.-]+")
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
    repo: str = ""
    base: str = ""

    @property
    def passed(self) -> bool:
        """True when no rule failed."""
        return not self.reasons

    def line(self) -> str:
        """The single verdict line the CLI prints."""
        parts = self.reasons + [f"note: {n}" for n in self.notes]
        word = "PASS" if self.passed else "FAIL"
        where = "".join(f" {k}={v}" for k, v in (("repo", self.repo), ("base", self.base)) if v)
        summary = "; ".join(parts) or "CI green, Codex reviewed head, no P1"
        return f"GATE: {word} head={self.head_sha}{where} {summary}"


def _login(item: Mapping) -> str | None:
    return (item.get("user") or {}).get("login")


def _is_codex(item: Mapping) -> bool:
    return _login(item) == CODEX_LOGIN


def _is_codex_root(item: Mapping) -> bool:
    return _is_codex(item) and not item.get("in_reply_to_id")


def _title(body: str) -> str:
    """The finding's headline: the badge's line with the badge markup removed."""
    first = body.strip().splitlines()[0] if body.strip() else ""
    return re.sub(r"\*\*<sub><sub>!\[P\d Badge\]\([^)]*\)</sub></sub>\s*", "", first).strip(" *")[:80]


def _check_ci(head_sha: str, check_runs: Iterable[Mapping], verdict: Verdict) -> None:
    latest: dict[str, Mapping] = {}
    for run in check_runs:
        if run.get("head_sha") != head_sha:
            continue
        name = run["name"]
        app_id = (run.get("app") or {}).get("id")
        if name in REQUIRED_CHECKS and app_id != ACTIONS_APP_ID:
            name = f"{name} (app {app_id})"  # cannot satisfy the required check, still has to be green
        if name not in latest or run["id"] > latest[name]["id"]:
            latest[name] = run
    for name in REQUIRED_CHECKS:
        if name not in latest:
            verdict.reasons.append(f"no {name} check run on head")
    for name, run in sorted(latest.items()):
        if run.get("status") != "completed":
            verdict.reasons.append(f"check {name} pending ({run.get('status')})")
        elif run.get("conclusion") not in (OK_CONCLUSIONS if name != TESTS_CHECK else {"success"}):
            verdict.reasons.append(f"check {name} {run.get('conclusion')}")


def _check_codex_review(
    head_sha: str,
    reviews: Iterable[Mapping],
    issue_comments: Iterable[Mapping],
    inline_comments: list[Mapping],
    verdict: Verdict,
) -> None:
    head_reviews = [r for r in reviews if _is_codex(r) and r.get("commit_id") == head_sha]
    for r in head_reviews:
        if _BLOCKING & set(_BADGE.findall(r.get("body") or "")):
            verdict.reasons.append(f"P0/P1 in the body of Codex review {r.get('id')} of head")
    codex_comments = sorted(
        (c for c in issue_comments if _is_codex(c)), key=lambda c: c.get("updated_at") or c.get("created_at") or ""
    )
    state: Mapping = {}
    quota = False  # Codex's latest word on the PR is that it hit its usage limit
    for c in codex_comments:
        m = _STATE.search(c.get("body") or "")
        if m:
            state = json.loads(m.group(1)).get("code_review") or {}
            quota = False
        elif _QUOTA in (c.get("body") or ""):
            quota = True
    head_state = state if state.get("commit_sha") == head_sha else {}
    if head_state.get("has_unresolved_findings") and not any(
        _is_codex_root(c) and c.get("original_commit_id") == head_sha for c in inline_comments
    ):
        verdict.reasons.append("Codex reports findings on head but no Codex inline comment on head is visible")
    status = head_state.get("status")
    if status in {"in_progress", "queued", "pending", "running"}:
        verdict.reasons.append(f"Codex review of head in progress ({status}); wait")
    elif head_reviews or status == "completed":
        return
    elif quota:
        verdict.reasons.append(f"Codex has not reviewed head: {QUOTA_FALLBACK}")
    else:
        verdict.reasons.append("Codex has not reviewed head")


def _check_inline(
    head_sha: str,
    inline_comments: list[Mapping],
    threads: Mapping[int, Mapping] | None,
    touched_paths: Mapping[str, set[str]],
    untrusted_logins: Collection[str],
    verdict: Verdict,
) -> None:
    untrusted = {login.casefold() for login in untrusted_logins}  # GitHub logins are case-insensitive
    replied = {
        c["in_reply_to_id"]
        for c in inline_comments
        if c.get("in_reply_to_id") and _login(c) and not _is_codex(c) and _login(c).casefold() not in untrusted
    }
    roots = [c for c in inline_comments if _is_codex_root(c)]
    if threads is None and roots:
        verdict.notes.append("thread data unavailable, every thread counted as unresolved")
    thread_map = threads or {}
    open_minor = 0
    for c in roots:
        body = c.get("body") or ""
        thread = thread_map.get(c["id"]) or {}
        resolver = (thread.get("resolvedBy") or {}).get("login")
        resolved = bool(thread.get("isResolved")) and resolver is not None and resolver.casefold() not in untrusted
        badges = set(_BADGE.findall(body))
        if badges and not badges & _BLOCKING:
            open_minor += not resolved
            continue
        kind = "P1" if badges else "Codex comment without a severity badge"
        where = f"{c.get('path')}:{c.get('line') or c.get('original_line')} (comment {c['id']}): {_title(body)}"
        original = c.get("original_commit_id")
        if original == head_sha:
            verdict.reasons.append(f"{kind} posted on head {where}")
            continue
        if resolved:
            continue
        if "isOutdated" in thread:
            anchored = not thread["isOutdated"]
        else:
            anchored = c.get("commit_id") == head_sha and c.get("line") is not None
        since = f"{kind} from {str(original)[:8]}"
        if anchored:
            verdict.reasons.append(f"{since} still anchored on head, thread unresolved {where}")
        elif c["id"] not in replied:
            verdict.reasons.append(f"{since} outdated, no reply {where}")
        elif c.get("path") not in touched_paths.get(original, set()):
            verdict.reasons.append(f"{since} outdated, file unchanged since {where}")
    if open_minor:
        verdict.notes.append(f"{open_minor} open P2/P3 inline comment(s), non-blocking")


def evaluate_gate(
    reviews: Iterable[Mapping],
    inline_comments: Iterable[Mapping],
    head_sha: str,
    check_runs: Iterable[Mapping],
    *,
    threads: Mapping[int, Mapping] | None = None,
    issue_comments: Iterable[Mapping] = (),
    touched_paths: Mapping[str, set[str]] | None = None,
    untrusted_logins: Collection[str] = (),
) -> Verdict:
    """Apply the module docstring's rules to the PR data and return the verdict.

    ``reviews``, ``inline_comments``, ``issue_comments`` and ``check_runs`` are the GitHub REST objects.
    ``threads`` maps a thread's first comment id to its GraphQL ``isResolved`` / ``isOutdated`` /
    ``resolvedBy``; ``None`` means the thread data could not be read. ``touched_paths`` maps a commit sha
    to the files changed between it and the head; a missing sha means unknown, which counts as unchanged.
    ``untrusted_logins`` (the PR author and the agent logins) neither resolve threads nor reply to them.
    """
    verdict = Verdict(head_sha)
    _check_ci(head_sha, check_runs, verdict)
    inline_comments = list(inline_comments)
    _check_codex_review(head_sha, reviews, issue_comments, inline_comments, verdict)
    _check_inline(head_sha, inline_comments, threads, touched_paths or {}, untrusted_logins, verdict)
    return verdict


def statuses_as_check_runs(statuses: Iterable[Mapping], head_sha: str) -> list[dict]:
    """Commit statuses (``commits/<sha>/status``) in the shape of check runs, so ``_check_ci`` reads both.

    A status is named ``status:<context>``, so it neither satisfies nor hides a required check run."""
    runs = []
    for s in statuses:
        state = s.get("state")
        runs.append(
            {
                "id": s.get("id", 0),
                "name": f"status:{s.get('context')}",  # its own name space: never shadows a check run
                "head_sha": head_sha,
                "status": "in_progress" if state == "pending" else "completed",
                "conclusion": {"success": "success", "pending": None}.get(state, "failure"),
            }
        )
    return runs


def touched_since(compare: Mapping, sha: str) -> set[str] | None:
    """The files changed from ``sha`` to the head, from a ``compare/<sha>...<head>`` payload, or ``None``.

    The three-dot compare diffs from the merge base, which is ``sha`` only while ``sha`` is an ancestor of
    the head. After a rebase it is the old fork point, and the list would hold at least every file the PR touches, so
    that case is unknown. So is a list that may have been cut at GitHub's cap.
    """
    files = compare.get("files") or []
    if (compare.get("merge_base_commit") or {}).get("sha") != sha or len(files) >= _COMPARE_FILE_CAP:
        return None
    return {f["filename"] for f in files}


# --- CLI: read-only GitHub reads -------------------------------------------------------------------------

_THREADS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $endCursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $endCursor) {
        nodes { isResolved isOutdated resolvedBy { login } comments(first: 1) { nodes { databaseId } } }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""


_COMPARE_JQ = "{merge_base_commit: {sha: .merge_base_commit.sha}, files: [.files[]? | {filename}]} | tojson"


def _gh_lines(*args: str) -> list:
    """Run ``gh api`` and parse its one-JSON-value-per-line output (from a ``--jq`` filter)."""
    run = subprocess.run(["gh", "api", *args], check=True, capture_output=True, encoding="utf-8", errors="replace")
    return [json.loads(line) for line in run.stdout.splitlines() if line.strip()]


def _agent_logins(repo: str) -> list[str] | None:
    """The agent logins: ``NOUS_PR_LOGINS`` from the environment plus the repository variable; ``None`` if neither."""
    values = [os.environ.get("NOUS_PR_LOGINS", "")]
    try:
        values += _gh_lines(f"repos/{repo}/actions/variables/NOUS_PR_LOGINS", "--jq", ".value | tojson")
    except (subprocess.CalledProcessError, ValueError):
        if not values[0].strip():
            return None
    logins = [login.strip() for value in values for login in value.split(",") if login.strip()]
    return list(dict.fromkeys(logins))


def fetch_and_evaluate(repo: str, number: int) -> Verdict:
    """Read the PR's data from GitHub with ``gh api`` (GETs and one GraphQL query) and evaluate it."""
    base = f"repos/{repo}"
    (pr,) = _gh_lines(
        f"{base}/pulls/{number}", "--jq", "{head: .head.sha, author: .user.login, base: .base.ref} | tojson"
    )
    head_sha = pr["head"]
    reviews = _gh_lines("--paginate", f"{base}/pulls/{number}/reviews?per_page=100", "--jq", ".[]")
    inline = _gh_lines("--paginate", f"{base}/pulls/{number}/comments?per_page=100", "--jq", ".[]")
    issue = _gh_lines("--paginate", f"{base}/issues/{number}/comments?per_page=100", "--jq", ".[]")
    runs = _gh_lines("--paginate", f"{base}/commits/{head_sha}/check-runs?per_page=100", "--jq", ".check_runs[]")
    statuses = _gh_lines("--paginate", f"{base}/commits/{head_sha}/status?per_page=100", "--jq", ".statuses[]")
    runs += statuses_as_check_runs(statuses, head_sha)
    owner, name = repo.split("/", 1)
    threads: dict[int, Mapping] | None
    try:
        nodes = _gh_lines(
            "graphql", "--paginate", "-f", f"query={_THREADS_QUERY}", "-f", f"owner={owner}", "-f",
            f"name={name}", "-F", f"number={number}", "--jq", ".data.repository.pullRequest.reviewThreads.nodes[]",
        )  # fmt: skip
        threads = {n["comments"]["nodes"][0]["databaseId"]: n for n in nodes if n["comments"]["nodes"]}
    except subprocess.CalledProcessError:
        threads = None
    touched: dict[str, set[str]] = {}
    shas = {c["original_commit_id"] for c in inline if _is_codex_root(c)}
    for sha in shas - {head_sha}:
        try:
            (compare,) = _gh_lines(f"{base}/compare/{sha}...{head_sha}", "--jq", _COMPARE_JQ)
        except subprocess.CalledProcessError:
            continue  # unknown (e.g. the commit is gone) counts as unchanged
        files = touched_since(compare, sha)
        if files is not None:
            touched[sha] = files
    agents = _agent_logins(repo)
    verdict = evaluate_gate(
        reviews,
        inline,
        head_sha,
        runs,
        threads=threads,
        issue_comments=issue,
        touched_paths=touched,
        untrusted_logins={pr["author"], *(agents or ())},
    )
    verdict.repo, verdict.base = repo, pr["base"]
    if agents is None:
        verdict.reasons.append("NOUS_PR_LOGINS unreadable, so an agent's own resolutions and replies would count")
    return verdict


def main(argv: list[str] | None = None) -> int:
    """Print the verdict line for one PR; return 0 for PASS, 1 for FAIL (including unreadable data)."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("pr", type=int, help="pull request number")
    parser.add_argument("--repo", default="tfatykhov/nous", help="owner/name (default: %(default)s)")
    args = parser.parse_args(argv)
    if not _REPO.fullmatch(args.repo) or {".", ".."} & set(args.repo.split("/")):
        print(f"GATE: FAIL head=unknown repo={args.repo} invalid --repo, expected owner/name")
        return 1
    try:
        verdict = fetch_and_evaluate(args.repo, args.pr)
    except Exception as exc:  # any failure to read or parse the PR must print FAIL, never a traceback
        detail = (getattr(exc, "stderr", "") or str(exc)).strip()[:300]
        print(f"GATE: FAIL head=unknown could not read PR #{args.pr}: {type(exc).__name__}: {detail}")
        return 1
    print(verdict.line())
    return 0 if verdict.passed else 1


if __name__ == "__main__":
    sys.exit(main())
