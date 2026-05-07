"""``python -m nous_eval.determinism_check`` — verify F051 retrieval is reproducible.

Closes EXECUTION-PLAN item 1.4 from the 2026-05-03 audit. The audit
surfaced that the same git_sha produced two different baseline MRRs
(0.810 vs 0.828) on 2026-05-03 — fixture drift or RuntimeConfig leak
between configs.

This CLI runs a single configuration N times against the same source(s)
and asserts byte-identical retrieved IDs across runs. Exit code:

- 0: all runs identical (deterministic)
- 1: divergence detected (caller should investigate)

Usage::

    NOUS_EVAL_DB_NAME=nous_eval_scratch \
    NOUS_EVAL_AGENT_ID=nous-prod-snapshot \
    uv run python -m nous_eval.determinism_check \
      --config baseline \
      --sources nous_prod \
      --runs 3 \
      --include-unreviewed

The check fails fast on the first divergence — partial output is
useful for debugging which qrel introduces non-determinism.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import sys
from pathlib import Path

from nous.config import Settings
from nous_eval.config import EvalSettings
from nous_eval.qrels_loader import QrelSource, load_qrels
from nous_eval.retrieval_runner import RetrievalConfig, run_matrix
from nous_eval.source_registry import SourceRegistry

logger = logging.getLogger(__name__)


def _compute_qrels_sha256(path: Path) -> str:
    """SHA256 of the qrels JSONL file's raw bytes.

    Used to pin fixture identity so we can detect "same git_sha,
    different fixture" drift in subsequent runs.
    """
    if not path.exists():
        return "missing"
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:16]  # 16-char prefix is enough to compare


async def _run_once(
    config: RetrievalConfig,
    qrels: list,
    eval_settings: EvalSettings,
    main_settings: Settings,
    top_k: int,
) -> dict[int, tuple[str, ...]]:
    """Run the matrix once; return {qrel_index: (retrieved_id_strs)}."""
    results = await run_matrix(
        configs=[config],
        qrels=qrels,
        eval_settings=eval_settings,
        main_settings_template=main_settings,
        top_k=top_k,
    )
    if not results or not results[0].per_qrel:
        return {}
    return {
        q.qrel_index: tuple(str(rid) for rid in q.retrieved_ids)
        for q in results[0].per_qrel
    }


async def _main(args) -> int:
    logging.basicConfig(level=getattr(logging, args.log_level.upper()))
    eval_settings = EvalSettings()
    main_settings = Settings()

    # Load qrels for the requested sources.
    registry = SourceRegistry.load(fixtures_dir=eval_settings.fixtures_dir)
    only = args.sources.split(",") if args.sources else None
    resolved = registry.resolve(only=only, include_unreviewed=args.include_unreviewed)

    if not resolved:
        print(f"ERROR: no sources resolved (only={only})", file=sys.stderr)
        return 1

    qrels = []
    fixture_hashes: dict[str, str] = {}
    for rs in resolved:
        if not rs.available:
            print(f"WARN: source {rs.spec.name} unavailable; skipping", file=sys.stderr)
            continue
        loaded = load_qrels(
            rs.resolved_path,
            source_override=QrelSource(rs.spec.name),
            review_filter_enabled=(
                bool(rs.spec.review_filter) and not rs.include_unreviewed
            ),
        )
        qrels.extend(loaded)
        fixture_hashes[rs.spec.name] = _compute_qrels_sha256(rs.resolved_path)

    if not qrels:
        print("ERROR: no qrels loaded", file=sys.stderr)
        return 1

    config = RetrievalConfig(name=args.config, flags={}, description=f"determinism N={args.runs}")
    print(f"Determinism check — config={args.config!r} runs={args.runs} qrels={len(qrels)}")
    for src, h in sorted(fixture_hashes.items()):
        print(f"  fixture[{src}] sha256={h}")

    runs: list[dict[int, tuple[str, ...]]] = []
    for i in range(args.runs):
        print(f"  run {i + 1}/{args.runs} ...", end=" ", flush=True)
        out = await _run_once(config, qrels, eval_settings, main_settings, args.top_k)
        runs.append(out)
        print(f"{len(out)} qrels scored")

    # Diff: compare every run to run 0.
    baseline = runs[0]
    diverged_qrels: list[int] = []
    for i, run in enumerate(runs[1:], start=1):
        for q_idx, ids in baseline.items():
            other = run.get(q_idx)
            if other is None:
                print(f"  ERR: run {i} missing qrel {q_idx}", file=sys.stderr)
                diverged_qrels.append(q_idx)
                continue
            if other != ids:
                diverged_qrels.append(q_idx)
                if len(diverged_qrels) <= 5:
                    print(f"  DIVERGENCE qrel={q_idx} run0_top3={ids[:3]} run{i}_top3={other[:3]}",
                          file=sys.stderr)

    if diverged_qrels:
        unique = sorted(set(diverged_qrels))
        print(f"\nFAIL: {len(unique)} qrels diverged across {args.runs} runs", file=sys.stderr)
        print(f"      diverged qrel indices: {unique[:20]}{'...' if len(unique) > 20 else ''}",
              file=sys.stderr)
        return 1

    print(f"\nPASS: all {len(qrels)} qrels produced byte-identical retrieved IDs across {args.runs} runs")
    return 0


def cli() -> None:
    p = argparse.ArgumentParser(
        description="F051 determinism check: same config N runs must produce identical IDs.",
    )
    p.add_argument("--config", default="baseline",
                   help="Config name from _DEFAULT_CONFIGS (default: baseline)")
    p.add_argument("--sources", default=None,
                   help="Comma-separated source names (default: enabled_by_default)")
    p.add_argument("--runs", type=int, default=3,
                   help="Number of runs to compare (default: 3)")
    p.add_argument("--top-k", type=int, default=10)
    p.add_argument("--include-unreviewed", action="store_true",
                   help="Bypass review_filter on sources that have one")
    p.add_argument("--log-level", default="warning",
                   help="Log level (debug, info, warning, error)")
    args = p.parse_args()
    raise SystemExit(asyncio.run(_main(args)))


if __name__ == "__main__":
    cli()
