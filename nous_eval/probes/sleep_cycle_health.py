"""Sleep-cycle health monitor — passive aggregator over recent sleep_completed events.

The sleep cycle runs ~10 phases nightly per agent (review_decisions,
prune, compress, reflect, resolve_contradictions, stale_scan,
cluster_consolidation, graph_densification, generalize, evolve_rubric).
Each phase emits stats into the ``sleep_completed`` event payload.

This probe reads the past N sleep_completed events from prod and
verdicts each phase against sanity bounds. It catches the failure mode
that is hardest to notice: a phase silently doing nothing for many
consecutive cycles. We discovered three such cases on 2026-05-03:

  - ``procedures_created = 0`` for 15 consecutive cycles → F012 K-Line
    procedure synthesis silently broken.
  - ``stale_deactivated = 0`` always → stale_scan never fires.
  - ``clusters_merged = 0`` always → F027 cluster consolidation
    rejects every candidate.

The bounds are intentionally conservative. A phase that legitimately
finds nothing is fine — the warning fires only when N consecutive
cycles all return zero on a metric the phase is designed to produce.

Connects to live PROD READ-ONLY (default 192.168.1.141:5432).

Run:
    set -a; source .env; set +a
    uv run python -m nous_eval.probes.sleep_cycle_health

Exit code (with --strict):
    0 — all phases healthy across the window
    1 — at least one phase shows a silent-failure pattern
    2 — env / connection error
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import asyncpg


# Phases we track and the field in event.data that signals the phase did work.
# A "zero across N consecutive cycles" pattern means the phase silently
# produced nothing — almost certainly a bug since N=14 nightly cycles
# represent ~2 weeks of typical agent activity.
@dataclass(frozen=True)
class PhaseSpec:
    name: str
    activity_field: str
    description: str


# Order chosen to match sleep_handler.py phase order so the report reads
# top-to-bottom in execution order.
PHASES: tuple[PhaseSpec, ...] = (
    PhaseSpec("reflect", "facts_created",
              "LLM cross-session pattern extraction → facts"),
    PhaseSpec("resolve_contradictions", "contradictions_resolved",
              "F031 SUPERSEDE/MERGE/REMOVE actions on contradicting fact pairs"),
    PhaseSpec("stale_scan", "stale_deactivated",
              "Deactivate facts older than the staleness threshold"),
    PhaseSpec("cluster_consolidation", "clusters_merged",
              "F027 merge clusters of similar facts into one"),
    PhaseSpec("graph_densification", "orphan_edges_created",
              "F040 create cross-type edges for orphan nodes"),
    PhaseSpec("graph_densification_ce", "ce_backfill_survived",
              "F042 cross-encoder validates densification candidates"),
    PhaseSpec("bridge_linker", "bridge_edges_created",
              "F022 cross-type bridge edges from heart seeds"),
    PhaseSpec("procedures", "procedures_created",
              "F012 K-Line synthesis from decision clusters"),
)


# Verdict thresholds. ``zero_warn_after`` is how many consecutive cycles
# of zero output before we cry foul on a phase. Lower for hot paths
# (reflect should produce >=1 fact most cycles), higher for legitimately-
# rare phases (procedures only synthesize when clusters exist).
@dataclass(frozen=True)
class PhaseBounds:
    zero_warn_after: int
    notes: str = ""


# Bounds calibrated from 15 sleep cycles 2026-04-26 to 2026-05-02 on
# agent_id=nous-default. See PR description for derivation.
_DEFAULT_BOUNDS: dict[str, PhaseBounds] = {
    "reflect": PhaseBounds(
        zero_warn_after=5,
        notes="Healthy prod baseline: 3-5 facts per cycle.",
    ),
    "resolve_contradictions": PhaseBounds(
        zero_warn_after=10,
        notes="Resolves 0-2 of 10 found per cycle (most downgrade to KEEP_BOTH).",
    ),
    "stale_scan": PhaseBounds(
        zero_warn_after=7,
        notes="Should find SOME stale facts within 1 week on an active agent.",
    ),
    "cluster_consolidation": PhaseBounds(
        zero_warn_after=14,
        notes="Cluster merges are rare (high similarity threshold) but shouldn't be 0 across 2 weeks.",
    ),
    "graph_densification": PhaseBounds(
        zero_warn_after=5,
        notes="0-58 edges per cycle baseline; 5+ zero cycles is a strong signal.",
    ),
    "graph_densification_ce": PhaseBounds(
        zero_warn_after=5,
        notes="CE survivors range 17-98 per cycle baseline.",
    ),
    "bridge_linker": PhaseBounds(
        zero_warn_after=14,
        notes="Bridge edges are rare (mostly 0, occasional 1-20). Long zero-streak common.",
    ),
    "procedures": PhaseBounds(
        zero_warn_after=21,
        notes=("Procedure synthesis requires successful-decision clusters; "
               "low-traffic or debugging-heavy agents can legitimately go "
               "2+ weeks between qualifying clusters. 21-cycle floor avoids "
               "false-positives on those agents."),
    ),
}


# Per-agent overrides — populate from env if needed.
# E.g., a fresh agent with little history would want laxer bounds across
# the board. Keep empty by default; the _DEFAULT_BOUNDS above are
# calibrated for active agents like nous-default.
_AGENT_OVERRIDES: dict[str, dict[str, PhaseBounds]] = {}


@dataclass
class CycleStat:
    cycle_at: object  # asyncpg returns a datetime
    data: dict


async def fetch_recent_cycles(
    conn: asyncpg.Connection, agent_id: str, n: int,
) -> list[CycleStat]:
    """Return the most recent ``n`` sleep_completed events for the agent."""
    rows = await conn.fetch(
        """
        SELECT created_at, data
        FROM nous_system.events
        WHERE event_type = 'sleep_completed'
          AND agent_id = $1
        ORDER BY created_at DESC
        LIMIT $2
        """,
        agent_id, n,
    )
    out: list[CycleStat] = []
    for r in rows:
        d = r["data"]
        if isinstance(d, str):
            try:
                d = json.loads(d)
            except json.JSONDecodeError:
                d = {}
        out.append(CycleStat(cycle_at=r["created_at"], data=d))
    return out


def analyze_phase(
    cycles: list[CycleStat], phase: PhaseSpec, bounds: PhaseBounds,
) -> dict:
    """For one phase, compute aggregate stats + verdict across cycles.

    cycles is in DESCENDING-time order (most recent first). The
    "consecutive zeros from now" calc walks forward until it finds a
    non-zero or runs out of cycles.
    """
    values: list[float] = []
    for c in cycles:
        v = c.data.get(phase.activity_field)
        if v is None:
            continue
        try:
            values.append(float(v))
        except (TypeError, ValueError):
            continue
    n = len(values)
    if n == 0:
        return {
            "phase": phase.name,
            "field": phase.activity_field,
            "n_cycles": 0,
            "verdict": "NO_DATA",
            "verdict_reason": "field absent in all sampled cycles",
            "min": None, "max": None, "mean": None, "median": None,
            "zero_streak": None,
        }

    # Consecutive zeros starting from the most-recent cycle. Walk the
    # SAME ordering we received cycles in — caller passes
    # most-recent-first per fetch_recent_cycles(), and we need
    # last_nonzero_cycle_at to align with the same scan.
    streak = 0
    last_nonzero_at = None
    for c, v in zip(cycles, [c.data.get(phase.activity_field) for c in cycles]):
        try:
            v_num = float(v) if v is not None else None
        except (TypeError, ValueError):
            continue
        if v_num is None:
            continue
        if v_num == 0:
            streak += 1
        else:
            last_nonzero_at = c.cycle_at
            break

    sorted_v = sorted(values)
    median = sorted_v[n // 2] if n % 2 == 1 else (sorted_v[n // 2 - 1] + sorted_v[n // 2]) / 2
    mean = sum(values) / n

    if streak >= bounds.zero_warn_after:
        verdict = "RED"
        reason = (f"{streak} consecutive zeros — "
                  f"{phase.name} appears silently broken")
    elif streak >= max(2, bounds.zero_warn_after // 2):
        verdict = "YELLOW"
        reason = (f"{streak} consecutive zeros — "
                  f"watch for {bounds.zero_warn_after}+ to escalate")
    else:
        verdict = "GREEN"
        reason = (f"min={min(values):g}, max={max(values):g}, "
                  f"mean={mean:.1f}, recent zero-streak={streak}")
    return {
        "phase": phase.name,
        "field": phase.activity_field,
        "n_cycles": n,
        "verdict": verdict,
        "verdict_reason": reason,
        "min": min(values), "max": max(values),
        "mean": mean, "median": median,
        "zero_streak": streak,
        # Surfaces "when did this phase last actually do something?" so
        # an operator triaging a RED verdict can immediately see
        # whether the phase has been broken since day one or just
        # regressed recently.
        "last_nonzero_cycle_at": last_nonzero_at,
    }


def overall_exit_code(per_phase: list[dict]) -> int:
    """1 if any phase RED, else 0."""
    return 1 if any(p["verdict"] == "RED" for p in per_phase) else 0


async def run(
    conn: asyncpg.Connection, agent_id: str, n_cycles: int,
    bounds: dict[str, PhaseBounds] | None = None,
) -> dict:
    """Fetch + analyze. Caller owns connection lifetime."""
    bounds = bounds or _DEFAULT_BOUNDS
    cycles = await fetch_recent_cycles(conn, agent_id, n_cycles)
    per_phase = [
        analyze_phase(cycles, p, bounds[p.name])
        for p in PHASES
    ]
    return {
        "agent_id": agent_id,
        "n_cycles_requested": n_cycles,
        "n_cycles_found": len(cycles),
        "earliest_cycle": cycles[-1].cycle_at if cycles else None,
        "latest_cycle": cycles[0].cycle_at if cycles else None,
        "phases": per_phase,
    }


def _print_report(result: dict) -> None:
    # Use ASCII-only output so this runs on Windows consoles without
    # UTF-8 enabled (the sys.stdout.reconfigure earlier is best-effort
    # and wrapped in a bare except).
    print()
    print("=" * 88)
    print(f"SLEEP CYCLE HEALTH - agent={result['agent_id']}, "
          f"window={result['n_cycles_found']} cycles")
    if result["earliest_cycle"] and result["latest_cycle"]:
        print(f"  range: {result['earliest_cycle']} -> "
              f"{result['latest_cycle']}")
    print("=" * 88)
    print()
    if result["n_cycles_found"] == 0:
        print("  NO sleep_completed events found in this window.")
        return
    header = (f"  {'phase':<24}  {'field':<24}  "
              f"{'min':>4}  {'max':>4}  {'mean':>6}  "
              f"{'streak':>6}  verdict")
    print(header)
    print("  " + "-" * (len(header) - 2))
    # ASCII-safe verdict markers (avoids UnicodeEncodeError on cp1252)
    _MARKERS = {"GREEN": "[OK]  ", "YELLOW": "[WARN]", "RED": "[FAIL]"}
    for p in result["phases"]:
        if p["verdict"] == "NO_DATA":
            print(f"  {p['phase']:<24}  {p['field']:<24}  "
                  f"  -     -       -       -    NO_DATA")
            continue
        marker = _MARKERS[p["verdict"]]
        print(f"  {p['phase']:<24}  {p['field']:<24}  "
              f"{p['min']:>4.0f}  {p['max']:>4.0f}  "
              f"{p['mean']:>6.1f}  {p['zero_streak']:>6}  "
              f"{marker} {p['verdict']}")
    print()
    # Surface RED reasons for quick triage; show last_nonzero_cycle_at
    # so the operator can immediately tell whether the phase regressed
    # recently or has been broken since day one.
    reds = [p for p in result["phases"] if p["verdict"] == "RED"]
    if reds:
        print("  RED phases (silent-failure pattern):")
        for p in reds:
            last_seen = (p.get("last_nonzero_cycle_at")
                         or "never in this window")
            print(f"    - {p['phase']}: {p['verdict_reason']}")
            print(f"        last non-zero output: {last_seen}")
        print()


def _build_md(result: dict) -> list[str]:
    md = [
        f"# Sleep cycle health — agent_id=`{result['agent_id']}`",
        f"- cycles in window: **{result['n_cycles_found']}** "
        f"(requested {result['n_cycles_requested']})",
    ]
    if result["earliest_cycle"] and result["latest_cycle"]:
        md.append(f"- range: {result['earliest_cycle']} → "
                  f"{result['latest_cycle']}")
    md += [
        "",
        "## Per-phase",
        "",
        "| phase | field | min | max | mean | recent zero-streak | verdict |",
        "|---|---|---:|---:|---:|---:|---|",
    ]
    for p in result["phases"]:
        if p["verdict"] == "NO_DATA":
            md.append(f"| {p['phase']} | `{p['field']}` | – | – | – | – | NO_DATA |")
            continue
        md.append(
            f"| {p['phase']} | `{p['field']}` | "
            f"{p['min']:.0f} | {p['max']:.0f} | {p['mean']:.1f} | "
            f"{p['zero_streak']} | **{p['verdict']}** |"
        )
    reds = [p for p in result["phases"] if p["verdict"] == "RED"]
    if reds:
        md += ["", "## Silent-failure phases", ""]
        for p in reds:
            md.append(f"- **{p['phase']}** — {p['verdict_reason']}")
    return md


async def _async_main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

    p = argparse.ArgumentParser(
        description="Sleep-cycle health monitor — silent-failure detector "
                    "across the past N sleep_completed events.",
    )
    p.add_argument("--prod-host",
                   default=os.environ.get("PROD_DB_HOST", "192.168.1.141"))
    p.add_argument("--prod-port", type=int,
                   default=int(os.environ.get("DB_PORT", "5432")))
    p.add_argument("--prod-user", default=os.environ.get("DB_USER", "nous"))
    p.add_argument("--prod-password", default=os.environ.get("DB_PASSWORD"))
    p.add_argument("--prod-db", default=os.environ.get("DB_NAME", "nous"))
    p.add_argument("--agent-id", default="nous-default")
    p.add_argument("--n-cycles", type=int, default=14,
                   help="Number of recent sleep_completed events to aggregate.")
    p.add_argument("--strict", action="store_true",
                   help="Exit 1 if any phase shows a silent-failure pattern.")
    p.add_argument("--out", type=Path,
                   default=Path("reports/eval_sleep_cycle_health.md"))
    p.add_argument("--out-json", type=Path,
                   default=Path("reports/eval_sleep_cycle_health.json"))
    args = p.parse_args(argv)

    if not args.prod_password:
        # The env var is named DB_PASSWORD (shared with main DB), not
        # PROD_DB_PASSWORD. Spell that out so an operator who set the
        # latter (mirroring PROD_DB_HOST) sees the right name to use.
        print(
            "ERROR: prod database password not set. Either set the env "
            "var DB_PASSWORD or pass --prod-password=<value>.",
            file=sys.stderr,
        )
        return 2

    conn = await asyncpg.connect(
        host=args.prod_host, port=args.prod_port,
        user=args.prod_user, password=args.prod_password,
        database=args.prod_db,
    )
    try:
        # Defense-in-depth: read-only at the Postgres session level.
        # Mirrors the F058 probe pattern.
        await conn.execute("SET default_transaction_read_only = on")
        result = await run(conn, args.agent_id, args.n_cycles)
    finally:
        await conn.close()

    _print_report(result)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out_json.write_text(
        json.dumps(result, indent=2, default=str), encoding="utf-8"
    )
    args.out.write_text("\n".join(_build_md(result)), encoding="utf-8")
    print(f"Wrote: {args.out}")
    print(f"Wrote: {args.out_json}")

    if args.strict:
        return overall_exit_code(result["phases"])
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_async_main(argv))


if __name__ == "__main__":
    raise SystemExit(main())
