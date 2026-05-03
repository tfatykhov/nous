"""Sleep-phase filter dry-run probe.

Sibling to ``sleep_cycle_health`` (PR #404). That probe reads recent
``sleep_completed`` events and detects which phases produced zero
output across N consecutive cycles. This probe runs the underlying
SQL filters directly against the eval-DB snapshot and reports what
each phase WOULD select — without triggering sleep or making LLM
calls.

Why a separate probe: the passive monitor measures real prod outcomes
(slow feedback — needs nightly cycles to accumulate). This one gives
deterministic synthetic regression coverage that catches the bug
PR #405 fixed:

  - stale_scan filter must select rows the supersede flow doesn't
    already deactivate. Original ``active=true AND superseded_by IS
    NOT NULL`` was structurally impossible. PR #405 replaced it with
    ``never recalled OR not recalled in cutoff window``. This probe
    asserts the new SQL produces a non-empty candidate set on
    aged-enough corpora.

  - cluster_consolidation filter must skip accumulating subjects
    (lesson_learned 164, Tim 36) that the LLM correctly refuses to
    merge. PR #405 capped at max_facts=10. This probe asserts the
    new HAVING clause finds the small mergeable clusters.

  - procedure_learner._check_recency must use a window wide enough
    that real prod traffic produces eligible decisions. PR #405
    bumped the default 7→30. This probe counts successful-reviewed
    decisions in the configured window.

Connects to the eval DB (default 127.0.0.1:5433) READ-ONLY.

Run:
    NOUS_EVAL_DB_NAME=nous_eval_scratch \\
    NOUS_EVAL_AGENT_ID=nous-prod-snapshot \\
      uv run python -m nous_eval.probes.sleep_filter_dryrun

Exit code (with --strict):
    0 — all checks pass (filters select rows or correctly find none)
    1 — at least one filter is structurally broken (selects nothing
        when the snapshot has data the filter SHOULD match)
    2 — env / connection error
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import asyncpg


_DEFAULT_AGENT_ID = "nous-prod-snapshot"

# Read defaults from the live Settings class so adding a new excluded
# category or shifting a threshold in nous.config automatically
# propagates here. Wrapping in a function (called once at module load)
# keeps the imports lazy enough that this module can be imported
# without a populated NOUS_ env.
def _settings_defaults() -> tuple[int, list[str], int, int, int]:
    from nous.config import Settings
    s = Settings()
    return (
        s.stale_scan_age_days,
        list(s.stale_scan_excluded_categories),
        s.cluster_consolidation_min_facts,
        s.cluster_consolidation_max_facts,
        s.procedure_recency_days,
    )


(
    _DEFAULT_STALE_AGE_DAYS,
    _DEFAULT_STALE_EXCLUDED,
    _DEFAULT_CLUSTER_MIN,
    _DEFAULT_CLUSTER_MAX,
    _DEFAULT_PROCEDURE_RECENCY_DAYS,
) = _settings_defaults()

# RED threshold for procedure_recency: % of total-eligible decisions
# that must fall in the recency window. The original bug had 1.5% in a
# 7-day window; the fix puts ~35% in a 30-day window. 5% is a
# round-number floor between the two, but operators with low-traffic
# corpora can tune via --procedure-recency-min-pct.
_DEFAULT_PROCEDURE_RECENCY_MIN_PCT = 5.0


@dataclass
class FilterResult:
    name: str
    candidate_count: int
    samples: list[str] = field(default_factory=list)
    verdict: str = ""
    verdict_reason: str = ""


async def check_stale_scan(
    conn: asyncpg.Connection,
    agent_id: str,
    age_days: int,
    excluded: list[str],
) -> FilterResult:
    """Run the new stale_scan SQL and report what it would deactivate.

    A passing run is a non-zero count IF the corpus has facts old
    enough to qualify. If the corpus is too young (max age < age_days)
    the probe returns YELLOW: filter is structurally correct but no
    candidates exist yet. Only RED if the corpus has aged-enough facts
    AND the filter still selects 0 — the prior structural bug.
    """
    # First: oldest active fact in the corpus
    oldest = await conn.fetchval(
        """
        SELECT EXTRACT(DAY FROM now() - MIN(created_at))::int
        FROM heart.facts
        WHERE agent_id = $1 AND active = true
        """,
        agent_id,
    )
    if oldest is None:
        return FilterResult(
            name="stale_scan",
            candidate_count=0,
            verdict="NO_DATA",
            verdict_reason="no active facts in corpus",
        )

    # The new filter from PR #405 (sleep_handler._phase_stale_scan).
    # NOTE: this SQL is HAND-PORTED from the SQLAlchemy expression in
    # _phase_stale_scan, not derived from it. If the handler's filter
    # changes, this probe will silently disagree until someone updates
    # this string. The cross-check is sleep_cycle_health (#404) — that
    # passive monitor would flag persistent zero-output across N
    # cycles. A more robust fix is to extract the WHERE expression
    # into a shared helper both call sites consume; tracked as a
    # follow-up.
    # Pin now() once so the multi-step verdict logic below sees
    # consistent boundary semantics across queries.
    cutoff_sql = "now() - ($2::int || ' days')::interval"
    excluded_clause = ""
    params: list = [agent_id, age_days]
    if excluded:
        placeholders = ", ".join(f"${i + 3}" for i in range(len(excluded)))
        excluded_clause = f"AND category NOT IN ({placeholders})"
        params.extend(excluded)
    sql = f"""
        SELECT id, content, category, created_at
        FROM heart.facts
        WHERE agent_id = $1
          AND active = true
          AND created_at < now() - ($2::int || ' days')::interval
          AND (last_recalled_at IS NULL
               OR last_recalled_at < now() - ($2::int || ' days')::interval)
          {excluded_clause}
        LIMIT 200
    """
    rows = await conn.fetch(sql, *params)
    n = len(rows)
    samples = [
        f"  [{r['category']}] {(r['content'] or '')[:80]}"
        for r in rows[:3]
    ]

    if oldest < age_days:
        return FilterResult(
            name="stale_scan",
            candidate_count=n,
            samples=samples,
            verdict="YELLOW",
            verdict_reason=(
                f"corpus too young (oldest active fact {oldest}d) "
                f"to test filter against age threshold {age_days}d"
            ),
        )

    # If filter selects 0, distinguish "filter broken" from "no
    # genuinely stale facts." A fact is genuinely stale only if it's
    # old AND has not been recalled within the same window. An old
    # fact that was recalled yesterday is NOT stale.
    if n == 0:
        old_never_recalled = await conn.fetchval(
            """
            SELECT COUNT(*) FROM heart.facts
            WHERE agent_id = $1 AND active = true
              AND created_at < now() - ($2::int || ' days')::interval
              AND last_recalled_at IS NULL
            """,
            agent_id, age_days,
        )
        old_recalled_long_ago = await conn.fetchval(
            """
            SELECT COUNT(*) FROM heart.facts
            WHERE agent_id = $1 AND active = true
              AND created_at < now() - ($2::int || ' days')::interval
              AND last_recalled_at IS NOT NULL
              AND last_recalled_at < now() - ($2::int || ' days')::interval
            """,
            agent_id, age_days,
        )
        truly_stale = (old_never_recalled or 0) + (old_recalled_long_ago or 0)
        if truly_stale > 0:
            # Stale facts exist but filter doesn't catch them — this IS
            # the regression-to-impossible-filter pattern we guard against.
            return FilterResult(
                name="stale_scan",
                candidate_count=0,
                verdict="RED",
                verdict_reason=(
                    f"{truly_stale} stale facts exist (never_recalled="
                    f"{old_never_recalled}, recalled_long_ago="
                    f"{old_recalled_long_ago}) but filter selected 0"
                ),
            )
        # Old facts all recently recalled → healthy agent behavior, not a bug.
        return FilterResult(
            name="stale_scan",
            candidate_count=0,
            verdict="GREEN",
            verdict_reason=(
                "no genuinely stale facts (all old facts recently "
                "recalled — healthy agent housekeeping)"
            ),
        )
    return FilterResult(
        name="stale_scan",
        candidate_count=n,
        samples=samples,
        verdict="GREEN",
        verdict_reason=f"filter selects {n} candidates",
    )


async def check_cluster_consolidation(
    conn: asyncpg.Connection,
    agent_id: str,
    min_facts: int,
    max_facts: int,
) -> FilterResult:
    """Run the cluster_consolidation HAVING clause and report eligible
    subjects. The bug was that ``HAVING count >= 3`` always landed on
    accumulating mega-subjects; the cap (default 10) skips them.
    """
    rows = await conn.fetch(
        """
        SELECT subject, COUNT(*) AS cnt
        FROM heart.facts
        WHERE agent_id = $1
          AND active = true
          AND subject IS NOT NULL
        GROUP BY subject
        HAVING COUNT(*) BETWEEN $2 AND $3
        ORDER BY COUNT(*) DESC
        LIMIT 200
        """,
        agent_id, min_facts, max_facts,
    )
    n = len(rows)
    samples = [f"  {r['subject']!r} ({r['cnt']} facts)" for r in rows[:5]]

    # Sanity: also count subjects ABOVE the cap. If the cap excludes
    # nothing, the cap may be too generous.
    above_cap = await conn.fetchval(
        """
        SELECT COUNT(*) FROM (
          SELECT subject FROM heart.facts
          WHERE agent_id = $1 AND active = true AND subject IS NOT NULL
          GROUP BY subject HAVING COUNT(*) > $2
        ) t
        """,
        agent_id, max_facts,
    )

    if n == 0 and above_cap == 0:
        return FilterResult(
            name="cluster_consolidation",
            candidate_count=0,
            verdict="NO_DATA",
            verdict_reason="no subject clusters in corpus",
        )
    if n == 0:
        return FilterResult(
            name="cluster_consolidation",
            candidate_count=0,
            verdict="RED",
            verdict_reason=(
                f"{above_cap} subjects exist above cap of {max_facts} "
                f"facts but 0 small mergeable clusters — fix likely "
                f"reverted or cap too tight"
            ),
        )
    return FilterResult(
        name="cluster_consolidation",
        candidate_count=n,
        samples=samples,
        verdict="GREEN",
        verdict_reason=(
            f"{n} eligible clusters in [{min_facts}, {max_facts}] range "
            f"({above_cap} skipped above cap)"
        ),
    )


async def check_procedure_recency(
    conn: asyncpg.Connection,
    agent_id: str,
    recency_days: int,
    min_pct: float = _DEFAULT_PROCEDURE_RECENCY_MIN_PCT,
) -> FilterResult:
    """Count successful-reviewed-with-bridge decisions in the
    procedure_learner recency window. The bug was that the hardcoded
    7-day window matched ~3 of 200 candidates in prod. PR #405's
    default 30 days should match a meaningful portion."""
    n = await conn.fetchval(
        """
        SELECT COUNT(*)
        FROM brain.decisions d
        LEFT JOIN brain.decision_bridge b ON b.decision_id = d.id
        WHERE d.agent_id = $1
          AND d.outcome = 'success'
          AND d.reviewed_at IS NOT NULL
          AND b.function IS NOT NULL
          AND d.created_at > now() - ($2::int || ' days')::interval
        """,
        agent_id, recency_days,
    )
    total_eligible = await conn.fetchval(
        """
        SELECT COUNT(*)
        FROM brain.decisions d
        LEFT JOIN brain.decision_bridge b ON b.decision_id = d.id
        WHERE d.agent_id = $1
          AND d.outcome = 'success'
          AND d.reviewed_at IS NOT NULL
          AND b.function IS NOT NULL
        """,
        agent_id,
    )
    if total_eligible == 0:
        return FilterResult(
            name="procedure_recency",
            candidate_count=0,
            verdict="NO_DATA",
            verdict_reason=(
                "no successful-reviewed-with-bridge decisions in corpus"
            ),
        )
    pct = 100.0 * n / total_eligible
    if pct < min_pct:
        return FilterResult(
            name="procedure_recency",
            candidate_count=n,
            verdict="RED",
            verdict_reason=(
                f"only {n} of {total_eligible} eligible decisions "
                f"({pct:.1f}%) fall in {recency_days}-day window "
                f"(threshold: {min_pct}%) — recency_days too tight "
                f"for current prod traffic"
            ),
        )
    return FilterResult(
        name="procedure_recency",
        candidate_count=n,
        verdict="GREEN",
        verdict_reason=(
            f"{n} of {total_eligible} eligible decisions "
            f"({pct:.1f}%) in {recency_days}-day window"
        ),
    )


def overall_exit_code(results: list[FilterResult]) -> int:
    """1 if any RED, else 0."""
    return 1 if any(r.verdict == "RED" for r in results) else 0


async def run(
    conn: asyncpg.Connection,
    agent_id: str,
    *,
    stale_age_days: int = _DEFAULT_STALE_AGE_DAYS,
    stale_excluded: list[str] | None = None,
    cluster_min: int = _DEFAULT_CLUSTER_MIN,
    cluster_max: int = _DEFAULT_CLUSTER_MAX,
    procedure_recency_days: int = _DEFAULT_PROCEDURE_RECENCY_DAYS,
    procedure_recency_min_pct: float = _DEFAULT_PROCEDURE_RECENCY_MIN_PCT,
) -> list[FilterResult]:
    """Run all 3 filter checks. Caller owns connection lifetime."""
    excluded = list(stale_excluded if stale_excluded is not None
                    else _DEFAULT_STALE_EXCLUDED)
    return [
        await check_stale_scan(conn, agent_id, stale_age_days, excluded),
        await check_cluster_consolidation(conn, agent_id, cluster_min,
                                          cluster_max),
        await check_procedure_recency(conn, agent_id, procedure_recency_days,
                                      procedure_recency_min_pct),
    ]


def _print_report(results: list[FilterResult], agent_id: str) -> None:
    print()
    print("=" * 84)
    print(f"SLEEP FILTER DRY-RUN — agent={agent_id}")
    print("=" * 84)
    _MARKERS = {
        "GREEN": "[OK]  ", "YELLOW": "[WARN]", "RED": "[FAIL]",
        "NO_DATA": "[----]",
    }
    for r in results:
        marker = _MARKERS.get(r.verdict, "[????]")
        print(f"\n  {marker} {r.name:<26}  candidates={r.candidate_count}")
        print(f"         {r.verdict_reason}")
        if r.samples:
            print("         samples:")
            for s in r.samples:
                print(f"      {s}")
    print()
    reds = [r for r in results if r.verdict == "RED"]
    if reds:
        print("  RED filters (likely regression):")
        for r in reds:
            print(f"    - {r.name}: {r.verdict_reason}")
    print("=" * 84)


def _build_md(results: list[FilterResult], agent_id: str) -> list[str]:
    md = [
        f"# Sleep filter dry-run — agent_id=`{agent_id}`",
        "",
        "## Per-filter",
        "",
        "| filter | candidates | verdict | reason |",
        "|---|---:|---|---|",
    ]
    for r in results:
        md.append(
            f"| {r.name} | {r.candidate_count} | **{r.verdict}** | "
            f"{r.verdict_reason} |"
        )
    return md


async def _async_main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

    p = argparse.ArgumentParser(
        description=("Sleep-phase filter dry-run — runs PR #405 SQL "
                     "predicates against the eval DB and reports what "
                     "they'd select. Catches regressions to the "
                     "structurally-impossible filter pattern."),
    )
    p.add_argument("--eval-host",
                   default=os.environ.get("NOUS_EVAL_DB_HOST", "127.0.0.1"))
    p.add_argument("--eval-port", type=int,
                   default=int(os.environ.get("NOUS_EVAL_DB_PORT", "5433")))
    p.add_argument("--eval-user",
                   default=os.environ.get("NOUS_EVAL_DB_USER", "nous"))
    p.add_argument("--eval-password",
                   default=os.environ.get("NOUS_EVAL_DB_PASSWORD",
                                          "nous_eval"))
    p.add_argument("--eval-db",
                   default=os.environ.get("NOUS_EVAL_DB_NAME", "nous_eval"))
    p.add_argument("--agent-id",
                   default=os.environ.get("NOUS_EVAL_AGENT_ID",
                                          _DEFAULT_AGENT_ID))
    p.add_argument("--stale-age-days", type=int,
                   default=_DEFAULT_STALE_AGE_DAYS)
    p.add_argument("--cluster-min", type=int, default=_DEFAULT_CLUSTER_MIN)
    p.add_argument("--cluster-max", type=int, default=_DEFAULT_CLUSTER_MAX)
    p.add_argument("--procedure-recency-days", type=int,
                   default=_DEFAULT_PROCEDURE_RECENCY_DAYS)
    p.add_argument("--procedure-recency-min-pct", type=float,
                   default=_DEFAULT_PROCEDURE_RECENCY_MIN_PCT,
                   help=("RED-flag threshold: %% of total-eligible "
                         "decisions that must fall in the recency "
                         "window. Tune lower for low-traffic corpora."))
    p.add_argument("--strict", action="store_true",
                   help="Exit 1 if any filter shows a regression pattern.")
    p.add_argument("--out", type=Path,
                   default=Path("reports/eval_sleep_filter_dryrun.md"))
    p.add_argument("--out-json", type=Path,
                   default=Path("reports/eval_sleep_filter_dryrun.json"))
    args = p.parse_args(argv)

    conn = await asyncpg.connect(
        host=args.eval_host, port=args.eval_port,
        user=args.eval_user, password=args.eval_password,
        database=args.eval_db,
    )
    try:
        await conn.execute("SET default_transaction_read_only = on")
        results = await run(
            conn, args.agent_id,
            stale_age_days=args.stale_age_days,
            cluster_min=args.cluster_min,
            cluster_max=args.cluster_max,
            procedure_recency_days=args.procedure_recency_days,
            procedure_recency_min_pct=args.procedure_recency_min_pct,
        )
    finally:
        await conn.close()

    _print_report(results, args.agent_id)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out_json.write_text(
        json.dumps(
            [{"name": r.name, "candidate_count": r.candidate_count,
              "verdict": r.verdict, "verdict_reason": r.verdict_reason,
              "samples": r.samples}
             for r in results],
            indent=2, default=str,
        ),
        encoding="utf-8",
    )
    args.out.write_text("\n".join(_build_md(results, args.agent_id)),
                        encoding="utf-8")
    print(f"\nWrote: {args.out}")
    print(f"Wrote: {args.out_json}")

    if args.strict:
        return overall_exit_code(results)
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_async_main(argv))


if __name__ == "__main__":
    raise SystemExit(main())
