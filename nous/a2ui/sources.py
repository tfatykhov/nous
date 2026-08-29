"""F092.1: server-side data sources for micro-apps.

A micro-app declares ``data_sources: [{key, source, params}]`` instead of
carrying inline data. The server resolves every declared source and builds
the data-model subtrees — the same authority principle as the Phase 2
template self-sourcing (the model must not be able to hand a surface a
stale or invented list), extended to composed surfaces. Where no source
exists for an intent, compose may fall back to model-supplied data, but
that subtree is stamped ``provenance: "model"`` and rendered amber — the
gap is visible, never silent.

``app.refresh`` re-runs these fetchers ONLY (no LLM). That is what makes
refresh honest: with model-supplied data, refresh would replay whatever
the model said last time; with sources it is a real re-read.

Every fetcher returns plain JSON-serializable data.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

Fetcher = Callable[[dict], Awaitable[Any]]


class UnknownSourceError(ValueError):
    """Raised when a declared source name is not registered.

    Surfaced to the compose repair loop (the model picked a name that does
    not exist) and to app.refresh (the registry shrank since compose).
    """


class SourceRegistry:
    """Named server-side fetchers a micro-app may bind data from."""

    def __init__(self) -> None:
        self._fetchers: dict[str, Fetcher] = {}

    def register(self, name: str, fetcher: Fetcher) -> None:
        self._fetchers[name] = fetcher

    def names(self) -> list[str]:
        return sorted(self._fetchers)

    async def resolve(self, data_sources: list[dict]) -> dict[str, Any]:
        """Resolve declared sources into data-model subtrees keyed by `key`.

        Raises UnknownSourceError on an unregistered source name. A fetcher
        that raises propagates — refresh/compose decide how to report it;
        swallowing it here would render a confident empty section.
        """
        model: dict[str, Any] = {}
        for decl in data_sources:
            key = str(decl.get("key") or "")
            name = str(decl.get("source") or "")
            if not key:
                raise ValueError("data_sources entries require a key")
            fetcher = self._fetchers.get(name)
            if fetcher is None:
                raise UnknownSourceError(
                    f"unknown data source {name!r}; available: {self.names()}"
                )
            model[key] = await fetcher(dict(decl.get("params") or {}))
        return model


def build_default_registry(
    *,
    heart: Any = None,
    brain: Any = None,
    dag_store: Any = None,
    heartbeat_runner: Any = None,
) -> SourceRegistry:
    """The Phase 3 fetcher set — thin wrappers over existing APIs.

    Each is registered only when its backing component is wired, so a
    deployment without (say) the heartbeat simply lacks that source and
    compose is told so via the registry names.
    """
    registry = SourceRegistry()

    if brain is not None:

        async def unreviewed_decisions(params: dict) -> list[dict]:
            rows = await brain.get_unreviewed(
                max_age_days=int(params.get("max_age_days", 30))
            )
            return [
                {
                    "id": str(d.id),
                    "description": d.description,
                    "confidence": d.confidence,
                    "stakes": d.stakes,
                    "category": d.category,
                }
                for d in rows[: int(params.get("limit", 20))]
            ]

        registry.register("unreviewed_decisions", unreviewed_decisions)

    if dag_store is not None:

        async def dag(params: dict) -> dict:
            dag_obj = await dag_store.get_dag(UUID(str(params.get("dag_id", ""))))
            if dag_obj is None:
                raise ValueError("DAG not found")
            name_by_id = {n.id: n.name for n in dag_obj.nodes}
            return {
                "dag_id": str(dag_obj.id),
                "name": dag_obj.name,
                "status": dag_obj.status,
                "nodes": [
                    {"name": n.name, "status": n.status, "node_type": n.node_type}
                    for n in dag_obj.nodes
                ],
                "edges": [
                    {
                        "from": name_by_id.get(e.from_node_id, ""),
                        "to": name_by_id.get(e.to_node_id, ""),
                    }
                    for e in dag_obj.edges
                ],
            }

        registry.register("dag", dag)

    if heartbeat_runner is not None and getattr(heartbeat_runner, "finding_store", None):

        async def heartbeat_findings(params: dict) -> list[dict]:
            items = heartbeat_runner.finding_store.get_digest_items()
            return [
                {
                    "fingerprint": t.fingerprint,
                    "message": t.finding.summary,
                    "urgency": t.finding.urgency,
                    "check": t.finding.check_name,
                    "state": str(t.state.value if hasattr(t.state, "value") else t.state),
                }
                for t in items[: int(params.get("limit", 20))]
            ]

        registry.register("heartbeat_findings", heartbeat_findings)

    if heart is not None:

        async def facts_search(params: dict) -> list[dict]:
            query = str(params.get("q") or params.get("query") or "")
            if not query:
                raise ValueError("facts_search requires params.q")
            rows = await heart.search_facts(query, limit=int(params.get("limit", 10)))
            return [
                {"id": str(f.id), "content": f.content, "category": f.category}
                for f in rows
            ]

        async def recent_episodes(params: dict) -> list[dict]:
            rows = await heart.episodes.list_recent(limit=int(params.get("limit", 10)))
            return [
                {
                    "id": str(e.id),
                    "summary": e.summary,
                    "started_at": e.started_at.isoformat() if e.started_at else None,
                    "outcome": e.outcome,
                }
                for e in rows
            ]

        async def subtasks(params: dict) -> list[dict]:
            rows = await heart.subtasks.list(
                status=params.get("status"), limit=int(params.get("limit", 20))
            )
            return [
                {
                    "id": str(s.id),
                    "task": s.task,
                    "status": s.status,
                    "created_at": s.created_at.isoformat() if s.created_at else None,
                }
                for s in rows
            ]

        async def schedules(params: dict) -> list[dict]:
            rows = await heart.schedules.list(limit=int(params.get("limit", 20)))
            return [
                {
                    "id": str(s.id),
                    "task": s.task,
                    "cron": s.cron_expr,
                    "next_fire_at": s.next_fire_at.isoformat() if s.next_fire_at else None,
                }
                for s in rows
            ]

        registry.register("facts_search", facts_search)
        registry.register("recent_episodes", recent_episodes)
        registry.register("subtasks", subtasks)
        registry.register("schedules", schedules)

    return registry
