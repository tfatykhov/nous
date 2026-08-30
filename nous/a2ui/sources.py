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
import time
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
# Deliberately 16k, not 12k: a 3-chart dashboard (F094 acceptance #7) needs
# three series to render REAL data, and at ~6k per downsampled 200-point series
# the old 12k left the third source ~0 chars → a fabricated 2-point trend. 16k
# fits three (~6k/6k/4k) while staying under the 20k censor gate with ~4k of
# headroom for the title + components (a chart app's component tree is small).
_TOTAL_BUDGET_CHARS = 16_000
_PER_SOURCE_BUDGET_CHARS = 6_000

# Server-owned ceiling on any caller/model-supplied limit (codex P2): the
# char budget trims AFTER materialization, so a prompted limit in the
# millions would still do the database work. Clamp before the query.
_MAX_ROWS = 50

# Fallback ceiling on resolving ALL of one surface's sources, used when no
# Settings are wired. Prefer `_source_budget_seconds`, which DERIVES the value
# — a hardcoded 45 silently assumes the default 120s `NOUS_TOOL_TIMEOUT`, and
# at the explicitly supported `NOUS_TOOL_TIMEOUT=60` it would let sources burn
# 45s and then start a 60s compose LLM, so the outer wrapper cancels the tool
# and returns the very generic timeout this budget exists to prevent (codex P1).
_TOTAL_SOURCE_SECONDS = 45.0

# Left for the push + validation work after the compose LLM returns.
_SOURCE_BUDGET_SAFETY_SECONDS = 10.0

# Head start given to a script worker's OWN deadline over the registry's
# `wait_for`, so a routine script timeout comes back as an outcome the source
# can turn into a shape-preserving failure rather than a cancellation that
# aborts the whole resolve. Covers run_python's `timeout + _TIMEOUT_GRACE`
# (2.0s) plus scheduling slack.
_WORKER_DEADLINE_MARGIN = 4.0

# Dashboard scripts may hold at most this many of the shared run_python slots
# (`NOUS_PROGRAMMATIC_TOOLS_MAX_CONCURRENT`, 4). The deadline pushed into the
# worker stops PYTHON-level runaway code, but a script blocked inside a C call
# — `urlopen`, `time.sleep` — cannot be interrupted at all and holds its slot
# until it returns. That is a documented property of run_python, not something
# this source introduced; what this source DOES change is how reachable it is,
# since fetching an external API is the advertised use and refreshes recur
# unattended. Killing the thread is not possible, so bound the blast radius
# instead: stalled dashboard scripts can never take the whole pool, and the
# agent's own interactive run_python always has capacity left (codex P1).
# Slots reserved for the agent's own interactive run_python; a dashboard
# script refuses to start when fewer than this many are free.
_INTERACTIVE_SLOT_RESERVE = 2
_MIN_SOURCE_SECONDS = 5.0


