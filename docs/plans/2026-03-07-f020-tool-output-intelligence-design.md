# F020 Tool Output Intelligence — Revised Design

**Date:** 2026-03-07
**Status:** Approved
**Depends on:** F016 (Context Pruning) — shipped

## Problem

Tool results enter `messages[]` at full size and stay that way until F016's age-based decay kicks in. Three specific gaps:

1. **No ingestion-time compression** — a 500-line grep output stays 500 lines until soft-trim at age 3+
2. **Soft-trim is content-blind** — `head N chars + tail N chars` knows nothing about what matters (errors, outliers, scores)
3. **Non-re-fetchable results are permanently lost** — when F016 hard-clears `web_search` / `web_fetch`, only regex-extracted scraps (URLs, paths, key-values, max 10) survive

## Solution: Two Phases

### Phase 1: SmartCompress (ingestion-time)

Statistically compress tool results immediately after dispatch, before appending to `messages[]`. Operates at L0 — before any F016 decay.

**Hook point:** `runner.py:1028-1036`, between `dispatch()` return and `tool_results_for_message.append()`.

**Pipeline:**
1. **Crushability check** — skip compression if uniqueness ratio > 0.9 AND no errors AND no score fields AND no numeric anomalies. Always safer to pass through than blindly drop.
2. **Content-type classification** — `dict_array`, `string_array`, `log_format`, `raw_text`, `small` (<500 chars, skip)
3. **Error & outlier preservation** — hard guarantee: lines with error/exception/failed/critical/traceback/fatal/panic are always kept. Structural outliers and rare status values preserved.
4. **Adaptive K (Kneedle-lite)** — find natural elbow in scored items instead of hardcoded head+tail. Score lines by relevance, find the cliff, keep top-K + errors.

**Compression strategies:**
- `string_array` (bash/grep): error lines + top-K by score + last N lines
- `dict_array` (JSON): top-K by score field or cluster dedup
- `log_format`: all ERROR/WARN + change points + first/last 5
- `raw_text` (large files): imports + class/function signatures (bodies stripped)

**Config:**
```env
NOUS_SMART_COMPRESS_ENABLED=true
NOUS_SMART_COMPRESS_MIN_CHARS=500
NOUS_SMART_COMPRESS_MAX_K=50
NOUS_SMART_COMPRESS_ELBOW_THRESHOLD=0.3
```

### Phase 2: ReversibleCache (Postgres-backed)

When a non-re-fetchable tool result is compressed, store the original in Postgres. The model can retrieve it on demand via `cache_retrieve`.

#### Deviation from Original Spec

| Original Spec | This Design | Rationale |
|---------------|-------------|-----------|
| Redis backend + TTL | **Postgres `heart.tool_cache`** | Already have Postgres with async SQLAlchemy. No new infrastructure. Session-scoped rows cleaned on session end. |
| Tool list built once, inject dynamically | **Rebuild tools per loop iteration** | 3-line change in `_tool_loop`. `cache_retrieve` appears only when cache has entries. |
| `rank_bm25` dependency | **Simple TF-IDF inline** | Avoid new dependency. Fallback to basic keyword matching if needed. |
| "Context Safety section" hint | **Tier 0 or Tier 1 context injection** | No "Context Safety" section exists. Use existing tiered context. |

#### Schema

```sql
CREATE TABLE heart.tool_cache (
  id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  agent_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  hash_key TEXT NOT NULL,
  tool_name TEXT NOT NULL,
  tool_input JSONB,
  original_content TEXT NOT NULL,
  compressed_at TIMESTAMPTZ DEFAULT now(),
  item_count INT,
  UNIQUE(session_id, hash_key)
);
```

Hash key: `SHA256(original_content)[:16]`.

#### Tool: cache_retrieve

```json
{
  "name": "cache_retrieve",
  "description": "Retrieve original content from a previously compressed search or fetch result.",
  "input_schema": {
    "type": "object",
    "properties": {
      "hash_key": {"type": "string", "description": "Hash key from the [SmartCompressed] marker"},
      "query": {"type": "string", "description": "Optional: return only items matching this query (BM25/TF-IDF)"}
    },
    "required": ["hash_key"]
  }
}
```

#### Dynamic Tool Injection

```python
# runner.py _tool_loop — move tool list inside loop
while turns < max_turns:
    tools = self._dispatcher.available_tools(frame_id)
    if await self._has_cache_entries(session_id):
        tools.append(CACHE_RETRIEVE_TOOL)
    api_response = await self._call_api(...)
```

#### Cleanup

Piggyback on existing `session_ended` event:

```python
async def _cleanup_tool_cache(event: SessionEndedEvent):
    async with db.session() as s:
        await s.execute(
            text("DELETE FROM heart.tool_cache WHERE session_id = :sid"),
            {"sid": event.session_id}
        )
        await s.commit()

bus.subscribe("session_ended", _cleanup_tool_cache)
```

#### Context Hint

When cache entries exist for the session, inject into context assembly:

```
Compressed results available:
- [abc123de] web_search("query"): 47 items, 12 shown. Use cache_retrieve("abc123de") for full results.
```

#### Non-Re-Fetchable Tools

Cached: `web_search`, `web_fetch`
Not cached: `read_file`, `bash`, `run_python`, `list_files`, `recall_deep` (all re-fetchable)

**Note:** `web_fetch` is currently missing from `TOOL_DECAY_PROFILES` (defaults to "standard", not "conservative"). Should be added as "conservative" to align with `web_search`.

## Spec Validation Findings

From codebase review (decision `e68e7fc0`):

- Hook point `runner.py:1028-1036` — **confirmed correct**
- F016 4-tier pruning pipeline — **confirmed accurate**
- `_extract_facts_before_clear` regex extraction — **confirmed, max 10 items**
- Decay profiles: standard=(3,8,12), conservative=(5,10,15), preserve=(8,999,20), aggressive=(2,4,8) — **confirmed**

## Implementation Priority

| # | Component | Estimate |
|---|-----------|----------|
| 1 | Crushability check + error preservation | ~60 LOC |
| 2 | Adaptive K for string_array | ~80 LOC |
| 3 | dict_array score detection + top-K | ~60 LOC |
| 4 | tool_cache table + ORM model | ~40 LOC |
| 5 | Cache store on compress + cache_retrieve handler | ~80 LOC |
| 6 | Dynamic tool injection in _tool_loop | ~10 LOC |
| 7 | Session-end cleanup handler | ~15 LOC |
| 8 | Context hint injection | ~30 LOC |
| 9 | BM25/TF-IDF search within cache | ~50 LOC |

**Phase 1 (items 1-3):** ~200 LOC, zero new infrastructure
**Phase 2 (items 4-9):** ~225 LOC, one new table, one event handler
