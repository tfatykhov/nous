"""F105: read-only replay of spreading activation (SA) seeds, with offline metrics.

Replays the seeds that live retrievals recorded in
``nous_system.retrieval_log.expansions`` through ``spreading_activation_search``
on a CLONE database, once per arm, and reports per arm: rows above the floor,
Jaccard@20 against the baseline arm, hub share, and latency p50/p95. Every arm is
built on the A2 arm's resolved config (``--base-config``), read by named keys
only. It is the offline gate of every later F105 task: a screen, not a
measurement of answer quality.

Expansion element keys, confirmed on the A2 clone (mab26_p_base) on 2026-10-26
with ``SELECT jsonb_pretty(e) FROM nous_system.retrieval_log r,
jsonb_array_elements(r.expansions) e``: ``seed_id``, ``seed_type``,
``seed_score``, ``neighbor_id``, ``neighbor_type``, ``stage``, ``hop``,
``edge_relation``, ``edge_weight``, ``path_strength``, ``extraction_method``,
``won_best_path``. SA rows have ``stage = 'stage4_spreading_activation'`` and
``edge_relation = 'spreading_activation'``; the other stage in the log,
``stage2b_heart_graph_memory`` (Path A), is not an SA seed and is skipped. Before
#807 an SA row was recorded under ``seed_type = 'multi'`` with a null
``seed_score``; those are skipped too.

Limitations, also written into every report:
  * ``expansions`` holds only the seeds whose path won at least one rendered
    row, so a replayed seed set is a subset of the live one.
  * The live call also excluded the retrieval's other candidates (heart and
    chunk hits, Path A neighbours). Only the seeds are known here, so a
    replayed window can hold rows the live window did not.

Read-only: every connection starts with ``default_transaction_read_only = on``
(asyncpg ``server_settings``; the clone's own default is off), and the script
checks it before reading.

Prod guard: the prod host is named by ``$PROD_HOST`` and/or ``--prod-host``
(compared case-insensitively), never by a literal here. A DSN on it is refused
unless ``--allow-prod-readonly`` is passed. With no prod host named, any host
that is not loopback (``localhost``, ``127.0.0.0/8``, ``::1``) might be prod, so
it is refused too unless ``--allow-prod-readonly`` is passed.

Usage (from the repo root):
  python scripts/diag/sa_replay.py --dsn postgresql://USER:PW@localhost:5433/mab26_p_base \\
      --agent all --since 2026-10-25T17:39:00Z --limit 300 \\
      --base-config PATH/TO/A2.env --arm baseline \\
      --arm 'deep=NOUS_SPREADING_ACTIVATION_MAX_DEPTH=3' --out report.json
``--agent`` repeats; ``all`` means every agent with post-#807 SA expansions
since ``--since``. ``--limit`` is per agent. ``baseline`` is the A2 base with no
override and always runs; each ``--arm name=KEY=VAL[,KEY=VAL]`` is applied on
top of the A2 base.
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402

# The pipeline's own window constants, so the replay cannot drift from them.
from nous.api.retrieval_pipeline import (  # noqa: E402
    _SPREADING_OVERFETCH_LIMIT,
    _SPREADING_RESULT_CAP,
)
from nous.brain.graph_constants import autobehavior_exclusion_sql  # noqa: E402
from nous.brain.spreading_activation import spreading_activation_search  # noqa: E402
from nous.config import Settings  # noqa: E402

SA_STAGE = "stage4_spreading_activation"
BASELINE = "baseline"
REPLAY_LIMIT = _SPREADING_OVERFETCH_LIMIT  # rows the pipeline asks the CTE for
JACCARD_K = _SPREADING_RESULT_CAP  # rows the pipeline can append
HUB_MIN_DEGREE = 50  # F105 plan, Task 3 gate
# Names both the summary key and the header field, so they cannot drift apart.
HUB_SHARE_METHOD = "seed_degree_only"

# A silent default for any of these would make the parity gate meaningless, so
# each must appear in the base config file, set or declared "# unset: KEY".
PARITY_KEYS: tuple[str, ...] = (
    "NOUS_SPREADING_SCORE_DEPTH1_PARITY",
    "NOUS_GRAPH_NEIGHBOR_SEED_SCORE_ENABLED",
    "NOUS_GRAPH_INFERRED_EDGE_PENALTY",
    "NOUS_SPREADING_ACTIVATION_DECAY",
)
# Every setting the CTE and the SA score path read. The two Stage 4 gates
# (graph_recall_enabled, spreading_activation_density_threshold) decide only
# whether SA runs, and the replay always runs the CTE, so they are left out.
BASE_CONFIG_KEYS: tuple[str, ...] = (
    "NOUS_SPREADING_ACTIVATION_ENABLED",
    "NOUS_SPREADING_ACTIVATION_DECAY",
    "NOUS_SPREADING_ACTIVATION_MAX_DEPTH",
    "NOUS_SPREADING_ACTIVATION_FLOOR",
    "NOUS_SPREADING_SCORE_DEPTH1_PARITY",
    "NOUS_GRAPH_NEIGHBOR_SEED_SCORE_ENABLED",
    "NOUS_GRAPH_INFERRED_EDGE_PENALTY",
    "NOUS_GRAPH_RECALL_DECAY",
    "NOUS_GRAPH_RECALL_MAX_EXPAND",
    "NOUS_DECISION_OUTCOME_SCORE_FACTORS",
    "NOUS_GRAPH_ADJACENCY_BOOST_EXCLUDE_DETERMINISTIC",
)

LIMITATIONS: tuple[str, ...] = (
    "expansions only holds winning seeds, so replayed seed sets are a subset of the live ones",
    "the live call also excluded the retrieval's other candidates; the replay excludes only the "
    "seeds, so a replayed window can hold rows the live window did not",
    "a screen, not a measurement of answer quality",
)
HUB_SHARE_NOTE = (
    f"{HUB_SHARE_METHOD} (weaker): every row is judged by the degree of its winning seed. Exact "
    "for depth-1 rows; a depth-2 row should be judged by the node it was reached from, which "
    "needs Task 3's parent_out"
)


@dataclass
class ReplaySeed:
    """One retrieval's recorded SA seeds, as ``(node_id, node_type, score)``."""

    retrieval_id: str
    agent_id: str
    query: str
    seeds: list[tuple[UUID, str, float]]


