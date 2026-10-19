"""Built-in completion checks for DAG nodes, run by the orchestrator like any shell ``completion_check``.

    python -m nous.dag.typed_checks pr <owner/repo> (--pr <n> | --head <branch>)

``pr`` passes once the pull request exists, open or merged, because a merge can take longer than a node may wait.
``--head`` names it by its head branch, for a node created before the PR is: the newest PR on ``<owner>:<branch>``,
and only one opened since the node's DAG was created (the orchestrator passes ``NOUS_DAG_CREATED_AT``), so a
branch name an older PR used cannot settle the node first.
Exit codes follow the completion_check contract:
- 0: the PR exists;
- 1: it was closed without a merge, or the arguments are wrong (both are final);
- 2: not found yet, or GitHub could not answer (an outage never fails a node).

``GITHUB_TOKEN`` from the environment is used when set. Whether the work was accepted is not decided here: the task
scorer reads the PR from GitHub itself (``nous/observability/github_verify.py``).
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Sequence
from datetime import UTC, datetime

import httpx

GITHUB_API = "https://api.github.com"
REQUEST_TIMEOUT = 10.0
EXISTS, FAILED, PENDING = 0, 1, 2
_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_BRANCH = re.compile(r"[A-Za-z0-9._/-]{1,200}")


class _UsageError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # argparse would exit 2, which reads as "still pending"
        raise _UsageError(message)


def _parser() -> _Parser:
    # No --help: it would exit 0, which reads as a passed check.
    parser = _Parser(prog="python -m nous.dag.typed_checks", add_help=False)
    sub = parser.add_subparsers(dest="kind", required=True)
    pr = sub.add_parser("pr", add_help=False)
    pr.add_argument("repo")
    which = pr.add_mutually_exclusive_group(required=True)
    which.add_argument("--pr", type=int)
    which.add_argument("--head")
    return parser


def _client(transport: httpx.BaseTransport | None) -> httpx.Client:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    # A renamed or transferred repository answers its old name with a redirect.
    return httpx.Client(
        base_url=GITHUB_API, headers=headers, timeout=REQUEST_TIMEOUT, transport=transport, follow_redirects=True
    )


def _dag_created_at() -> datetime | None:
    """When this node's DAG was created: the orchestrator passes it to every completion check."""
    value = os.environ.get("NOUS_DAG_CREATED_AT", "").strip()
    try:
        parsed = datetime.fromisoformat(value) if value else None
    except ValueError:
        return None
    if parsed is not None and parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _opened(pull: dict) -> datetime | None:
    value = pull.get("created_at")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else None
    except ValueError:
        return None


def _state(pull: dict) -> tuple[int, str]:
    number = pull.get("number")
    if pull.get("merged_at"):
        return EXISTS, f"PR #{number} is merged"
    if pull.get("state") == "open":
        return EXISTS, f"PR #{number} is open"
    return FAILED, f"PR #{number} was closed without a merge"


def check_pr(
    repo: str, number: int | None, head: str | None, *, transport: httpx.BaseTransport | None = None
) -> tuple[int, str]:
    """(exit code, one-line reason) for the ``pr`` check."""
    try:
        with _client(transport) as client:
            if number is not None:
                response = client.get(f"/repos/{repo}/pulls/{number}")
                if response.status_code == 404:
                    return PENDING, f"no PR #{number} in {repo} yet"
                response.raise_for_status()
                return _state(response.json())
            # A transferred repository's branches are under its new owner: filter on the repository as it is now.
            found = client.get(f"/repos/{repo}")
            if found.status_code == 404:
                return PENDING, f"no repository {repo} visible yet"
            found.raise_for_status()
            repo = found.json().get("full_name") or repo
            owner = repo.split("/", 1)[0]
            response = client.get(
                f"/repos/{repo}/pulls",
                params={
                    "state": "all",
                    "head": f"{owner}:{head}",
                    "sort": "created",
                    "direction": "desc",
                    "per_page": 1,
                },
            )
            response.raise_for_status()
            pulls = response.json()
            if not pulls:
                return PENDING, f"no PR from {owner}:{head} in {repo} yet"
            since, opened = _dag_created_at(), _opened(pulls[0])
            if since is not None and (opened is None or opened < since):
                # The branch name was used by an older PR: this DAG's own PR is not there yet.
                return PENDING, f"no PR from {owner}:{head} in {repo} opened since this DAG started"
            return _state(pulls[0])
    except Exception as exc:  # noqa: BLE001 - anything unexpected is "ask again", never a definite failure
        # An outage, a non-JSON body, JSON of an unexpected shape: only a PR closed without a merge (or bad
        # arguments) may fail the node, so everything else reads as "not decided yet".
        return PENDING, f"GitHub did not answer as expected ({type(exc).__name__}); will retry"


def main(argv: Sequence[str] | None = None, *, transport: httpx.BaseTransport | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        if not _REPO.fullmatch(args.repo) or any(part in (".", "..") for part in args.repo.split("/")):
            raise _UsageError(f"repo must be owner/name, got {args.repo!r}")
        if args.pr is not None and args.pr < 1:
            raise _UsageError("--pr must be a positive number")
        if args.head is not None and not _BRANCH.fullmatch(args.head):
            raise _UsageError(f"--head is not a branch name: {args.head!r}")
    except _UsageError as exc:
        print(f"typed_checks: {exc}", file=sys.stderr)
        return FAILED
    code, reason = check_pr(args.repo, args.pr, args.head, transport=transport)
    print(reason, file=sys.stderr if code == FAILED else sys.stdout)
    return code


if __name__ == "__main__":
    sys.exit(main())
