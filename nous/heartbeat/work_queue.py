"""F064.6: work-queue ingress heartbeat check + adapter layer.

The adapter pattern is an ABC (not a Protocol). Note that F033's
SearchProvider is a typing.Protocol — F064 spec said the work-queue
adapter would be "modeled on F033" but verified that's wrong: F033
is structural subtyping, not registration. F064.6 chose the BaseCheck
pattern (ABC) for consistency with the heartbeat registry.

v1 ships `FileJsonlAdapter` only. `GithubIssuesAdapter` and
`LinearAdapter` are stubs that raise NotImplementedError until v2.
"""

from __future__ import annotations

import hashlib
import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from nous.heartbeat.registry import BaseCheck
from nous.heartbeat.schemas import CheckResult, Finding

if TYPE_CHECKING:
    from nous.config import Settings
    from nous.dag.orchestrator import DAGOrchestrator
    from nous.dag.store import DAGStore
    from nous.heart.work_queue import WorkQueueItemManager

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Adapter contract
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkItem:
    """A single work-queue item as surfaced by an adapter."""

    external_id: str
    title: str
    body: str
    state: str  # adapter-native state name
    terminal: bool
    payload: dict = field(default_factory=dict)


class WorkQueueAdapter(ABC):
    """Base class for work-queue ingress adapters.

    Subclasses implement `source_name` and `list_active` (and optionally
    `get_state` if state queries are needed for reconciliation).
    """

    DEFAULT_FRAME_TYPE: str = "research"

    @property
    @abstractmethod
    def source_name(self) -> str:
        ...

    @abstractmethod
    async def list_active(self) -> list[WorkItem]:
        """Return all currently active (non-terminal) work items the
        adapter knows about, plus any items whose terminal-state should
        trigger DAG cancellation (those have terminal=True)."""
        ...


class FileJsonlAdapter(WorkQueueAdapter):
    """Smallest-useful adapter: reads a JSONL file once per `list_active`.

    Each line is parsed as a WorkItem-shaped dict:
        {"external_id": "...", "title": "...", "body": "...",
         "state": "open" | "closed", "terminal": false, "payload": {}}

    `external_id` falls back to a sha256 of the body so operators don't
    have to compute it manually; that hash is also stable across runs.
    """

    source_name: str = "file_jsonl"

    def __init__(self, path: str) -> None:
        self._path = Path(path)

    async def list_active(self) -> list[WorkItem]:
        if not self._path.exists():
            logger.warning(
                "F064.6: file_jsonl path does not exist: %s", self._path,
            )
            return []
        items: list[WorkItem] = []
        try:
            content = self._path.read_text(encoding="utf-8")
        except OSError as e:
            logger.warning(
                "F064.6: failed to read %s: %s", self._path, e,
            )
            return []
        for line_no, raw in enumerate(content.splitlines(), start=1):
            raw = raw.strip()
            if not raw or raw.startswith("#"):
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError as e:
                logger.warning(
                    "F064.6: skipping invalid JSONL at line %d: %s",
                    line_no, e,
                )
                continue
            if not isinstance(obj, dict):
                continue
            external_id = obj.get("external_id")
            body = obj.get("body", "")
            if not external_id:
                # Derive a stable ID from body if author didn't supply one.
                external_id = hashlib.sha256(
                    body.encode("utf-8")
                ).hexdigest()[:16]
            items.append(WorkItem(
                external_id=str(external_id),
                title=str(obj.get("title", external_id)),
                body=str(body),
                state=str(obj.get("state", "open")),
                terminal=bool(obj.get("terminal", False)),
                payload=obj if isinstance(obj, dict) else {},
            ))
        return items


class GithubIssuesAdapter(WorkQueueAdapter):
    """v2 stub. F064.6-v2 will implement GitHub issues ingestion."""

    source_name: str = "github_issues"

    async def list_active(self) -> list[WorkItem]:
        raise NotImplementedError(
            "F064.6-v2: github_issues adapter not yet implemented; "
            "use NOUS_WORK_QUEUE_SOURCE=file_jsonl in v1."
        )


class LinearAdapter(WorkQueueAdapter):
    """v2 stub. F064.6-v2 will implement Linear issues ingestion."""

    source_name: str = "linear"

    async def list_active(self) -> list[WorkItem]:
        raise NotImplementedError(
            "F064.6-v2: linear adapter not yet implemented; "
            "use NOUS_WORK_QUEUE_SOURCE=file_jsonl in v1."
        )


