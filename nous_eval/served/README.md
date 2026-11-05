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
refuses one git would track). A directory inside a clone of this repo needs an ignore rule first:
the repo's `.gitignore` has none for eval data, so add the directory to `.git/info/exclude` (or
keep the data outside the clone). Nothing the CLI prints contains query or memory text. Tests use
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
  date_windows_unpadded.jsonl  date-window parses (unpadded), keyed (as-of date, query, model)
  query_expansions.jsonl       query-expansion variants, keyed (query hash, model, max variants)
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
(`nous_eval*`, `nous_test*`, `beam*`; `--i-know-this-is-scratch` for a test database named
otherwise) instead of a clone. A prod copy (`nous_prod*`), a served template or another run's clone
(`nous_eval_served_*`), and the server's `postgres`, `template0` and `template1` are refused as
`--db-name` whatever flag is passed, and a run drops only the clone it created. A clone is named
`nous_eval_served_run_<entry>_<tag>`; a name over Postgres' 63-character limit keeps its first 54
characters plus 8 hex of a hash of the full name, so every tag `--tag` accepts (up to 40
characters) gives a valid, distinct name. Path arguments are resolved against the directory the
command started in. Every explicit output file
(`queries --out`, `gold --out`, `compare --out`) must be one git would not track, checked before
anything is read from the database or asked of an LLM.

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
A layer only collects what it asks for (a dotenv or `--set` string, a snapshot value); Settings
parses and validates once, on the merged requests, so a cross-field validator sees the base and
the arm together (a base `NOUS_CONTEXT_QUERY_MODE=conditional` plus an arm
`NOUS_CONTEXT_SKIP_UNCHANGED_QUERY=true` runs with the skip on). The run then checks that the
merged Settings hold every requested value, as each field parses it with no cross-field
validator, and stops, naming the field, when a validator changed one.

Prod resolves `vector_weight`, `rrf_k` and `cross_encoder_enabled` through the runtime overrides in
`nous_system.config`; the run loads them from its database as `main.py` does, and an arm that sets
one of those keys wins over the stored value. The fusion parameters are also published to the
environment, because `nous.heart.search` builds its own `Settings()` per query; the run checks what
those leaf resolvers return at the start and counts every value they return during the run.

`meta.json` records: the effective value and source layer of every applied field (and a fixed list
of retrieval knobs), the base files' sha256, the runtime overrides, the leaf values, the code root's
HEAD, branch and dirty-file count, a sha256 of this package's files (the instrument), the inputs'
sha256, gold validity, cross-encoder device, and table writes during the run. `compare` lists every
difference between two runs before the numbers and says whether exactly one thing differs. The
database counts as one: a clone's template (its generated name is not a variable), or the scratch
database a `--db-name` run read directly.

## Pinned code

`--code` takes a directory or a commit (checked out once into a detached worktree under
`--worktree-dir`, default `<repo>/../nous-served-wt/<sha12>`). The CLI then re-runs itself there
(`python -P -m nous_eval.served` from the same working directory, with `PYTHONPATH` = that root,
`NOUS_*`/`DB_*` removed from the environment and `PYTHONHASHSEED=0`; `-P` keeps the working
directory off `sys.path`). `nous` and `nous_eval` must come from the same root, so the stack wiring
(which mirrors `main.py`) moves with the code under test. Uncommitted changes under `nous/`,
`nous_eval/`, `sql/`, `pyproject.toml` or `uv.lock` are refused unless `--allow-dirty`. The
instrument hash covers this package and the `nous_eval` modules it imports.

## The database

A clone gives every run the same starting bytes. Reads write too (frame usage, working memory,
access counters) and sessions commit per call, so a rolled-back transaction would not isolate a
run. The template must have no other connection open while it is copied. Two read-path writes
that would make results depend on query order are held off for every arm: fact access tracking
and the keyed leg's own tracking.

## The judge

Sonnet 5.5 labels each rendered item relevant or not, with the earlier harnesses' prompts
(recall_deep: the fusion/spreading A/B prompt, items shown to 800 characters, 25 per call;
pre-turn: the query-mode A/B prompt, 700 characters, 30 per call), through
`nous.api.anthropic_client` with the Claude Code preamble. The request carries `model`, `system`,
`messages` and `max_tokens` (8000, shared with default-on thinking) and no sampling parameter:
the current API version rejects one, and the label cache, not the sampler, makes labels repeatable. A label is keyed by what the judge saw:
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
Ranks are the positions the model reads: the order the formatter reported emitting (Heart Memory
as grouped by session and date, then Graph-Connected and Brain Decisions, Parent Episode Context,
stored examples), not the pipeline's `final_rank` (kept beside). Each call checks that order against
the ids its output printed (`read_order_matches_text`; None when the output printed no id).

The text the judge sees for a recall item is what the formatter wrote for it, taken from the
formatter's own output elements (each emission notes the element it was written at): a decision's
outcome, category, stakes, confidence and pattern, a fact's date, recency and source tags and its
merge-history lines, an exemplar's utterance as cut to 500 characters with its label, a parent
episode's summary. The furniture that changes with rank, score or the finding leg is left out, so
the same item shown by another arm keeps its label: the list number, an exemplar's similarity,
the Heart Memory `[type]` tag (the judge gets the type apart), the `[via ...]` provenance tag and
the `(id: ..., score ...)` trailer. A decision shown in two sections is judged on its first.
Each call checks that the captured elements joined are the tool's output
(`output_matches_capture`) and counts rendered items with no captured text (`text_unmatched`: an
element of an unexpected shape, a parent line not found, or no elements captured).

