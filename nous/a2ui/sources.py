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

import asyncio
import json
import logging
import math
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

Fetcher = Callable[[dict], Awaitable[Any]]

# F094 §4.1 — server-side point cap per series. A chart binds one component
# regardless of point count, but the WIRE payload and the censor budget both
# grow with points, so the source downsamples above this (never truncates —
# a partial trend reads as a finished one, the §1.1 failure in a new costume).
_MAX_SERIES_POINTS = 200

# The push censor gate FAILS CLOSED above 20,000 flattened chars, surfacing
# as an opaque "Surface blocked" (rev-arch #9). Sources bound rows but not
# characters — ten episode summaries can clear 20k on their own — so
# resolve() enforces a serialized budget well under the gate, trimming list
# tails with an EXPLICIT marker rather than a silent cut.
_TOTAL_BUDGET_CHARS = 12_000
_PER_SOURCE_BUDGET_CHARS = 6_000

# Server-owned ceiling on any caller/model-supplied limit (codex P2): the
# char budget trims AFTER materialization, so a prompted limit in the
# millions would still do the database work. Clamp before the query.
_MAX_ROWS = 50


def _limit(params: dict, default: int) -> int:
    try:
        requested = int(params.get("limit", default))
    except (TypeError, ValueError):
        requested = default
    return max(1, min(requested, _MAX_ROWS))


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
        spent = 0
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
            value = await fetcher(dict(decl.get("params") or {}))
            budget = min(_PER_SOURCE_BUDGET_CHARS, _TOTAL_BUDGET_CHARS - spent)
            if is_series(value):
                # A series is EXEMPT from _bound's wholesale-dict-replacement
                # (F094): _bound would swap the whole {kind:series,...} object
                # for a {_truncated} marker, and the model's chart — bound
                # correctly — would then fail the series-shape rule against a
                # marker. Series bound themselves by DOWNSAMPLING points to
                # fit both the point cap and the char budget, staying a valid
                # series throughout.
                value, size = _bound_series(value, budget)
            else:
                value, size = _bound(value, budget)
            spent += size
            model[key] = value
        return model


def is_series(value: Any) -> bool:
    """A source's series contract: {kind:"series", points:[{t,v|...}], ...}."""
    return isinstance(value, dict) and value.get("kind") == "series"


def _bound_series(series: dict, budget: int) -> tuple[dict, int]:
    """Fit a series under the char budget by downsampling points, never by
    replacing the object. Returns the (possibly downsampled) series and its
    serialized size."""
    points = series.get("points") or []
    size = len(json.dumps(series, default=str))
    if size <= budget or len(points) <= 2:
        return series, size
    # Binary-search the largest point count that fits (endpoints preserved).
    lo, hi = 2, len(points)
    best = _downsample_series(series, lo)
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = _downsample_series(series, mid)
        if len(json.dumps(candidate, default=str)) <= budget:
            best = candidate
            lo = mid + 1
        else:
            hi = mid - 1
    return best, len(json.dumps(best, default=str))


def _downsample_series(series: dict, target: int) -> dict:
    """Stride downsample preserving first + last AND every gap placeholder
    (LTTB deferred to F094 P3). Stamps meta.downsampled_from with the ORIGINAL
    length so the renderer can mark it, carried across repeated downsampling."""
    points = series.get("points") or []
    original = series.get("meta", {}).get("downsampled_from") or len(points)
    if len(points) <= target:
        return series
    # A point that omits ANY rendered key is a gap for that key's line — and
    # the renderer breaks the line there. A naive stride can skip it while
    # keeping full points on both sides, silently bridging a real gap (codex
    # P1). This must be PER KEY: a multi-series point that carries `a` but omits
    # `b` is a gap for `b` even though it is not a bare `{t}` placeholder. So a
    # point is a gap-boundary unless it carries every value key seen anywhere;
    # retain all of those, stride-sample the rest into the remaining budget.
    value_keys: set = set()
    for p in points:
        value_keys |= {k for k in p if k != "t"}
    gap_idx = [i for i, p in enumerate(points) if value_keys - set(p)]
    finite_idx = [i for i, p in enumerate(points) if not (value_keys - set(p))]
    finite_budget = max(2, target - len(gap_idx))
    if len(finite_idx) > finite_budget:
        fstep = len(finite_idx) / finite_budget
        picked = [
            finite_idx[min(len(finite_idx) - 1, int(i * fstep))]
            for i in range(finite_budget)
        ]
        picked[-1] = finite_idx[-1]
    else:
        picked = finite_idx
    keep = set(picked) | set(gap_idx)
    keep.add(0)
    keep.add(len(points) - 1)  # endpoints are the visible range — never dropped
    kept_idx = sorted(keep)
    if len(kept_idx) > target:
        # Pathological (gaps alone exceed the budget): stride the kept set down,
        # still pinning the endpoints.
        kstep = len(kept_idx) / target
        pos = {int(i * kstep) for i in range(target)}
        pos.add(0)
        pos.add(len(kept_idx) - 1)
        kept_idx = [kept_idx[min(len(kept_idx) - 1, j)] for j in sorted(pos)]
    out = dict(series)
    out["points"] = [points[i] for i in kept_idx]
    meta = dict(series.get("meta") or {})
    meta["downsampled_from"] = original
    out["meta"] = meta
    return out


