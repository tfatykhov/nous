"""F056 Phase 2: per-handler eval CLIs.

Each module under this package implements a `python -m nous_eval.handlers.<name>`
CLI that exercises one production handler in isolation, computes a mechanical
metric, and persists results to `nous_system.eval_runs` (eval DB) for the
weekly regression cron to consume.

Spec: docs/features/F056-eval-framework-phase2.md
Sequencing: PR #0 (regression.py extensions) -> PR #1 admission ->
PR #2 dedup -> PR #3 backfill -> PR #4 summary.
"""
