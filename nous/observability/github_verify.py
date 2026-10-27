"""Task scoring v4: verify a PR check against GitHub, from the scorer itself.

A node whose ``completion_check`` is the PR check (``python -m nous.dag.typed_checks pr ...``) passes once its PR
exists, because a merge takes longer than a node may wait. Whether the work was accepted is decided here, by the
scoring job reading the PR from GitHub. The orchestrator's pass is Nous's own harness, and a row in its database is
something Nous could write; the PR on GitHub is not.

``v2_verified`` iff all of:
- the PR's author is one of the agent's logins (``agent_logins``);
- it was opened at or after the DAG was created;
- it is merged, by one of the accepted mergers (``merger_logins``, the owner). The list is an allowlist: a merge by
  the agent, by any ``[bot]`` login, or by anyone else confirms nothing.

One PR credits at most one leaf, ever:
- the stored rows decide first. A credit already given stands, and is not read from GitHub again: a merge is final.
- then the earliest leaf of the pass, by (DAG created_at, node id).

When the PR cannot be decided yet (still open, GitHub unreachable or rate-limited, the pass's call cap reached),
``v2_hold`` keeps the row provisional for a bounded time (``task_scoring.V2_HOLD_EXTRA_DAYS``). The reader remembers
failures as well as answers for the pass, so a later leaf naming the same PR is held too and cannot take it from the
earlier one. Anything else earns nothing and says why in the evidence. Nothing here writes: the job
scores what this returns.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

import httpx

from nous.observability.task_scoring import PrCheck, TaskSignals

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
GITHUB_CALL_CAP = 60  # requests per scoring pass; past it, the remaining PRs are held, not decided
REQUEST_TIMEOUT = 10.0
_RATE_LIMITED = frozenset({403, 429})  # GitHub's answer to a spent rate limit: stop asking for this pass
_LATE = datetime.max.replace(tzinfo=UTC)


class GitHubUnavailable(Exception):
    """GitHub could not answer this pass (transport, HTTP status, or the call cap). The message is the reason."""


class GitHubReader:
    """Read-only GitHub REST client for one pass, with a call cap and a memo. An empty token reads anonymously.

    Failures are remembered as well as answers, so every leaf of a pass that asks the same question gets the same
    result. A rate-limit answer (403 or 429) ends the pass's asking altogether.
    """

    def __init__(
        self,
        token: str | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        call_cap: int = GITHUB_CALL_CAP,
        user_agent: str = "nous-task-scorer",
        timeout: float = REQUEST_TIMEOUT,
    ) -> None:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": user_agent,
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.AsyncClient(  # follow_redirects: a renamed repository redirects its old name
            base_url=GITHUB_API, headers=headers, timeout=timeout, transport=transport, follow_redirects=True
        )
        self._call_cap = call_cap
        self.calls = 0
        self._memo: dict[str, Any] = {}
        self._failed: dict[str, str] = {}
        self._stopped: str | None = None

    async def aclose(self) -> None:
        await self._client.aclose()

    @property
    def rate_limited(self) -> bool:
        """Whether GitHub answered one of this reader's requests with a rate limit (403 or 429): it asks no more."""
        return self._stopped is not None

    async def _get(self, path: str, params: Mapping[str, str | int] | None = None) -> Any:
        key = f"{path}?{urlencode(sorted((params or {}).items()))}"
        if key in self._memo:
            return self._memo[key]
        if key in self._failed:
            raise GitHubUnavailable(self._failed[key])
        if self._stopped is not None:
            raise GitHubUnavailable(self._stopped)
        if self.calls >= self._call_cap:
            raise GitHubUnavailable("call cap reached")
        self.calls += 1
        try:
            response = await self._client.get(path, params=dict(params or {}))
        except httpx.HTTPError as exc:
            self._failed[key] = type(exc).__name__
            raise GitHubUnavailable(self._failed[key]) from exc
        if response.status_code == 404:
            value = None
        elif response.status_code != 200:
            self._failed[key] = f"HTTP {response.status_code}"
            if response.status_code in _RATE_LIMITED:
                self._stopped = self._failed[key]
            elif response.status_code == 401:
                logger.warning("Task scores: GitHub refused NOUS_SCORER_GITHUB_TOKEN (HTTP 401)")
            raise GitHubUnavailable(self._failed[key])
        else:
            try:
                value = response.json()
            except ValueError as exc:  # a 200 whose body is not JSON (a proxy page, a truncated answer)
                self._failed[key] = "malformed response"
                raise GitHubUnavailable(self._failed[key]) from exc
        self._memo[key] = value
        return value

    async def _shaped(self, path: str, params: Mapping[str, str | int] | None, kind: type) -> Any:
        """``_get``, with the answer's shape checked: None is GitHub's 404; any other shape than ``kind`` (a proxy's
        JSON, a changed API) is unavailable, so a row is held rather than decided on an answer that is not one."""
        value = await self._get(path, params)
        if value is None or isinstance(value, kind):
            return value
        raise GitHubUnavailable("unexpected response")

    async def pull(self, repo: str, number: int) -> dict[str, Any] | None:
        """The PR, or None when there is no such PR."""
        return await self._shaped(f"/repos/{repo}/pulls/{number}", None, dict)

    async def check_runs(self, repo: str, sha: str) -> dict[str, Any] | None:
        """The first page (100) of check runs on commit ``sha``, or None on a 404."""
        return await self._shaped(f"/repos/{repo}/commits/{sha}/check-runs", {"per_page": 100}, dict)

    async def reviews(self, repo: str, number: int) -> list[Any] | None:
        """The first page (100) of the PR's reviews, oldest first, or None on a 404."""
        return await self._shaped(f"/repos/{repo}/pulls/{number}/reviews", {"per_page": 100}, list)

    async def canonical_repo(self, repo: str) -> str:
        """The repository's current ``owner/name``: a transferred repository's branches are under its new owner."""
        value = await self._shaped(f"/repos/{repo}", None, dict)
        name = value.get("full_name") if value is not None else None
        return name if isinstance(name, str) else repo

    async def pull_number_for_head(self, repo: str, head: str, since: datetime, until: datetime | None) -> int | None:
        """The PR the node's check passed on: the newest whose head is ``<owner>:<head>`` (owner of the repository as
        it is now) opened in [``since``, ``until``], the DAG's start and the node's completion, or None.

        The check itself takes the newest PR on the branch opened since its DAG began, and the node completes on the
        poll that sees it, so the same rule bounded by the node's completion finds the same PR. A branch name a later
        DAG reuses gets a PR opened after that, which is the later task's.
        """
        canonical = await self.canonical_repo(repo)
        owner = canonical.split("/", 1)[0]
        value = await self._shaped(
            f"/repos/{canonical}/pulls",
            {"state": "all", "head": f"{owner}:{head}", "sort": "created", "direction": "desc", "per_page": 100},
            list,
        )
        for item in value or ():
            opened = _when(item.get("created_at")) if isinstance(item, dict) else None
            if opened is None or not isinstance(item.get("number"), int) or opened < since:
                continue
            if until is None or opened <= until:
                return item["number"]
        return None