@dataclass
class ArmRun:
    """One arm's CTE result for one retrieval.

    ``rows`` are ``spreading_activation_search``'s ``(id, type, activation,
    depth)``; ``seed_of`` maps each row to its winning path's seed.
    """

    rows: list[tuple[UUID, str, float, int]]
    seconds: float
    seed_of: dict[UUID, UUID] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def jaccard_at_k(a: list[UUID], b: list[UUID], k: int) -> float:
    """Jaccard overlap of the top ``k`` of two ranked id lists (two empty windows: 1.0)."""
    sa, sb = set(a[:k]), set(b[:k])
    if not sa and not sb:
        return 1.0
    return len(sa & sb) / len(sa | sb)


def percentile(xs: list[float], p: float) -> float:
    """The ``p``-th percentile (0-100), linear interpolation between closest ranks."""
    if not xs:
        raise ValueError("percentile of an empty list")
    s = sorted(xs)
    rank = (len(s) - 1) * p / 100.0
    lo = int(rank)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (rank - lo)


def hub_share(
    rows: list[tuple[UUID, str, float, int]],
    parents: dict[UUID, UUID],
    seeds: set[UUID],
    degree: dict[UUID, int],
    min_degree: int,
) -> float:
    """Share of SA rows whose winning path leaves a node of degree >= ``min_degree``.

    ``parents[row]`` is the node the row's winning path left: its seed for a
    depth-1 row, the via node for a depth-2 row (Task 3's ``parent_out``).
    Until Task 3 the replay passes the winning seed instead, which is exact for
    depth-1 rows only (``HUB_SHARE_NOTE``). A seed's own row leaves nothing and
    is not counted; a row with no parent counts as not leaving a hub.
    """
    counted = [r for r in rows if r[0] not in seeds]
    if not counted:
        raise ValueError("hub share of no SA rows")
    hits = sum(1 for r in counted if degree.get(parents.get(r[0]), 0) >= min_degree)
    return hits / len(counted)