def _source_budget_seconds(settings: Any, *, for_compose: bool = True) -> float:
    """Seconds all sources of one surface may take, derived from the enclosing
    timeouts: whatever `NOUS_TOOL_TIMEOUT` leaves after reserving the compose
    LLM rounds and a safety margin, clamped so it is neither negative nor
    unbounded (prod runs `NOUS_TOOL_TIMEOUT=2000`).

    `for_compose=False` reserves NOTHING for the LLM, because `app.refresh`
    re-runs fetchers ONLY — no compose round follows it. Charging refresh for
    three LLM rounds it will never make left it with the 5s floor at default
    settings, which would cut off exactly the long-running external-API script
    this feature exists to keep live (codex P1)."""
    if settings is None:
        return _TOTAL_SOURCE_SECONDS
    from .compose import MAX_REPAIRS

    tool_timeout = float(getattr(settings, "tool_timeout", 120) or 120)
    compose_timeout = float(getattr(settings, "a2ui_compose_timeout_seconds", 60) or 60)
    # Reserve EVERY round the composer may run, not one: an invalid first
    # response costs up to MAX_REPAIRS more LLM calls, and reserving a single
    # round let two of them overrun the tool timeout before a repair or the
    # markdown fallback could return (codex P1).
    rounds = (MAX_REPAIRS + 1) if for_compose else 0
    available = tool_timeout - (compose_timeout * rounds) - _SOURCE_BUDGET_SAFETY_SECONDS
    return max(_MIN_SOURCE_SECONDS, min(_TOTAL_SOURCE_SECONDS, available))


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

    def __init__(self, settings: Any = None) -> None:
        self._fetchers: dict[str, Fetcher] = {}
        # Optional so existing constructions (and tests) keep working; when
        # present the resolution budget is derived rather than assumed.
        self._settings = settings

    def register(self, name: str, fetcher: Fetcher) -> None:
        self._fetchers[name] = fetcher

    def names(self) -> list[str]:
        return sorted(self._fetchers)

    async def resolve(
        self, data_sources: list[dict], *, for_compose: bool = True
    ) -> dict[str, Any]:
        """Resolve declared sources into data-model subtrees keyed by `key`.

        Raises UnknownSourceError on an unregistered source name. A fetcher
        that raises propagates — refresh/compose decide how to report it;
        swallowing it here would render a confident empty section.
        """
        model: dict[str, Any] = {}
        spent = 0
        # Wall-clock budget across ALL sources in one resolve (codex P1).
        # Sources run sequentially, and an `agent_script` gets the full
        # `programmatic_tools_timeout` (90s) each — so two slow ones, or one
        # plus the compose LLM, blow the 120s tool timeout wrapping the whole
        # compose call and the app never gets built. Bounding the total here
        # rather than per-source also covers a slow DB fetcher.
        # NOT named `budget` — that name is reused below for the per-source
        # CHAR budget, and the collision would make these timeout messages
        # report byte counts as seconds from the second source onward.
        time_budget = _source_budget_seconds(self._settings, for_compose=for_compose)
        deadline = time.monotonic() + time_budget
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
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"source resolution exceeded {time_budget:.0f}s before "
                    f"reaching {name!r} — use fewer or faster sources"
                )
            call_params = dict(decl.get("params") or {})
            # Reserved key: lets a fetcher that runs agent code push the SHARED
            # deadline down into its own worker instead of being merely
            # cancelled here — a cancelled await leaves the worker holding its
            # run slot (codex P1). Ordinary fetchers ignore it.
            # Deliberately SHORTER than this loop's own `wait_for`: run_python
            # waits `timeout + grace`, so passing the identical value would let
            # the outer cancel win and abort the whole compose/refresh instead
            # of letting agent_script return its shape-preserving failure
            # (codex P1). The worker must expire first.
            call_params["_remaining_seconds"] = max(
                1.0, remaining - _WORKER_DEADLINE_MARGIN
            )
            try:
                value = await asyncio.wait_for(
                    fetcher(call_params), timeout=remaining
                )
            except TimeoutError as exc:
                raise TimeoutError(
                    f"source {name!r} exceeded the {time_budget:.0f}s "
                    "resolution budget shared by all sources on this surface"
                ) from exc
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
    """Fit a series under the char budget by downsampling points. Returns the
    (possibly downsampled) series and its serialized size — or, when even the
    2-point minimum does not fit, an honest empty series, NEVER a downsampled
    stub that exceeds its budget."""
    points = series.get("points") or []
    size = len(json.dumps(series, default=str))
    if size <= budget:
        return series, size
    if len(points) > 2:
        # Binary-search the largest point count that fits (endpoints preserved).
        lo, hi = 2, len(points)
        best: tuple[dict, int] | None = None
        while lo <= hi:
            mid = (lo + hi) // 2
            candidate = _downsample_series(series, mid)
            csize = len(json.dumps(candidate, default=str))
            if csize <= budget:
                best = (candidate, csize)
                lo = mid + 1
            else:
                hi = mid - 1
        if best is not None:
            return best
    # Even 2 points do not fit (earlier series on this surface consumed the
    # budget): a 2-point line stamped downsampled_from:200 is a FABRICATED
    # trend, the §1.1 "published a false statement" failure. An explicit empty
    # series with a reason is honest; the stub is not (rev-be/codex P1). It also
    # keeps `spent` accurate — the old stub overran _TOTAL_BUDGET_CHARS.
    empty = empty_series(
        "char budget exhausted — earlier series on this surface used it",
        unit=str(series.get("unit", "")),
    )
    return empty, len(json.dumps(empty, default=str))


def _stride_pick(idxs: list[int], budget: int) -> list[int]:
    """Evenly sample ``budget`` items from ``idxs``, preserving both ends."""
    if len(idxs) <= budget:
        return list(idxs)
    step = len(idxs) / budget
    out = [idxs[min(len(idxs) - 1, int(i * step))] for i in range(budget)]
    out[-1] = idxs[-1]
    return out


def _finite_at(point: dict, key: str) -> bool:
    v = point.get(key)
    return isinstance(v, (int, float)) and math.isfinite(v)


