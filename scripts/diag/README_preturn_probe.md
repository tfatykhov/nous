Replays ContextEngine's fact pipeline stage-by-stage (intent-plan rewrite → fetch-limit → recency-resolve → frame-boost → diversity → relevance-filter → budget-truncation) against the eval DB, reporting which gate drops each gold fact.

```bash
uv run python scripts/diag/probe_preturn_fact_gate.py --questions failing_questions.jsonl --frame question --agent-id nous-default
```

Input JSONL format (one JSON object per line): `{"question": "Who wrote Past Masters?", "gold": "Madonna"}`
