# F075 — Python-Level Spec Review

**Reviewer:** python-pro (Claude code reviewer)
**Date:** 2026-05-27
**Spec:** `docs/features/F075-temporal-fact-extraction.md` (Draft v1)
**Scope:** Python-level correctness — pydantic v2 idioms, SQLAlchemy migration shape, async retrofit script, retrieval boost step, type safety, test idioms.

---

## TL;DR

Spec is implementable as drafted, but six concrete Python-level corrections are needed before code. The biggest is **P1-1**: the spec invents an `ExtractedFact` pydantic model that does not exist in the codebase — the current `FactExtractor` (`nous/handlers/fact_extractor.py:153-208`) passes raw dicts straight into `FactInput`. Spec must either create `ExtractedFact` (and wire `_extract_facts` through it) or augment `FactInput` directly. The other findings are smaller but blocking: malformed `WHERE id = (SELECT id FROM batch)` SQL (P1-2), missing `r.score or 0.0` defensive coalesce in the boost (P2-2), and unspecified ORM mapping for the new `event_date` column (P1-4).

---

## P1 — Must-fix before implementation

### P1-1. `ExtractedFact` pydantic model does not exist in the codebase

**Spec claim (§Layer 1, lines 119-121):**
> Add `event_date: str | None` to `ExtractedFact` Pydantic model (`nous/heart/schemas.py`). Validator: must match `^\d{4}-\d{2}-\d{2}$` OR be `None`.

**Reality:** No such class. `nous/heart/schemas.py:85-99` defines `FactInput` (write-path into Heart). `FactExtractor._extract_facts` (`nous/handlers/fact_extractor.py:297-324`) returns `list[dict[str, Any]]` — raw JSON from `parse_llm_json`. The dicts are read by `_store_extracted_facts` (`nous/handlers/fact_extractor.py:154-208`) which builds `FactInput` directly via `fact.get("subject")`, `fact.get("content")`, etc. **There is no intermediate pydantic boundary today.**

**Two clean options — pick one in the impl plan:**

1. **Create `ExtractedFact` as a real boundary** in `nous/heart/schemas.py`:

   ```python
   class ExtractedFact(BaseModel):
       subject: str
       content: str
       category: str | None = None
       confidence: float = Field(default=0.7, ge=0.0, le=1.0)
       event_date: str | None = Field(
           default=None,
           pattern=r"^\d{4}-\d{2}-\d{2}$",
       )
   ```

   Then refactor `_extract_facts` to return `list[ExtractedFact]` (using `ExtractedFact.model_validate(d)` per dict in a try/except — fail-soft per spec §Layer 1 closing paragraph). This is the cleanest pydantic v2 idiom; `Field(pattern=...)` is the v2-native way for simple regex (matches `nous_eval/handlers/_models.py:25,42,108`).