The run records how many calls disagreed (`read_order_mismatches`), how many rendered items no
formatter section reported (`unplaced`), `text_unmatched`, and how many calls' output differed from
the capture (`output_mismatches`). A call that returned nothing ("No results found.") emits no
element; its check reads the formatter's returned text, it scores as a query with 0 items, and it is
counted apart (`empty_results`, in the run record and in the scores); `judge` refuses a run where one is non-zero unless
`--allow-unchecked`. The tool's output is what the eval measures; in a live turn the tool result
may still pass through the dispatcher's output compression before the model reads it. A tool call that returned an error renders
nothing and counts as a judged query with precision 0. Set `gold`: gold-in-served, gold-in-top-10,
reciprocal rank, per gold type; an episode is served when its summary or any chunk of it is
rendered, and the summary-only rates are reported beside (`served_summary`, `top10_summary`).

Pre-turn, set `prod`: micro precision overall and per item type (the sections), items and relevant
items per build, total and memory tokens, build latency. A row's items, their order and the text
the judge sees all come from one place, the final text of each judged section. Every call the
build makes to a section formatter (facts, decisions, Past Episodes, procedure lines and blocks)
is recorded; each item's own line is re-rendered from it, with its own full-text exemption,
lineage and date flag. Each section's final content is then split into those lines, after the
temporal date sort, the `fact_format_max_chars` cut and the section budget cut, and the
changed-tools note line is set aside. Recent Conversations (built inline) is split into one
block per episode the build kept. Sections are taken in the order the final system prompt holds
them, so Recent Conversations comes before Past Episodes. An item the budget cut part-way is
judged on what survived.

Per build checks, summed per run, each expected 0; `judge` refuses a run where one is not:
`unplaced` (the trace says rendered, no formatter produced it and no text holds it), `untraced`
(in the text, not rendered per the trace), `unmatched_segments` (section text no item produced)
and `sections_not_in_prompt`. `budget_cut_but_traced` is recorded, not refused: the item's line
was cut by the section budget, but the trace's approximate budget check (a 40-character content
probe) found its content elsewhere and kept it rendered. `fragment_untraced` is its mirror, also
recorded, not refused: the budget left only the start of an item's line (such as `- [succ...`),
the trace's probe dropped it, and it is judged as shown. The text decides. Set `gold`: gold-in-context (the
gold item rendered, or its text in the system prompt) and reciprocal rank among rendered items of
its type.

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

A check that could not be measured (no gold rows in both runs, no query judged in both) fails the
win, named in `why_not`. Each run's labels come from its own data dir and judge model. A change to
a runtime-backed setting (`vector_weight`, `rrf_k`, `cross_encoder_enabled`) counts as one variable.

## Caveats

- Latency is measured where the run runs (the cross-encoder on that machine's CPU), not on the
  prod host.
- Every run makes a warm pass over all its inputs (results discarded) before the measured pass,
  so each measured call finds its query embedded, the cross-encoder loaded and the buffers warm,
  whichever arm ran first; `meta.json` records `embeddings_added_while_measuring` (0 expected).
  A run takes about twice as long as its measured pass.
- Query-expansion variants (when an arm turns expansion on) are shared by every run of a data dir
  in `query_expansions.jsonl`, keyed (query hash, model, max variants), so arms on fresh clones
  retrieve with the same variants; the clone's own table is still read first, as prod does.
- Date-window parses are fixed per (as-of date, query, model) in `date_windows_unpadded.jsonl`,
  stored unpadded and padded per run with its own `date_leg_pad_days`, and filled before any call is
  measured, so latency excludes the parse and arms see the same windows. An input's as-of date is
  its logged date, else `--as-of`; it travels with the input into the prefill, the warm pass and the
  measured call, so two inputs with the same text and different dates each resolve against their own.
- Each logged input keeps the conversation it came from (`source_session`: a recall query's
  `session_id` from `retrieval_log`, a pre-turn prompt's session). Prod passes that session to
  recall_deep and to the pre-turn build, and both leave its ongoing episode and that episode's
  dialogue chunks out (`own_session_exclusions`, #854; `_is_own_session_episode`, #857). The replay
  hands the source session to exactly those two functions and keeps its own session ids (none for
  recall_deep; per build a fresh `<session>-<key>-served` / `-warm` id, one per input, so two prompts
  of one conversation never share the unchanged-query memo): the copy holds each
  conversation's state at the end of the dump, not at the logged turn, so the real id would also
  bring in later turns' F055 residual activation and working memory, cache hints, and an
  unchanged-query memo the warm and measured passes would share. Gold questions have no source
  conversation. Each row counts the candidates its trace dropped as `own_session`
  (`own_session_excluded`); the run records how many inputs had one (`own_session_queries`) and
  how many rows were left out (`own_session_rows`).
- The inputs' fingerprint (`inputs_sha256`) covers each input's text, gold, effective as-of date,
  source conversation, first-turn state and turn number; `compare` also lists differing `--as-of`
  and `--warm`.
- Pre-turn builds use a fixed identity prompt and no conversation history, as the query-mode A/B
  did. An input is a first turn as pre_turn would compute it for a fresh session: a turn-1 prompt
  whose kind does not run as a subtask (`subtask`, `dynamic`, `heartbeat` do); gold questions are
  later turns.
