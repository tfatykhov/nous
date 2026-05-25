# F069 — Document-aware ingestion

**Status:** 📝 Draft (2026-05-25)
**Proposed by:** Tim
**Depends on:** F067 (Episode Chunks + Parent Episode) — ✅ shipped
**Related:** Tool result pruning (`NOUS_TOOL_SOFT_TRIM_*`), F050 multi-query expansion

---

## Problem

When Nous reads a document via `web_fetch` (arxiv paper, news article, doc page) or `read_file` (local PDF/markdown), the content currently flows through the same path as conversational dialogue:

1. Tool result lands in conversation messages
2. **Soft-trim kicks in at `NOUS_TOOL_SOFT_TRIM_CHARS=9000`** — preserves first 2K + last 2K, drops the middle with a `... [trimmed N chars] ...` marker
3. At session end, `EpisodeSummarizer` builds a transcript from the (already-trimmed) messages
4. F067 chunker runs with one fixed strategy: 600-char sliding window, 80-char overlap

Three concrete losses for document content:

1. **Heavy mid-content truncation.** A 50K-word arxiv paper effectively reduces to ~4K chars (2K head + 2K tail) per `web_fetch` call. The methods, results, and discussion sections — the parts most likely to contain answers to later questions — get dropped before chunking even starts.
2. **Chunk size mismatch.** 600 chars (~100 words) is appropriate for dialogue turns but slices paper paragraphs mid-thought. Most modern doc-RAG systems use 300-500 word chunks for academic / structured content.
3. **No structure awareness.** A paper has `# Abstract`, `## Methods`, `## Results` sections; markdown has headings; code has functions. The fixed-window chunker treats `"References"` and `"[42] Smith et al. 2020 ..."` as adjacent text.

### What this is not

- **Not** a replacement for F067 chunks on dialogue. Conversation chunking stays at 600 chars — it works well there.
- **Not** an attempt to store entire papers verbatim in Postgres. Selective verbatim with semantic boundaries is the goal.

---

## Reference: how gbrain handles it