def to_series(
    records: list[dict],
    t_key: str,
    v_key: str,
    *,
    unit: str = "",
    value_keys: list[str] | None = None,
) -> dict:
    """General normalizer: a record list → the F094 series contract, so any
    existing record-list fetcher becomes chartable without a rewrite.

    - Sorts ascending by ``t_key``.
    - Single-series (``value_keys`` None): each point is ``{t, v}`` from
      ``t_key``/``v_key``; a point whose value is non-finite keeps its ``t`` but
      omits ``v`` and is counted in ``meta.dropped`` (a dropped reading and a
      zero reading are different facts — never coerce to 0).
    - Multi-series (``value_keys`` given): each point keeps ``t`` plus every
      listed numeric key; a per-key non-finite value is omitted from that
      point. The result carries ``keys`` so LineChart/validation can check
      arity and key presence.
    - A dropped reading is retained as a ``t``-only PLACEHOLDER, never removed:
      the renderer breaks the line at a point whose value key is absent (the
      index is preserved) and counts it dropped. Removing the point would let
      the retained points fall consecutive and ``lineSegments`` bridge a real
      gap — the §1.1 "partial reads as complete" failure at point granularity
      (codex P1).
    - Caps to ``_MAX_SERIES_POINTS`` by downsampling (``meta.downsampled_from``).
    """
    keys = value_keys or [v_key]
    ordered = sorted(records, key=lambda r: str(r.get(t_key, "")))
    points: list[dict] = []
    dropped = 0
    for rec in ordered:
        t = rec.get(t_key)
        if t is None:
            continue
        point: dict[str, Any] = {"t": _iso(t)}
        any_finite = False
        for k in keys:
            raw = rec.get(k)
            if isinstance(raw, (int, float)) and math.isfinite(raw):
                point[k if value_keys else "v"] = raw
                any_finite = True
        if not any_finite:
            dropped += 1
        points.append(point)
    result: dict[str, Any] = {
        "kind": "series",
        "points": points,
        "unit": unit,
        "meta": {"dropped": dropped, "downsampled_from": None},
    }
    if value_keys:
        result["keys"] = list(value_keys)
    if len(points) > _MAX_SERIES_POINTS:
        result = _downsample_series(result, _MAX_SERIES_POINTS)
    return result


def empty_series(reason: str, *, unit: str = "") -> dict:
    """An explicit empty series (F094 R5 / §3.1) — a missing db or drifted
    schema returns this, and the renderer draws the empty state with the
    reason, never a blank box or a confident zero."""
    return {
        "kind": "series",
        "points": [],
        "unit": unit,
        "meta": {"dropped": 0, "downsampled_from": None, "reason": reason},
    }


def _iso(t: Any) -> str:
    """Coerce a timestamp/date to an ISO string; the renderer displays it,
    never parses it for math (F094 series contract)."""
    if hasattr(t, "isoformat"):
        return t.isoformat()
    return str(t)