# ---------------------------------------------------------------------------
# Seeds and degrees
# ---------------------------------------------------------------------------


def parse_replay_seeds(rows: Iterable[Mapping[str, Any]]) -> list[ReplaySeed]:
    """Turn ``retrieval_log`` rows into replay seeds; retrievals with none are dropped.

    A seed is taken from an SA-stage expansion with a real ``seed_type`` (not the
    pre-#807 ``multi``) and a numeric ``seed_score``. A seed named by several
    expansions of one retrieval is kept once, in first-seen order.
    """
    out: list[ReplaySeed] = []
    for row in rows:
        expansions = row["expansions"]
        if isinstance(expansions, str):
            expansions = json.loads(expansions)
        seeds: dict[UUID, tuple[UUID, str, float]] = {}
        for e in expansions or []:
            score = e.get("seed_score")
            if (
                e.get("stage") != SA_STAGE
                or e.get("seed_type") in (None, "multi")
                or not isinstance(score, (int, float))
                or isinstance(score, bool)
            ):
                continue
            sid = UUID(e["seed_id"])
            seeds.setdefault(sid, (sid, e["seed_type"], float(score)))
        if seeds:
            out.append(ReplaySeed(
                retrieval_id=row["id"], agent_id=row["agent_id"],
                query=row["query"] or "", seeds=list(seeds.values()),
            ))
    return out


# A retrieval qualifies when at least one of its expansions is a post-#807 SA seed.
_HAS_SA_SEED = f"""EXISTS (
    SELECT 1 FROM jsonb_array_elements(r.expansions) e
    WHERE e->>'stage' = '{SA_STAGE}' AND e->>'seed_type' <> 'multi'
      AND jsonb_typeof(e->'seed_score') = 'number')"""


async def load_replay_seeds(
    session: AsyncSession, agent_id: str, since: datetime, limit: int
) -> list[ReplaySeed]:
    """The agent's first ``limit`` retrievals since ``since`` that recorded SA seeds."""
    result = await session.execute(text(f"""
        SELECT r.id, r.agent_id, r.query, r.expansions
        FROM nous_system.retrieval_log r
        WHERE r.agent_id = :agent_id AND r.timestamp >= :since AND {_HAS_SA_SEED}
        ORDER BY r.timestamp, r.id
        LIMIT :limit
    """), {"agent_id": agent_id, "since": since, "limit": int(limit)})
    return parse_replay_seeds(result.mappings().all())


async def list_replay_agents(session: AsyncSession, since: datetime) -> list[str]:
    """Every agent with post-#807 SA seeds since ``since`` (``--agent all``)."""
    result = await session.execute(text(f"""
        SELECT DISTINCT r.agent_id FROM nous_system.retrieval_log r
        WHERE r.timestamp >= :since AND {_HAS_SA_SEED}
        ORDER BY 1
    """), {"since": since})
    return list(result.scalars().all())


async def load_degrees(session: AsyncSession, agent_id: str) -> dict[UUID, int]:
    """Each node's degree over the edges the CTE can traverse (one GROUP BY).

    Undirected and agent-scoped like the traversal, under the same
    ``autobehavior_exclusion_sql`` predicate; a self-loop counts once.
    """
    excl = autobehavior_exclusion_sql()
    result = await session.execute(text(f"""
        SELECT node_id, COUNT(*) AS degree FROM (
            SELECT source_id AS node_id FROM brain.graph_edges
            WHERE agent_id = :agent_id AND {excl}
            UNION ALL
            SELECT target_id FROM brain.graph_edges
            WHERE agent_id = :agent_id AND {excl} AND target_id <> source_id
        ) ends
        GROUP BY node_id
    """), {"agent_id": agent_id})
    return {row.node_id: int(row.degree) for row in result}


