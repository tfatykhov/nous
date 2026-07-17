# R3 Keyed Fact Selection Implementation Plan (F085)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make F084's enumerative facts *selectable* by exact entity key: bidirectional entity indexing (R3.1), one canonical key normalizer used by writer and reader (R3.2), and a land-dark keyed retrieval leg (R3.3) — per the MAB team's `nous-r3-keyed-selection-requirements.md`.

**Architecture:** A new `heart.fact_entity_keys` join table (migration 065, DDL-only) indexes every participating entity of a keyed fact — subject AND proper-noun object/value entities — emitted in the SAME extraction LLM call (new `entities` array in the R1 schema). A single canonicalizer `normalize_key` v2 moves to `nous/heart/keys.py` (NFC, underscore→space, leading-article strip, intra-word-hyphen-preserving punctuation strip, fixpoint-iterated for idempotency) and is applied defensively at the store boundary. All data movement (re-normalize existing keys in place, seed subject-key rows, LLM value-side backfill with a column watermark) lives in one script. The read leg fires on flag + entity-presence (NOT frame — the MAB eval path has no frame concept), fetches active facts by exact key ranked by matched-key count then recency/ordinal, and merges ADDITIVELY (fresh `PipelineResult`s, bounded K, sub-head score, id-dedup — never mutates or reorders existing results) with `retrieval_leg='keyed'` provenance.

**Tech Stack:** Python 3.12, SQLAlchemy 2 async ORM + raw `text()` SQL, PostgreSQL 17, pydantic v2, pytest (+`postgres_only` marker), Haiku via `call_background_llm_structured`.

## Global Constraints