def _pivot(
    rows: list[tuple], categories: list[str], calendar: list[str] | None = None
) -> list[dict]:
    """Turn grouped (t, category, count) rows into one record per t with a
    numeric key per category (0 where a category had no rows that day), so
    to_series can produce a multi-series chart.

    ``calendar`` (a list of ISO dates for the requested window) materializes
    EVERY day as a zero record, not just the days SQL returned. LineChart
    positions points by array index and never parses timestamps, so a missing
    zero-throughput day would let two distant active dates fall adjacent and a
    quiet day vanish — a misleading trend (codex P2). Days SQL returns outside
    the calendar are still included (union), never dropped."""
    by_t: dict[str, dict[str, Any]] = {}
    for key in calendar or []:
        by_t[key] = {"t": key, **{c: 0 for c in categories}}
    for t, cat, n in rows:
        key = _iso(t)
        rec = by_t.setdefault(key, {"t": key, **{c: 0 for c in categories}})
        if str(cat) in categories:
            rec[str(cat)] = int(n)
    return [by_t[k] for k in sorted(by_t)]


def _calendar(days: int) -> list[str]:
    """The last ``days`` ISO dates ending today (UTC, matching SQL ``now()``),
    oldest→newest — the window a daily series must fully materialize."""
    today = datetime.now(UTC).date()
    return [(today - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]


def _bound(value: Any, budget: int) -> tuple[Any, int]:
    """Trim a fetched value to the char budget with an explicit marker.

    Lists lose tail entries (with a trailing truncation marker); anything
    else that overflows is replaced by a marker object. Never a silent cut.
    """
    size = len(json.dumps(value, default=str))
    if size <= budget:
        return value, size
    if isinstance(value, list):
        trimmed = list(value)
        while trimmed and len(json.dumps(trimmed, default=str)) > max(budget - 80, 0):
            trimmed.pop()
        omitted = len(value) - len(trimmed)
        trimmed.append({"_truncated": True, "omitted": omitted})
        return trimmed, len(json.dumps(trimmed, default=str))
    marker = {"_truncated": True, "reason": f"value exceeded {budget} chars"}
    return marker, len(json.dumps(marker))


def build_default_registry(
    *,
    heart: Any = None,
    brain: Any = None,
    dag_store: Any = None,
    heartbeat_runner: Any = None,
    database: Any = None,
    health_db_path: str | None = None,
) -> SourceRegistry:
    """The Phase 3 fetcher set + F094 series sources.

    Each is registered only when its backing component is wired, so a
    deployment without (say) the heartbeat simply lacks that source and
    compose is told so via the registry names. ``database`` enables the
    grouped-over-time series sources; ``health_db_path`` the external
    health series (defensive — absent db ⇒ an explicit empty series).
    """
    registry = SourceRegistry()

    if brain is not None:

        async def unreviewed_decisions(params: dict) -> list[dict]:
            # Bound pushed into SQL (codex round 3): a Python slice after
            # .all() still materializes the whole age window.
            rows = await brain.get_unreviewed(
                max_age_days=int(params.get("max_age_days", 30)),
                limit=_limit(params, 20),
            )
            return [
                {
                    "id": str(d.id),
                    "description": d.description,
                    "confidence": d.confidence,
                    "stakes": d.stakes,
                    "category": d.category,
                }
                for d in rows
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
                for t in items[: _limit(params, 20)]
            ]

        registry.register("heartbeat_findings", heartbeat_findings)

    if heart is not None:

        async def facts_search(params: dict) -> list[dict]:
            query = str(params.get("q") or params.get("query") or "")
            if not query:
                raise ValueError("facts_search requires params.q")
            rows = await heart.search_facts(query, limit=_limit(params, 10))
            return [
                {"id": str(f.id), "content": f.content, "category": f.category}
                for f in rows
            ]

        async def recent_episodes(params: dict) -> list[dict]:
            rows = await heart.episodes.list_recent(limit=_limit(params, 10))
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
                status=params.get("status"), limit=_limit(params, 20)
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
            rows = await heart.schedules.list(limit=_limit(params, 20))
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

    # --- F094 series sources — the general "any dashboard" enablers. -------
    if database is not None and brain is not None:
        from sqlalchemy import text

        agent_id = getattr(brain, "agent_id", None)

        async def decision_outcomes_series(params: dict) -> dict:
            """Reviewed decisions per day by outcome — a multi-series chart
            that makes the calibration loop visible in a surface, not a
            report."""
            days = max(1, min(int(params.get("days", 90)), 365))
            outcomes = ["success", "partial", "failure", "noise", "superseded"]
            async with database.session() as session:
                rows = (
                    await session.execute(
                        text(
                            "SELECT reviewed_at::date AS d, outcome, count(*) "
                            "FROM brain.decisions WHERE agent_id = :aid "
                            "AND reviewed_at IS NOT NULL "
                            "AND reviewed_at >= now() - make_interval(days => :days) "
                            "GROUP BY 1, 2 ORDER BY 1"
                        ),
                        {"aid": agent_id, "days": days},
                    )
                ).all()
            records = _pivot(
                [(r[0], r[1], r[2]) for r in rows], outcomes, calendar=_calendar(days)
            )
            return to_series(records, "t", "v", unit="decisions", value_keys=outcomes)

        registry.register("decision_outcomes_series", decision_outcomes_series)

    if database is not None:
        from sqlalchemy import text

        agent_id = getattr(brain, "agent_id", None) if brain is not None else None

        async def dag_throughput_series(params: dict) -> dict:
            """Completed vs failed DAG nodes per day."""
            days = max(1, min(int(params.get("days", 30)), 180))
            cats = ["completed", "failed"]
            aid_clause = "AND e.agent_id = :aid " if agent_id else ""
            async with database.session() as session:
                rows = (
                    await session.execute(
                        text(
                            "SELECT n.completed_at::date AS d, n.status, count(*) "
                            "FROM nous_system.dag_nodes n "
                            "JOIN nous_system.execution_dags e ON n.dag_id = e.id "
                            "WHERE n.completed_at IS NOT NULL "
                            "AND n.status IN ('completed','failed') "
                            f"{aid_clause}"
                            "AND n.completed_at >= now() - make_interval(days => :days) "
                            "GROUP BY 1, 2 ORDER BY 1"
                        ),
                        {"aid": agent_id, "days": days} if agent_id else {"days": days},
                    )
                ).all()
            records = _pivot(
                [(r[0], r[1], r[2]) for r in rows], cats, calendar=_calendar(days)
            )
            return to_series(records, "t", "v", unit="nodes", value_keys=cats)

        registry.register("dag_throughput_series", dag_throughput_series)

    if health_db_path:

        async def health_series(params: dict) -> dict:
            """A metric over time from the external health SQLite db. The db
            and its schema are owned by the health integration, not this
            repo, so this reads a documented contract table
            ``health_metrics(metric TEXT, ts TEXT ISO, value REAL)`` and
            returns an EXPLICIT empty series (never a blank box) when the db
            or table is missing or the schema has drifted (F094 R5)."""
            import os
            import sqlite3

            metric = str(params.get("metric") or "")
            if not metric:
                return empty_series("no metric requested", unit="")
            if not os.path.exists(health_db_path):
                return empty_series(f"health db not found at {health_db_path}")

            # Bound the read IN SQLite and run the synchronous driver OFF the
            # event loop (codex P2): an unbounded fetchall() on a multi-year
            # metric would do the whole scan, hold it all in memory, and block
            # the loop for every other async request during compose/refresh.
            # The later 200-point cap prevents none of that. Cap to the most
            # recent N rows (DESC + LIMIT), then re-order ascending for the
            # series contract.
            row_cap = max(1, min(int(params.get("limit", 2000)), 5000))

            def _read() -> list[dict]:
                conn = sqlite3.connect(f"file:{health_db_path}?mode=ro", uri=True)
                try:
                    cur = conn.execute(
                        "SELECT ts, value FROM health_metrics "
                        "WHERE metric = ? ORDER BY ts DESC LIMIT ?",
                        (metric, row_cap),
                    )
                    fetched = cur.fetchall()
                finally:
                    conn.close()
                return [
                    {"t": row[0], "v": row[1]}
                    for row in reversed(fetched)
                    if row[0] is not None
                ]

            try:
                records = await asyncio.to_thread(_read)
            except sqlite3.Error as exc:
                return empty_series(f"health db read failed ({exc})")
            if not records:
                return empty_series(f"no rows for metric {metric!r}", unit=str(params.get("unit", "")))
            return to_series(records, "t", "v", unit=str(params.get("unit", "")))

        registry.register("health_series", health_series)

    return registry
