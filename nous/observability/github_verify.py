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
        self._failed: dict[str, str] = {}
        self._stopped: str | None = None

    async def aclose(self) -> None:
        await self._client.aclose()

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


def _known_number(check: PrCheck, task_id: str, stored: StoredPrEvidence) -> int | None:
    """The PR number without asking GitHub: ``--pr``, or the number an earlier pass pinned for ``--head``."""
    if check.number is not None:
        return check.number
    repo, _, pinned = stored.pins.get(task_id, "").rpartition("#")
    return int(pinned) if repo == check.repo.lower() and pinned.isdigit() else None


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
        number = _known_number(check, signals.task_id, stored)
        if number is not None:
            key = _pr_key(check.repo, number)
            evidence["v2_pr"] = [key]
            holder = stored.credited.get(key)
            if holder == signals.task_id:
                return credit(key)  # an earlier pass verified it: a merge is final, so GitHub is not asked again
            if holder is not None:
                return outcome("v2_pr_duplicate", holder)  # another leaf took it for good: no call needed
        try:
            if number is None:  # --head: resolve once; the pin keeps a newer PR on the branch from replacing it
                number = await self._reader.newest_pull_number(check.repo, check.head or "")
                if number is None:
                    return outcome("v2_rejected", "no such PR")
                evidence["v2_pr"] = [_pr_key(check.repo, number)]
            pull = await self._reader.pull(check.repo, number)
        except GitHubUnavailable as exc:
            return outcome("v2_unavailable", str(exc), v2_hold=True)
        if pull is None:
            return outcome("v2_rejected", "no such PR")
        # The key is the repository GitHub answered for, so two spellings of it (letter case, a renamed repository
        # that redirects) are one PR.
        base = pull.get("base")
        canonical = (base.get("repo") or {}).get("full_name") if isinstance(base, dict) else None
        key = _pr_key(canonical if isinstance(canonical, str) else check.repo, pull.get("number", number))
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