- Branch: `feat/r3-keyed-selection` in worktree `E:\Projects\nous\.claude\worktrees\plan12-graph-seed-score` (off origin/main a3b7416). **Every subagent must `cd` there and verify the branch first** — subagents default to the main checkout.
- Land-dark: `NOUS_KEYED_FACT_LEG_ENABLED` default **false**; with the flag off, `run_recall_pipeline` output is **byte-identical** to base (pinned by test). Write-side entity emission rides `extraction_enumerative_enabled` (already default false) — no new write-side master flag.
- Acceptance evaluation is executed by the MAB team with their scripts, **outside this session**. Do not run retrieval evals. Our deliverable is implementation + unit/integration tests only.
- Migration 065 is DDL-only; **no `/* */` block comments, no `$$` dollar-quoting** (migrator limitation, migrator.py:44-45). Data movement goes in the script, never the migration.
- Any read of `fact_entity_keys` MUST join `heart.facts` with `f.active = true` — entity rows are never cleaned on supersession (soft-delete invariant); standalone entity-table reads are forbidden.
- `normalize(normalize(x)) == normalize(x)` is a hard property (hand-rolled test; `hypothesis` is not a dependency).
- Key normalization happens at the PRODUCER (the enumerative extractor is today's only subject/attribute key producer; `_learn` does NOT re-normalize those columns) plus a defensive idempotent re-normalize of `entity_keys` at the `_learn` insert. The R2 exact-equality consumers (facts.py:1727-1733, sleep sweep :1540-1605) rely on format consistency — which is why the backfill must run promptly after deploy: until `phase_normalize` runs, v2-keyed new facts and v1-keyed legacy facts silently miss conflicts across the format boundary. `attribute_key` semantic stays as-is (max_len=100).
- Tests: real Postgres via `@pytest.mark.postgres_only` where DB features (ANY(), FK CASCADE) are used; unit tests must pass on the default SQLite runner. CI (fresh DB) is the merge gate — the shared local dev DB has known dirty-state failures.
- Commit style `feat:`/`fix:`/`test:` with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>` trailer. One logical change per commit.
- Type hints everywhere; docstrings on public functions; `mapped_column()` ORM style.

---

### Task 1: Canonicalizer v2 in `nous/heart/keys.py`

**Files:**
- Create: `nous/heart/keys.py`
- Modify: `nous/handlers/enumerative_extractor.py` (replace local `normalize_key` with import re-export; delete `_PUNCT`/`_WS` if unused elsewhere in the file — `_WS` is NOT used elsewhere; `_LIST_MARKER`/`_STATEMENT_LINE` stay)
- Test: `tests/test_entity_keys.py` (new)

**Interfaces:**
- Produces: `normalize_key(raw: str | None, *, max_len: int = 200) -> str | None` and `extract_entity_candidates(text: str, *, vocab: frozenset[str] | None = None, max_candidates: int = 8) -> list[str]` in `nous.heart.keys`. `normalize_key` stays importable as `nous.handlers.enumerative_extractor.normalize_key` (compat re-export; existing test imports at tests/test_write_path_adjudication.py:88 keep working).
- Consumes: nothing.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_entity_keys.py
"""R3 (F085): canonical key normalization + entity-candidate extraction."""
import unicodedata

from nous.heart.keys import extract_entity_candidates, normalize_key


class TestNormalizeKeyV2:
    def test_underscores_become_spaces(self):
        assert normalize_key("thomas_kyd") == "thomas kyd"

    def test_leading_article_stripped(self):
        assert normalize_key("The Marriage of Figaro") == "marriage of figaro"
        assert normalize_key("a red car") == "red car"
        assert normalize_key("An Apple") == "apple"

    def test_article_strip_iterates_to_fixpoint(self):
        # single-pass stripping would return "a red car" here
        assert normalize_key("the a red car") == "red car"

    def test_bare_article_is_preserved(self):
        # the whole key IS the article -> keep it rather than return None
        assert normalize_key("the") == "the"

    def test_intra_word_hyphen_preserved(self):
        assert normalize_key("cross-encoder") == "cross-encoder"

    def test_dangling_hyphen_removed(self):
        assert normalize_key("cross -encoder") == "cross encoder"
        assert normalize_key("- leading") == "leading"

    def test_nfc_unicode(self):
        composed = "café"                                   # U+00E9
        decomposed = unicodedata.normalize("NFD", "café")   # e + U+0301
        assert normalize_key(composed) == normalize_key(decomposed) == "café"

    def test_possessive_and_punctuation(self):
        assert normalize_key("Tim's Laptop") == "tims laptop"
        assert normalize_key("  RED   Car!! ") == "red car"

    def test_empty_none(self):
        assert normalize_key(None) is None
        assert normalize_key("") is None
        assert normalize_key("   ") is None
        assert normalize_key("!!!") is None

    def test_max_len_cap(self):
        assert len(normalize_key("x" * 300)) == 200
        assert len(normalize_key("x" * 300, max_len=100)) == 100

    def test_idempotent_property(self):
        cases = [
            "The Marriage of Figaro", "thomas_kyd", "the a red car",
            "cross-encoder", "Tim's Laptop", "café",
            unicodedata.normalize("NFD", "café"),
            "the " + "ab-" * 80,   # truncation lands mid-token -> dangling hyphen
            "A  b__c--d", "-", "the the the", "an an apple",
        ]
        for raw in cases:
            once = normalize_key(raw)
            assert normalize_key(once) == once, raw
```

- [ ] **Step 2: Run tests to verify they fail**

Run (from the worktree): `uv run pytest tests/test_entity_keys.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'nous.heart.keys'`

- [ ] **Step 3: Implement `nous/heart/keys.py`**

```python
"""R3 (F085): single canonical key normalizer + query-side entity candidates.

ONE canonicalizer for subject_key, attribute_key, and entity keys, used by
BOTH the write path (enumerative extractor, Heart.learn, backfill) and the
read path (keyed retrieval leg). The R2 conflict lookups compare keys by
exact string equality (facts.py), so every producer and consumer MUST route
through normalize_key.
"""
from __future__ import annotations

import re
import unicodedata

# Strip punctuation except hyphens (handled separately) — \w keeps letters,
# digits, underscore; underscores are converted to spaces beforehand.
_PUNCT = re.compile(r"[^\w\s-]")
_DANGLING_HYPHEN = re.compile(r"(?<!\w)-|-(?!\w)")
_WS = re.compile(r"\s+")
_ARTICLES = ("a ", "an ", "the ")


def _normalize_once(s: str, max_len: int) -> str:
    s = unicodedata.normalize("NFC", s).lower()
    s = s.replace("_", " ")
    s = _PUNCT.sub("", s)
    s = _DANGLING_HYPHEN.sub(" ", s)
    s = _WS.sub(" ", s).strip()
    for art in _ARTICLES:
        if s.startswith(art):
            s = s[len(art):]
            break
    return s[:max_len].strip()


def normalize_key(raw: str | None, *, max_len: int = 200) -> str | None:
    """Canonicalize an entity/attribute key (R3.2).

    lowercase; NFC; underscores -> spaces; strip punctuation except
    intra-word hyphens; collapse whitespace; strip leading articles
    (a/an/the); cap at max_len. Iterated to a fixpoint so
    normalize_key(normalize_key(x)) == normalize_key(x) always holds
    (single-pass article stripping and cap-induced dangling hyphens would
    otherwise break idempotency).
    """
    if not raw:
        return None
    s = raw
    for _ in range(10):  # fixpoint loop; bounded defensively, converges in <=3
        nxt = _normalize_once(s, max_len)
        if nxt == s:
            break
        s = nxt
    return s or None


_QUOTED = re.compile(r"\"([^\"]{2,80})\"|'([^']{2,80})'|“([^”]{2,80})”")
# Runs of TitleCase words, skipping sentence-initial position (mirrors
# intent.py:148 discipline); allows lowercase connectors inside the run.
_CAP_SPAN = re.compile(
    r"(?<!^)(?<![.!?]\s)"
    r"\b[A-Z][\w'’-]*(?:\s+(?:of|the|de|la|van|von|[A-Z][\w'’-]*))*"
)
# NOTE (review P2-3): 'and' is deliberately NOT a connector — "The Marriage of
# Figaro and The Barber of Seville" must yield TWO spans, not one merged key.


def extract_entity_candidates(
    text: str,
    *,
    vocab: frozenset[str] | None = None,
    max_candidates: int = 8,
) -> list[str]:
    """NER-lite (R3.3 v1): quoted spans + capitalized spans + known-key
    n-gram matches against the agent's key vocabulary. Returns NORMALIZED,
    deduplicated candidate keys, quoted-first, capped at max_candidates.
    The vocab leg recovers lowercase/sentence-initial entities the
    capitalized-span heuristic misses ("the marriage of figaro").
    """
    seen: set[str] = set()
    out: list[str] = []

    def _add(raw: str) -> None:
        key = normalize_key(raw)
        if key and key not in seen:
            seen.add(key)
            out.append(key)

    for m in _QUOTED.finditer(text):
        _add(next(g for g in m.groups() if g))
    for m in _CAP_SPAN.finditer(text):
        # trim trailing lowercase connectors captured by the run
        span = re.sub(r"\s+(?:of|the|de|la|van|von)$", "", m.group(0))
        _add(span)
    if vocab:
        tokens = (normalize_key(text, max_len=1000) or "").split()
        for n in range(4, 0, -1):  # longest grams first
            for i in range(len(tokens) - n + 1):
                gram = " ".join(tokens[i : i + n])
                if gram in vocab and gram not in seen:
                    seen.add(gram)
                    out.append(gram)
    return out[:max_candidates]
```

- [ ] **Step 4: Rewire `enumerative_extractor.py`**

Replace lines 24-25 (`_PUNCT`/`_WS` — used nowhere else) and lines 31-39 (`normalize_key` def) with the import below. **KEEP lines 26-28** (`_LIST_MARKER`, `_STATEMENT_LINE` — used by `density_score` at :49; `re` import stays):

```python
from nous.heart.keys import normalize_key  # noqa: F401 — re-exported; R3.2 single canonicalizer
```

(Keep `_LIST_MARKER` and `_STATEMENT_LINE`. `_to_fact_inputs` call sites at :291-292 are unchanged.)

- [ ] **Step 5: Run new + pinned tests**

Run: `uv run pytest tests/test_entity_keys.py tests/test_write_path_adjudication.py -v`
Expected: all PASS. The pinned asserts at test_write_path_adjudication.py:91-136 (`"Tim's Laptop"→"tims laptop"`, `"  RED   Car!! "→"red car"`, max_len caps, extractor `"red car"`/`"owner"`) survive v2 by construction — if any fails, the implementation is wrong, not the test.

- [ ] **Step 6: Commit**

```bash
git add nous/heart/keys.py nous/handlers/enumerative_extractor.py tests/test_entity_keys.py
git commit -m "feat(heart): R3.2 canonical key normalizer v2 + NER-lite entity candidates in shared nous/heart/keys.py"
```

---

### Task 2: Migration 065 + `FactEntityKey` ORM model

**Files:**
- Create: `sql/migrations/065_fact_entity_keys.sql`
- Modify: `nous/storage/models.py` (after the `Fact` class, ~:606)
- Test: `tests/test_entity_keys.py` (append)

**Interfaces:**
- Produces: table `heart.fact_entity_keys(fact_id UUID FK CASCADE, entity_key VARCHAR(200), agent_id VARCHAR(100), created_at, PK(fact_id, entity_key))`, index `idx_fact_entity_keys_agent_key(agent_id, entity_key)`; column `heart.facts.entity_keys_extracted_at TIMESTAMPTZ NULL` (backfill watermark); ORM `FactEntityKey` + `Fact.entity_keys_extracted_at`.
- Consumes: `heart.facts` (existing).

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_entity_keys.py
import pytest
from sqlalchemy import select

from nous.storage.models import FactEntityKey


@pytest.mark.postgres_only
class TestFactEntityKeysSchema:
    async def test_insert_and_cascade_delete(self, session, make_fact):
        # make_fact: use the existing fixture pattern from test_write_path_adjudication.py
        # (insert a Fact row directly); if no fixture exists, create the Fact inline.
        fact = await make_fact(content="The author of X is Thomas Kyd.")
        session.add(FactEntityKey(fact_id=fact.id, entity_key="thomas kyd", agent_id=fact.agent_id))
        session.add(FactEntityKey(fact_id=fact.id, entity_key="x", agent_id=fact.agent_id))
        await session.flush()
        rows = (await session.execute(
            select(FactEntityKey).where(FactEntityKey.fact_id == fact.id)
        )).scalars().all()
        assert {r.entity_key for r in rows} == {"thomas kyd", "x"}
        await session.delete(fact)   # hard delete only in tests: FK must CASCADE
        await session.flush()
        rows = (await session.execute(
            select(FactEntityKey).where(FactEntityKey.fact_id == fact.id)
        )).scalars().all()
        assert rows == []
```

(**`make_fact` does NOT exist anywhere in the suite (review arch-P1-B) — define it in this test file**: a function-scoped async fixture that constructs `Fact(agent_id=..., content=..., subject_key=..., attribute_key=..., active=True, ...)` with sensible defaults overridable via kwargs, `session.add` + `await session.flush()`, returns the Fact. For THIS schema test the conftest `session` fixture is fine — insert and read happen on the same connection. Tests that read through `heart` (Tasks 4/5) must instead seed via `heart.db.session()` + commit; see Task 5.)

- [ ] **Step 2: Run test to verify it fails**

Run: `$env:NOUS_TEST_DB='postgres'; uv run pytest tests/test_entity_keys.py::TestFactEntityKeysSchema -v`
Expected: FAIL with `ImportError: cannot import name 'FactEntityKey'`

- [ ] **Step 3: Write migration `sql/migrations/065_fact_entity_keys.sql`**

```sql
-- 065: R3.1 bidirectional entity indexing (F085).
-- Join table: every participating entity of a keyed fact is a retrieval key.
-- DDL only - data movement (re-normalize + seed + LLM value-side) lives in
-- scripts/backfill_r3_entity_keys.py. Reads MUST join heart.facts on
-- active = true (entity rows are not cleaned on supersession).
CREATE TABLE IF NOT EXISTS heart.fact_entity_keys (
    fact_id UUID NOT NULL REFERENCES heart.facts(id) ON DELETE CASCADE,
    entity_key VARCHAR(200) NOT NULL,
    agent_id VARCHAR(100) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (fact_id, entity_key)
);

CREATE INDEX IF NOT EXISTS idx_fact_entity_keys_agent_key
    ON heart.fact_entity_keys (agent_id, entity_key);

-- R3.2 backfill watermark: statement-level resume marker for the value-side
-- extraction backfill; also stamped by the live write path.
ALTER TABLE heart.facts ADD COLUMN IF NOT EXISTS entity_keys_extracted_at TIMESTAMPTZ;
```

- [ ] **Step 4: Add ORM model in `nous/storage/models.py`** (after `Fact`, matching `EpisodeChunk`/`EpisodeDecision` style)

```python
class FactEntityKey(Base):
    """R3.1 (F085): entity-key index rows for keyed facts.

    No ORM relationship on purpose: facts are soft-deleted (Python-side
    cascade would never fire) and DB-level ON DELETE CASCADE covers test
    hard-deletes; skipping the relationship avoids async lazy-load traps.
    """

    __tablename__ = "fact_entity_keys"
    __table_args__ = {"schema": "heart"}

    fact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("heart.facts.id", ondelete="CASCADE"), primary_key=True
    )
    entity_key: Mapped[str] = mapped_column(String(200), primary_key=True)
    agent_id: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
