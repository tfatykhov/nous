#!/usr/bin/env python3
"""Fail a pull request that changes a protected path under the agent's identity.

The protected paths are the ones in ``.github/CODEOWNERS``: this script reads that file, so the list is
kept in one place. CODEOWNERS patterns are limited here to a subset whose meaning is the same for GitHub
and for this script: rooted (``/``-prefixed) paths where ``*`` stays inside one path segment and ``**``
appears only as a trailing ``/**`` (everything below a directory, the same as a trailing ``/``). Anything
else makes the script fail, so the two readings cannot drift apart. A pattern without a trailing ``/`` or
``/**`` must not name a directory (GitHub would also cover what is below it); tests/test_protected_paths.py
checks that against the tracked files.

A pull request is flagged when either

* it was opened by one of ``NOUS_PR_LOGINS`` and its net change (merge base to head) touches a protected
  path, or
* a commit in the range was authored or committed under one of ``NOUS_COMMIT_EMAILS`` and that commit
  touches a protected path (a merge commit counts against each parent).

Exit status: 0 clean, 1 flagged, 2 unusable input.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

_SUPPORTED = re.compile(r"^/[A-Za-z0-9._\-/*]+$")


class UnsupportedPattern(ValueError):
    """A CODEOWNERS pattern outside the subset this script understands."""


def parse_codeowners(text: str) -> list[str]:
    """Return the path patterns of a CODEOWNERS file, in order. Raises UnsupportedPattern."""
    patterns: list[str] = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        pattern = line.split()[0]
        if not _SUPPORTED.match(pattern) or "**" in pattern.removesuffix("/**"):
            raise UnsupportedPattern(f"CODEOWNERS line {lineno}: unsupported pattern {pattern!r}")
        patterns.append(pattern)
    return patterns


def pattern_regex(pattern: str) -> re.Pattern[str]:
    """Compile one rooted pattern for ``fullmatch``. A trailing ``/`` or ``/**`` means everything below the
    directory."""
    body = pattern[1:]
    if body.endswith("/"):
        body += "**"
    out: list[str] = []
    i = 0
    while i < len(body):
        if body.startswith("**", i):
            out.append(".*")
            i += 2
        elif body[i] == "*":
            out.append("[^/]*")
            i += 1
        else:
            out.append(re.escape(body[i]))
            i += 1
    return re.compile("".join(out), re.DOTALL)


def protected(paths: Iterable[str], patterns: Sequence[str]) -> list[str]:
    """The paths that match at least one pattern."""
    compiled = [pattern_regex(p) for p in patterns]
    return sorted({p for p in paths if any(rx.fullmatch(p) for rx in compiled)})


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def net_changed_paths(base: str, head: str) -> list[str]:
    """Paths changed between the merge base and head. Renames are reported as a delete plus an add."""
    return [p for p in _git("diff", "--name-only", "--no-renames", "-z", f"{base}...{head}").split("\0") if p]


def commit_changed_paths(commit: str) -> list[str]:
    """Paths one commit changes (against each parent for a merge commit)."""
    out = _git("diff-tree", "-r", "-m", "--root", "--no-commit-id", "--name-only", "--no-renames", "-z", commit)
    return [p for p in out.split("\0") if p]


def commits_by(emails: Sequence[str], base: str, head: str) -> list[str]:
    """Commits in base..head whose author or committer email is in ``emails`` (case-insensitive)."""
    wanted = {e.lower() for e in emails if e}
    flagged: list[str] = []
    # NUL between the fields and records split on \n alone: an email can hold a space, or a character
    # that splitlines() would break on.
    out = _git("log", "--no-show-signature", "--format=%H%x00%ae%x00%ce", f"{base}..{head}")
    for line in out.split("\n"):
        if not line:
            continue
        sha, author, committer = line.split("\0")
        if author.lower() in wanted or committer.lower() in wanted:
            flagged.append(sha)
    return flagged


def violations(
    patterns: Sequence[str], base: str, head: str, emails: Sequence[str], pr_login: str, logins: Sequence[str]
) -> list[str]:
    """Human-readable findings; empty when the pull request is clean."""
    findings: list[str] = []
    if pr_login and pr_login.lower() in {x.lower() for x in logins if x}:
        for path in protected(net_changed_paths(base, head), patterns):
            findings.append(f"opened by {pr_login}: changes {path}")
    for sha in commits_by(emails, base, head):
        for path in protected(commit_changed_paths(sha), patterns):
            findings.append(f"commit {sha[:10]} (agent identity): changes {path}")
    return findings


def _split(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--codeowners", default=".github/CODEOWNERS")
    parser.add_argument("--base", required=True, help="the base branch ref")
    parser.add_argument("--head", required=True, help="the pull request head ref")
    parser.add_argument("--pr-author", default="", help="login that opened the pull request")
    args = parser.parse_args(argv)
    emails = _split(os.environ.get("NOUS_COMMIT_EMAILS", ""))
    logins = _split(os.environ.get("NOUS_PR_LOGINS", ""))
    if not emails:
        print("protected_paths: NOUS_COMMIT_EMAILS is empty", file=sys.stderr)
        return 2
    if not logins:
        print(
            "::notice::NOUS_PR_LOGINS is empty: only the commit-email rule is active, and a committer can change "
            "its email. Set the repository variable to the agent account's login."
        )
    try:
        patterns = parse_codeowners(Path(args.codeowners).read_text(encoding="utf-8-sig"))
        findings = violations(patterns, args.base, args.head, emails, args.pr_author, logins)
    except (OSError, UnsupportedPattern, subprocess.CalledProcessError) as exc:
        print(f"protected_paths: {exc}", file=sys.stderr)
        return 2
    for finding in findings:
        print(f"::error::protected path: {finding}")
    if findings:
        print(
            f"protected_paths: {len(findings)} finding(s). A change under the agent's identity to a path in "
            "CODEOWNERS needs the owner to make it."
        )
        return 1
    print("protected_paths: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