[gbrain](https://github.com/garrytan/gbrain) (single-user knowledge brain by Garry Tan) ships a 3-tier chunker that dispatches by content type:

| Strategy | Algorithm | Size | When |
|---|---|---|---|
| Recursive | 5-level delimiter hierarchy (paragraphs → lines → sentences → clauses → whitespace) | 300 words, 50-word overlap | Timeline data, bulk import |
| Semantic | Per-sentence embed + Savitzky-Golay topic-boundary filter | Variable | "Compiled truth" / quality content |
| LLM-guided (Haiku) | Pre-split 128-word candidates, Haiku scans sliding windows for topic shifts | Variable | High-value content (`--chunker llm`) |

They also store a `chunk_source` field on each chunk so retrieval can weight differently (compiled_truth chunks rank above timeline chunks).

---

## Goals

1. **Preserve more content** when Nous reads documents — past the current 9K soft-trim cap.
2. **Chunk strategy per content type** — documents get larger, structure-aware chunks; dialogue keeps current fixed-window.
3. **Tag chunks with source type** so retrieval can apply differential weighting.
4. **Backward compatible** — existing `heart.episode_chunks` for dialogue stays unchanged; documents add new rows or new columns.

## Non-goals

- Re-ingesting the existing prod corpus (this is forward-looking).
- Semantic chunking (Savitzky-Golay) in v1 — too much complexity for the first ship; recursive chunker with structure awareness is enough.
- Multi-vector / ColBERT embeddings.

---

## Design

### 1. Add `source_kind` to `heart.episode_chunks`

```sql
ALTER TABLE heart.episode_chunks
  ADD COLUMN source_kind VARCHAR(32) NOT NULL DEFAULT 'dialogue';
```

Values:
- `dialogue` — chunked from conversation transcript (current behavior, F067)
- `document` — chunked from a structured document (paper, article, doc page)
- `code` — chunked from source code (future)

### 2. Document-detection in tool result handling

When a tool result lands in conversation messages, classify it:

```python
def classify_tool_result(tool_name: str, args: dict, result: str) -> str:
    """Return 'dialogue' (no change) or 'document' (special handling)."""
    if tool_name == "web_fetch":
        # Heuristic: arxiv, doi.org, journal hosts, github raw markdown
        url = args.get("url", "")
        if any(host in url for host in ("arxiv.org", "doi.org", "openreview.net",
                                         "raw.githubusercontent.com", ".pdf")):
            return "document"
        # Length heuristic: >5000 chars of mostly-text content
        if len(result) > 5000 and _looks_like_document(result):
            return "document"
    if tool_name == "read_file":
        ext = args.get("path", "").split(".")[-1].lower()
        if ext in ("md", "pdf", "txt", "tex", "rst"):
            return "document"
    return "dialogue"
```

### 3. Skip soft-trim for `document` results, chunk separately

When `classify_tool_result == "document"`:

1. Do NOT apply the `NOUS_TOOL_SOFT_TRIM_*` truncation to this tool result.
2. After the session ends and `EpisodeSummarizer` runs, AS WELL AS chunking the dialogue transcript (`source_kind='dialogue'`), separately chunk the document content with the document chunker (`source_kind='document'`).
3. Document chunks reference the same `episode_id` so they're co-discoverable but get their own treatment at retrieval time.

### 4. Document chunker

Larger, structure-aware:

| Parameter | Document | Dialogue (existing) |
|---|---|---|
| Chunk size | 1500 chars (~250 words) | 600 chars |
| Overlap | 200 chars | 80 chars |
| Boundary preference | paragraph → sentence → word | word-only |
| Min chunk size | 100 chars | 50 chars |

Boundary algorithm (recursive split, gbrain-style):

```python
def chunk_document(text: str, target_size: int = 1500, overlap: int = 200) -> list[str]:
    """Recursive split: try paragraph breaks first, fall back to sentence,
    fall back to word boundaries. Preserves natural structure."""
    if len(text) <= target_size:
        return [text]
    # Try paragraph split (\n\n)
    chunks = _recursive_split(
        text,
        delimiters=["\n\n", "\n", ". ", " "],
        target_size=target_size,
        overlap=overlap,
    )
    return chunks
```

### 5. Retrieval changes

When `recall_deep` returns chunks, the formatter and ranker can use `source_kind`:

- **Display**: distinguish `[doc: paper-title-or-url]` from `[chunk]` in snippet output
- **Ranking**: option to boost document chunks for "factual" question types (could be a per-frame setting)
- **Dedup**: gbrain's 4-layer dedup applied — per-source cap so one paper can't flood top-K

This work mostly lands in `_format_pipeline_text` and the existing chunk-recall stage. No new retrieval pipeline path needed.

### 6. Settings

| Flag | Default | Purpose |
|---|---|---|
| `NOUS_DOCUMENT_INGEST_ENABLED` | `false` | Master switch for F069 ingestion path |
| `NOUS_DOCUMENT_CHUNK_SIZE` | `1500` | chars per document chunk |
| `NOUS_DOCUMENT_CHUNK_OVERLAP` | `200` | chars overlap between document chunks |
| `NOUS_DOCUMENT_AUTO_DETECT` | `true` | Auto-classify tool results vs require explicit tag |
| `NOUS_DOCUMENT_SKIP_SOFT_TRIM` | `true` | When `true`, skip `NOUS_TOOL_SOFT_TRIM_*` for document tool results |

---

## Rollout

### Phase 1 — Schema + opt-in chunking
- Migration to add `source_kind` column (default `'dialogue'`, all existing rows backfilled)
- `EpisodeSummarizer.summarize_episode` detects document content in conversation messages and chunks separately
- New document chunker module (`nous/heart/document_chunker.py`)
- Tests + small spike on a single arxiv paper to validate

### Phase 2 — Tool-result classification
- `classify_tool_result` helper in tool dispatch path
- Soft-trim bypass for `document`-classified results
- New env flags wired
- Eval on a synthetic doc-QA test (NOT LME — that's dialogue-shaped)

### Phase 3 — Retrieval differentiation
- Formatter shows `source_kind` per chunk
- Per-source dedup at retrieval (gbrain's 4-layer pattern)
- Optional rank weighting per source kind

---

## Open questions

1. **Where to detect documents?** Tool-dispatch time vs. EpisodeSummarizer time. Tool-time is more precise (we know the tool name) but adds complexity to the tool path. Summarizer-time is simpler but harder to undo the soft-trim that already happened. Lean toward **tool-dispatch time** + an opt-out env flag.
2. **Storage volume.** A typical arxiv paper at 50K words → ~40-50 chunks at 1500 chars. With Tim's avg ~10 papers/month, that's 500 chunks/month → 6K/year. At 1536d embeddings × 4 bytes × 6K = 36MB/year. Trivial.
3. **What's a "document"?** The heuristic above (host whitelist + length + content-look) is arbitrary. Probably need to refine after seeing real misclassifications. v1 ships with conservative detection (only obvious docs) + explicit override via tag.
4. **Re-ingest existing prod content?** No — F069 is forward-looking only. Backfill is a separate question.

---

## Provenance

- gbrain V0 design doc (`docs/GBRAIN_V0.md` chunking section) — read 2026-05-25
- Tim's observation 2026-05-25: arxiv paper content gets pruned to 4K chars at tool result, then chunked as dialogue. Wanted a separate path.
- F067 ship (PR #441, #443) — established chunk pipeline infrastructure we'll extend.
