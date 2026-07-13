Replays ContextEngine's fact pipeline stage-by-stage (intent-plan rewrite → fetch-limit → recency-resolve → frame-boost → diversity → relevance-filter → budget-truncation) against the eval DB, reporting which gate drops each gold fact.

```bash
uv run python scripts/diag/probe_preturn_fact_gate.py --questions failing_questions.jsonl --frame question --agent-id nous-default
```

Input JSONL format (one JSON object per line): `{"question": "Who wrote Past Masters?", "gold": "Madonna"}`

**Embedding-model gotcha (silently-garbage ranks):** `NOUS_EMBEDDING_MODEL` must match the model the corpus was embedded with, or every rank is meaningless noise (query and corpus live in different vector spaces; scores still look plausible). The `nous_eval_prod` corpus on :5433 is `text-embedding-3-large` @1536 — run with `NOUS_EMBEDDING_MODEL=text-embedding-3-large`. Sanity check: a known-stored fact should rank top-3 for a direct question about it before trusting any DROP output.
