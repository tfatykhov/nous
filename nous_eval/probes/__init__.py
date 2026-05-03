"""Diagnostic probes for retrieval-quality investigation.

Each probe targets a specific *behavioural* dimension of recall (rather
than the aggregate MRR/P@K metrics that ``nous_eval.retrieval`` already
covers). Probes are designed to:

- Catch regressions on classes of query that the F051 corpus underweights
  (e.g., architectural / "tell me about X" questions).
- Produce human-readable per-scenario verdicts so a regression points
  directly at the failing case.

Run a probe via ``python -m nous_eval.probes.<name>``.
"""