def _downsample_series(series: dict, target: int) -> dict:
    """Stride downsample that preserves, PER KEY, first + last, every line
    BREAK, and the peak + trough (full LTTB deferred to F094 P3). Stamps
    meta.downsampled_from with the ORIGINAL length, carried across repeats."""
    points = series.get("points") or []
    # `series.get("meta", {})` would crash on a hand-built fetcher that sets
    # "meta": None (None.get); `or {}` closes it (rev-be P3).
    original = (series.get("meta") or {}).get("downsampled_from") or len(points)
    if len(points) <= target:
        return series
    value_keys: set = set()
    for p in points:
        value_keys |= {k for k in p if k != "t"}
    # A "gap for key k" is any index whose k is not a finite number (a bare {t}
    # placeholder is a gap for every key; a multi-series point missing only one
    # key is a gap for that one). Stride only over FULLY finite points; the rest
    # are gap-boundaries handled by per-key representatives below.
    gap_by_key = {
        k: {i for i in range(len(points)) if not _finite_at(points[i], k)}
        for k in value_keys
    }
    gap_set = set().union(*gap_by_key.values()) if gap_by_key else set()
    finite_idx = [i for i in range(len(points)) if i not in gap_set]

    # Peak + trough of EACH key are load-bearing — striding them away hides a
    # spike/anomaly, and the renderer derives its domain only from kept points
    # (codex P1). Pin them into the mandatory set alongside the endpoints.
    mandatory = {0, len(points) - 1}
    for k in value_keys:
        vals = [(points[i][k], i) for i in range(len(points)) if _finite_at(points[i], k)]
        if vals:
            mandatory.add(min(vals)[1])
            mandatory.add(max(vals)[1])

    def _reps(picked: list[int]) -> set[int]:
        # One representative gap PER KEY between each consecutive kept-finite
        # pair — a break for key `a` at 251 and one for `b` at 252 in the same
        # interval must BOTH survive, or that key's line bridges (codex P1).
        reps: set[int] = set()
        for a, b in zip(picked, picked[1:]):
            for k in value_keys:
                rep = next((g for g in range(a + 1, b) if g in gap_by_key[k]), None)
                if rep is not None:
                    reps.add(rep)
        return reps

    # Largest finite budget whose (finite + per-key gaps + mandatory) set still
    # fits target. Binary search — the same shape as _bound_series.
    lo, hi = 2, min(target, max(2, len(finite_idx)))
    best: set[int] = set(mandatory)
    while lo <= hi:
        mid = (lo + hi) // 2
        picked = _stride_pick(finite_idx, mid)
        keep = set(picked) | _reps(picked) | mandatory
        if len(keep) <= target:
            best = keep
            lo = mid + 1
        else:
            hi = mid - 1
    kept_idx = sorted(best)
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


# A stored dashboard script is agent-authored code, not prose — cap it so a
# runaway generation cannot park an unbounded blob in app_spec and re-compile
# it on every refresh.
_MAX_SCRIPT_CHARS = 16_000

# Hard ceiling on a script's SERIALIZED result, enforced before it reaches
# `_bound` (codex P1). Every other source is row-capped at `_MAX_ROWS`, but a
# script can construct or fetch anything, and `_bound`'s trim pops ONE list
# entry and re-serializes the remainder per iteration — quadratic. Handing it
# a 100k-row result would do that work on the MAIN EVENT LOOP, outside the
# script's own deadline, stalling every other request. One O(n) measurement
# here turns that into an explicit, self-describing rejection.
_MAX_SCRIPT_RESULT_CHARS = 256_000


_SCRIPT_SHAPES = ("records", "series")


def _is_valid_series(value: Any) -> bool:
    """The FULL series contract, not just the `kind` tag `is_series` checks.

    Every POINT must be a mapping too (codex P2): `_downsample_series` does
    `for k in p` and `p.get(...)`, so a `null` or int point raises there — and
    that happens inside `_bound_series`, turning script data into a 500 rather
    than the shape-preserving failure value.
    """
    if not (is_series(value) and isinstance(value.get("points"), list)):
        return False
    meta = value.get("meta")
    if meta and not isinstance(meta, dict):
        # `_downsample_series` does `(series.get("meta") or {}).get(...)`, so a
        # truthy non-mapping such as "bad" raises AttributeError inside
        # `_bound_series` — a 500 rather than the promised failure value.
        return False
    return all(isinstance(p, dict) for p in value["points"])


