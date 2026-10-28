# F020 — Tool Output Intelligence (SmartCompress + ReversibleCache)

> **Status:** Planned
> **Priority:** P1
> **Depends on:** F016 (Context Pruning), Postgres (`heart.tool_cache`)

---

## Overview

Two complementary improvements to tool output handling that operate at **L0 (ingestion-time)** — before F016's age-based decay, before results ever enter `messages[]`.

- **Phase 1 — SmartCompress:** Statistical ingestion-time compression. Preserves what matters (errors, outliers, scored items), adapts aggressively only when the signal is clear enough to know what to drop.
- **Phase 2 — ReversibleCache:** Hash-keyed cache for non-re-fetchable tool outputs (`bash`, `web_search`, `web_fetch`, `dag_manage`). When content is compressed, the original is stored in Postgres (`heart.tool_cache`) and retrievable on demand via a `cache_retrieve` tool.

These are additive to F016. SmartCompress reduces initial footprint so F016 pruning kicks in later and less aggressively. ReversibleCache closes the gap where F016's hard-clear permanently loses non-re-fetchable content.

---

## Current State (F016 baseline)

```
Tool result → messages[] (full size) → age 5: soft-trim → age 10: metadata-degrade → age 15: hard-clear
```

**Problems:**
- Results enter messages[] at full size — a 500-item grep output stays 500 lines until it ages out
- Soft-trim is dumb: `head N chars + tail N chars`, knows nothing about content importance
- Hard-clear of `web_search` results is permanent — original 500 results are gone, model gets only the regex-extracted facts
- No error preservation: a critical error buried at line 247 of a grep output gets trimmed away

---

## Phase 1: SmartCompress

### Hook Location

`nous/api/runner.py` — in `_tool_loop()`, immediately after tool result is received, before appending to `messages[]`:

```python
result_text = await smart_compress(tool_name, tool_input, result_text, settings)
```

Single entry point. Returns the original string unchanged if compression is skipped.

---

### Step 1: Crushability Check (safety gate)

Before any compression, evaluate whether we have enough signal to know what's important. If not — **pass through uncompressed**.