def build_adapter(settings: "Settings") -> WorkQueueAdapter:
    """Factory: instantiate the configured adapter."""
    source = settings.work_queue_source
    if source == "file_jsonl":
        return FileJsonlAdapter(settings.work_queue_file_jsonl_path)
    if source == "github_issues":
        return GithubIssuesAdapter()
    if source == "linear":
        return LinearAdapter()
    raise ValueError(f"Unknown work_queue_source: {source!r}")


# ---------------------------------------------------------------------------
# Heartbeat check
# ---------------------------------------------------------------------------


# Grace window before the reconciler picks up an orphan. Plan §9.3 + plan
# revisions: long enough to outlive any normal create+dispatch round-trip
# (sub-minute on healthy DB), short enough to recover within one tick of
# the reconciler.
_RECONCILER_GRACE = timedelta(minutes=5)


class WorkQueueCheck(BaseCheck):
    """F064.6: poll the configured adapter, claim new items, dispatch
    DAGs, and reconcile terminal-state items.

    Constructed externally with the heart's WorkQueueItemManager, the
    DAG store + orchestrator, and a DAGCreateRequest factory. The
    factory pattern keeps this module agnostic of how the caller
    chooses to shape the per-item DAG (single subtask vs multi-node).
    """

    name = "work_queue"

    def __init__(
        self,
        *,
        adapter: WorkQueueAdapter,
        items_mgr: "WorkQueueItemManager",
        dag_store: "DAGStore",
        orchestrator: "DAGOrchestrator",
        settings: "Settings",
        request_factory=None,
    ) -> None:
        super().__init__()
        self._adapter = adapter
        self._items = items_mgr
        self._dag_store = dag_store
        self._orchestrator = orchestrator
        self._settings = settings
        self._request_factory = request_factory or self._default_request_factory
        self.interval = settings.work_queue_interval_seconds

    async def run(self) -> CheckResult:
        """Poll the adapter and process new + terminal items.

        Returns a CheckResult with one Finding per dispatch (so the
        heartbeat dashboard surfaces what was ingested). An empty queue
        is success (zero findings), NOT failure — per plan §9.6.
        """
        try:
            items = await self._adapter.list_active()
        except NotImplementedError:
            # v2 stubs raise this. Treat as a no-op success — operators
            # configured a stub source and shouldn't be paged for it.
            logger.info(
                "F064.6: adapter %r raised NotImplementedError; treating as no-op",
                self._adapter.source_name,
            )
            return CheckResult(has_updates=False, findings=[])
        except Exception:
            logger.exception(
                "F064.6: adapter %r failed during list_active",
                self._adapter.source_name,
            )
            # Re-raise so circuit-breaker increments via mark_failure.
            raise

        findings: list[Finding] = []
        dispatched_this_tick = 0
        cap = self._settings.work_queue_max_dags_per_tick

        for item in items:
            # @codex P1 on 73f7e81: terminal-state items must be processed
            # regardless of the per-tick dispatch cap. Otherwise a queue
            # whose active items are listed first can starve closed-item
            # cancellation forever, leaving DAGs running for items that
            # were closed externally hours ago.
            if item.terminal:
                await self._handle_terminal(item, findings)
                continue
            if dispatched_this_tick >= cap:
                continue
            dispatched = await self._claim_and_dispatch(item, findings)
            if dispatched:
                dispatched_this_tick += 1

        # Reconciler: pick up orphans that committed `claim_for_dispatch`
        # but never reached mark_dispatched (process restart, partial
        # commit, etc.). Bounded by remaining tick cap.
        if dispatched_this_tick < cap:
            stale = await self._items.list_undispatched(
                self._adapter.source_name, _RECONCILER_GRACE,
            )
            for row in stale:
                if dispatched_this_tick >= cap:
                    break
                # Reconstruct a minimal WorkItem from the persisted payload.
                payload = row.payload or {}
                item = WorkItem(
                    external_id=row.external_id,
                    title=str(payload.get("title", row.external_id)),
                    body=str(payload.get("body", "")),
                    state=str(payload.get("state", "open")),
                    terminal=bool(payload.get("terminal", False)),
                    payload=payload,
                )
                dispatched = await self._reconcile_orphan(row.id, item, findings)
                if dispatched:
                    dispatched_this_tick += 1

        return CheckResult(
            has_updates=bool(findings),
            findings=findings,
        )

    # ------------------------------------------------------------------
    # Dispatch + reconciliation helpers
    # ------------------------------------------------------------------

    async def _claim_and_dispatch(
        self, item: WorkItem, findings: list[Finding]
    ) -> bool:
        """Atomic claim + DAG create + mark_dispatched. Returns True iff
        a new DAG was created for this item.

        @codex P1 on 73f7e81: dag_create and mark_dispatched can't share
        a SQLAlchemy session because DAGStore.create owns its own. To
        avoid the duplicate-dispatch race (mark_dispatched fails after
        dag_create succeeds → reconciler picks up the orphan and creates
        a SECOND DAG for the same item), we cancel the DAG when the link
        fails. The unique-on-(agent_id, source, external_id) row is
        already committed and prevents another claim, so the reconciler
        will retry the link (not re-create) on its next pass.
        """
        claimed = await self._items.claim_for_dispatch(
            source=self._adapter.source_name,
            external_id=item.external_id,
            payload=item.payload,
        )
        if claimed is None:
            # Already seen (this tick or a prior tick). The reconciler
            # path handles orphan re-dispatch; nothing to do here.
            return False
        dag = None
        try:
            request = self._request_factory(item)
            dag = await self._dag_store.create(request)
            await self._items.mark_dispatched(claimed.id, dag.id)
        except Exception as e:
            logger.exception(
                "F064.6: create+dispatch failed for %s/%s",
                self._adapter.source_name, item.external_id,
            )
            # If dag_create succeeded but mark_dispatched raised, cancel
            # the orphan DAG so it doesn't run unmoored. Best-effort —
            # if cancel also fails we just leak the DAG; the reconciler
            # eventually retries the mark_dispatched on this row.
            if dag is not None:
                try:
                    await self._orchestrator.cancel_dag(
                        dag.id,
                        reason="F064.6: mark_dispatched failed; cancelling orphan",
                    )
                except Exception:
                    logger.exception(
                        "F064.6: cleanup cancel_dag failed for %s",
                        dag.id,
                    )
            findings.append(Finding(
                source=self._adapter.source_name,
                summary=f"Failed to dispatch work-queue item {item.external_id}: {e}",
                urgency="normal",
                check_name=self.name,
            ))
            return False
        # Audit DG-6 (2026-06-09): actually START the DAG. create() only
        # persists it as `pending`; without start_dag the DAG idled until the
        # orchestrator's stale-pending sweep rescued it (~300s+). The item is
        # already linked via mark_dispatched, so a start failure degrades to
        # the recovery sweep — but we surface that via the Finding rather than
        # emitting a green "Dispatched" that would mask a systemic all-start-
        # failing incident as healthy (review P2).
        started = await self._safe_start_dag(dag.id)
        findings.append(self._dispatch_finding(item, dag.id, started))
        return True

    def _dispatch_finding(self, item: WorkItem, dag_id, started: bool) -> Finding:
        if started:
            return Finding(
                source=self._adapter.source_name,
                summary=f"Dispatched work-queue item {item.external_id} as DAG {dag_id}",
                urgency="low",
                check_name=self.name,
            )
        return Finding(
            source=self._adapter.source_name,
            summary=(
                f"Work-queue item {item.external_id}: DAG {dag_id} created but "
                f"failed to start; awaiting orchestrator recovery sweep"
            ),
            urgency="normal",
            check_name=self.name,
        )

    async def _safe_start_dag(self, dag_id) -> bool:
        """Start a freshly-created DAG. Returns True on success (DG-6).

        The item is already linked to the DAG, so a start failure is non-fatal:
        the orchestrator's recovery sweep promotes the still-`pending` DAG on a
        later tick. We return the outcome so the caller can surface a real
        failure instead of reporting a green dispatch.
        """
        try:
            await self._orchestrator.start_dag(dag_id)
            return True
        except ValueError as e:
            # Expected benign race: the recovery sweep (or a concurrent tick)
            # already moved this DAG out of 'pending'. Not an error.
            logger.debug("start_dag(%s) no-op (already started): %s", dag_id, e)
            return True
        except Exception:
            logger.exception(
                "F064.6/DG-6: start_dag failed for %s; recovery sweep will promote it",
                dag_id,
            )
            return False

    async def _reconcile_orphan(
        self, row_id, item: WorkItem, findings: list[Finding]
    ) -> bool:
        """Re-attempt dispatch for a row that committed claim but never
        reached mark_dispatched (partial-commit / restart recovery).

        @codex P1 on 51c1f78: same atomicity story as _claim_and_dispatch.
        If create() succeeds but mark_dispatched() raises, the new DAG
        would run unmoored AND the next reconciler tick picks the row
        again and creates another DAG. Cancel the orphan on
        mark_dispatched failure to prevent the duplicate.
        """
        dag = None
        try:
            request = self._request_factory(item)
            dag = await self._dag_store.create(request)
            await self._items.mark_dispatched(row_id, dag.id)
        except Exception as e:
            logger.exception(
                "F064.6: reconciler re-dispatch failed for orphan %s",
                row_id,
            )
            if dag is not None:
                try:
                    await self._orchestrator.cancel_dag(
                        dag.id,
                        reason="F064.6: reconciler mark_dispatched failed; cancelling orphan",
                    )
                except Exception:
                    logger.exception(
                        "F064.6: reconciler cleanup cancel_dag failed for %s",
                        dag.id,
                    )
            findings.append(Finding(
                source=self._adapter.source_name,
                summary=f"Reconciler failed for orphan {item.external_id}: {e}",
                urgency="normal",
                check_name=self.name,
            ))
            return False
        # DG-6: start the recovered DAG (see _claim_and_dispatch).
        started = await self._safe_start_dag(dag.id)
        if started:
            findings.append(Finding(
                source=self._adapter.source_name,
                summary=f"Reconciler dispatched orphan {item.external_id} as DAG {dag.id}",
                urgency="low",
                check_name=self.name,
            ))
        else:
            findings.append(Finding(
                source=self._adapter.source_name,
                summary=(
                    f"Reconciler: orphan {item.external_id} DAG {dag.id} created "
                    f"but failed to start; awaiting recovery sweep"
                ),
                urgency="normal",
                check_name=self.name,
            ))
        return True

    async def _handle_terminal(
        self, item: WorkItem, findings: list[Finding]
    ) -> None:
        """Cancel any DAG dispatched for this item, then record terminal."""
        existing = await self._items.get_by_external(
            self._adapter.source_name, item.external_id,
        )
        if existing is None:
            # Item went terminal before we ever saw it active. Record
            # state so we don't re-dispatch if it flips back to active.
            placeholder = await self._items.claim_for_dispatch(
                source=self._adapter.source_name,
                external_id=item.external_id,
                payload=item.payload,
            )
            if placeholder is not None:
                await self._items.mark_terminal(placeholder.id, item.state)
            return
        if existing.dispatched_at is None or existing.dag_id is None:
            # Claimed but never dispatched, OR placeholder from a prior
            # terminal-only sighting. Just record the terminal state.
            await self._items.mark_terminal(existing.id, item.state)
            return
        try:
            await self._orchestrator.cancel_dag(
                existing.dag_id,
                reason=f"work_queue terminal state: {item.state}",
            )
        except Exception as e:
            logger.exception(
                "F064.6: cancel_dag failed for terminal item %s",
                item.external_id,
            )
            findings.append(Finding(
                source=self._adapter.source_name,
                summary=f"cancel_dag failed for terminal item {item.external_id}: {e}",
                urgency="high",
                check_name=self.name,
            ))
            return
        # Only commit terminal_state AFTER cancel succeeds — a failed
        # cancel forces a retry next tick.
        await self._items.mark_terminal(existing.id, item.state)
        findings.append(Finding(
            source=self._adapter.source_name,
            summary=(
                f"Cancelled DAG {existing.dag_id} for terminal item "
                f"{item.external_id} (state={item.state})"
            ),
            urgency="low",
            check_name=self.name,
        ))

    # ------------------------------------------------------------------
    # Default per-item DAG factory
    # ------------------------------------------------------------------

    def _default_request_factory(self, item: WorkItem):
        """Build a minimal single-subtask DAG for the item. Callers can
        override via the `request_factory` constructor arg if they want
        a richer multi-node DAG per item."""
        # Lazy import to keep this module importable when dag isn't wired.
        from nous.dag._workspace import sanitize_segment
        from nous.dag.schemas import DAGCreateRequest, DAGNodeSpec, DAGNodeType

        # Sanitize external_id for use as a DAG/node name. The strict
        # sanitize raises on unsafe chars; for adapter-supplied external
        # ids we use the lenient form (substitute unsafe → "_") so a
        # GitHub issue number like "ORG/REPO#42" still produces a valid
        # DAG. Cross-cutting with F064.3.
        import re
        safe_ext = re.sub(r"[^A-Za-z0-9._-]", "_", item.external_id)[:60]
        # Validate the final form one more time to catch reserved cases.
        try:
            sanitize_segment(safe_ext)
        except ValueError:
            # Fall back to a hash if sanitize still rejects (e.g. all-dots).
            import hashlib
            safe_ext = "wq-" + hashlib.sha256(
                item.external_id.encode("utf-8")
            ).hexdigest()[:12]

        return DAGCreateRequest(
            name=f"work_queue:{safe_ext}",
            description=item.title,
            source="heartbeat",
            nodes=[
                DAGNodeSpec(
                    name=safe_ext,
                    type=DAGNodeType.subtask,
                    instructions=item.body,
                    frame_type=self._adapter.DEFAULT_FRAME_TYPE,
                ),
            ],
        )
