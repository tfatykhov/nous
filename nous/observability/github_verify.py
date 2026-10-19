"""Task scoring v4: verify a PR check against GitHub, from the scorer itself.

A node whose ``completion_check`` is the PR check (``python -m nous.dag.typed_checks pr ...``) passes once its PR
exists, because a merge takes longer than a node may wait. Whether the work was accepted is decided here, by the
scoring job reading the PR from GitHub. The orchestrator's pass is Nous's own harness, and a row in its database is
something Nous could write; the PR on GitHub is not.

``v2_verified`` iff all of:
- the PR's author is one of the agent's logins;
- it was opened at or after the DAG was created;
- it is merged, by a login that is not the agent's.

One PR credits at most one leaf, ever:
- the stored rows decide first (a credit already given stands);
- then the earliest leaf of the pass, by (DAG created_at, node id).

When the PR cannot be decided yet (still open, GitHub unreachable, or the pass's call cap reached), ``v2_hold`` keeps
the row provisional for a bounded time (``task_scoring.V2_HOLD_EXTRA_DAYS``). Anything else earns nothing and says why
in the evidence. Nothing here writes: the job scores what this returns.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

import httpx

from nous.observability.task_scoring import TaskSignals

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
GITHUB_CALL_CAP = 60  # requests per scoring pass; past it, the remaining PRs are held, not decided
REQUEST_TIMEOUT = 10.0
_LATE = datetime.max.replace(tzinfo=UTC)


class GitHubUnavailable(Exception):
    """GitHub could not answer this pass (transport, HTTP status, or the call cap). The message is the reason."""


class GitHubReader:
    """Read-only GitHub REST client for one pass, with a call cap and a memo. An empty token reads anonymously."""

    def __init__(
        self,
        token: str | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        call_cap: int = GITHUB_CALL_CAP,
    ) -> None:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "nous-task-scorer",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.AsyncClient(
            base_url=GITHUB_API, headers=headers, timeout=REQUEST_TIMEOUT, transport=transport
        )
        self._call_cap = call_cap
        self.calls = 0
        self._memo: dict[str, Any] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: Mapping[str, str | int] | None = None) -> Any:
        key = f"{path}?{urlencode(sorted((params or {}).items()))}"
        if key in self._memo:
            return self._memo[key]
        if self.calls >= self._call_cap:
            raise GitHubUnavailable("call cap reached")
        self.calls += 1
        try:
            response = await self._client.get(path, params=dict(params or {}))
        except httpx.HTTPError as exc:
            raise GitHubUnavailable(type(exc).__name__) from exc
        if response.status_code == 404:
            value = None
        elif response.status_code != 200:
            raise GitHubUnavailable(f"HTTP {response.status_code}")
        else:
            value = response.json()
        self._memo[key] = value
        return value

    async def pull(self, repo: str, number: int) -> dict[str, Any] | None:
        """The PR, or None when there is no such PR."""
        value = await self._get(f"/repos/{repo}/pulls/{number}")
        return value if isinstance(value, dict) else None

    async def newest_pull_number(self, repo: str, head: str) -> int | None:
        """The number of the newest PR whose head is ``<owner>:<head>``, or None."""
        owner = repo.split("/", 1)[0]
        value = await self._get(
            f"/repos/{repo}/pulls",
            {"state": "all", "head": f"{owner}:{head}", "sort": "created", "direction": "desc", "per_page": 1},
        )
        if isinstance(value, list) and value and isinstance(value[0], dict) and isinstance(value[0].get("number"), int):
            return value[0]["number"]
        return None


@dataclass(frozen=True)
class StoredPrEvidence:
    """What earlier passes recorded (scoring.task_scores, scorer v4 and later)."""

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
            pins[task_id] = pin[0]
        for key in evidence.get("v2_pr_merged") or ():
            if isinstance(key, str):
                credited.setdefault(key, task_id)
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


class PrVerifier:
    """Decides the PR checks of one pass. ``agent_logins`` are the GitHub logins Nous opens PRs as."""

    def __init__(self, reader: GitHubReader, agent_logins: Iterable[str]) -> None:
        self._reader = reader
        self._agents = frozenset(login.strip().lower() for login in agent_logins if login.strip())

    async def verify(self, leaves: Sequence[TaskSignals], stored: StoredPrEvidence) -> list[TaskSignals]:
        """``leaves`` with every PR check decided; the rest unchanged, in the same order."""
        checked = [s for s in leaves if s.pr_check is not None]
        if not checked or not self._agents:
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

        try:
            number = check.number
            if number is None:  # --head: resolved once, then pinned so a newer PR on the branch cannot replace it
                pin = stored.pins.get(signals.task_id, "")
                repo, _, pinned = pin.rpartition("#")
                if repo == check.repo and pinned.isdigit():
                    number = int(pinned)
                else:
                    number = await self._reader.newest_pull_number(check.repo, check.head or "")
            pull = await self._reader.pull(check.repo, number) if number is not None else None
        except GitHubUnavailable as exc:
            return outcome("v2_unavailable", str(exc), v2_hold=True)
        if pull is None:
            return outcome("v2_rejected", "no such PR")
        key = f"{check.repo}#{pull.get('number', number)}"
        evidence["v2_pr"] = [key]
        if _login(pull.get("user")) not in self._agents:
            return outcome("v2_rejected", "not opened by the agent")
        opened = _when(pull.get("created_at"))
        if opened is None:
            return outcome("v2_rejected", "no opening time")
        if signals.dag_created_at is not None and opened < signals.dag_created_at:
            return outcome("v2_rejected", "opened before the DAG")
        if not pull.get("merged_at"):
            if pull.get("state") == "open":
                return outcome("v2_pending", "open", v2_hold=True)
            return outcome("v2_rejected", "closed without a merge")
        merger = _login(pull.get("merged_by"))
        if merger is None or merger in self._agents:
            return outcome("v2_rejected", "merged by the agent")
        holder = taken.get(key)
        if holder is not None and holder != signals.task_id:
            return outcome("v2_pr_duplicate", holder)
        taken[key] = signals.task_id
        evidence["v2_pr_merged"] = [key]
        return replace(signals, evidence=evidence, v2_verified=True)
