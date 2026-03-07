# F020 — Tool Output Intelligence (SmartCompress + ReversibleCache)

> **Status:** Planned
> **Priority:** P1
> **Depends on:** F016 (Context Pruning), Redis

---

## Overview

Two complementary improvements to tool output handling that operate at **L0 (ingestion-time)** — before F016's age-based decay, before results ever enter `messages[]`.

- **Phase 1 — SmartCompress:** Statistical ingestion-time compression. Preserves what matters (errors, outliers, scored items), adapts aggressively only when the signal is clear enough to know what to drop.
- **Phase 2 — ReversibleCache:** Hash-keyed cache for non-re-fetchable tool outputs (`web_search`, `web_fetch`). When content is compressed, the original is stored in Redis and retrievable on demand via a `cache_retrieve` tool.

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
| `dict_array` | Valid JSON array of objects | Score/rank + cluster dedup + adaptive K |
| `string_array` | Newline-separated lines (bash/grep) | Error preservation + adaptive K on scores |
| `log_format` | Timestamps + log levels | Error/change-point preservation |
| `raw_text` | Large read_file output | Structural section detection |
| `small` | < 500 chars | No compression |

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

**For `dict_array` (JSON API results):**
1. Detect score field (bounded numeric, often sorted descending)
2. If found: use top-K by score (Kneedle elbow on score distribution)
3. If not found: use cluster dedup (deduplicate near-identical items, keep one per cluster)
4. Always include error items and structural outliers

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
NOUS_SMART_COMPRESS_MIN_CHARS=500      # Below this, never compress
NOUS_SMART_COMPRESS_MAX_K=50           # Max items to keep per result
NOUS_SMART_COMPRESS_ELBOW_THRESHOLD=0.3  # Score cliff to detect elbow
```

---

## Phase 2: ReversibleCache

### The Gap

`web_search` and `web_fetch` are **non-re-fetchable**: when F016 hard-clears them, the original results are permanently lost. Re-calling `web_search` would return different results (stale, different ranking). The model is left with only the regex-extracted facts (URLs, paths, key-values) from pre-prune extraction.

### Solution

When a non-re-fetchable tool result is compressed, store the original in Redis keyed by content hash. Inject a `cache_retrieve` tool so the model can get it back on demand.

---

### Cache Storage

```
Redis key:  nous:tool_cache:{session_id}:{SHA256(original_content)[:16]}
TTL:        hard_clear_age × avg_turn_seconds (default: 15 × 120s = 30 min)
Value:      {tool_name, tool_input, original_content, compressed_at, item_count}
```

**Non-re-fetchable tools (cached):** `web_search`, `web_fetch`, any future API-call tools
**Re-fetchable tools (not cached):** `read_file`, `bash`, `run_python`, `list_files`

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

When active cache entries exist, append to the "Context Safety" section:

```
Compressed results available:
- [abc123de] web_search("PyJWT vulnerabilities 2026"): 47 items, 12 shown. Call cache_retrieve("abc123de") for more.
- [def456gh] web_fetch("https://owasp.org/..."): 8,200 chars, summary shown. Call cache_retrieve("def456gh") for full content.
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
| 4 | ReversibleCache (Redis store + TTL) | ~100 | Closes web_search gap |
| 5 | `cache_retrieve` tool injection | ~80 | Makes cache usable |
| 6 | BM25 search within cache | ~50 | Targeted retrieval |
| 7 | `raw_text` structural section detection | ~80 | Large file compression |

**Minimum viable:** Items 1-2 (140 LOC). Immediate wins with zero new infrastructure.
**Full Phase 1:** Items 1-3 (200 LOC). Complete SmartCompress.
**Full Phase 2:** Items 4-6 (230 LOC). Requires Redis (already in Nous infrastructure).

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