2. **Skip `ExtractedFact`, add `event_date` directly to `FactInput`** (`nous/heart/schemas.py:85`) and to the SQLAlchemy `Fact` model (`nous/storage/models.py:469`). The fact_extractor's current "dict → FactInput" boundary already does the validation. Add a `@field_validator("event_date", mode="before")` on `FactInput` that:
   - coerces empty string to None,
   - rejects malformed ISO strings (raises pydantic `ValueError` → caller catches → drops the field per spec's fail-soft requirement).

   This is the **smaller change** and matches the existing architecture better.

Recommend Option 2 unless arch review wants a public DTO layer.

**Pydantic v2 idiom for the validator (if going stricter than regex):**

```python
from datetime import date
from pydantic import field_validator

@field_validator("event_date", mode="before")
@classmethod
def _validate_iso_date(cls, v: str | None) -> str | None:
    if v is None or v == "":
        return None
    try:
        date.fromisoformat(v)  # validates YYYY-MM-DD shape
    except (ValueError, TypeError):
        raise ValueError(f"event_date must be ISO YYYY-MM-DD, got {v!r}")
    return v
```

Using `date.fromisoformat` is stricter than the regex (rejects `2024-02-30`, `2024-13-01`) and matches what Layer 3 will do at read time. **Strongly prefer this over the bare regex.**

### P1-2. Backfill SQL `WHERE id = (SELECT id FROM batch)` is malformed for batches > 1

**Spec §Layer 4, lines 303-314:**

```sql
WITH batch AS (
  SELECT id FROM heart.facts ... LIMIT $2
)
UPDATE heart.facts
SET event_date = $3, updated_at = NOW()
WHERE id = (SELECT id FROM batch);
```

`= (SELECT ...)` raises `more than one row returned by a subquery used as an expression` at runtime when `LIMIT > 1`. The spec text immediately below the SQL ("Per-row update, not bulk, because each row gets a different LLM-derived date") makes the intent clear: **drop the CTE entirely** and just do per-row update keyed on `id = :id`, mirroring F047 (`nous/handlers/actionability_backfill.py:198-216`):

```sql
UPDATE heart.facts
SET event_date = :event_date, updated_at = NOW()
WHERE id = :id
```

The CTE in the spec confuses fetch (batched) with update (per-row). Keep them separate as F047 does (`_fetch_batch` returns N rows; `_update_actionable` runs per-row).

### P1-3. Advisory lock: prefer F049's `hashlib.sha256 % (2**31)` over `hashtext`

**Spec §Layer 4, lines 251-253** mentions `pg_try_advisory_xact_lock` keyed on SHA-256 of agent_id. Two existing precedents in the repo:

- **F047 backfill** (`nous/handlers/actionability_backfill.py:108-115`): `hashlib.sha256(...).digest()[:8]` → `int.from_bytes(..., "big", signed=True)` → full 64-bit signed bigint passed to `pg_try_advisory_lock` (session-scoped, paired with `pg_advisory_unlock`).
- **F049 WM sweep** (`nous/heart/working_memory.py:343-348`): `hashlib.sha256(...).digest()[:4]` → `int.from_bytes(..., "big") % (2**31)` → 31-bit unsigned key passed to `pg_try_advisory_xact_lock` (transaction-scoped, auto-released).

`hashtext(agent_id)::bigint` would be simpler but **not portable to SQLite tests** (F047's lock gate already has a `try/except` exactly for this — see `nous/handlers/actionability_backfill.py:78-86`). Use Python-side hashing.

**For this backfill: copy F047's pattern** (session-scoped lock + explicit unlock + SQLite-safe try/except). The backfill is a one-shot script, not a hot path; transaction-scoped (F049 style) would lock for the entire script duration which is fine but less explicit on failure. F047 is the closer analog.

### P1-4. ORM mapping for `event_date` not specified

The spec adds a SQL column at migration 053 but never says how `Fact` SQLAlchemy class (`nous/storage/models.py:469-511`) is updated. **Required additions:**

```python
from datetime import date  # add to imports at top
from sqlalchemy import Date

# In Fact class, near actionable (line 503-505):
# F075: Temporal anchor for date-arithmetic queries
event_date: Mapped[date | None] = mapped_column(Date, nullable=True, default=None)
```

Without this, ORM reads will silently miss the column. Add to the spec's §Tests under "Acceptance criteria #2 (existing tests pass)" — `tests/test_storage_models.py` if it touches `Fact` will need a corresponding addition.

Also: spec proposes surfacing `event_date` via `r.metadata.get("event_date")` in Layer 3 (line 215). That requires the recall pipeline to **actually populate** `metadata["event_date"]` when constructing `PipelineResult` from `RecallResult`. Currently `RecallResult.metadata` (`nous/heart/schemas.py:340`) is `dict = {}` and gets type-specific fields. Spec must explicitly add: "Heart.recall populates `event_date` in `RecallResult.metadata` for fact rows when the column is non-NULL." Otherwise Layer 3 reads None and silently no-ops forever.

---

## P2 — Should fix

### P2-1. `_apply_date_boost` should defensively coalesce `r.score`

**Spec §Layer 3, line 223:**

```python
r_boosted = replace(r, score=r.score * settings.date_aware_boost_factor)
```

`PipelineResult.score` is typed `float` (`retrieval_pipeline.py:62`) but the existing twin `_apply_adjacency_boost` uses `(r.score or 0.0) * boost` (`retrieval_pipeline.py:736`) defensively. Match that idiom — costs nothing, prevents a class of None-multiply crashes if a future caller constructs `PipelineResult(score=None)` somewhere upstream.

Also: the spec's sort key `key=lambda r: r.score` (line 227) should be `key=lambda r: r.score or 0.0` to match `_apply_adjacency_boost:737`.

### P2-2. Migration 053 style: pick BEGIN/COMMIT or not

Existing migrations are inconsistent:
- `052_f069_document_source_kind.sql:22,34` — wraps in `BEGIN; ... COMMIT;`
- `034_fact_actionability.sql` — no transaction wrapper

Both work (psycopg/asyncpg DDL is autocommit per statement unless wrapped). **Recommend matching 052** (the most recent migration) and wrapping the `ALTER TABLE` + `CREATE INDEX` + `COMMENT` in `BEGIN; ... COMMIT;` so either all three apply or none do.

The spec's draft 053 omits the transaction wrapper. Minor — should be added when impl PR lands.

### P2-3. `LLMClient` choice for the backfill script

Spec §Layer 4 says "Per-row LLM call (cheap — Haiku)" without specifying client construction. **Correct pattern** (from F047, but `actionability_backfill.py` actually receives a pre-built `classifier` injected with a client — not directly comparable):

```python
# scripts/backfill_temporal_facts.py
from nous.config import Settings
from nous.api.anthropic_client import create_client
from nous.handlers import call_background_llm_structured

settings = Settings()  # loads from env
client = create_client(settings)  # AnthropicClient Protocol
await client.start()
try:
    # ... batched extraction loop, calling
    # call_background_llm_structured(client, model=settings.actionability_model, ...)
finally:
    await client.close()
```

`create_client` is at `nous/api/anthropic_client.py:1182`. The `LLMClient` protocol at `nous/handlers/__init__.py:23-28` is what `call_background_llm` consumes. **Use `call_background_llm_structured`** (`__init__.py:86`) with a `{"event_date": {"type": "string"} | null, "reason": "..."}` tool input_schema — the tool_use trick gives guaranteed JSON, no `parse_llm_json` repair fallback needed. Cleaner than the spec's pseudo-`USER` prompt with prose JSON instructions.

### P2-4. Token budget exhaustion path — F047 model

Spec §Layer 4 ("Token budget exhausted → clean halt with resume marker") matches F047 closely but doesn't name the mechanism. **F047's pattern** (`nous/handlers/actionability_backfill.py:55-65`) injects a `_budget_check` callable into the classifier; classifier returns "default" tier and increments a separate counter. **For F075** the equivalent is:

```python
# In the per-row loop:
if not self._budget_ok():
    logger.info(
        "F075 backfill: token budget exhausted at %d/%d rows. "
        "Re-run with same flags; NULL filter makes this idempotent.",
        processed, total_eligible,
    )
    break
```

The "resume marker" is **implicit** — the `event_date IS NULL` filter on `_fetch_batch` is the resume marker. Don't add an external marker file; spec already correctly notes this in line 318 ("Idempotence: the `event_date IS NULL` predicate makes the script safe to re-run after crash"). Just make sure the impl actually checks `_budget_ok` per row, not per batch.

### P2-5. `dataclasses.replace` import for Layer 3

Spec §Layer 3 shows `replace(r, score=...)` without an import. Existing pattern in `retrieval_pipeline.py:696,754` is `from dataclasses import replace` **inside the function body**, not module-level. Either works; spec should pick one. Module-level is fine since `dataclasses` is stdlib and free. Minor — pick consistent style.

---

## P3 — Nits / observations

### P3-1. pytest-asyncio config

Already in auto-mode (`pyproject.toml:59` — `asyncio_mode = "auto"`). The proposed `tests/test_temporal_*` files don't need `@pytest.mark.asyncio` decorators on individual tests when the function is `async def`. Spec doesn't mention decorators; just confirming no config change is required.

### P3-2. `tests/test_fact_extractor.py` does NOT exist

Spec §Tests "No regression in `tests/test_fact_extractor.py`" — that file isn't in the repo. Only `tests/test_fact_extractor_episode_id.py` exists (single-purpose F022 audit regression). The new `tests/test_temporal_extractor.py` will be the primary FactExtractor test suite. Either rename the spec reference or note that the existing file is the F022 narrow test.

### P3-3. `dateparser` not in dependencies

Spec §Layer 1 closing paragraph and §Deferred F075.4 reference `dateparser` as a v2 fallback. `dateparser` is a heavyweight dep (~3 MB, pulls `pytz`, `tzlocal`, etc.). If it's truly v2-deferred, no action; if anyone wants to land it in v1, it must be added to `pyproject.toml` and **not** as a runtime-optional like `sentence-transformers`. Date parsing is a critical path, not feature-flagged. Just keep it out of v1 as the spec correctly proposes.

### P3-4. `event_date` Python type: `date` not `datetime`, not `str`

Type-safety flow (spec §Layer 1 + §Schema):
- Pydantic field: `str | None` (LLM emits ISO string)
- DB column: `DATE` (Postgres native)
- ORM read: `Mapped[date | None]` (Python `datetime.date`)
- Layer 3 read: `date.fromisoformat(ed)` already returns `date`, comparison `window[0] <= event_date <= window[1]` requires `window[0]: date` not `datetime`.

**Spec is consistent here.** Just flag for impl: do NOT use `datetime` anywhere; mixing `date` and `datetime` in comparisons raises `TypeError`. `_infer_query_date_window` must return `(date, date) | None`, not `(datetime, datetime) | None`.

### P3-5. `r.metadata.get("event_date")` in Layer 3 → see P1-4

Cross-referenced above. Without RecallResult.metadata propagation from Heart.recall, this `.get()` returns None and the whole boost is a silent no-op. Important enough to surface in arch review too.

---

## Summary of changes the spec needs before impl PR

1. **Decide:** create `ExtractedFact` model OR extend `FactInput` directly (P1-1).
2. **Fix backfill SQL:** drop the broken CTE; use per-row UPDATE keyed on `:id` (P1-2).
3. **Specify advisory lock implementation:** copy F047's `hashlib.sha256().digest()[:8]` → signed bigint pattern (P1-3).
4. **Add ORM mapping for `event_date`** in `nous/storage/models.py:469` Fact class (P1-4).
5. **Add `RecallResult.metadata["event_date"]` propagation step** in Heart.recall — without this, Layer 3 is dead code (P1-4 corollary).
6. **Use `date.fromisoformat` in validator**, not bare regex (P1-1 strengthening).
7. Smaller polish: `r.score or 0.0` coalesce, BEGIN/COMMIT in 053, `call_background_llm_structured` over prose JSON prompt.

None of these are architectural — they're Python-level corrections to make the spec implementable as written. Total spec-edit surface: ~30 lines across §Layer 1, §Layer 4, §Schema migration.