async def run_arm(session: AsyncSession, rs: ReplaySeed, settings: Settings) -> ArmRun:
    """Run the CTE for one retrieval under one arm's settings, timed.

    Same window as the pipeline (``REPLAY_LIMIT``), with the seeds excluded as
    the pipeline excludes them.
    """
    seed_of: dict[UUID, UUID] = {}
    t0 = time.perf_counter()
    rows = await spreading_activation_search(
        session, rs.agent_id, rs.seeds, settings,
        limit=REPLAY_LIMIT, exclude_ids={s[0] for s in rs.seeds}, seed_out=seed_of,
    )
    return ArmRun(rows=rows, seconds=time.perf_counter() - t0, seed_of=seed_of)


# ---------------------------------------------------------------------------
# Base config and arms
# ---------------------------------------------------------------------------


def _env_names() -> dict[str, str]:
    """``NOUS_*`` env name -> Settings field, as pydantic-settings resolves them."""
    out: dict[str, str] = {}
    for name, info in Settings.model_fields.items():
        alias = info.validation_alias
        out[alias.upper() if isinstance(alias, str) else f"NOUS_{name.upper()}"] = name
    return {k: v for k, v in out.items() if k.startswith("NOUS_")}


def _field_name(key: str) -> str:
    name = _env_names().get(key)
    if name is None:
        # Settings has extra="ignore": an unknown key would silently be a no-op arm.
        raise ValueError(f"{key} is not a NOUS_ Settings field")
    return name


def _scan_env_file(env_file: Path, keys: Sequence[str]) -> tuple[dict[str, str], set[str]]:
    """The named keys' values, and the named keys declared ``# unset: KEY``."""
    wanted = set(keys)
    values: dict[str, str] = {}
    unset: set[str] = set()
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("#"):
            body = line[1:].strip()
            if body.startswith("unset:") and body[len("unset:"):].strip() in wanted:
                unset.add(body[len("unset:"):].strip())
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if sep and key in wanted:
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key] = value
    return values, unset - set(values)


def load_base_config(env_file: Path, keys: Sequence[str] = BASE_CONFIG_KEYS) -> dict[str, str]:
    """The base config's values for the named keys only; no other line is returned.

    A key declared ``# unset: KEY`` (how the A2 file records a setting prod does
    not set) takes the Settings default. Raises for a parity key the file does
    not mention at all.
    """
    values, unset = _scan_env_file(env_file, keys)
    missing = [k for k in PARITY_KEYS if k in keys and k not in values and k not in unset]
    if missing:
        raise ValueError(
            f"{env_file.name}: no line for parity key(s) {', '.join(missing)} "
            "(set it or declare '# unset: KEY')"
        )
    return values


def base_config_sources(env_file: Path, keys: Sequence[str] = BASE_CONFIG_KEYS) -> dict[str, str]:
    """Per named key: ``file`` (set there), ``unset`` (declared unset) or ``default``."""
    values, unset = _scan_env_file(env_file, keys)
    return {k: "file" if k in values else "unset" if k in unset else "default" for k in keys}


@contextmanager
def _isolated_nous_env(values: Mapping[str, str]) -> Iterator[None]:
    """``os.environ`` with every ``NOUS_*`` entry replaced by ``values``, then restored."""
    saved = {k: v for k, v in os.environ.items() if k.upper().startswith("NOUS_")}
    for k in saved:
        del os.environ[k]
    os.environ.update(values)
    try:
        yield
    finally:
        for k in [k for k in os.environ if k.upper().startswith("NOUS_")]:
            del os.environ[k]
        os.environ.update(saved)


def build_settings(base: Mapping[str, str], overrides: Mapping[str, str] | None = None) -> Settings:
    """Settings from ``base`` plus ``overrides`` only: no ``.env`` file, no ambient ``NOUS_*``.

    Built through the environment, as the app builds it, so values are parsed
    and validated (``model_copy(update=...)`` would skip both).
    """
    env = {**base, **(overrides or {})}
    for key in env:
        _field_name(key)
    with _isolated_nous_env(env):
        return Settings(_env_file=None)


