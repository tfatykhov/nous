# Served-precision eval (`nous_eval.served`)

Measures what Nous puts in front of the model: is it relevant, does the memory that should
come back come back, and what does it cost in tokens and latency. Two entry points share one
library:

- `recall`: the `recall_deep` tool, called as the agent calls it (`limit=10`, no type filter);
- `preturn`: the pre-turn context build (frame, intent plan, deictic and recap rescues,
  `ContextEngine.build`), as `layer.py` runs it.

A run is one arm on one entry point. `compare` pairs two runs. Developer-only, like the rest of
`nous_eval`: the prod Dockerfile copies `nous/` only and `.dockerignore` excludes `/nous_eval/`.

## Data stays out of the repo

Every input, output, label and cache lives in a data directory that git must ignore (the CLI
refuses a tracked one). Nothing the CLI prints contains query or memory text. Tests use
synthetic fixtures only.

```
<data>/
  base/…                      base settings layers (allowlisted snapshot .json, NOUS_* .env)
  recall/queries*.jsonl        {"key", "query", "logged_at"?}           (set prod)
  preturn/prod_templated_prompts.json                                   (set prod)
  gold/<type>.jsonl            {"id", "question", "gold": [id], "gold_type", "gold_category", "gold_content"}
  judge/<model>.jsonl          the shared label cache, one file per judge model
  cost_ledger.jsonl            one row per LLM call: purpose, run, model, usage
  embedding_cache.jsonl        query embeddings (float32), shared by every run
  date_windows.jsonl           date-window parses, keyed (as-of date, query, model)
  runs/<entry>/<tag>/          meta.json, rows.jsonl, scores.json
```

## Commands

```bash
D=<data dir>
BASE="--base $D/base/prod_settings.json --base $D/base/prod_flags.env"
DB="--db-template <template database>"   # each run gets CREATE DATABASE ... TEMPLATE, dropped after

# one arm (the control: no --set), pinned to a commit
python -m nous_eval.served recall run --data-dir $D --tag base --queries $D/recall/queries.jsonl \
    --gold $D/gold/fact.jsonl --gold $D/gold/episode.jsonl --gold $D/gold/decision.jsonl \
    --as-of 2026-11-05 $BASE $DB --code <commit> --env-file <dotenv with OPENAI_API_KEY / ANTHROPIC_*>
# the treatment: exactly one variable
python -m nous_eval.served recall run ... --tag cut20 --arm cut20 --set NOUS_SOME_FLAG=20

python -m nous_eval.served recall judge $D/runs/recall/base                  # Sonnet 5.5, shared cache
python -m nous_eval.served recall judge $D/runs/recall/base --model claude-opus-5-5 --sample 40
python -m nous_eval.served recall score $D/runs/recall/base                  # -> scores.json
python -m nous_eval.served compare $D/runs/recall/base $D/runs/recall/cut20 --gold-loss-tolerance 0.02

python -m nous_eval.served preturn run --data-dir $D --tag base --prompts $D/preturn/prod_templated_prompts.json \
    --gold … $BASE $DB --code <commit>
python -m nous_eval.served preturn gold --type decision --n 150 --out $D/gold/decision.jsonl …
python -m nous_eval.served recall queries --out $D/recall/queries.jsonl --days 30 …   # from a copy's retrieval_log
python -m nous_eval.served cost --data-dir $D [--run <tag>]
```

`--n 20` runs a fixed-seed sample (a smoke). `--db-name` runs on a scratch database directly
(`nous_eval*`, `nous_test*`, `beam*`) instead of a clone.

## What one variable means here

Settings layers, later wins:

1. code defaults, with every `NOUS_*`/`DB_*` variable hidden;
2. each `--base` file in order: a `.json` snapshot (`{"settings": {field: value}}`, as an
   allowlisted reader inside the prod container writes it) or a dotenv file of `NOUS_*` flags
   (a repeated name: the last line wins);
3. the eval disables (`retrieval_runner._EVAL_DISABLE_FIELDS`: event bus, sleep, extraction and the
   other writers), except query expansion, which keeps the base's value;
4. the arm's `--set NOUS_NAME=VALUE` overrides;
5. the run's database and agent.

Credential, endpoint and connection names are never applied from a layer and never recorded.
After loading the layers the run checks the Settings object holds every applied value.

Prod resolves `vector_weight`, `rrf_k` and `cross_encoder_enabled` through the runtime overrides in
`nous_system.config`; the run loads them from its database as `main.py` does, and an arm that sets
one of those keys wins over the stored value. The fusion parameters are also published to the
environment, because `nous.heart.search` builds its own `Settings()` per query; the run checks what
those leaf resolvers return at the start and counts every value they return during the run.