@dataclass(frozen=True)
class StoredPrEvidence:
    """What earlier passes recorded (scoring.task_scores, scorer v4 and later). Only the scorer writes that schema."""

    pins: Mapping[str, str] = field(default_factory=dict)  # task id -> "owner/repo#N" its --head resolved to
    credited: Mapping[str, str] = field(default_factory=dict)  # "owner/repo#N" -> the task id it credited


def stored_pr_evidence(rows: Iterable[tuple[str, Mapping[str, Any] | None]]) -> StoredPrEvidence:
    """Fold stored (task id, evidence) rows, oldest first, into pins and credits. The first credit of a PR stands."""
    pins: dict[str, str] = {}
    credited: dict[str, str] = {}
    for task_id, evidence in rows:
        if not isinstance(evidence, Mapping):
            continue
        pin = evidence.get("v2_pr")
        if isinstance(pin, list) and pin and isinstance(pin[0], str):
            pins[task_id] = pin[0].lower()
        for key in evidence.get("v2_pr_merged") or ():
            if isinstance(key, str):
                credited.setdefault(key.lower(), task_id)
    return StoredPrEvidence(pins=pins, credited=credited)


def _when(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _login(user: Any) -> str | None:
    login = user.get("login") if isinstance(user, dict) else None
    return login.lower() if isinstance(login, str) else None


def _logins(values: Iterable[str]) -> frozenset[str]:
    return frozenset(login.strip().lower() for login in values if login.strip())


def _pr_key(repo: str, number: Any) -> str:
    """One PR's key: GitHub matches owner and repository names case-insensitively, so the key is lower-case."""
    return f"{repo.lower()}#{number}"


def _known_pr(check: PrCheck, task_id: str, stored: StoredPrEvidence) -> tuple[str, int] | None:
    """(repository, number) without asking GitHub: what an earlier pass pinned for this task, else ``--pr``.

    A pin belongs to this task (only the scorer writes it, and a node's check does not change), and it names the
    repository GitHub answered for, so it is used whatever spelling the command has: a renamed repository's old
    name would otherwise re-resolve the branch every pass and let a newer PR on it replace the pinned one.
    """
    repo, _, pinned = stored.pins.get(task_id, "").rpartition("#")
    if repo and pinned.isdigit() and (check.number is None or int(pinned) == check.number):
        return repo, int(pinned)
    if check.number is not None:
        return check.repo, check.number
    return None


class PrVerifier:
    """Decides the PR checks of one pass. ``agent_logins`` are the GitHub logins Nous opens PRs as; ``merger_logins``
    the logins whose merge confirms the work (the owner). Both empty or either empty: nothing is verified."""

    def __init__(self, reader: GitHubReader, agent_logins: Iterable[str], merger_logins: Iterable[str]) -> None:
        self._reader = reader
        self._agents = _logins(agent_logins)
        self._mergers = _logins(merger_logins) - self._agents

    async def verify(self, leaves: Sequence[TaskSignals], stored: StoredPrEvidence) -> list[TaskSignals]:
        """``leaves`` with every PR check decided; the rest unchanged, in the same order."""
        checked = [s for s in leaves if s.pr_check is not None]
        if not checked or not self._agents or not self._mergers:
            return list(leaves)
        taken = dict(stored.credited)
        decided: dict[str, TaskSignals] = {}
        for signals in sorted(checked, key=lambda s: (s.dag_created_at or _LATE, s.node_id or "", s.task_id)):
            decided[signals.task_id] = await self._decide(signals, stored, taken)
        return [decided.get(s.task_id, s) for s in leaves]

    async def _decide(self, signals: TaskSignals, stored: StoredPrEvidence, taken: dict[str, str]) -> TaskSignals:
        check = signals.pr_check
        assert check is not None
        evidence = {key: list(ids) for key, ids in signals.evidence.items()}

        def outcome(reason_key: str, reason: str, **flags: bool) -> TaskSignals:
            evidence[reason_key] = [reason]
            return replace(signals, evidence=evidence, **flags)

        def credit(key: str) -> TaskSignals:
            taken[key] = signals.task_id
            evidence["v2_pr_merged"] = [key]
            return replace(signals, evidence=evidence, v2_verified=True)

        if signals.dag_created_at is None:
            return outcome("v2_rejected", "no DAG creation time")
        known = _known_pr(check, signals.task_id, stored)
        repo, number = known if known is not None else (check.repo, None)
        if number is not None:
            key = _pr_key(repo, number)
            evidence["v2_pr"] = [key]
            holder = stored.credited.get(key)
            if holder == signals.task_id:
                return credit(key)  # an earlier pass verified it: a merge is final, so GitHub is not asked again
            if holder is not None:
                return outcome("v2_pr_duplicate", holder)  # another leaf took it for good: no call needed
        try:
            if number is None:  # --head: resolve once; the pin keeps a newer PR on the branch from replacing it
                number = await self._reader.pull_number_for_head(
                    check.repo, check.head or "", signals.dag_created_at, signals.node_completed_at
                )
                if number is None:
                    return outcome("v2_rejected", "no such PR")
                evidence["v2_pr"] = [_pr_key(check.repo, number)]
            pull = await self._reader.pull(repo, number)
        except GitHubUnavailable as exc:
            return outcome("v2_unavailable", str(exc), v2_hold=True)
        if pull is None:
            return outcome("v2_rejected", "no such PR")
        # The key is the repository GitHub answered for, so two spellings of it (letter case, a renamed repository
        # that redirects) are one PR.
        base = pull.get("base")
        canonical = (base.get("repo") or {}).get("full_name") if isinstance(base, dict) else None
        key = _pr_key(canonical if isinstance(canonical, str) else repo, pull.get("number", number))
        evidence["v2_pr"] = [key]
        if _login(pull.get("user")) not in self._agents:
            return outcome("v2_rejected", "not opened by the agent")
        opened = _when(pull.get("created_at"))
        if opened is None:
            return outcome("v2_rejected", "no opening time")
        if opened < signals.dag_created_at:
            return outcome("v2_rejected", "opened before the DAG")
        if not pull.get("merged_at"):
            if pull.get("state") == "open":
                return outcome("v2_pending", "open", v2_hold=True)
            return outcome("v2_rejected", "closed without a merge")
        merger = _login(pull.get("merged_by"))
        if merger is None:
            return outcome("v2_rejected", "no merger")
        if merger in self._agents:
            return outcome("v2_rejected", "merged by the agent")
        if merger.endswith("[bot]") or merger not in self._mergers:
            return outcome("v2_rejected", "not merged by an accepted login")
        holder = taken.get(key)
        if holder is not None and holder != signals.task_id:
            return outcome("v2_pr_duplicate", holder)
        return credit(key)