_OVERRIDE_SPLIT = re.compile(r",(?=NOUS_[A-Z0-9_]*=)")


def parse_arm(spec: str) -> tuple[str, dict[str, str]]:
    """``baseline`` or ``name=KEY=VAL[,KEY=VAL]``; a value may contain commas (JSON)."""
    if spec == BASELINE:
        return BASELINE, {}
    name, sep, rest = spec.partition("=")
    name = name.strip()
    if not sep or not name or not rest:
        raise ValueError(f"arm {spec!r}: expected 'baseline' or name=KEY=VAL[,KEY=VAL]")
    if name == BASELINE:
        raise ValueError("the baseline arm is the base config and takes no overrides")
    overrides: dict[str, str] = {}
    for part in _OVERRIDE_SPLIT.split(rest):
        key, sep, value = part.partition("=")
        key = key.strip()
        if not sep:
            raise ValueError(f"arm {name!r}: {part!r} is not KEY=VAL")
        _field_name(key)
        overrides[key] = value
    return name, overrides


def _norm_host(host: str | None) -> str:
    return (host or "").strip().strip("[]").rstrip(".").lower()


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_dsn_host(dsn: str, allow_prod: bool, prod_host: str | None = None) -> None:
    """Exit unless the DSN's host is safe to read without ``allow_prod``.

    The prod hosts are ``prod_host`` (``--prod-host``) and ``$PROD_HOST``. With
    neither set, every host but loopback is treated as possibly prod (fail closed),
    and so is a DSN whose host cannot be read.
    """
    if allow_prod:
        return
    host = _norm_host(urlsplit(dsn).hostname)
    prod = {_norm_host(h) for h in (prod_host, os.environ.get("PROD_HOST")) if h and h.strip()}
    if not host:
        raise SystemExit("cannot read the DSN's host: pass --allow-prod-readonly to proceed")
    if host in prod:
        raise SystemExit(f"refusing the prod host {host}: replay a clone, or pass --allow-prod-readonly")
    if not prod and not _is_loopback(host):
        raise SystemExit(
            f"no prod host named (PROD_HOST or --prod-host), so the remote host {host} may be prod: "
            "name the prod host, or pass --allow-prod-readonly"
        )


class _Embedder(Protocol):
    async def embed(self, text: str) -> list[float]: ...


async def embed_queries(
    queries: Sequence[str], provider: _Embedder, cache_path: Path
) -> dict[str, list[float]]:
    """Embeddings of the non-empty queries, cached in a JSON file so a rerun is free."""
    cache: dict[str, list[float]] = (
        json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    )
    wanted = [q for q in dict.fromkeys(queries) if q]
    missing = [q for q in wanted if q not in cache]
    for q in missing:
        cache[q] = list(await provider.embed(q))
    if missing:
        tmp = cache_path.with_name(cache_path.name + ".tmp")
        tmp.write_text(json.dumps(cache), encoding="utf-8")
        tmp.replace(cache_path)
    return {q: cache[q] for q in wanted}


# ---------------------------------------------------------------------------
# Replay and report
# ---------------------------------------------------------------------------


def _resolved(settings: Settings, keys: Iterable[str]) -> dict[str, Any]:
    return {k: getattr(settings, _field_name(k)) for k in keys}