```

And on `Fact` (beside `overrides_prior`, ~:599):

```python
    entity_keys_extracted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
```

(Match the exact import names already used in models.py — `ForeignKey`, `String`, `DateTime`, `func` are already imported for `EpisodeChunk`.)

- [ ] **Step 5: Run test to verify it passes**

Run: `$env:NOUS_TEST_DB='postgres'; uv run pytest tests/test_entity_keys.py::TestFactEntityKeysSchema -v`
Expected: PASS. **Schema reality (review db-P2-4): conftest does NOT apply migrations.** CI applies every `sql/migrations/*.sql` via `psql -f` (ci.yml:76-79); the SQLite unit path builds schema from `Base.metadata.create_all` (so the ORM model covers it); the shared LOCAL dev DB gets 065 only by hand — run `psql -f sql/migrations/065_fact_entity_keys.sql` against it once before local postgres_only runs.

- [ ] **Step 6: Commit**

```bash
git add sql/migrations/065_fact_entity_keys.sql nous/storage/models.py tests/test_entity_keys.py
git commit -m "feat(storage): migration 065 - heart.fact_entity_keys join table + extraction watermark (R3.1)"
```

---

### Task 3: Write path — entity emission in extraction + atomic child-row insert in `Heart.learn`

**Files:**
- Modify: `nous/handlers/enumerative_extractor.py` (`_EXTRACTION_SCHEMA` :62-103, `_EXTRACTION_PROMPT` :105-112, `_to_fact_inputs` :286-330)
- Modify: `nous/heart/schemas.py` (`FactInput`, beside :118-127)
- Modify: `nous/heart/facts.py` (`_learn` insert block :683-716; `_confirm_duplicate` :1203-1233)
- Modify: `nous/config.py` (two fields, beside the 064 block :246-302)
- Test: `tests/test_entity_keys.py` (append)

**Interfaces:**
- Consumes: `normalize_key` from Task 1; `FactEntityKey` from Task 2.
- Produces: `FactInput.entity_keys: list[str]` (default `[]`); config `entity_keys_max_per_fact: int = 8` (`NOUS_ENTITY_KEYS_MAX_PER_FACT`), `entity_key_min_chars: int = 3` (`NOUS_ENTITY_KEY_MIN_CHARS`); helper `is_keyable_entity(key: str, *, min_chars: int) -> bool` in `nous/heart/keys.py`; entity rows written inside the same transaction as the fact row; `fact.entity_keys_extracted_at` stamped when entity keys were provided.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_entity_keys.py
from nous.heart.keys import is_keyable_entity
from nous.heart.schemas import FactInput


class TestStopPolicy:
    def test_scalars_rejected(self):
        assert not is_keyable_entity("1876", min_chars=3)      # numeric
        assert not is_keyable_entity("12.5", min_chars=3)
        assert not is_keyable_entity("ab", min_chars=3)        # too short
        assert not is_keyable_entity("red", min_chars=3)       # scalar stoplist
        assert not is_keyable_entity("true", min_chars=3)

    def test_entities_accepted(self):
        assert is_keyable_entity("thomas kyd", min_chars=3)
        assert is_keyable_entity("belgium", min_chars=3)
        assert is_keyable_entity("cross-encoder", min_chars=3)


class TestExtractorEntityEmission:
    def test_to_fact_inputs_builds_entity_keys(self):
        from nous.handlers.enumerative_extractor import EnumerativeExtractor
        from types import SimpleNamespace
        settings = SimpleNamespace(
            temporal_extraction_enabled=False,
            entity_keys_max_per_fact=8,
            entity_key_min_chars=3,
        )
        ex = EnumerativeExtractor(heart=None, settings=settings, llm_client=None, embedder=None)
        raw = [{
            "content": "The author of The Marriage of Figaro is Thomas Kyd.",
            "subject_key": "The Marriage of Figaro",
            "attribute_key": "author",
            "entities": ["The Marriage of Figaro", "Thomas Kyd", "1876", "red"],
        }]
        (fi,) = ex._to_fact_inputs(raw, chunk_index=0, episode_id=None)
        assert fi.subject_key == "marriage of figaro"
        # subject key unioned; scalars dropped; all normalized
        assert fi.entity_keys == ["marriage of figaro", "thomas kyd"]

    def test_entity_keys_capped(self):
        from nous.handlers.enumerative_extractor import EnumerativeExtractor
        from types import SimpleNamespace
        settings = SimpleNamespace(
            temporal_extraction_enabled=False,
            entity_keys_max_per_fact=3,
            entity_key_min_chars=3,
        )
        ex = EnumerativeExtractor(heart=None, settings=settings, llm_client=None, embedder=None)
        raw = [{
            "content": "c.", "subject_key": "subj",
            "attribute_key": "attr",
            "entities": [f"entity number {i}" for i in range(10)],
        }]
        (fi,) = ex._to_fact_inputs(raw, chunk_index=0, episode_id=None)
        assert len(fi.entity_keys) == 3
        assert fi.entity_keys[0] == "subj"   # subject first (when it passes the stop-policy)


@pytest.mark.postgres_only
class TestLearnWritesEntityRows:
    async def test_entity_rows_same_txn_and_stamp(self, heart, session):
        fi = FactInput(
            content="The author of The Marriage of Figaro is Thomas Kyd.",
            subject="marriage of figaro",
            subject_key="marriage of figaro",
            attribute_key="author",
            entity_keys=["marriage of figaro", "thomas kyd"],
            source="enumerative_extractor",
        )
        result = await heart.learn(fi)
        rows = (await session.execute(
            select(FactEntityKey).where(FactEntityKey.fact_id == result.id)
        )).scalars().all()
        assert {r.entity_key for r in rows} == {"marriage of figaro", "thomas kyd"}
        fact = await session.get(Fact, result.id)
        assert fact.entity_keys_extracted_at is not None

    async def test_rejected_fact_writes_no_rows(self, heart, session):
        fi = FactInput(content="x", subject="s", entity_keys=["thomas kyd"])  # below min-content floor
        result = await heart.learn(fi)
        assert isinstance(result, FactRejected)
        n = (await session.execute(
            select(func.count()).select_from(FactEntityKey)
            .where(FactEntityKey.entity_key == "thomas kyd")
        )).scalar_one()
        assert n == 0
```

(Adapt fixture names to the actual `heart`/`session` fixtures in tests/conftest.py; import `Fact`, `FactRejected`, `func` as needed — copy the import block style from test_write_path_adjudication.py.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_entity_keys.py::TestStopPolicy tests/test_entity_keys.py::TestExtractorEntityEmission -v`
Expected: FAIL (`is_keyable_entity` not defined; `FactInput` has no `entity_keys`).

- [ ] **Step 3: Implement**

3a. `nous/heart/keys.py` — append:

```python
_NUMERIC = re.compile(r"[\d\s.,:/-]+")
# Code-side safety net; the extraction prompt is the primary proper-noun
# filter (R3.1 stop-policy). Deliberately small.
_SCALAR_STOP = frozenset({
    "red", "green", "blue", "black", "white", "yellow", "orange", "purple",
    "true", "false", "yes", "no", "none", "null", "unknown",
    # article-strip + question-word collisions (review devil-P3-5): keys like
    # "The Who"->"who" would otherwise create query-token junk buckets
    "the", "who", "what", "when", "where", "why", "how", "this", "that",
})


def is_keyable_entity(key: str, *, min_chars: int) -> bool:
    """R3.1 stop-policy: index proper-noun/entity values, never scalars.
    key must already be normalized."""
    if not key or len(key) < min_chars:
        return False
    if _NUMERIC.fullmatch(key):
        return False
    if key in _SCALAR_STOP:
        return False
    return True
```

3b. `nous/heart/schemas.py` — `FactInput` gains (beside `attribute_key`):

```python
    entity_keys: list[str] = Field(
        default_factory=list,
        description="R3.1: normalized keys of ALL participating entities (subject + proper-noun object/value side).",
    )
```

3c. `nous/handlers/enumerative_extractor.py`:
- `_EXTRACTION_SCHEMA` per-fact properties gains (after `attribute_key`):

```python
                    "entities": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": 8,
                        "description": (
                            "ALL named entities participating in this statement - the "
                            "subject AND any object/value-side entity (people, works, "
                            "places, organizations, products). NEVER scalar values: no "
                            "numbers, dates, colors, or common nouns."
                        ),
                    },
```

(`required` stays `["content", "subject_key", "attribute_key"]` — entities optional, subject unioned in code.)
- `_EXTRACTION_PROMPT` gains one line after the event_date line: `For each fact also list its participating named entities (subject and object side); never list scalar values.`
- `_to_fact_inputs` — after `akey` is computed, before the `FactInput(...)` append:

```python
            max_keys = getattr(self._settings, "entity_keys_max_per_fact", 8)
            min_chars = getattr(self._settings, "entity_key_min_chars", 3)
            entity_keys: list[str] = []
            raw_entities = f.get("entities") or []
            for cand in [skey, *[str(e) for e in raw_entities if e]]:
                nk = normalize_key(cand)
                if nk and nk not in entity_keys and is_keyable_entity(nk, min_chars=min_chars):
                    entity_keys.append(nk)
                if len(entity_keys) >= max_keys:
                    break
```

and pass `entity_keys=entity_keys` in the `FactInput(...)` constructor. Import `is_keyable_entity` at top beside `normalize_key`. **NO stop-policy exemption for the subject key** (review devil-P2-1): R2's conflict lookup reads `facts.subject_key` directly, not the entity table, so indexing a scalar subject ("red") gives R2 nothing and creates exactly the junk buckets R3.1 warns about. A scalar subject simply produces no entity row; `facts.subject_key` itself is untouched.

3d. `nous/heart/facts.py` `_learn` — immediately after `await session.flush()` (:716), inside the same session/txn:

```python
        if input.entity_keys:
            seen_keys: set[str] = set()
            max_keys = self._settings.entity_keys_max_per_fact
            for raw_key in input.entity_keys:
                nk = normalize_key(raw_key)  # defensive re-normalize; idempotent (R3.2)
                if nk and nk not in seen_keys:
                    seen_keys.add(nk)
                    session.add(FactEntityKey(fact_id=fact.id, entity_key=nk, agent_id=self.agent_id))
                if len(seen_keys) >= max_keys:
                    break
            fact.entity_keys_extracted_at = datetime.now(UTC)
```

Imports: `from nous.heart.keys import normalize_key` and `FactEntityKey` beside the existing `Fact` model import. Use the manager's actual agent-id/settings attribute names — read the surrounding code (`self._agent_id` vs `self.agent_id`, `self._settings`) and match.

3e. `nous/heart/facts.py` `_confirm_duplicate` — inside the existing both-keys-NULL backfill branch (:1224-1233), after the pair assignment:

```python
            if input.entity_keys and dupe.entity_keys_extracted_at is None:
                seen_dupe_keys: set[str] = set()
                for raw_key in input.entity_keys[: self._settings.entity_keys_max_per_fact]:
                    nk = normalize_key(raw_key)
                    if nk and nk not in seen_dupe_keys:
                        seen_dupe_keys.add(nk)
                        await session.execute(
                            pg_insert(FactEntityKey)
                            .values(fact_id=dupe.id, entity_key=nk, agent_id=self.agent_id)
                            .on_conflict_do_nothing()
                        )
                dupe.entity_keys_extracted_at = datetime.now(UTC)
```

**MUST be conflict-tolerant** (review db-P2-2): the backfill's `phase_seed` inserts subject-key rows WITHOUT stamping `entity_keys_extracted_at`, so a live dedup-confirm on a seeded-but-not-yet-extracted fact would PK-collide and abort the whole learn txn with a plain `session.add`. Use `pg_insert(...).on_conflict_do_nothing()` (already imported in facts.py as `pg_insert`) + an in-loop seen-set (review arch-P3-4). On the SQLite unit path this branch is unreachable for entity rows only if guarded — use the same dialect guard idiom facts.py already uses around `pg_insert`/advisory-lock code (grep `dialect.name` in facts.py and copy it).

3f. `nous/config.py` — in the 064 block:

```python
    entity_keys_max_per_fact: int = Field(
        default=8, ge=1,
        description="R3.1 (F085): max entity-key index rows per fact (subject key always included).",
    )
    entity_key_min_chars: int = Field(
        default=3, ge=1,
        description="R3.1 (F085): stop-policy floor - normalized entity keys shorter than this are not indexed (subject key exempt).",
    )
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_entity_keys.py tests/test_write_path_adjudication.py -v` then `$env:NOUS_TEST_DB='postgres'; uv run pytest tests/test_entity_keys.py -v`
Expected: all PASS (existing extractor tests unaffected — `entities` absent → `entity_keys == [subject_key]`).

- [ ] **Step 5: Commit**

```bash
git add nous/handlers/enumerative_extractor.py nous/heart/schemas.py nous/heart/facts.py nous/heart/keys.py nous/config.py tests/test_entity_keys.py
git commit -m "feat(heart): R3.1 entity emission in extraction + atomic entity-key rows in Heart.learn"
```

---

### Task 4: Backfill script (re-normalize + seed + LLM value-side)

**Files:**
- Create: `scripts/backfill_r3_entity_keys.py`
- Test: `tests/test_backfill_entity_keys.py` (new)

**Interfaces:**
- Consumes: `normalize_key`/`is_keyable_entity` (Task 1/3), `FactEntityKey` + `entity_keys_extracted_at` (Task 2), `call_background_llm_structured` (existing, nous/handlers/__init__).
- Produces: CLI `python scripts/backfill_r3_entity_keys.py --agent-id A [--dry-run] [--phase normalize|seed|extract|all] [--batch-size 500] [--llm-batch 40] [--max-llm-calls 0=unlimited]`. Follow `scripts/backfill_supersession.py` conventions exactly: `--agent-id` required, rollback watermark printed BEFORE writes, per-phase counts, `sys.path.insert` bootstrap, `await db.disconnect()`.

- [ ] **Step 1: Write the failing tests** (test the phase functions directly, not the CLI)

```python
# tests/test_backfill_entity_keys.py
"""R3.2 backfill: re-normalize keys in place; seed subject rows; value-side extract."""
import pytest
from sqlalchemy import select, func

from nous.storage.models import Fact, FactEntityKey


@pytest.mark.postgres_only
class TestBackfillPhases:
    async def test_normalize_phase_rewrites_old_format_keys(self, session, make_fact):
        f = await make_fact(subject_key="thomas_kyd", attribute_key="the_author")
        from scripts.backfill_r3_entity_keys import phase_normalize
        counts = await phase_normalize(session, agent_id=f.agent_id, dry_run=False)
        await session.flush()
        await session.refresh(f)
        assert f.subject_key == "thomas kyd"
        assert f.attribute_key == "author"
        assert counts["facts_updated"] == 1
        # idempotent: second run is a no-op
        counts2 = await phase_normalize(session, agent_id=f.agent_id, dry_run=False)
        assert counts2["facts_updated"] == 0

    async def test_normalize_phase_dry_run_writes_nothing(self, session, make_fact):
        f = await make_fact(subject_key="thomas_kyd")
        from scripts.backfill_r3_entity_keys import phase_normalize
        counts = await phase_normalize(session, agent_id=f.agent_id, dry_run=True)
        await session.refresh(f)
        assert f.subject_key == "thomas_kyd"
        assert counts["facts_updated"] == 1  # counted, not written

    async def test_seed_phase_inserts_subject_rows_idempotently(self, session, make_fact):
        f = await make_fact(subject_key="thomas kyd")
        from scripts.backfill_r3_entity_keys import phase_seed
        await phase_seed(session, agent_id=f.agent_id, dry_run=False)
        await phase_seed(session, agent_id=f.agent_id, dry_run=False)  # ON CONFLICT DO NOTHING
        n = (await session.execute(
            select(func.count()).select_from(FactEntityKey).where(FactEntityKey.fact_id == f.id)
        )).scalar_one()
        assert n == 1

    async def test_extract_phase_resumes_via_watermark(self, session, make_fact, monkeypatch):
        f1 = await make_fact(subject_key="k1", content="The capital of Belgium is Brussels.")
        f2 = await make_fact(subject_key="k2", content="The author of X is Thomas Kyd.")
        from unittest.mock import AsyncMock
        import scripts.backfill_r3_entity_keys as bf
        monkeypatch.setattr(bf, "call_background_llm_structured", AsyncMock(return_value={
            "items": [
                {"index": 0, "entities": ["Belgium", "Brussels"]},
                {"index": 1, "entities": ["Thomas Kyd", "X"]},
            ]
        }))
        counts = await bf.phase_extract(session, agent_id=f1.agent_id, settings=bf_settings(),
                                        llm_client=object(), llm_batch=40, max_llm_calls=0, dry_run=False)
        await session.flush()
        for f, expected in ((f1, {"k1", "belgium", "brussels"}), (f2, {"k2", "thomas kyd"})):
            rows = {r.entity_key for r in (await session.execute(
                select(FactEntityKey).where(FactEntityKey.fact_id == f.id))).scalars().all()}
            assert expected <= rows
            await session.refresh(f)
            assert f.entity_keys_extracted_at is not None
        # resume: second call finds no NULL-watermark facts, zero LLM calls
        mock2 = AsyncMock()
        monkeypatch.setattr(bf, "call_background_llm_structured", mock2)
        await bf.phase_extract(session, agent_id=f1.agent_id, settings=bf_settings(),
                               llm_client=object(), llm_batch=40, max_llm_calls=0, dry_run=False)
        mock2.assert_not_awaited()
```

("X" normalizes to "x", len 1 < 3 → dropped by stop-policy: assert with `<=` set-containment as shown. `bf_settings()` = tiny SimpleNamespace helper with `background_model`, `entity_keys_max_per_fact=8`, `entity_key_min_chars=3` — define at module top of the test.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `$env:NOUS_TEST_DB='postgres'; uv run pytest tests/test_backfill_entity_keys.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'scripts.backfill_r3_entity_keys'`

- [ ] **Step 3: Implement `scripts/backfill_r3_entity_keys.py`**

Copy the skeleton of `scripts/backfill_supersession.py` (arg parsing, DB bootstrap with `sys.path.insert`, watermark print, `await db.disconnect()` in `finally`). Phases as importable async functions (session-injected, testable without CLI):

```python
"""R3.2/R3.1 backfill (F085): (1) re-normalize existing F084 keys in place,
(2) seed subject-key entity rows, (3) LLM value-side entity extraction.

Rollback: entity rows this run -> DELETE FROM heart.fact_entity_keys
WHERE agent_id = :a AND created_at >= :watermark. Phase 1 key rewrites are
value-idempotent (normalize is a fixpoint) and cannot be auto-rolled back -
the watermark timestamp is printed for audit.

Resume: phase 3 processes only facts WHERE entity_keys_extracted_at IS NULL
(statement-level watermark, R3.2 hardening item); safe to kill and re-run.
"""

_VALUE_EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "maxItems": 40,
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "entities": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
                },
                "required": ["index", "entities"],
            },
        }
    },
    "required": ["items"],
}

_VALUE_EXTRACTION_PROMPT = """For each numbered statement, list ALL named entities that
participate in it - the subject AND any object/value-side entity (people, works,
places, organizations, products). NEVER list scalar values: no numbers, dates,
colors, or common nouns. Return one item per statement index.

<statements>
{numbered}
</statements>"""


async def phase_normalize(session, *, agent_id: str, dry_run: bool) -> dict[str, int]:
    # SELECT id, subject_key, attribute_key FROM heart.facts
    #  WHERE agent_id=:a AND (subject_key IS NOT NULL OR attribute_key IS NOT NULL)
    # paged by id; for each row compute normalize_key(subject_key),
    # normalize_key(attribute_key, max_len=100); if changed and not dry_run:
    # UPDATE the row. Then the entity-rows pass: SELECT fact_id, entity_key
    # FROM heart.fact_entity_keys ek JOIN heart.facts f ON f.id=ek.fact_id
    # WHERE f.agent_id=:a; ONLY for rows where new_key != old_key (review
    # db-P3-5: an unconditional INSERT+DELETE would DELETE already-canonical
    # rows after their insert conflicts): INSERT new (ON CONFLICT DO NOTHING)
    # then DELETE old row. Return counts:
    # {"facts_scanned", "facts_updated", "entity_rows_rewritten"}.
    ...

async def phase_seed(session, *, agent_id: str, dry_run: bool) -> dict[str, int]:
    # SELECT id, subject_key, agent_id FROM heart.facts
    #  WHERE agent_id=:a AND subject_key IS NOT NULL
    # then FILTER in Python: is_keyable_entity(subject_key, min_chars=...)
    # (review devil-P2-1: scalar subjects like "red" must NOT be indexed -
    # the stop-policy applies to subject keys here exactly as at write time);
    # INSERT the survivors ON CONFLICT DO NOTHING. dry_run: count only.
    ...

async def phase_extract(session, *, agent_id: str, settings, llm_client,
                        llm_batch: int, max_llm_calls: int, dry_run: bool) -> dict[str, int]:
    # loop: SELECT id, content, subject_key FROM heart.facts
    #   WHERE agent_id=:a AND subject_key IS NOT NULL
    #     AND entity_keys_extracted_at IS NULL
    #   ORDER BY learned_at LIMIT :llm_batch
    # -> build numbered prompt -> call_background_llm_structured(
    #      model=settings.background_model, schema=_VALUE_EXTRACTION_SCHEMA,
    #      max_tokens=2000)
    # -> None result: log warning, STOP (loud, resumable)
    # -> per item: VALIDATE index (review devil-P2-2): 0 <= index < len(batch)
    #    and not already seen this batch, else WARN + skip the item (never
    #    crash the batch on a malformed LLM index);
    #    keys = subject_key + normalized entities, ALL through
    #    is_keyable_entity (subject included - no exemption);
    #    INSERT ... ON CONFLICT DO NOTHING;
    #    stamp entity_keys_extracted_at = now(UTC) for EVERY fact in the
    #    batch that the LLM returned a (possibly empty) item for - that IS
    #    the resume marker; facts the LLM omitted stay NULL and are retried
    # -> honor max_llm_calls; dry_run: count batches only, no LLM calls/writes
    ...
```

Write the full bodies (the comments above are the specification; the implementer writes real code — batched loops, `text()` SQL with `ON CONFLICT DO NOTHING` via `sqlalchemy.dialects.postgresql.insert(...).on_conflict_do_nothing()`, progress logging every batch, totals printed at the end).

**Session/commit contract (review db-P2-3):** the phase functions NEVER call `session.commit()` — the tests drive them with the conftest rollback-isolated session and flush only; a commit inside a phase would commit the test fixture's outer transaction and leak rows into the shared dev DB. Only CLI `main()` commits: it opens its OWN session per batch (`async with db.session() as s: ... await s.commit()`), so a kill mid-run loses at most one batch. `main()` prints the rollback watermark BEFORE any write. `--max-llm-calls` defaults to **2000** (covers ~46k facts at llm-batch 40 with headroom; 0 = unlimited must be explicit, review devil-P2-2).

- [ ] **Step 4: Run tests to verify they pass**

Run: `$env:NOUS_TEST_DB='postgres'; uv run pytest tests/test_backfill_entity_keys.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/backfill_r3_entity_keys.py tests/test_backfill_entity_keys.py
git commit -m "feat(scripts): R3.2 backfill - re-normalize keys in place + seed + LLM value-side entity extraction"
```

---

### Task 5: Keyed retrieval leg (land-dark) in `run_recall_pipeline`

**Files:**
- Modify: `nous/heart/facts.py` (two new read methods at class end)
- Modify: `nous/api/retrieval_pipeline.py` (`_PipelineAccumulator` :142-191, new stage after Stage 1.5 chunks :422-460, assembly :257-288, `PipelineStats` :93-134, new `_keyed_to_pipeline` converter)
- Modify: `nous/api/tools.py` (`_via_tag` :301-306)
- Modify: `nous/config.py` (three fields)
- Test: `tests/test_keyed_fact_leg.py` (new)

**Interfaces:**
- Consumes: `extract_entity_candidates` (Task 1), `fact_entity_keys` table (Task 2).
- Produces:
  - `FactManager.entity_key_vocabulary(limit: int = 50_000) -> frozenset[str]`
  - `FactManager.fetch_by_entity_keys(keys: list[str], limit: int) -> list[Row]` — rows `(id, content, learned_at, source_ordinal, matched)`; **joins facts on active=true**; ordered `matched DESC, learned_at DESC, source_ordinal DESC NULLS LAST`
  - Config: `keyed_fact_leg_enabled: bool = False` (`NOUS_KEYED_FACT_LEG_ENABLED`), `keyed_fact_leg_k: int = 8, ge=1` (`NOUS_KEYED_FACT_LEG_K`), `keyed_fact_leg_score: float = 0.55, ge=0, le=1` (`NOUS_KEYED_FACT_LEG_SCORE`)
  - `PipelineStats` gains `keyed_leg_used: bool = False`, `n_keyed: int = 0`, `n_keyed_dup: int = 0`
  - Keyed `PipelineResult`s: `type="fact"`, `source="heart"`, `metadata={"retrieval_leg": "keyed", "matched_keys": <int>}`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_keyed_fact_leg.py
"""R3.3 (F085): keyed retrieval leg - land-dark, additive-only, bounded."""
import pytest

from nous.api.retrieval_pipeline import run_recall_pipeline


@pytest.mark.postgres_only
class TestKeyedFactLeg:
    async def test_flag_on_without_candidates_matches_flag_off(self, heart, brain, settings, seed_keyed_corpus):
        # review devil-P3-3: comparing default-off vs explicit-off is tautological.
        # The real invariant: flag ON with a query yielding zero entity candidates
        # (no capitals, no quotes, nothing in vocab) takes the same path as OFF.
        q = "nothing here matches any indexed entity at all"
        off = await run_recall_pipeline(q, heart, brain, settings)
        on = await run_recall_pipeline(
            q, heart, brain, settings.model_copy(update={"keyed_fact_leg_enabled": True}))
        assert [(r.id, r.score, r.metadata) for r in off.results] == \
               [(r.id, r.score, r.metadata) for r in on.results]
        # NOTE: the corpus vocab contains "marriage of figaro"/"thomas kyd"/"belgium";
        # none of these n-grams occur in q. True flag-OFF byte-identity vs the
        # pre-feature baseline is pinned by the existing recall_deep snapshot test.

    async def test_keyed_hit_retrieved_with_provenance(self, heart, brain, settings, seed_keyed_corpus):
        # seed_keyed_corpus stores a fact whose embedding will NOT rank for the
        # query (no embedder in test -> Stage 1 empty) but which carries entity
        # keys {"marriage of figaro", "thomas kyd"}.
        s = settings.model_copy(update={"keyed_fact_leg_enabled": True})
        out = await run_recall_pipeline('Who is the author of "The Marriage of Figaro"?', heart, brain, s)
        keyed = [r for r in out.results if r.metadata.get("retrieval_leg") == "keyed"]
        assert keyed and keyed[0].id == seed_keyed_corpus["gold_id"]
        assert keyed[0].type == "fact" and keyed[0].source == "heart"
        assert out.stats.keyed_leg_used and out.stats.n_keyed >= 1

    async def test_superseded_fact_not_returned(self, heart, brain, settings, seed_keyed_corpus):
        # seed an inactive fact sharing the entity key
        s = settings.model_copy(update={"keyed_fact_leg_enabled": True})
        out = await run_recall_pipeline('Tell me about "The Marriage of Figaro"', heart, brain, s)
        ids = {r.id for r in out.results}
        assert seed_keyed_corpus["superseded_id"] not in ids

    async def test_k_cap_and_scores_bounded(self, heart, brain, settings, seed_keyed_corpus_many):
        s = settings.model_copy(update={"keyed_fact_leg_enabled": True, "keyed_fact_leg_k": 3})
        out = await run_recall_pipeline('Facts about "Belgium"', heart, brain, s)
        keyed = [r for r in out.results if r.metadata.get("retrieval_leg") == "keyed"]
        assert len(keyed) == 3
        assert all(0.0 <= r.score <= s.keyed_fact_leg_score for r in keyed)

    async def test_dedup_skips_existing_ids(self, heart, brain, settings, seed_keyed_corpus):
        # when Stage 1 already returned the fact, the keyed leg must not add a
        # second PipelineResult with the same id
        ...  # seed with embedder stub so Stage 1 returns the gold fact, then assert
             # single occurrence + stats.n_keyed_dup == 1


class TestEntityCandidateVocabLeg:
    def test_vocab_recovers_lowercase_entity(self):
        from nous.heart.keys import extract_entity_candidates
        vocab = frozenset({"marriage of figaro", "thomas kyd"})
        got = extract_entity_candidates("who wrote the marriage of figaro?", vocab=vocab)
        assert "marriage of figaro" in got
```

Fixtures `seed_keyed_corpus`/`seed_keyed_corpus_many` (one active gold fact with keys, one `active=False` fact sharing a key; the "many" variant = 5 active facts keyed "belgium" with distinct `learned_at`). **CRITICAL (review arch-P1-C): seed via `async with heart.db.session() as s: ... await s.commit()` — NOT the conftest `session` fixture.** The keyed leg reads through its own pooled connection; rows seeded on the conftest session's uncommitted rollback-isolated connection are invisible to it and the tests would fail empty. Committed rows must be cleaned up in fixture teardown (DELETE by the test's fact ids — entity rows CASCADE). Seed `agent_id` MUST equal `heart.agent_id` (Settings default `nous-default`) or the `ek.agent_id = :a` filter drops everything. Also define a LOCAL `brain` fixture in this file (review arch-P1-A: there is NO conftest `brain` fixture — copy the per-file pattern from tests/test_spreading_activation.py:26, `async def brain(db, settings)`).

- [ ] **Step 2: Run tests to verify they fail**

Run: `$env:NOUS_TEST_DB='postgres'; uv run pytest tests/test_keyed_fact_leg.py -v`
Expected: FAIL (`keyed_fact_leg_enabled` unknown Settings field / no keyed results).

- [ ] **Step 3: Implement**

3a. `nous/heart/facts.py` — append two methods to `FactManager` (raw `text()` SQL, PG-only path — guarded by the flag which is off on SQLite runs):

```python
    async def entity_key_vocabulary(self, limit: int = 50_000) -> frozenset[str]:
        """R3.3: distinct entity keys of ACTIVE facts for this agent (NER-lite
        vocab matching). Active join per the fact_entity_keys read invariant."""
        async with self.db.session() as session:
            rows = await session.execute(
                text("SELECT DISTINCT ek.entity_key FROM heart.fact_entity_keys ek "
                     "JOIN heart.facts f ON f.id = ek.fact_id "
                     "WHERE ek.agent_id = :a AND f.active = true LIMIT :lim"),
                {"a": self.agent_id, "lim": limit},
            )
            return frozenset(r[0] for r in rows)

    async def fetch_by_entity_keys(self, keys: list[str], limit: int = 8):
        """R3.3: active facts matching any entity key, ranked by matched-key
        count then recency/ordinal. MUST join facts on active=true (entity
        rows survive supersession)."""
        if not keys:
            return []
        async with self.db.session() as session:
            rows = await session.execute(
                text(
                    "SELECT f.id, f.content, f.learned_at, f.source_ordinal, "
                    "       COUNT(DISTINCT ek.entity_key) AS matched "
                    "FROM heart.fact_entity_keys ek "
                    "JOIN heart.facts f ON f.id = ek.fact_id "
                    "WHERE ek.agent_id = :a AND ek.entity_key = ANY(:keys) "
                    "  AND f.active = true "
                    "GROUP BY f.id, f.content, f.learned_at, f.source_ordinal "
                    "ORDER BY matched DESC, f.learned_at DESC, "
                    "         f.source_ordinal DESC NULLS LAST "
                    "LIMIT :lim"
                ),
                {"a": self.agent_id, "keys": keys, "lim": limit},
            )
            return list(rows)
```

(`text()` + `= ANY(:keys)` with a plain Python list works through asyncpg — proven precedent at facts.py:2556; no ARRAY bindparam needed. Manager attributes are `self.db` / `self.agent_id` / `self._settings`.)

3b. `nous/api/retrieval_pipeline.py`:
- Module constants + TTL cache (mirror the density-gate pattern :53-61):

```python
_KEY_VOCAB_TTL_SECONDS = 300.0
# retrieval_pipeline.py does `import weakref` (no bare WeakKeyDictionary name)
# and does not import Any at runtime — use weakref.* and a string annotation.
_key_vocab_cache: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


async def _get_entity_key_vocab(heart) -> frozenset[str]:
    cached = _key_vocab_cache.get(heart)
    now = time.monotonic()
    if cached is not None and now - cached[1] < _KEY_VOCAB_TTL_SECONDS:
        return cached[0]
    vocab = await heart.facts.entity_key_vocabulary()
    _key_vocab_cache[heart] = (vocab, now)
    return vocab
```

- `_PipelineAccumulator` gains `keyed_results: list = field(default_factory=list)` and `keyed_leg_used: bool = False`, `n_keyed_dup: int = 0`.
- New stage in `_run_stages`, immediately after Stage 1.5 chunks (:460), same try/except + `n_stage_errors["keyed"]` discipline as other stages:

```python
        # Stage 1.6 (R3.3, F085): keyed fact leg — land-dark, flag + entity-presence
        # gated (NOT frame-gated: the eval harness has no frame concept).
        if settings.keyed_fact_leg_enabled:
            try:
                vocab = await _get_entity_key_vocab(heart)
                candidates = extract_entity_candidates(query, vocab=vocab)
                if candidates:
                    acc.keyed_leg_used = True
                    acc.keyed_results = await heart.facts.fetch_by_entity_keys(
                        candidates, limit=settings.keyed_fact_leg_k
                    )
            except Exception as exc:
                acc.stage_errors["keyed"] = acc.stage_errors.get("keyed", 0) + 1  # accumulator field is stage_errors (:191), NOT n_stage_errors (that's the stats field)
                logger.warning("keyed fact leg failed: %s", exc)
```

- Converter (near `_chunks_to_pipeline`):

```python
def _keyed_to_pipeline(rows, settings, existing_ids: set) -> tuple[list[PipelineResult], int]:
    """Additive-only: fresh PipelineResults, id-deduped against every other
    leg, scores in a bounded band UNDER the RRF head (base - 0.005*rank,
    clamped >= 0) so keyed hits can enter context without displacing
    higher-scoring direct/chunk hits (the -5.0pp lesson)."""
    out, dups = [], 0
    base = settings.keyed_fact_leg_score
    for rank, row in enumerate(rows):
        if row.id in existing_ids:
            dups += 1
            continue
        out.append(PipelineResult(
            id=row.id, type="fact",
            description=row.content, score=max(0.0, base - 0.005 * rank),
            source="heart",
            metadata={"retrieval_leg": "keyed", "matched_keys": int(row.matched)},
        ))
    return out, dups
```

- Assembly (in `run_recall_pipeline`, after all other legs are appended and BEFORE `_attach_contradictions`/rerank): collect `existing_ids = {r.id for r in results}`, call the converter, then **stable score-ordered insertion — NOT tail-append** (review devil-P1: `rerank_by_score` defaults False and `multi_turn_eval.py:266/:373` never passes True, so tail-appended keyed hits would sit at position 11+ and be sliced off by `[:top_k]` — the leg would measure as a no-op on the acceptance harness):

```python
    # Additive-only placement: each keyed hit is inserted before the first
    # existing result with a strictly lower score. Only keyed hits move;
    # every existing result keeps its relative order — attribution-clean for
    # the MAB flag-on/off A/B, and works identically on the rerank=False
    # (multi_turn_eval) and rerank=True (retrieval_runner) paths. When
    # rerank_by_score=True the later global sort yields the same final order.
    for kr in keyed:
        pos = next((i for i, r in enumerate(results) if r.score < kr.score), len(results))
        results.insert(pos, kr)
```

Wire `keyed_leg_used=acc.keyed_leg_used`, `n_keyed=len(keyed)`, `n_keyed_dup=dups` into the `PipelineStats(...)` construction at retrieval_pipeline.py:343 (the only all-kwargs site; fields added with defaults so bare `PipelineStats()` sites stay valid).
- Import `extract_entity_candidates` from `nous.heart.keys`, `WeakKeyDictionary` already imported for the density cache.

3c. `nous/api/tools.py` `_via_tag` (:301-306 — it branches on `metadata["stage_origin"]`, not edge_relation) — add a first branch, matching the existing `"[via x] "` trailing-space prefix format:

```python
    if r.metadata.get("retrieval_leg") == "keyed":
        return "[via keyed] "
```

3d. `nous/config.py`:

```python
    keyed_fact_leg_enabled: bool = Field(
        default=False,
        description="R3.3 (F085) master switch: exact entity-key retrieval leg in run_recall_pipeline. Land-dark.",
    )
    keyed_fact_leg_k: int = Field(
        default=8, ge=1,
        description="R3.3: bounded allotment - max keyed facts merged per query.",
    )
    keyed_fact_leg_score: float = Field(
        default=0.55, ge=0.0, le=1.0,
        description="R3.3: score band ceiling for keyed hits (RRF [0,1] scale, below the direct-hit head).",
    )
```

- [ ] **Step 4: Run tests**

Run: `$env:NOUS_TEST_DB='postgres'; uv run pytest tests/test_keyed_fact_leg.py tests/test_retrieval_pipeline.py -v` and `uv run pytest tests/test_entity_keys.py -v`
Expected: all PASS; existing pipeline tests untouched (flag off).

- [ ] **Step 5: Commit**

```bash
git add nous/heart/facts.py nous/api/retrieval_pipeline.py nous/api/tools.py nous/config.py tests/test_keyed_fact_leg.py
git commit -m "feat(retrieval): R3.3 keyed fact leg - land-dark, additive-only, bounded allotment with provenance"
```

---

### Task 6: Docs + full-suite gate

**Files:**
- Create: `docs/features/F085-keyed-fact-selection.md`
- Modify: `docs/features/INDEX.md` (one row), `CLAUDE.md` (shipped table row + 5 env-var rows: `NOUS_KEYED_FACT_LEG_ENABLED/K/SCORE`, `NOUS_ENTITY_KEYS_MAX_PER_FACT`, `NOUS_ENTITY_KEY_MIN_CHARS`)

- [ ] **Step 1: Write `docs/features/F085-keyed-fact-selection.md`** — sections: Why (the measured chain from the R3 requirements: existence fixed by F084, −5.0pp selection failure, keyed-sim 0.20-0.23 → index deficiency), R3.1 design (join table, stop-policy, same-call emission), R3.2 (canonicalizer v2 rules + fixpoint idempotency + why the backfill is load-bearing given exact-equality R2 consumers), R3.3 (gating = flag + entity-presence, additive-only merge rationale, score band, provenance), Backfill runbook (three phases, watermark/rollback/resume semantics, ordering: normalize → seed → extract), Acceptance (MAB-owned gates 1-4, verbatim from the requirements doc), Non-goals (multi-hop, LLM entity linking in read path, scalar keys, chunk-store changes, prod enablement).
- [ ] **Step 2: Add the INDEX.md and CLAUDE.md rows** (match surrounding format exactly).
- [ ] **Step 3: Full-suite gate**

Run: `uv run pytest tests/ -x -q` (SQLite) and `$env:NOUS_TEST_DB='postgres'; uv run pytest tests/ -q`
Expected: no NEW failures vs origin/main baseline (local dev DB has known pre-existing dirty-state failures; CI is the true gate).

- [ ] **Step 4: Commit**

```bash
git add docs/features/F085-keyed-fact-selection.md docs/features/INDEX.md CLAUDE.md
git commit -m "docs: F085 keyed fact selection feature doc + env vars"
```

---

## Amendments after 3-agent team review (BINDING — folded into the tasks above)

Verdicts: rev-arch APPROVE-WITH-FIXES, rev-db APPROVE-WITH-FIXES, rev-devil APPROVE-WITH-FIXES. All fixes below are already reflected in the task bodies; this section records provenance so implementers know these choices are deliberate:

1. **[devil-P1, placement]** Keyed hits use stable score-ordered INSERTION, never tail-append: `rerank_by_score` defaults False and multi_turn_eval never sets it, so tail-appended hits would be sliced off at `[:top_k]` — the leg would be a measured no-op on the MAB acceptance path (same failure mode that blinded the 2026-07-01 graph-qrel miner).
2. **[arch-P1-A/B/C, test wiring]** No conftest `brain` or `make_fact` fixtures exist — define locally. Any test that reads through `heart`'s own connections must seed via `heart.db.session()` + commit (+ teardown cleanup), with `agent_id == heart.agent_id`; the conftest `session` fixture is rollback-isolated on a different connection and its rows are invisible cross-connection.
3. **[devil-P2-1, db]** Stop-policy applies to subject keys in the ENTITY INDEX everywhere (write path, phase_seed, phase_extract). R2 reads `facts.subject_key` directly, so exempting subjects buys R2 nothing and creates the R3.1 junk buckets. `facts.subject_key` column itself is never filtered.
4. **[db-P2-2]** `_confirm_duplicate` entity inserts must be `pg_insert(...).on_conflict_do_nothing()` + seen-set: seeded-but-not-extracted facts have rows WITHOUT the watermark stamp; a plain `session.add` would PK-collide and abort a live learn.
5. **[db-P2-3]** Backfill phase functions never commit; only CLI `main()` commits (own session per batch). Tests drive phases on the rollback-isolated conftest session.
6. **[db-P2-4]** conftest does NOT apply migrations — CI `psql -f` loop does; SQLite path uses `create_all`; local dev DB needs manual 065 apply.
7. **[devil-P2-2]** phase_extract validates LLM indices (bounds + dup → WARN + skip); `--max-llm-calls` defaults 2000, 0=unlimited must be explicit.
8. **[devil-P2-3]** `and` removed from `_CAP_SPAN` connectors (two coordinated titles must yield two spans).
9. **[literals]** `self.db` (not `_db`); `weakref.WeakKeyDictionary()`; `acc.stage_errors` (not `n_stage_errors`); extractor swap keeps lines 26-28; `ANY(:keys)` plain-list precedent facts.py:2556; `_via_tag` returns `"[via keyed] "` and branches live beside the `stage_origin` checks; `UUID(as_uuid=True)` on `fact_id`.
10. **[devil-P3-3]** Flag-off test reworked to flag-ON-no-candidates == flag-OFF; pre-feature byte-identity rides the existing recall_deep snapshot.
11. **[devil-P3-5]** `_SCALAR_STOP` extended with article/question words ("the", "who", ...) — article-stripped titles ("The Who"→"who") must not become vocab junk grams.
12. **[db-P3-1]** `entity_key_vocabulary` joins facts on `active = true` (read invariant applies to vocab too).
13. **[db-P3-5]** phase_normalize entity-row rewrite only where `new_key != old_key` (unconditional INSERT+DELETE would delete already-canonical rows).
14. **[accepted v1 risks — document in F085 doc, no code]** giant-bucket sort cost bounded by stop-policy + K + corpus size; vocab TTL 300s staleness window; `_SCALAR_STOP` hardcoded (min-chars is the only env knob — partial meet of R3.1 "configurable stop-policy"); eval's per-config fresh Heart makes the vocab cache a no-op there (harmless).
15. **[db-obs, deploy]** Run the backfill promptly post-deploy: between v2-normalizer deploy and `phase_normalize`, R2 conflict detection silently misses v1↔v2 cross-format pairs (no data loss; conflicts surface once normalized — the sleep sweep picks them up).

## Self-Review Notes (author)

- R3.1 ✅ Task 2 (table) + Task 3 (emission, stop-policy, attribute_key untouched). Join table chosen (MAB preference + episode_chunks precedent).
- R3.2 ✅ Task 1 (canonicalizer, idempotency property) + Task 3 defensive re-normalize at learn + Task 4 in-place backfill (watermark, per-agent, dry-run, resume column = the two hardening items).
- R3.3 ✅ Task 5 (flag default false, NER-lite incl. vocab matching, exact-key fetch with active join, bounded K, provenance, one round only).
- Deviations from the requirements text (validated against code, to be echoed in the PR):
  1. Gating is flag + entity-presence, NOT "question turns" — the pipeline receives no frame/intent and the MAB harness has no frame; frame-gating would make their acceptance replay blind to the leg.
  2. "The extractor already parses both sides" is false in code — entities are a NET-NEW schema field, still zero extra LLM passes (same call).
  3. Bounded fetch alone cannot guarantee chunk non-displacement under the single global score sort; we additionally bound the score band (default 0.55 < RRF head) and merge additively. Gate 2 (their displacement check) remains the empirical arbiter; `NOUS_KEYED_FACT_LEG_SCORE`/`_K` are the tuning knobs.
