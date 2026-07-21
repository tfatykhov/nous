# ICL Exemplar Mode Implementation Plan (F086)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Store `utterance\nlabel: N` exemplar streams as individually-embedded fact rows (parse-only, zero-LLM) and add a land-dark exemplar retrieval leg that injects the K nearest labeled examples, targeting the MAB program's ICL loss (live 0.555 vs embedding-kNN sim 0.82).

**Architecture:** Write path = new pure parser module + modal routing seam in `FactExtractor.extract_and_store` checked BEFORE the R1 enumerative branch (validated: R1's `is_enumerable` provably does NOT fire on label-streams; exemplar detection needs its own predicate). Facts carry `source='exemplar_extractor'`, content = the full pair text (exactly what the 0.82 sim embedded), `subject_key=NULL` (keeps the default-ON D2 same-slot machinery and F085 entity emission fully short-circuited), `attribute_key='label'` as a marker. Read path = Stage 1.7 in `run_recall_pipeline` modeled line-for-line on the Stage 1.6 keyed leg: TTL-cached exists-probe, cheap classification-shaped query gate, source-filtered cosine fetch, similarity floor, score-banded **stable insertion** (never tail-append — MAB scores at `rerank_by_score=False`), dedicated `=== Nearest stored examples ===` text block, PipelineStats + recall_deep INFO telemetry. Backfill reads `heart.episode_chunks` (validated: `episodes.transcript` is 8000-char-capped at capture; chunks are where the data demonstrably lives).

**Tech Stack:** Python 3.12 / SQLAlchemy async / pgvector / pydantic-settings. Tests: pytest + real Postgres (`NOUS_TEST_DB=postgres`).

## Global Constraints (binding, from the MAB spec + validation)

- **Zero LLM anywhere in the exemplar leg** — write path is parse-only; read path is embed+fetch only. (R1 calls Haiku; exemplar must NOT reuse R1's extraction path.)
- **Both flags default False (land-dark):** `NOUS_EXEMPLAR_EXTRACTION_ENABLED` (write), `NOUS_EXEMPLAR_MODE_ENABLED` (read). Flags off ⇒ byte-identical behavior, pinned by the existing recall_deep snapshot test.
- **Fact shape (gate-1 sim parity):** `content = f"{utterance}\nlabel: {label}"` — the full pair text, exactly what the MAB embedding-kNN sim (maj@5 0.82, text-embedding-3-large@1536) embedded. Any deviation invalidates the pre-green gate 1.
- **`subject_key=None` on every exemplar fact** — D2 same-slot routing (`same_slot_conflict_routing_enabled` default TRUE, `facts.py:756-762` requires both keys) and F085 entity emission (`facts.py:935`) must both short-circuit. `attribute_key="label"` per spec (marker only). `entity_extraction_complete=True` (F085 backfill must never Haiku-sweep exemplars).
- **`source='exemplar_extractor'` must be added to** admission `bypass_sources` (`heart/admission.py:138`) AND the source-aware min-chars floor at BOTH enforcement sites (`facts.py:520-522` and `:643-645`).
- **Different-label near-duplicates must never dedup-drop** (label-aware guard on Leg-2 dedup).
- **Read leg merges via score-banded stable insertion** (the `results.insert(pos, …)` loop at `retrieval_pipeline.py:371-376`), never tail-append; K default 25 (`NOUS_EXEMPLAR_TOP_K`); similarity floor `NOUS_EXEMPLAR_MIN_SIMILARITY` (0.30) bounds false-trigger displacement (gate 2).
- **Injection informs, never forces:** the text block must state the model may override the labels (trec_coarse parametric is already 0.90).
- **Trigger heuristic must NOT exclude questions generally** — trec exemplar queries ARE questions. Only memory-referential interrogatives are blocked.
- **Caps + loud truncation:** `NOUS_EXEMPLAR_MAX_PER_EPISODE` (5000), truncation logs WARNING, never silent (R1.3 convention).
- **Telemetry surfaced in the recall_deep INFO line** (`tools.py:988-1007`), not internal-only.
- **Embedding model/dims parity (spec-review I2):** query↔exemplar cosine is meaningful only when the deployment's embedding config matches the sim (`text-embedding-3-large` @ 1536). This is a standing operational constraint — state it in the feature doc AND the PR body; the code stays config-driven.
- **Gate-1 re-sim precondition (spec-review C1):** the spec's clause "if the shipped index/normalization differs from the sim … re-run the sim against the implementation's own index before building the read path" is an OPEN EXTERNAL PRECONDITION for MAB (same pattern as R3v2's sim-parity contract). The feature doc + PR body must quote the clause verbatim and list what could differ: (a) parser normalization (prefix-strip/assistant-skip — provably inert on clean `utterance\nlabel: N` chunks, pinned by test), (b) embedding model/dims, (c) the source-filtered index itself. MAB re-runs `probe_icl_exemplar_emb.py` against the shipped `source='exemplar_extractor'` rows and confirms maj@5 ≥ 0.75 before flipping the read flag.
- **Land-dark contract is BOTH-flags-off (arch-review M3):** with write ON / read OFF, exemplar facts surface via ordinary Stage-1 recall as plain facts (no read path filters by source). Byte-identity holds only with both flags off. Document: do not enable write-only in an A/B-compared corpus.
- **Do NOT filter question-text leakage (spec-review M4):** the 0.82 sim was measured WITH the 7–8/200 leakage present; filtering would break parity. One-line note in the feature doc so nobody adds a "cleanup" later.
- Acceptance gates 1–4 run EXTERNALLY in the MAB agent session; the feature doc must quote them VERBATIM from the requirements doc (`C:\Users\User\.claude\uploads\6d7efa56-587e-443a-bea4-851ef321738d\e20985c9-nousiclexemplarmoderequirements.md` §"Acceptance gates").
- Every new table/column: none needed (migration 066 is index-only). Postgres gate for tests: `NOUS_TEST_DB=postgres uv run pytest tests/test_exemplar_mode.py tests/test_retrieval_pipeline.py tests/test_fact_extraction.py -q` + full SQLite run clean.

---

### Task 1: Exemplar parser module (pure functions)

**Files:**
- Create: `nous/heart/exemplars.py`
- Test: `tests/test_exemplar_mode.py` (new file, class `TestExemplarParser`)

**Interfaces:**
- Produces: `ExemplarPair` (frozen dataclass: `text: str`, `label: str`, `ordinal: int`), `exemplar_density(text: str) -> float`, `is_exemplar_stream(text: str, threshold: float) -> bool`, `parse_exemplars(text: str) -> list[ExemplarPair]`, `parse_label(content: str) -> str | None`. All pure, no I/O, no settings import.

- [ ] **Step 1: Write failing tests** — in `tests/test_exemplar_mode.py`:

```python
"""F086 ICL exemplar mode tests."""
from nous.heart.exemplars import (
    ExemplarPair, exemplar_density, is_exemplar_stream, parse_exemplars, parse_label,
)

PURE_STREAM = "how do I reset my card pin\nlabel: 21\nmy card is lost\nlabel: 41\nwhat's the exchange rate\nlabel: 32\n"
TRANSCRIPT_STREAM = (
    "User: how do I reset my card pin\nlabel: 21\n"
    "Assistant: Noted.\n"
    "User: my card is lost\nlabel: 41\n"
    "Assistant: Stored.\n"
    "User: what's the exchange rate\nlabel: 32\n"
)

class TestExemplarParser:
    def test_density_pure_stream_is_high(self):
        assert exemplar_density(PURE_STREAM) >= 0.9

    def test_density_prose_is_zero(self):
        prose = "\n".join(f"This is ordinary sentence number {i}." for i in range(10))
        assert exemplar_density(prose) == 0.0

    def test_density_short_input_is_zero(self):
        assert exemplar_density("hello\nlabel: 1\n") == 0.0  # < 3 pairs

    def test_is_exemplar_stream_threshold(self):
        assert is_exemplar_stream(PURE_STREAM, threshold=0.8)
        assert not is_exemplar_stream("just chatting about the weather today", threshold=0.8)

    def test_parse_pure_stream(self):
        pairs = parse_exemplars(PURE_STREAM)
        assert [p.label for p in pairs] == ["21", "41", "32"]
        assert pairs[0].text == "how do I reset my card pin"
        assert [p.ordinal for p in pairs] == [0, 1, 2]

    def test_parse_transcript_skips_assistant_and_strips_user_prefix(self):
        pairs = parse_exemplars(TRANSCRIPT_STREAM)
        assert [p.label for p in pairs] == ["21", "41", "32"]
        assert pairs[1].text == "my card is lost"  # no "User: " prefix

    def test_parse_multiline_utterance(self):
        s = "line one\nline two of same utterance\nlabel: 7\nnext utt\nlabel: 8\n"
        pairs = parse_exemplars(s)
        assert pairs[0].text == "line one\nline two of same utterance"
        assert pairs[0].label == "7"

    def test_parse_skips_empty_utterance(self):
        s = "label: 5\nreal utterance\nlabel: 6\n"
        pairs = parse_exemplars(s)
        assert len(pairs) == 1 and pairs[0].label == "6"

    def test_parse_label_from_content(self):
        assert parse_label("some utterance\nlabel: 42") == "42"
        assert parse_label("no label here") is None
        assert parse_label("text\nlabel: atm_support") == "atm_support"
```

- [ ] **Step 2: Run to verify FAIL** — `uv run pytest tests/test_exemplar_mode.py -q` → ImportError.

- [ ] **Step 3: Implement** `nous/heart/exemplars.py`:

```python
"""F086: pure parsing/detection for ICL exemplar streams (`utterance\\nlabel: N`).

Zero-LLM by design. The R1 enumerative heuristic (`is_enumerable`) does NOT
fire on label-streams (label lines fail both its regexes), so exemplar
detection is a distinct predicate, checked BEFORE R1 in the extractor seam.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# A label line: `label: <value>` — value may be numeric or symbolic (trec: "LOC").
_LABEL_LINE = re.compile(r"^\s*label\s*:\s*(\S.*?)\s*$", re.IGNORECASE)
# Transcript speaker prefixes (layer.py capture format).
_USER_PREFIX = re.compile(r"^User:\s?", re.IGNORECASE)
_ASSISTANT_PREFIX = re.compile(r"^Assistant:", re.IGNORECASE)

_MIN_PAIRS = 3  # below this, never classify as an exemplar stream


@dataclass(frozen=True)
class ExemplarPair:
    text: str
    label: str
    ordinal: int


def _content_lines(text: str) -> list[str]:
    """Non-empty lines with Assistant lines removed and User: prefixes stripped."""
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or _ASSISTANT_PREFIX.match(line):
            continue
        out.append(_USER_PREFIX.sub("", line))
    return out


def exemplar_density(text: str) -> float:
    """Fraction-of-pair-structure score in [0, 1].

    A pure alternating stream has 50% label lines -> density 1.0 (the 2x
    factor). Assistant lines are excluded from the denominator so ack turns
    do not dilute a genuine stream.
    """
    lines = _content_lines(text)
    if len(lines) < 2 * _MIN_PAIRS:
        return 0.0
    n_labels = sum(1 for line in lines if _LABEL_LINE.match(line))
    if n_labels < _MIN_PAIRS:
        return 0.0
    return min(1.0, 2.0 * n_labels / len(lines))


def is_exemplar_stream(text: str, threshold: float) -> bool:
    return exemplar_density(text) >= threshold


def parse_exemplars(text: str) -> list[ExemplarPair]:
    """Walk lines, accumulating utterance lines until each label line."""
    pairs: list[ExemplarPair] = []
    acc: list[str] = []
    for line in _content_lines(text):
        m = _LABEL_LINE.match(line)
        if m:
            utterance = "\n".join(acc).strip()
            if utterance:
                pairs.append(ExemplarPair(text=utterance, label=m.group(1), ordinal=len(pairs)))
            acc = []
        else:
            acc.append(line)
    return pairs


def parse_label(content: str) -> str | None:
    """Extract the label from a stored exemplar fact's content (last label line)."""
    for line in reversed(content.splitlines()):
        m = _LABEL_LINE.match(line)
        if m:
            return m.group(1)
    return None
```

- [ ] **Step 4: Run to verify PASS** — `uv run pytest tests/test_exemplar_mode.py -q`.
- [ ] **Step 5: Commit** — `feat(heart): F086 exemplar stream parser (pure, zero-LLM)`.

---

### Task 2: Config flags + write-path wiring (modal routing, floors, admission, dedup guard)

**Files:**
- Modify: `nous/config.py` (new flag family, near the keyed family `:315-342`)
- Modify: `nous/handlers/fact_extractor.py` (routing seam `:244-279` — exemplar check BEFORE the R1 branch)
- Create: `nous/handlers/exemplar_ingest.py` (the parse→cap→embed→learn leg)
- Modify: `nous/heart/facts.py` (source-aware floor at `:520-522` and `:643-645`; label-aware dedup guard where `_find_duplicate`'s result is consumed in `_learn`)
- Modify: `nous/heart/admission.py` (`bypass_sources` at `:138`)
- Test: `tests/test_exemplar_mode.py` (class `TestExemplarIngest`), plus keep `tests/test_fact_extraction.py` green

**Interfaces:**
- Consumes: Task 1's `is_exemplar_stream`/`parse_exemplars`; `embed_batch` (`brain/embeddings.py:174`); `Heart.learn(fact_input, precomputed_embedding=vec)` (R1 pattern, `enumerative_extractor.py:368-403`).
- Produces: `ingest_exemplars(heart, settings, transcript, episode_id, agent_id, logger) -> int` (count stored); config fields listed below.

**Config fields (exact, in `nous/config.py`, `NOUS_` env prefix auto-derives):**

```python
# --- F086 ICL exemplar mode ---
exemplar_extraction_enabled: bool = Field(default=False, description="F086 write-path master switch: parse-only exemplar extraction of `utterance\\nlabel: N` streams into individually-embedded facts (source='exemplar_extractor'). Zero LLM.")
exemplar_density_threshold: float = Field(default=0.8, ge=0.0, le=1.0, description="F086 exemplar_density score at/above which a transcript routes to exemplar extraction (checked before R1).")
exemplar_max_per_episode: int = Field(default=5000, ge=1, description="F086 cap on exemplar facts stored per episode; truncation logs WARNING (never silent).")
exemplar_min_content_chars: int = Field(default=5, ge=0, description="F086 source-aware min-content floor for exemplar facts (labels/utterances are short; global 30-char floor would reject them).")
exemplar_mode_enabled: bool = Field(default=False, description="F086 read-path master switch: exemplar retrieval leg in run_recall_pipeline (land-dark).")
exemplar_top_k: int = Field(default=25, ge=1, description="F086 max exemplars fetched/injected per query.")
exemplar_leg_score: float = Field(default=0.55, ge=0.0, le=1.0, description="F086 score-band ceiling for exemplar hits (below the RRF direct-hit head; per-rank decay 0.005).")
exemplar_min_similarity: float = Field(default=0.30, ge=0.0, le=1.0, description="F086 cosine floor — exemplars below this similarity are not merged (bounds false-trigger displacement, gate 2).")
exemplar_max_query_words: int = Field(default=64, ge=1, description="F086 trigger gate: queries longer than this many words are not classification-shaped.")
```

- [ ] **Step 1: Failing tests** (`TestExemplarIngest`, postgres-gated like the existing heart fixtures in `tests/test_keyed_fact_leg.py` — reuse that file's `heart` fixture idiom: seed via `heart.db.session()` + commit + teardown):

```python
class TestExemplarIngest:
    async def test_ingest_stores_pair_facts(self, heart, settings):
        # settings.exemplar_extraction_enabled = True
        n = await ingest_exemplars(heart, settings, PURE_STREAM, episode_id, agent_id, logger)
        assert n == 3
        # fetch rows: source='exemplar_extractor', content == "utterance\nlabel: N",
        # subject_key IS NULL, attribute_key == 'label', source_ordinal == pair ordinal,
        # embedding IS NOT NULL

    async def test_min_chars_floor_source_aware(self, heart, settings):
        # utterance "yes" + label "1" -> content "yes\nlabel: 1" (12 chars < 30 global floor)
        # must STORE (exemplar floor 5), not reject

    async def test_admission_bypassed(self, heart, settings):
        # thousands-similar shape: two near-identical banking utterances with different labels
        # both stored (bypass) — no admission rejection event

    async def test_different_label_near_dupes_not_dropped(self, heart, settings):
        # same utterance text, different labels -> BOTH stored (label-aware dedup guard)
        # identical utterance AND identical label -> second one deduped
        # spec-review I1b: assert on DB ROW COUNTS (SELECT count(*) WHERE source='exemplar_extractor'),
        # never on ingest_exemplars' return value

    async def test_cap_truncates_loudly(self, heart, settings, caplog):
        # exemplar_max_per_episode=2, stream of 3 -> 2 stored + WARNING containing 'truncat'

    async def test_extractor_routes_modal_before_r1(self, ...):
        # extract_and_store with exemplar_extraction_enabled + exemplar-shaped transcript:
        # exemplar leg stores, R1/Haiku path NOT invoked (assert llm mock not called),
        # legacy candidate path NOT run (modal). Flag off -> legacy path unchanged.
```

- [ ] **Step 2: Run to verify FAIL.**
- [ ] **Step 3: Implement.** Key code:

`nous/handlers/exemplar_ingest.py`:

```python
"""F086 write path: parse exemplar streams and store as embedded facts. Zero LLM."""
from __future__ import annotations

import logging
from uuid import UUID

from nous.heart.exemplars import parse_exemplars
from nous.heart.schemas import FactInput, FactRejected  # implementer: verify FactRejected's home module


async def ingest_exemplars(heart, settings, text: str, episode_id: UUID | None,
                           agent_id: str, logger: logging.Logger) -> int:
    pairs = parse_exemplars(text)
    if not pairs:
        return 0
    cap = settings.exemplar_max_per_episode
    truncated = len(pairs) > cap
    if truncated:
        logger.warning(
            "F086 exemplar ingest: %d pairs exceed exemplar_max_per_episode=%d — "
            "coverage is TRUNCATED (truncated=true)", len(pairs), cap)
        pairs = pairs[:cap]
    inputs = [
        FactInput(
            content=f"{p.text}\nlabel: {p.label}",
            subject=p.text[:200],
            subject_key=None,                      # keeps D2/R2 + entity emission short-circuited
            attribute_key="label",
            category="exemplar",                    # verify against FactInput.category typing; use nearest allowed value if constrained
            confidence=1.0,
            source="exemplar_extractor",
            source_episode_id=episode_id,
            source_text=f"{p.text}\nlabel: {p.label}",
            source_ordinal=p.ordinal,
            entity_keys=[],
            entity_extraction_complete=True,        # F085 backfill must skip these
        )
        for p in pairs
    ]
    embedder = getattr(heart, "_embeddings", None)
    vectors: list[list[float] | None] = [None] * len(inputs)
    if embedder is not None:
        try:
            vectors = await embedder.embed_batch([i.content for i in inputs])
        except Exception:
            logger.warning("F086 batch embed failed; falling back to per-fact", exc_info=True)
            vectors = [None] * len(inputs)
    # Arch-review C1: heart.learn returns FactDetail | FactRejected, NEVER None.
    # A dedup-confirm also returns FactDetail, so count NEW rows by unseen id
    # (mirrors the R1 template's isinstance check, enumerative_extractor.py:389-403).
    stored = 0
    seen_ids: set = set()
    for fi, vec in zip(inputs, vectors):
        try:
            result = await heart.learn(fi, precomputed_embedding=vec)
            if not isinstance(result, FactRejected) and result.id not in seen_ids:
                seen_ids.add(result.id)
                stored += 1
        except Exception:
            logger.warning("F086 exemplar learn failed for ordinal=%s", fi.source_ordinal, exc_info=True)
    logger.info("F086 exemplar ingest: parsed=%d stored=%d truncated=%s episode=%s",
                len(pairs), stored, truncated, episode_id)
    return stored
```

(Implementer: verify `FactInput` field names against `nous/heart/schemas.py` — `category` is free-form `str | None`, `entity_extraction_complete` default False — and match the R1 leg's actual `heart.learn` call signature at `enumerative_extractor.py:389-403`. Note `seen_ids` catches intra-batch dedup-confirms (a confirm returns the EXISTING row's FactDetail); cross-run re-ingest may over-count `stored` — that's telemetry-only, and the tests assert on DB rows, not the count (spec-review I1b).)

`fact_extractor.py` routing seam — insert BEFORE the R1 enumerative block at `:244` (between the empty-summary guard and R1; the F084 lesson: the block must not sit after an early return):

```python
# F086: exemplar streams route modal, checked BEFORE R1 (R1's density
# heuristic does not fire on label-streams; this one does, and is parse-only).
if (
    getattr(self._settings, "exemplar_extraction_enabled", False)
    and transcript
    and is_exemplar_stream(transcript, getattr(self._settings, "exemplar_density_threshold", 0.8))
):
    try:
        n = await ingest_exemplars(self._heart, self._settings, transcript,
                                   _parse_episode_uuid(episode_id),  # arch-review I1: episode_id is str here
                                   self._agent_id, logger)
        if n > 0:
            return []  # arch-review I2: extract_and_store -> list[UUID]; bare return breaks consumers
        # zero stored -> fall through to legacy (F084 fall-through convention)
    except Exception:
        logger.warning("F086 exemplar leg failed; falling through to legacy", exc_info=True)
```

(Implementer: mirror the surrounding method's actual local names — `transcript`/`episode_id`/`self._agent_id` per `fact_extractor.py:244-279`; `_parse_episode_uuid` is the module's existing helper at `fact_extractor.py:17`, used identically by R1 at `:259`. Optionally return the stored UUIDs from `ingest_exemplars` instead of a count if consumers benefit — keep the `list[UUID]` contract either way.)

`facts.py` floor (both sites, exact pattern of the enumerative branch at `:521-522` / `:644-645`):

```python
elif input.source == "exemplar_extractor" and self._settings is not None:
    min_chars = self._settings.exemplar_min_content_chars
```

(spec-review M5: both enumerative sites carry the `self._settings is not None` guard — mirror it exactly.)

`admission.py:138`: add `"exemplar_extractor"` to `bypass_sources`.

`facts.py` label-aware dedup guard — **arch-review C2 (binding): clear the `found` TUPLE before the unpack**, not `dupe` after it. `dupe` is only bound inside `if found is not None:` (`facts.py:733-734`) and the fall-through else at `:781` calls `_classify_dupe_in_band(dupe, …)` which dereferences it — a post-unpack `dupe = None` CRASHES instead of bypassing:

```python
found = await self._find_duplicate(embedding, exclude_ids, session, ...)
# F086: different label = different exemplar; never dedup-drop (and never route/classify).
if (
    found is not None
    and input.source == "exemplar_extractor"
    and parse_label(found[0].content or "") != parse_label(input.content)
):
    found = None
if found is not None:
    dupe, dupe_similarity = found
    ...  # existing branches unchanged
```

(Implementer: `parse_label` from Task 1; import at module top or locally per file style. This bypasses the D2 branch, the F075 date branch, AND the `_classify_dupe_in_band` else — all consumers live inside the `if found is not None:` block.)

- [ ] **Step 4: Run to verify PASS** — `NOUS_TEST_DB=postgres uv run pytest tests/test_exemplar_mode.py tests/test_fact_extraction.py -q`.
- [ ] **Step 5: Commit** — `feat(write-path): F086 exemplar ingest leg - modal routing, floors, admission bypass, label-aware dedup guard`.

---

### Task 3: Migration 066 + source-filtered vector fetch + exists-probe cache

**Files:**
- Create: `sql/migrations/066_f086_exemplar_indexes.sql`
- Modify: `nous/heart/facts.py` (two new methods + cache fields + invalidation)
- Test: `tests/test_exemplar_mode.py` (class `TestExemplarFetch`)

**Interfaces:**
- Produces: `FactManager.has_exemplars() -> bool` (TTL-cached, 300s, generation-counter invalidation — exact shape of `entity_key_vocabulary` at `facts.py:3649-3690`); `FactManager.fetch_exemplars_by_vector(query_embedding: list[float], limit: int) -> list[ExemplarHit]` where `ExemplarHit` = NamedTuple `(id: UUID, content: str, similarity: float)`.

- [ ] **Step 1: Failing tests:**

```python
class TestExemplarFetch:
    async def test_fetch_orders_by_cosine_and_filters_source(self, heart):
        # seed 3 exemplar facts + 1 normal fact with an embedding near the query
        # fetch returns only source='exemplar_extractor' rows, ordered by similarity desc, respects limit

    async def test_fetch_excludes_inactive(self, heart):
        # deactivated exemplar not returned

    async def test_has_exemplars_probe_and_invalidation(self, heart):
        assert await heart.facts.has_exemplars() is False   # empty store, cached
        # learn one exemplar fact -> cache invalidated -> True without waiting for TTL
```

- [ ] **Step 2: Run to verify FAIL.**
- [ ] **Step 3: Implement.**

`sql/migrations/066_f086_exemplar_indexes.sql` (index-only; no `;` inside `--` comments per the migration convention):

```sql
-- F086 ICL exemplar mode - retrieval indexes for source-filtered cosine fetch
-- Partial HNSW keeps the exemplar walk off the global embedding index
CREATE INDEX IF NOT EXISTS idx_facts_exemplar_embedding
    ON heart.facts USING hnsw (embedding vector_cosine_ops)
    WHERE source = 'exemplar_extractor';

CREATE INDEX IF NOT EXISTS idx_facts_exemplar_agent
    ON heart.facts (agent_id)
    WHERE source = 'exemplar_extractor' AND active = true;
```

`facts.py` fetch — off the `_find_similar_for_dedup` template (`:2901-2932`), same operator/guards + `set_local_ef_search(session, 100)`:

```python
class ExemplarHit(NamedTuple):
    id: UUID
    content: str
    similarity: float

async def fetch_exemplars_by_vector(self, query_embedding: list[float], limit: int = 25) -> list[ExemplarHit]:
    """F086: K nearest active exemplar facts by cosine (source-filtered)."""
    vec_lit = "[" + ",".join(str(x) for x in query_embedding) + "]"
    async with self.db.session() as session:   # arch-review M1: attrs are self.db / self.agent_id
        await set_local_ef_search(session, 100)
        rows = (await session.execute(text("""
            SELECT id, content,
                   1 - (embedding <=> CAST(:v AS vector)) AS similarity
            FROM heart.facts
            WHERE agent_id = :agent_id
              AND active = true
              AND source = 'exemplar_extractor'
              AND embedding IS NOT NULL
            ORDER BY embedding <=> CAST(:v AS vector)
            LIMIT :limit
        """), {"v": vec_lit, "agent_id": self.agent_id, "limit": limit})).fetchall()
    return [ExemplarHit(id=r.id, content=r.content, similarity=float(r.similarity)) for r in rows]
```

`has_exemplars` — clone the vocab-cache shape (`facts.py:188, :3649-3690`): instance fields `_exemplar_exists_cache: tuple[bool, float] | None`, `_exemplar_exists_gen: int`; TTL constant 300.0; snapshot gen before the `await`, store only if unchanged; SQL `SELECT EXISTS(SELECT 1 FROM heart.facts WHERE agent_id = :a AND source = 'exemplar_extractor' AND active = true)`. **Invalidation (arch-review I3, binding): the entity-vocab invalidation site (`:582-583`) is gated `if input.entity_keys or input.subject_key:` — exemplars have both empty/None so it NEVER fires for them.** Add a sibling branch in the same post-commit `finally` (`facts.py:545-583`): `if input.source == "exemplar_extractor": self._invalidate_exemplar_cache()` (gen bump + cache None). The `finally` runs on the `session is None` path — correct, `ingest_exemplars` calls `heart.learn(...)` without a session.

- [ ] **Step 4: Run to verify PASS** (postgres gate — HNSW/pgvector paths need the real DB; SQLite tests for this class should skip, following the file's existing postgres-skip idiom).
- [ ] **Step 5: Commit** — `feat(heart): F086 migration 066 + source-filtered exemplar vector fetch + exists-probe cache`.

---

### Task 4: Read path — Stage 1.7 exemplar leg in run_recall_pipeline

**Files:**
- Modify: `nous/api/retrieval_pipeline.py` (accumulator fields, Stage 1.7 in `_run_stages` after Stage 1.6, converter, banded merge at assembly, PipelineStats fields)
- Test: `tests/test_exemplar_mode.py` (class `TestExemplarLeg`) + `tests/test_retrieval_pipeline.py` stays green

**Interfaces:**
- Consumes: Task 3's `has_exemplars` / `fetch_exemplars_by_vector`; embedder via the pipeline's existing embedding provider access (implementer: locate how Stage 1.5's chunk leg obtains the query embedding at `:1599-1673` and reuse the same provider — the process-level LRU (`NOUS_EMBEDDING_CACHE_SIZE`) makes a second embed of the same query free).
- Produces: `PipelineResult`s with `type="fact"`, `source="heart"`, `metadata={"retrieval_leg": "exemplar", "label": <str|None>, "similarity": <float>}`; `PipelineStats.exemplar_leg_used: bool`, `PipelineStats.n_exemplar: int`, `PipelineStats.n_exemplar_dup: int`; `stage_errors["exemplar"]` isolation.

- [ ] **Step 1: Failing tests:**

```python
class TestExemplarLeg:
    async def test_flag_off_byte_identical(self, ...):
        # exemplar facts seeded, exemplar_mode_enabled=False -> results identical to baseline run

    async def test_leg_merges_banded_not_tail(self, ...):
        # flag on, classification query, seeded exemplars near query:
        # exemplar rows appear at score-band positions (visible in top-10 with rerank_by_score=False),
        # metadata retrieval_leg == 'exemplar', label parsed, scores == 0.55 - 0.005*rank

    async def test_similarity_floor_bounds_false_trigger(self, ...):
        # spec-review I3a: query semantically far from exemplars -> 0 exemplar rows merged
        # AND the ORDERED list of non-exemplar result ids == the flag-off run's ordered list
        # (membership + order, not count — gate 2's real invariant)

    async def test_merged_exemplars_do_not_displace(self, ...):
        # spec-review I3b: classification-shaped query, exemplars DO merge (above floor):
        # the non-exemplar SUBSEQUENCE (ids in order, exemplar rows removed) is
        # membership+order identical to the flag-off run — additive-only, positions preserved

    async def test_trigger_gates(self, ...):
        # long query (> exemplar_max_query_words) -> leg skipped (exemplar_leg_used False)
        # memory-referential query ("what did I say about my card") -> skipped
        # plain question ("what is the capital of france") -> NOT skipped (trec-shape must trigger)
        # empty exemplar store -> skipped via exists-probe

    async def test_dedup_against_existing_results(self, ...):
        # an exemplar fact already surfaced by Stage 1 -> not duplicated; n_exemplar_dup counts it

    async def test_stage_error_isolated(self, ...):
        # fetch raises -> stage_errors['exemplar']==1, other legs' results intact
```

- [ ] **Step 2: Run to verify FAIL.**
- [ ] **Step 3: Implement.** Key pieces:

Trigger heuristic (module-level in `retrieval_pipeline.py`):

```python
# F086: memory-referential interrogatives are NOT classification-shaped.
# Deliberately narrow — trec-style classification queries ARE questions and must trigger.
_MEMORY_REFERENTIAL = re.compile(
    r"\b(did (i|we|you)|what did|what have (i|we)|remind me|last time|earlier"
    r"|previous(ly)?|we (discussed|talked)|you (said|told|mentioned))\b", re.IGNORECASE)

def _is_classification_shaped(query: str, max_words: int) -> bool:
    words = query.split()
    return 0 < len(words) <= max_words and not _MEMORY_REFERENTIAL.search(query)
```

Stage 1.7 (in `_run_stages`, directly after the Stage 1.6/round-2 block, own try/except → `stage_errors["exemplar"]`; accumulator gains `exemplar_hits: list = field(default_factory=list)` and `exemplar_leg_used: bool = False`):

```python
# Stage 1.7 (F086): exemplar leg - K nearest labeled examples by cosine.
if (
    getattr(settings, "exemplar_mode_enabled", False)
    and (search_all or "fact" in search_types)
    and _is_classification_shaped(query, getattr(settings, "exemplar_max_query_words", 64))
):
    try:
        if await heart.facts.has_exemplars():
            acc.exemplar_leg_used = True
            # arch-review M2: exact chunk-leg idiom (retrieval_pipeline.py:1627-1632);
            # the process LRU (NOUS_EMBEDDING_CACHE_SIZE) makes the repeat embed free.
            embedder = getattr(heart, "_embeddings", None)
            q_vec = (await embedder.embed(query)) if embedder is not None else None
            if q_vec:
                hits = await heart.facts.fetch_exemplars_by_vector(
                    q_vec, limit=getattr(settings, "exemplar_top_k", 25))
                floor = getattr(settings, "exemplar_min_similarity", 0.30)
                acc.exemplar_hits = [h for h in hits if h.similarity >= floor]
    except Exception:
        logger.warning("exemplar leg failed", exc_info=True)
        acc.stage_errors["exemplar"] = acc.stage_errors.get("exemplar", 0) + 1
```

Assembly merge — directly after the keyed/keyed_r2 merges (`:359-424` area), same stable-insertion idiom (converter `_exemplar_to_pipeline(hits, settings, existing_ids)` mirrors `_keyed_to_pipeline` `:1470-1518`: score `max(0.0, exemplar_leg_score - 0.005*rank)`, dedup vs `existing_ids` counting `n_exemplar_dup`, metadata carries `label=parse_label(content)` + `similarity`):

```python
if acc.exemplar_hits:
    existing_ids = {r.id for r in results}
    exemplar_rows, acc.n_exemplar_dup = _exemplar_to_pipeline(acc.exemplar_hits, settings, existing_ids)
    for er in exemplar_rows:
        pos = next((i for i, r in enumerate(results) if (r.score or 0.0) < er.score), len(results))
        results.insert(pos, er)
```

PipelineStats: add `exemplar_leg_used: bool = False`, `n_exemplar: int = 0`, `n_exemplar_dup: int = 0`; wire at the assembly site (`:491-511`).

- [ ] **Step 4: Run to verify PASS** — `NOUS_TEST_DB=postgres uv run pytest tests/test_exemplar_mode.py tests/test_retrieval_pipeline.py -q`.
- [ ] **Step 5: Commit** — `feat(retrieval): F086 Stage 1.7 exemplar leg - trigger gates, similarity floor, banded merge`.

---

### Task 5: recall_deep rendering block + telemetry

**Files:**
- Modify: `nous/api/tools.py` (`_format_pipeline_text` `:267-481`, `recall_deep` `:868-1071`)
- Test: `tests/test_exemplar_mode.py` (class `TestExemplarRendering`) + the existing recall_deep snapshot test stays byte-identical (flags off)

**Interfaces:**
- Consumes: Task 4's `PipelineResult`s (`metadata.retrieval_leg == "exemplar"`) and `PipelineStats` fields.
- Produces: `=== Nearest stored examples ===` section; recall_deep INFO line extended with `exemplar_leg_used=%s n_exemplar=%d`.

- [ ] **Step 1: Failing tests:**

```python
class TestExemplarRendering:
    def test_exemplars_render_in_dedicated_block_not_heart_memory(self, ...):
        # results containing 2 exemplar rows + 1 normal fact:
        # text has '=== Nearest stored examples ===', lists 'utterance -> label: N' with sim,
        # exemplar rows ABSENT from the Heart Memory section,
        # block contains the may-override instruction

    def test_no_exemplars_no_block(self, ...):
        # zero exemplar rows -> no section header (flag-off snapshot byte-identity)
```

- [ ] **Step 2: Run to verify FAIL.**
- [ ] **Step 3: Implement.** In `_format_pipeline_text`: exclude `r.metadata.get("retrieval_leg") == "exemplar"` from the `heart_results` filter (`:323-327`); collect them separately; after the Parent Episode block (`:473-479`), emit:

```python
if exemplar_rows:
    results_text.append("\n=== Nearest stored examples ===")
    results_text.append(
        "The stored examples most similar to the query, with their stored labels. "
        "Treat them as evidence for classification-style answers; you may override "
        "them if your own judgment clearly disagrees.")
    for i, r in enumerate(exemplar_rows, 1):
        sim = r.metadata.get("similarity")
        sim_s = f" [sim {sim:.2f}]" if isinstance(sim, (int, float)) else ""
        results_text.append(f"{i}.{sim_s} {r.description}")
```

(`r.description` already holds the pair text `utterance\nlabel: N` — implementer: confirm how PipelineResult.description is populated for fact rows and keep the pair text intact, truncating only at an extreme bound, e.g. 500 chars.) In `recall_deep`'s consolidated INFO line (`:988-1007`), append `exemplar_leg_used=%s n_exemplar=%d` from stats (the `n_keyed_r2` precedent at `:1005-1006`).

- [ ] **Step 4: Run to verify PASS** — plus the untouched snapshot: `NOUS_TEST_DB=postgres uv run pytest tests/test_exemplar_mode.py tests/test_retrieval_pipeline.py tests/test_recall_deep_snapshot.py -q` (implementer: locate the actual snapshot test file name via grep before running).
- [ ] **Step 5: Commit** — `feat(tools): F086 Nearest-stored-examples block + recall_deep telemetry`.

---

### Task 6: Backfill script + docs

**Files:**
- Create: `scripts/backfill_exemplar_facts.py`
- Create: `docs/features/F086-icl-exemplar-mode.md`
- Modify: `docs/features/INDEX.md` (one row), `CLAUDE.md` (env-var table rows for all 9 flags, F086 row in the shipped table)
- Test: `tests/test_exemplar_mode.py` (class `TestExemplarBackfill` — parse/plan logic only; DB e2e is covered by ingest tests)

**Backfill contract (follows `scripts/backfill_enumerative_facts.py` + `backfill_r3_entity_keys.py` idioms exactly):**
- `--agent-id` required; `--dry-run` reports episodes/pairs without writing; `--max-episodes`; `--since ISO`.
- **Reads `heart.episode_chunks`** grouped by `episode_id` ordered by `chunk_index` (NOT `episodes.transcript` — validated 8000-char capture cap). Each chunk parsed INDEPENDENTLY with `parse_exemplars` (chunk-boundary fragments and 80-char-overlap repeats are absorbed by Leg-2 dedup + the label-aware guard; MAB's own measurement calls fragments harmless).
- Episode qualifies when the concatenated chunk density passes `is_exemplar_stream` (same threshold flag).
- DB-clock watermark via `SELECT now()` printed as ROLLBACK KEY before any write; live-write guard (abort if exemplar facts newer than watermark exist unless `--include-live-writes`); rollback phase: `--phase rollback --watermark <iso>` → `UPDATE heart.facts SET active=false WHERE agent_id=:a AND source='exemplar_extractor' AND created_at >= :w` (soft, never hard-delete).
- Per-episode cap + loud truncation; `ordinal` numbering continues across chunks of the same episode; idempotent re-run via dedup.
- Smoke-test discipline: `--max-episodes 2` first (threshold-yield memory).

**Feature doc must contain:** summary, the two falsified spec assumptions (R1-heuristic mismatch, 8000-char capture cap) and their resolutions, fact-shape contract (sim-parity: full pair text embedded), all 9 flags, the trigger heuristic + its trec rationale, the similarity floor as the gate-2 mechanism, **the four acceptance gates quoted VERBATIM** from the requirements doc, **the gate-1 re-sim clause quoted verbatim with the three could-differ items** (parser normalization / embedding model+dims / the shipped index) as an open MAB precondition, **the embedding-parity operational constraint** (text-embedding-3-large@1536), **the gate-4 composite note** (spec-review M3: byte-identity = formatter snapshot + read-results identity test + write mock-not-called test), **the do-not-filter-leakage note** (spec-review M4), **the land-dark contract** (arch-review M3: both-flags-off is the byte-identical config; write-on/read-off surfaces exemplars as plain Stage-1 facts), **the pre-gate-3 blocklist scan item for MAB** (spec-review M2: scan the 5 sources' gold queries against `_MEMORY_REFERENTIAL`; banking77 shapes like "did i get charged…" are the risk — adjust patterns if any gold query matches), and the documented deviations list.

- [ ] **Step 1: Failing tests** (backfill's chunk-grouping + qualification + ordinal-continuation logic as pure functions).
- [ ] **Step 2: Run to verify FAIL.**
- [ ] **Step 3: Implement script + docs.**
- [ ] **Step 4: Run to verify PASS**; full file green: `NOUS_TEST_DB=postgres uv run pytest tests/test_exemplar_mode.py -q`.
- [ ] **Step 5: Commit** — `feat(scripts): F086 exemplar backfill (episode_chunks source, watermark/rollback) + docs`.

---

## Self-Review Notes (author)

- Spec coverage: R-ICL.1 → Tasks 1/2/3/6; R-ICL.2 → Tasks 3/4/5; gates quoted in Task 6 doc; non-goals respected (no multi-round, no LLM trigger).
- Deviations from spec text (document in Task 6): (1) detection is a NEW predicate, not "the R1 heuristic" (validated mismatch); (2) backfill reads episode_chunks, not transcripts (validated capture cap); (3) `subject_key=NULL` while spec's option-B sketch implied keyed rows (D2 collision safety — spec silent on subject_key); (4) **`value=label` is encoded in content text** (spec-review I1a: `heart.facts` has no value column and `FactInput` no value field — the label lives in the pair text + `attribute_key='label'` marker + `parse_label` recovery; gate-1 parity forces this encoding anyway); (5) similarity floor added (spec gate 2 needs a mechanism); (6) write-side flag added (spec names only the read flag; land-dark needs both). NOT a deviation (spec-review M1): the memory-referential-only blocklist is a FAITHFUL operationalization of the spec's own qualifier "no interrogative **about stored content**" — document as such, with the trec_coarse .90-live/.82-sim support.
- Type consistency: `ExemplarPair`/`ExemplarHit`/`parse_label` names used identically across tasks; `ingest_exemplars` signature consistent between Tasks 2 and 6.