def _score_retrieval(
    rs: ReplaySeed, runs: dict[str, ArmRun], arms: dict[str, Settings], degree: dict[UUID, int]
) -> dict[str, Any]:
    """Per-arm metrics of one retrieval; rows count when above that arm's floor."""
    seed_ids = {s[0] for s in rs.seeds}
    above: dict[str, list[tuple[UUID, str, float, int]]] = {}
    for name, run in runs.items():
        floor = float(getattr(arms[name], "spreading_activation_floor", 0.1))
        above[name] = [r for r in run.rows if r[2] > floor]
    baseline_ids = [r[0] for r in above[BASELINE]]
    per_arm: dict[str, Any] = {}
    for name, run in runs.items():
        kept = above[name][:JACCARD_K]  # what the pipeline can append
        per_arm[name] = {
            "n_rows": len(run.rows),
            "n_above_floor": len(above[name]),
            "seconds": run.seconds,
            "jaccard_at_k": jaccard_at_k([r[0] for r in above[name]], baseline_ids, JACCARD_K),
            "n_kept": len(kept),
            "hub_share": (
                hub_share(kept, run.seed_of, seed_ids, degree, HUB_MIN_DEGREE) if kept else None
            ),
        }
    return {
        "retrieval_id": rs.retrieval_id, "agent_id": rs.agent_id,
        "n_seeds": len(rs.seeds), "arms": per_arm,
    }