def _script_failure(reason: str, shape: str) -> Any:
    """A failed script's value, in the SHAPE the app was built against.

    Refresh does not re-run binding validation, so whatever a failure returns
    must remain a valid binding for the components ALREADY on the surface.

    The two shapes are deliberately asymmetric, and the asymmetry is honest:

    - ``series`` carries its reason in ``meta``, which the chart's §3.1 empty
      state renders — the failure is visible in the app.
    - ``records`` returns an EMPTY LIST. A one-row ``[{"_error": …}]`` was
      tried and is worse (codex P1): a repeat template's children bind fields
      like ``name``/``value``, so the marker row renders as a row of blanks
      and nothing is bound to ``_error`` — the reason is invisible AND the
      table now lies about having a row. The record schema the surface was
      built against cannot be reconstructed at failure time, so the honest
      value is "no rows", with the reason going to the log for the operator.
    """
    if shape == "series":
        return empty_series(reason)
    logger.warning("agent_script (records) failed: %s", reason)
    return []


def build_default_registry(
    *,
    heart: Any = None,
    brain: Any = None,
    dag_store: Any = None,
    heartbeat_runner: Any = None,
    database: Any = None,
    health_db_path: str | None = None,
    run_script: Any = None,
    settings: Any = None,
) -> SourceRegistry:
    """The Phase 3 fetcher set + F094 series sources.

    Each is registered only when its backing component is wired, so a
    deployment without (say) the heartbeat simply lacks that source and
    compose is told so via the registry names. ``database`` enables the
    grouped-over-time series sources; ``health_db_path`` the external
    health series (defensive — absent db ⇒ an explicit empty series).
    """
    registry = SourceRegistry(settings)

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

    if run_script is not None:

        async def agent_script(params: dict) -> Any:
            """Data the AGENT itself produced, re-runnable.

            Every other source is a fetcher someone added in code, so a domain
            with no fetcher could only ever be a model-supplied SNAPSHOT: an
            app with no `data_sources` is refused a refresh outright
            (``compose.refresh_data``), which is exactly the property that made
            it no better than the emailed static HTML this feature replaces.

            This source closes that: the agent writes the code that produces
            the data, that code is stored in ``app_spec.data_sources[].params``,
            and ``refresh_data`` re-resolves it — so the app is genuinely live
            without anyone adding a fetcher or setting an env var. The script
            may reach whatever it needs (it has full builtins, the same as the
            agent's own ``run_python``), which is what makes "plug in an
            external source with no code change" true.

            It re-runs UNATTENDED, so memory writes are disabled (``learn_fact``
            raises) — reads are what a dashboard needs. Execution reuses the
            run_python sandbox verbatim: one run slot, the wall-clock deadline
            tracer, the settrace shims. A failing script yields an explicit
            error value, never a stale one and never a blank box, so one broken
            source cannot fail the whole app's refresh.
            """
            code = params.get("code")
            if not isinstance(code, str) or not code.strip():
                raise ValueError("agent_script requires params.code (the Python to run)")
            if len(code) > _MAX_SCRIPT_CHARS:
                raise ValueError(
                    f"agent_script code is {len(code)} chars — max {_MAX_SCRIPT_CHARS}"
                )
            shape = str(params.get("shape") or "records")
            if shape not in _SCRIPT_SHAPES:
                raise ValueError(
                    f"agent_script shape must be one of {list(_SCRIPT_SHAPES)}, got {shape!r}"
                )
            # Push the SHARED source deadline into the worker so the thread
            # itself stops there and releases its run slot; cancelling only the
            # await would let concurrent slow refreshes starve every later
            # run_python of slots (codex P1).
            remaining = params.get("_remaining_seconds")
            # Gate on the GLOBAL in-flight count, not a local semaphore
            # (codex P1): a semaphore around the await releases as soon as the
            # coroutine returns, while a worker blocked in a C call keeps its
            # global run slot alive well past that — so repeated refreshes
            # could still stack blocked threads until interactive calls
            # starved. `run_python_active_runs()` reflects the threads that are
            # actually alive, which is the only signal that tracks a blocked
            # worker.
            try:
                from nous.api.tools import run_python_active_runs

                in_flight = run_python_active_runs()
                capacity = int(
                    getattr(settings, "programmatic_tools_max_concurrent", 4) or 4
                )
            except Exception:  # pragma: no cover - defensive
                in_flight, capacity = 0, 4
            allowed = capacity - _INTERACTIVE_SLOT_RESERVE
            if allowed < 1:
                # A pool too small to reserve anything (the supported
                # NOUS_PROGRAMMATIC_TOOLS_MAX_CONCURRENT=1) must not let an
                # unattended script take the only slot — if it blocked in C
                # code every interactive call would be rejected indefinitely,
                # which is the opposite of what this gate promises (codex P1).
                return _script_failure(
                    f"script pool of {capacity} cannot reserve "
                    f"{_INTERACTIVE_SLOT_RESERVE} slots for interactive use — "
                    "raise NOUS_PROGRAMMATIC_TOOLS_MAX_CONCURRENT to use "
                    "dashboard scripts",
                    shape,
                )
            if in_flight >= allowed:
                return _script_failure(
                    f"{in_flight} script slots already in use — dashboard "
                    "scripts yield to interactive use; try refresh again",
                    shape,
                )
            outcome = await run_script(
                code,
                timeout=float(remaining) if remaining else None,
                max_result_chars=_MAX_SCRIPT_RESULT_CHARS,
            )
            if not outcome.get("ok"):
                return _script_failure(str(outcome.get("error") or "script failed"), shape)
            # Already JSON-normalized inside the worker (default=str,
            # allow_nan=False), so nothing here re-touches the raw object or
            # re-serializes it — the size is measured from that same encoding.
            if outcome.get("result_chars", 0) > _MAX_SCRIPT_RESULT_CHARS:
                return _script_failure(
                    f"script result is {outcome['result_chars']} chars — max "
                    f"{_MAX_SCRIPT_RESULT_CHARS}; aggregate or limit it in the script",
                    shape,
                )
            result = outcome.get("result")
            if result is None:
                return _script_failure(
                    "script set no `result` — assign the data to a variable "
                    "named `result` (a list of records, or a series dict)",
                    shape,
                )
            # A declared shape the script did not produce is a script bug, and
            # catching it HERE keeps a chart from ever binding to records.
            declared_keys = params.get("series_keys")
            if shape == "series" and not (
                isinstance(declared_keys, list) and declared_keys
            ):
                # REQUIRED, not optional (codex P2): without it a first
                # single-value result validates a Sparkline and a later
                # multi-series refresh is accepted unchecked, leaving the chart
                # empty — and refresh never re-runs binding validation.
                raise ValueError(
                    "agent_script with shape='series' requires params.series_keys "
                    "— the keys the chart binds (['v'] for Sparkline/BarChart)"
                )
            if shape == "series" and not _is_valid_series(result):
                # The FULL contract, not just `kind` (codex P1): refresh skips
                # binding validation, so a later {"kind":"series","points":{}}
                # would replace a working chart with something the renderer
                # shows as "not a series" instead of the empty state.
                return _script_failure(
                    f"script declared shape 'series' but returned "
                    f"{type(result).__name__} — build one with to_series(...)",
                    shape,
                )
            if shape == "series":
                # Refresh skips binding validation, so a later result that
                # silently changes series MODE (single <-> multi) or drops a
                # LineChart's declared key would leave the existing chart
                # rendering nothing (codex P2). The agent declares the key
                # contract its chart binds; every resolve must honour it.
                raw_keys = result.get("keys") if is_series(result) else None
                if raw_keys is not None and not (
                    isinstance(raw_keys, list)
                    and all(isinstance(k, str) for k in raw_keys)
                ):
                    # `set([["value"]])` raises TypeError: unhashable list, and
                    # that escapes _script_failure — one malformed result would
                    # turn compose/refresh into a 500 despite this source's
                    # failure-isolation contract (codex P2).
                    return _script_failure(
                        "series `keys` must be a list of strings, got "
                        f"{type(raw_keys).__name__}",
                        shape,
                    )
                actual = set(raw_keys or ["v"]) if is_series(result) else set()
                missing = sorted(set(map(str, declared_keys)) - actual)
                if missing:
                    return _script_failure(
                        f"series no longer carries declared key(s) "
                        f"{missing} — it now has {sorted(actual)}; a chart bound "
                        "to the old keys would render nothing",
                        shape,
                    )
            if shape == "records" and not (
                isinstance(result, list) and all(isinstance(r, dict) for r in result)
            ):
                # Every ELEMENT must be a mapping, not just the container
                # (codex P2): `["offline"]` passed a list-only check, and a
                # Repeat child binding a relative `name` against a scalar item
                # resolves to undefined — blank rows, and never routed through
                # _script_failure so no reason is reported anywhere.
                bad = (
                    type(result).__name__
                    if not isinstance(result, list)
                    else "a list containing non-object items"
                )
                return _script_failure(
                    f"script declared shape 'records' but returned {bad} — "
                    "return a list of dicts",
                    shape,
                )
            return result

        registry.register("agent_script", agent_script)

    return registry