**Skip compression if ALL of the following:**
- Uniqueness ratio > 0.9 (every line/item is distinct — no clusters to collapse)
- No error lines detected
- No score field detected (can't rank items)
- No numeric anomalies

**Rationale:** It is always better to pass through a large result than to randomly drop items when you can't tell what matters. This prevents the worst failure mode: losing something important because the compressor had no signal.

---

### Step 2: Content-Type Classification

Classify the result to pick the right compression strategy:

| Type | Detection | Strategy |
|------|-----------|----------|
| `dict_array` | Valid JSON array of objects | Error items + at most `max_k` others, ranked by a score field if there is one; no dedup |
| `string_array` | Newline-separated lines (bash/grep) | Error preservation + adaptive K on scores |
| `log_format` | Timestamps + log levels | Error/change-point preservation |
| `raw_text` | Large read_file output | Structural section detection |
| `small` | < `NOUS_SMART_COMPRESS_MIN_CHARS` (8000 by default) | No compression |

---

### Step 3: Error & Outlier Preservation (hard guarantee)

**Regardless of compression strategy, always preserve:**

- Lines/items containing: `error`, `exception`, `failed`, `critical`, `traceback`, `fatal`, `panic`
- Structural outliers: items with fields that < 5% of other items have
- Rare values in status-like fields (e.g., 95% are `"active"`, one is `"suspended"`)

This is a hard guarantee, not a best-effort. These lines are extracted first and added back after compression.

---

### Step 4: Adaptive K (Kneedle-lite)

Instead of hardcoded `head N + tail N` chars, find the natural elbow in the data.

**For `string_array` (bash/grep outputs):**
1. Score each line: error keywords = 1.0, contains path/URL = 0.8, contains number = 0.6, blank = 0.0
2. Sort by score descending
3. Find elbow: where `score[i] - score[i+1] > threshold` (cliff in relevance)
4. K = elbow index, capped at `max_k` (default 50 lines)
5. Allocate: first 30% of K from start of original, last 15% from end, remainder = highest-scored
6. Emit the kept lines in their original order, with one `[… result lines A–B omitted (N lines) …]` line per omitted run. Pass the result through unchanged when it has at most `max_k` lines, or when the cut, gap lines included, would keep 80% or more of its characters

**For `dict_array` (JSON API results):**
1. Detect score field (bounded numeric, often sorted descending)
2. If found: keep the top `max_k` items by score
3. If not found: keep the first `max_k` items in their original order. Nothing is deduplicated: a repeated item is emitted as often as it is kept. At most `max_k` items in all: passed through unchanged
4. Always include error items and structural outliers
5. Pass the result through unchanged when every item is kept, or when the cut would keep 80% or more of its characters

---

### Compression Strategies

**`string_array` (bash, grep, file listings):**
```
Before: 500 grep lines, 12,000 chars
After:  error lines (always) + top 23 lines by score + last 7 lines = ~800 chars
        "[SmartCompressed: 500→30 lines, 23 by relevance, 7 error/outlier preserved]"
```

**`dict_array` (JSON responses):**
```
Before: [{"id": ..., "score": 0.92, ...}, ... × 500 items]
After:  top 23 by score + 3 outliers
        "[SmartCompressed: 500→26 items, K=23 by score, 3 outliers]"
```

**`log_format`:**
```
Before: 2,000 log lines
After:  all ERROR/WARN lines + change points + first/last 5 lines
        "[SmartCompressed: 2000→45 lines, change-point detection]"
```

**`raw_text` (large read_file):**
```
Before: 800-line Python file
After:  imports + class definitions + constants + function signatures (no bodies)
        (only if file > soft_trim threshold; otherwise pass through)
```

---

### Configuration

```env
NOUS_SMART_COMPRESS_ENABLED=true
NOUS_SMART_COMPRESS_MIN_CHARS=8000     # Below this, never compress (500 until 2026-10-28)
NOUS_SMART_COMPRESS_MAX_K=50           # Max items to keep per result
NOUS_SMART_COMPRESS_ELBOW_THRESHOLD=0.3  # Score cliff to detect elbow
```

---

## Phase 2: ReversibleCache

### The Gap

`web_search` and `web_fetch` are **non-re-fetchable**: when F016 hard-clears them, the original results are permanently lost. Re-calling `web_search` would return different results (stale, different ranking). The model is left with only the regex-extracted facts (URLs, paths, key-values) from pre-prune extraction.

`bash` is non-re-fetchable too: re-running a command is not guaranteed to reproduce its output, and it may be slow or have side effects. Before 2026-10-28 a cut bash result kept no original, so the lines SmartCompress omitted were lost.

### Solution

When a non-re-fetchable tool result is compressed, store the original in Postgres (`heart.tool_cache`) keyed by content hash. Inject a `cache_retrieve` tool so the model can get it back on demand.

### As built (2026-10-28): every cut can be undone

- **bash is cached.** `NON_REFETCHABLE_TOOLS` (`nous/api/tool_cache.py`) is `bash`, `web_search`, `web_fetch` and `dag_manage`. The original is stored as is, with no secret filtering: the cache is internal.
- **The key is in the marker.** Once the cache write succeeds, the runner adds the key to the final marker, before the original size: `[SmartCompressed: 240→43 lines, 6 error/outlier preserved, order kept, gaps marked, cache_retrieve key 3f2a9c0b1d4e5f60718293a4b5c6d7e8, 12345 chars original]` (`add_cache_key` in `nous/api/smart_compress.py`). A bash `Exit code:` line stays last. If the write fails, the marker carries no key. The model can retrieve a result in the same turn it was cut, without waiting for the next turn's context hint.
- **`lines` ranges.** `cache_retrieve(hash_key, lines="A-B")` returns lines A to B (1-based, inclusive; `"A–B"` with an en dash and `"A"` also work), numbered over `original_content.split("\n")`. This is the numbering of the gap markers, so the range in `[… result lines 61–118 omitted (58 lines) …]` returns exactly the omitted lines. The output starts with `[result lines A–B of N]`. An end past the last line is clipped. A start past the last line, a start below 1, a start after the end, or a malformed value returns a plain message that names N. With `query` as well, the range is taken first and then filtered.
- **A 30,000-char cap per retrieval** (`_MAX_RETRIEVE_CHARS`). The header and the shown lines count toward it; the footer does not. Full or ranged output over the cap is cut after the last whole line that fits and ends with `[… result lines K–B not shown (M lines); call cache_retrieve with lines="K-B" …]`, so following the footers pages through the whole original. A single line longer than the cap is cut at the cap, and the footer says so; `cache_retrieve` cannot page within a line. `query` output keeps its 50-line / 20-item limits and is then cut at the cap with a plain footer.
- **`cache_retrieve` output is never compressed.** `smart_compress()` passes it through in code, ahead of the configurable `smart_compress_exempt_tools`. Even the `[]` kill switch does not reach it.
- **5 hints.** The Cached Results context section lists only the 5 newest entries of the session (by `compressed_at`), one line each, naming the key and saying `lines="A-B"` fetches a range.
- **3-day purge.** Rows are deleted when their session ends (`session_ended`). A session that never ends was never cleaned up, so each insert also deletes the same agent's rows older than `CACHE_RETENTION` (3 days), in the insert's transaction. Another agent's rows are never touched.

---

### Cache Storage

One `heart.tool_cache` row per cached original, unique on `(session_id, hash_key)`:

```
hash_key:   SHA256(original_content)[:32]
Columns:    agent_id, session_id, tool_name, tool_input, original_content, compressed_at, item_count
Cleanup:    on session_ended, plus a per-agent purge of rows older than 3 days on each insert
```

**Non-re-fetchable tools (cached):** `bash`, `web_search`, `web_fetch`, `dag_manage`, any future API-call tools
**Re-fetchable tools (not cached):** `read_file`, `run_python`, `list_files`

---

### Injected Tool: `cache_retrieve`

Added to the LLM's tool list **only when** there are active cache entries for the current session:

```json
{
  "name": "cache_retrieve",
  "description": "Retrieve original content from a previous search or fetch result that was compressed. Use when you need more detail from a compressed web search or API result.",
  "input_schema": {
    "type": "object",
    "properties": {
      "hash_key": {
        "type": "string",
        "description": "The hash key shown in the compressed result marker (e.g. 'abc123de')"
      },
      "query": {
        "type": "string",
        "description": "Optional search query. If provided, returns only the most relevant items from the cached result instead of everything."
      }
    },
    "required": ["hash_key"]
  }
}
```

---

### Context Hint

When active cache entries exist, a "Cached Results" section lists the 5 newest, one line each:

```
Compressed results available:
- bash("cat -n build.log"): 241 items, cache_retrieve key 3f2a9c0b1d4e5f60718293a4b5c6d7e8; lines="A-B" fetches a range.
- web_search("PyJWT vulnerabilities 2026"): 47 items, cache_retrieve key 0c1d2e3f405162738495a6b7c8d9e0f1; lines="A-B" fetches a range.
```

---

### BM25 Search Within Cache

When `query` is passed to `cache_retrieve`:
- Tokenize cached items and score against query using BM25
- Return top 20 matches instead of full dump
- This is cheap (in-process), deterministic, and more useful than re-running the search

**Libraries:** `rank_bm25` (pure Python, already likely in deps) or simple TF-IDF fallback.

---

### Retrieval Flow

```
Turn N:   web_search → 47 results → SmartCompress → 12 shown in messages[]
                                                   → original 47 cached (hash: abc123de)

Turn N+5: context hint: "[abc123de] 47 items, 12 shown"
          model calls: cache_retrieve("abc123de", query="CVE score")
          → BM25 on cached 47 items → top 5 by CVE relevance returned
          → model answers with full detail
```

---

## Implementation Priority

| # | Component | LOC est. | Value |
|---|-----------|----------|-------|
| 1 | Crushability check + error preservation | ~60 | Prevents worst failure mode |
| 2 | Adaptive K for `string_array` | ~80 | Biggest compression win for bash/grep |
| 3 | `dict_array` score detection + top-K | ~60 | JSON API responses |
| 4 | ReversibleCache (Postgres `heart.tool_cache` + cleanup) | ~100 | Closes web_search gap |
| 5 | `cache_retrieve` tool injection | ~80 | Makes cache usable |
| 6 | BM25 search within cache | ~50 | Targeted retrieval |
| 7 | `raw_text` structural section detection | ~80 | Large file compression |

**Minimum viable:** Items 1-2 (140 LOC). Immediate wins with zero new infrastructure.
**Full Phase 1:** Items 1-3 (200 LOC). Complete SmartCompress.
**Full Phase 2:** Items 4-6 (230 LOC). Uses the existing Postgres database (`heart.tool_cache`).

---

## What This Changes

### Before F020 (F016 only)
```
grep -rn "auth" src/  → 8,200 chars → messages[] → age 6 → metadata-degrade (first line only)
web_search("CVE")     → 3,100 chars → messages[] → age 15 → hard-clear → regex facts only
```

### After F020
```
grep -rn "auth" src/  → SmartCompress → 420 chars (errors + top-K) → messages[] → age 15 → metadata-degrade
web_search("CVE")     → SmartCompress → 380 chars (top-12) + cache[abc123] → messages[] → model can retrieve
```

Results are smaller from turn 1, F016 kicks in later, errors are never lost, web search is reversible.

---

## Non-Goals

- **TOIN learning system** — field importance learning from retrieval patterns. Too complex, insufficient retrieval data yet. Revisit at v1.0.
- **Full Kneedle algorithm** — the lite version (elbow detection on score cliff) is sufficient.
- **Context Tracker proactive expansion** — preemptively expanding cache before model asks. Future work.
- **TIME_SERIES strategy** — not a primary use case for current Nous workloads.
- **Batch API support** — not relevant to current Nous architecture.

---

## Open Questions

1. **`raw_text` compression for read_file** — the `preserve` decay profile (F016) was chosen because code files are information-dense. SmartCompress could still reduce initial size by stripping function bodies while keeping signatures. Is this safe or does it risk hiding implementation details the model needs?

2. **Cache TTL granularity** — fixed 30min TTL vs. turn-count-based TTL that tracks actual conversation pace. Turn-count is more semantically correct but requires tracking average turn duration.

3. **BM25 dependency** — add `rank_bm25` to deps, or implement simple TF-IDF inline? `rank_bm25` is 1 file, no transitive deps.
