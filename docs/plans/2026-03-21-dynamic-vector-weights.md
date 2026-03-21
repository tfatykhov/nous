# Plan: Dynamic Vector Weight Config + Runtime API

## Task
Add dynamic vector weight config, consolidate hybrid search, and add runtime API.

## Context
The Nous hybrid search currently has hardcoded 0.7 vector / 0.3 keyword weights in two places. We need to:
1. Consolidate to a single configurable weight
2. Make it overridable via env var
3. Add a runtime API endpoint so weights can be changed without redeployment

This is prep work for F025: Retrieval Self-Optimization, where we'll sweep different weight ratios against a test set.

### Step 1: Add config entry
- In `nous/config.py`, add a new config field: `vector_weight: float = 0.7`
- It should be settable via env var `NOUS_VECTOR_WEIGHT`
- Follow the existing pattern for how other config values are loaded

### Step 2: Update hybrid_search()
- In `nous/heart/search.py`, the `hybrid_search()` function (line 25) already accepts `vector_weight: float = 0.7` as a parameter
- Change the default to read from config instead of hardcoding 0.7
- Keep the function signature accepting an optional `vector_weight` parameter for callers that want to override
- keyword_weight is already computed as `1.0 - vector_weight` on line 64 — that's fine

### Step 3: Fix the duplicate in facts.py
- In `nous/heart/facts.py` lines 829-833, `search_facts_by_subject()` has its OWN hardcoded `0.7` and `0.3` in raw SQL that bypasses `hybrid_search()` entirely
- Refactor it to either:
  a. Call the shared `hybrid_search()` function, OR
  b. At minimum, read the weight from config instead of hardcoding
- Option (b) may be simpler since the query has fact-specific filters (`category`, `agent_id`). Just replace the hardcoded `0.7` and `0.3` with values from config.
- Make sure the behavior and return format stay compatible with existing callers

### Step 4: Config table + Runtime API endpoint

#### Config table
- Create `nous_system.config` key-value table:
  ```sql
  CREATE TABLE IF NOT EXISTS nous_system.config (
    key VARCHAR PRIMARY KEY,
    value JSONB NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW()
  );
  ```
- This serves as a general-purpose runtime config store (F025 will add more knobs: relevance floor, graph expansion depth, etc.)
- On startup, load persisted overrides from this table before falling back to env/defaults

#### API endpoints
- There is no existing `/admin` router in `nous/api/rest.py` — you'll need to create one
- Add `POST /admin/search-weights` endpoint
  - Request body: `{ "vector_weight": 0.6 }`
  - Sets the global default at runtime
  - Validate input: vector_weight must be between 0.0 and 1.0
  - **Log the change at INFO level** so weight changes can be correlated with search quality in logs
- Add `GET /admin/search-weights` that returns the current active weight **and its source**:
  ```json
  {
    "vector_weight": 0.6,
    "keyword_weight": 0.4,
    "source": "runtime_override"
  }
  ```
  - `source` is one of: `"caller_override"`, `"runtime_override"`, `"env_var"`, `"default"`
  - Makes debugging trivial — you always know *why* a weight is active
- Store the override both in-memory (module-level variable) and persist to `nous_system.config` table
- On startup, check the config table for a persisted override before falling back to env/default

### Step 5: Wire it all together
- `hybrid_search()` resolution order should be:
  1. Explicit `vector_weight` param passed by caller (highest priority)
  2. Runtime override set via API (if set)
  3. Env var `NOUS_VECTOR_WEIGHT` / config default (if set)
  4. Default 0.7 (fallback)
- Same resolution for the facts.py query
- Make sure this precedence is clear in code comments

### Validation
- Grep the entire codebase for any remaining hardcoded `0.7` or `0.3` related to search weights
- Run existing tests: `pytest tests/` to make sure nothing breaks
- The default behavior should be identical to before (0.7/0.3) — we're just making it configurable
- Test the API endpoints manually or with a quick test:
  - GET /admin/search-weights → returns 0.7, source: "default"
  - POST /admin/search-weights with 0.6 → returns success, logs at INFO
  - GET /admin/search-weights → returns 0.6, source: "runtime_override"
  - Run a search → confirm it uses 0.6
  - Restart container → confirm 0.6 persists from config table