def _summarise(retrievals: list[dict[str, Any]], arm_names: list[str]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for name in arm_names:
        per = [r["arms"][name] for r in retrievals]
        if not per:
            summary[name] = {"retrievals": 0}
            continue
        ms = [p["seconds"] * 1000 for p in per]
        kept = sum(p["n_kept"] for p in per)
        hubs = sum(p["hub_share"] * p["n_kept"] for p in per if p["hub_share"] is not None)
        entry: dict[str, Any] = {
            "retrievals": len(per),
            "retrievals_with_rows_above_floor": sum(1 for p in per if p["n_above_floor"]),
            "rows_above_floor": sum(p["n_above_floor"] for p in per),
            "rows_above_floor_mean": sum(p["n_above_floor"] for p in per) / len(per),
            f"jaccard_at_{JACCARD_K}_vs_baseline_mean": sum(p["jaccard_at_k"] for p in per) / len(per),
            f"hub_share_{HUB_SHARE_METHOD}": hubs / kept if kept else None,
            "latency_ms_p50": percentile(ms, 50),
            "latency_ms_p95": percentile(ms, 95),
        }
        if name != BASELINE:
            added = [
                (r["arms"][name]["seconds"] - r["arms"][BASELINE]["seconds"]) * 1000
                for r in retrievals
            ]
            entry["added_latency_ms_p95"] = percentile(added, 95)
        summary[name] = entry
    return summary


async def replay(
    session: AsyncSession, by_agent: dict[str, list[ReplaySeed]], arms: dict[str, Settings]
) -> list[dict[str, Any]]:
    """Run every arm on every retrieval. The arm order rotates per retrieval so
    no arm always pays the cold cache."""
    names = list(arms)
    out: list[dict[str, Any]] = []
    i = 0
    for agent_id, seeds in by_agent.items():
        if not seeds:
            continue
        degree = await load_degrees(session, agent_id)
        for rs in seeds:
            order = names[i % len(names):] + names[: i % len(names)]
            i += 1
            runs = {name: await run_arm(session, rs, arms[name]) for name in order}
            out.append(_score_retrieval(rs, runs, arms, degree))
            await session.rollback()  # end the read-only transaction per retrieval
    return out


def _git_head() -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parent,
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--dsn", required=True, help="postgresql://... of a CLONE database")
    p.add_argument("--agent", action="append", required=True,
                   help="agent id (repeatable), or 'all'")
    p.add_argument("--since", required=True, type=datetime.fromisoformat,
                   help="ISO timestamp with zone, e.g. 2026-10-25T17:39:00Z")
    p.add_argument("--limit", type=int, default=300, help="retrievals per agent")
    p.add_argument("--base-config", required=True, type=Path,
                   help="the A2 arm's env file (read by named keys only)")
    p.add_argument("--arm", action="append", default=[],
                   help="'baseline' or name=KEY=VAL[,KEY=VAL] (repeatable)")
    p.add_argument("--out", required=True, type=Path, help="report JSON path")
    p.add_argument("--prod-host", help="the prod database host to refuse (also $PROD_HOST)")
    p.add_argument("--allow-prod-readonly", action="store_true")
    p.add_argument("--embed-queries", action="store_true",
                   help="embed each replayed query (for Task 5 arms); needs OPENAI_API_KEY")
    p.add_argument("--embedding-cache", type=Path,
                   help="query embedding cache (default: next to --out)")
    args = p.parse_args(argv)
    if args.since.tzinfo is None:
        p.error("--since needs a time zone (e.g. a trailing Z)")
    return args


async def _main(args: argparse.Namespace) -> dict[str, Any]:
    base_values = load_base_config(args.base_config)
    sources = base_config_sources(args.base_config)
    specs = [parse_arm(s) for s in args.arm]
    if BASELINE not in [n for n, _ in specs]:
        specs.insert(0, (BASELINE, {}))
    if len({n for n, _ in specs}) != len(specs):
        raise SystemExit("arm names must be unique")
    arms = {name: build_settings(base_values, over) for name, over in specs}
    base = arms[BASELINE]

    url = args.dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    engine = create_async_engine(
        url, connect_args={"server_settings": {"default_transaction_read_only": "on"}},
    )
    try:
        async with AsyncSession(engine) as session:
            read_only = (await session.execute(text("SHOW transaction_read_only"))).scalar()
            if read_only != "on":
                raise SystemExit(f"session is not read-only (transaction_read_only={read_only})")
            agents: list[str] = []
            for a in args.agent:
                agents.extend(await list_replay_agents(session, args.since) if a == "all" else [a])
            agents = list(dict.fromkeys(agents))
            by_agent = {a: await load_replay_seeds(session, a, args.since, args.limit) for a in agents}
            await session.rollback()

            embeddings = None
            if args.embed_queries:
                from nous.brain.embeddings import EmbeddingProvider

                if not base.openai_api_key:
                    raise SystemExit("--embed-queries needs OPENAI_API_KEY")
                cache = args.embedding_cache or args.out.with_name(args.out.stem + ".query_embeddings.json")
                provider = EmbeddingProvider(
                    api_key=base.openai_api_key, model=base.embedding_model,
                    dimensions=base.embedding_dimensions,
                )
                try:
                    vecs = await embed_queries(
                        [rs.query for seeds in by_agent.values() for rs in seeds], provider, cache)
                finally:
                    await provider.close()
                embeddings = {"cache": str(cache), "queries": len(vecs)}

            retrievals = await replay(session, by_agent, arms)
    finally:
        await engine.dispose()

    dsn = urlsplit(args.dsn)
    names = list(arms)
    return {
        "header": {
            "tool": "scripts/diag/sa_replay.py",
            "git_head": _git_head(),
            "generated_at": datetime.now(UTC).isoformat(),
            "database": {"host": dsn.hostname, "port": dsn.port, "name": dsn.path.lstrip("/")},
            "transaction_read_only": read_only,
            "agents": agents,
            "since": args.since.isoformat(),
            "limit_per_agent": args.limit,
            "replay_limit": REPLAY_LIMIT,
            "jaccard_k": JACCARD_K,
            "hub_min_degree": HUB_MIN_DEGREE,
            "hub_share_method": HUB_SHARE_METHOD,
            "hub_share_note": HUB_SHARE_NOTE,
            "base_config": {
                "file": str(args.base_config),
                "keys": {
                    k: {"value": v, "source": sources[k]}
                    for k, v in _resolved(base, BASE_CONFIG_KEYS).items()
                },
            },
            "arms": {name: _resolved(arms[name], over) for name, over in specs},
            "seed_types": dict(Counter(
                s[1] for seeds in by_agent.values() for rs in seeds for s in rs.seeds)),
            "query_embeddings": embeddings,
            "limitations": list(LIMITATIONS),
        },
        "summary": _summarise(retrievals, names),
        "retrievals": retrievals,
    }


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point: replay, write the report, print the per-arm summary."""
    args = _parse_args(argv)
    check_dsn_host(args.dsn, args.allow_prod_readonly, args.prod_host)
    report = asyncio.run(_main(args))
    args.out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report["summary"], indent=2))
    print(f"wrote {args.out} ({len(report['retrievals'])} retrievals)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