`meta.json` records: the effective value and source layer of every applied field (and a fixed list
of retrieval knobs), the base files' sha256, the runtime overrides, the leaf values, the code root's
HEAD, branch and dirty-file count, a sha256 of this package's files (the instrument), the inputs'
sha256, gold validity, cross-encoder device, and table writes during the run. `compare` lists every
difference between two runs before the numbers and says whether exactly one thing differs.

## Pinned code

`--code` takes a directory or a commit (checked out once into a detached worktree under
`--worktree-dir`, default `<repo>/../nous-served-wt/<sha12>`). The CLI then re-runs itself there
(`python -m nous_eval.served` with `PYTHONPATH` = that root, `NOUS_*`/`DB_*` removed from the
environment, the data dir as working directory). `nous` and `nous_eval` must come from the same
root, so the stack wiring (which mirrors `main.py`) moves with the code under test. Uncommitted
changes are refused unless `--allow-dirty`.

## The database

A clone gives every run the same starting bytes. Reads write too (frame usage, working memory,
access counters) and sessions commit per call, so a rolled-back transaction would not isolate a
run. The template must have no other connection open while it is copied. Two read-path writes
that would make results depend on query order are held off for every arm: fact access tracking
and the keyed leg's own tracking.

## The judge

Sonnet 5.5 labels each rendered item relevant or not, with the earlier harnesses' prompts
(recall_deep: the fusion/spreading A/B prompt, items shown to 800 characters, 25 per call;
pre-turn: the query-mode A/B prompt, 700 characters, 30 per call), `temperature` 0, through
`nous.api.anthropic_client` with the Claude Code preamble. A label is keyed by what the judge saw:
(entry point, query or prompt as shown, item type, item text as shown). Arms that render the same
item for the same query share one label; a label already cached is never asked again. A query's
pending items go to the judge in label-key (hash) order, not render order: judged in render
order, the top items share one call and the tail another, and the judge grades each item against
its call-mates (measured on a 250-query baseline: top-10 items labelled stricter, the tail more
lenient, than in mixed calls). Opus 5.5
judges a fixed-seed sample of a run's queries (`judge --model claude-opus-5-5 --sample N`);
`score` reports raw agreement and Cohen's kappa on the pairs both labelled. Every call's usage
goes to the cost ledger, priced at list prices in `judge.PRICES`.

## Metrics

recall_deep, set `prod` (precision metrics on queries whose every rendered item is labelled):
precision of the rendered list (mean of per-query precision, and micro), relevant items in the top
10, P@10, MRR, items, rendered tokens (output characters / 4), wall latency p50/p95, precision per
entry leg (spreading rows arrive as `graph_expanded`) and per rank band (1-10, 11-30, 31-60, 61+).
Ranks are render positions. Set `gold`: gold-in-served, gold-in-top-10, reciprocal rank, per gold
type.

Pre-turn, set `prod`: micro precision overall and per item type (the sections), items and relevant
items per build, total and memory tokens, build latency. Set `gold`: gold-in-context (the gold item
rendered, or its text in the system prompt) and reciprocal rank among rendered items of its type.

`score --keys <file>` restricts a summary to a key subset.

## Gold

An LLM writes a short chat message that asks for one sampled memory back; that memory is the gold
item (`preturn gold`). Facts: active, embedded, 40-400 characters, two strata (tier-1
rule/person/preference, and the rest), minus facts already in context for content-free probe
prompts. Episodes: open, or closed and not abandoned, summary 120-3000 characters, tagged and
untagged. Decisions: not superseded, embedded, description 40-600 characters. A question is dropped
when the LLM returns null, when it is not 20-90 characters, or when it shares too many distinctive
words with the memory (1 for a fact, 2 for an episode or a decision). The same gold questions run
through both entry points. At run time a gold item that is no longer active on the copy is marked
invalid and left out of gold metrics.

## compare and the win condition

Deltas are B minus A over the inputs both runs answered, with a paired bootstrap 95% CI (2000
resamples). The win condition, from the precision plan with the review's explicit tolerance:

1. precision or tokens improve: the precision CI lies above 0, or the tokens CI lies below 0;
2. gold-in-served (recall_deep) or gold-in-context (pre-turn), pooled over gold types, is
   non-inferior: CI lower bound >= `-gold_loss_tolerance` (a required argument); for recall_deep
   relevant-in-top-10 too (`--top10-loss-tolerance`, default 0);
3. p95 latency grows by at most `--max-p95-regression` (default 0.20) of A's p95.

## Caveats

- Latency is measured where the run runs (the cross-encoder on that machine's CPU), not on the
  prod host.
- Date-window parses are fixed per (as-of date, query, model) in `date_windows.jsonl` and filled
  before any call is measured, so latency excludes the parse and arms see the same windows. A
  query's as-of date is its logged date, else `--as-of`.
- Pre-turn builds use a fixed identity prompt and no conversation history, as the query-mode A/B
  did.
