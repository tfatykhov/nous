"""F056: per-handler fixture row pydantic models.

Each handler's fixture has its own row schema. This module collects them
all in one place so future handlers can reuse the validation patterns
(e.g. `reviewed_by` provenance field) without re-implementing.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class AdmissionRow(BaseModel):
    """One row in `tests/fixtures/handlers/admission_labeled.jsonl`.

    Per F056 spec §A: 50 candidate facts, 25 positive (admit) + 25 negative
    (reject), with hand-labeled gold and a `reviewed_by` provenance field
    for filtering AI-only drafts out of gating runs.
    """

    row_id: str = Field(min_length=1, description="Stable ID for deterministic row ordering.")
    content: str = Field(min_length=1)
    subject: str | None = None
    category: str | None = None  # preference / technical / person / tool / concept / rule
    source_text: str | None = None  # transcript context for admission grounding
    label: str = Field(pattern="^(admit|reject)$")
    rationale: str | None = None  # why this fact should be admitted/rejected
    reviewed_by: str | None = None  # "tim", "tim+ai-draft", or None for AI-only


class DedupPair(BaseModel):
    """One row in `tests/fixtures/handlers/dedup_paraphrases.jsonl`.

    Per F056 spec §B: 30 paraphrase pairs, 20 should dedup (semantic
    paraphrase of anchor) + 10 should be distinct (similar wording but
    different meaning). Both content fields must be >= 30 chars per
    Heart.learn's F038-1.2 short-content reject (`facts.py:312`).
    """

    row_id: str = Field(min_length=1, description="Stable ID for deterministic ordering.")
    anchor: str = Field(min_length=30, description=">= 30 chars or Heart.learn rejects it")
    paraphrase: str = Field(min_length=30, description=">= 30 chars or Heart.learn rejects it")
    expected: str = Field(pattern="^(dedup|distinct)$")
    rationale: str | None = None
    reviewed_by: str | None = None


class SummaryRow(BaseModel):
    """One row in `tests/fixtures/handlers/summary_transcripts.jsonl`.

    Per F056 spec §D: 80 transcripts (raised from N=20 in v1 — Wilson 95%
    CI for baseline 0.85 at N=20 is ~30pp wide; the 5pp gate would either
    thrash with false positives or never fire). At N=80 paired comparison
    in regression.py tightens effective sensitivity to comfortably catch
    5pp drift.

    Each row: a transcript (>= 50 chars per `episode_summarizer.py:130`'s
    short-transcript skip) + 3-7 gold key-points the produced summary
    MUST surface + gold themes.

    Mixed provenance per spec §"Closed open questions" #2: AI-drafted
    gold key-points reviewed by Tim → reviewed_by="tim+ai-draft" (gate-
    eligible) or reviewed_by="tim" (hand-curated, gate-eligible). AI-only
    rows have reviewed_by=None and are skipped from the gating run unless
    `--include-unreviewed` is passed.
    """

    row_id: str = Field(min_length=1)
    transcript: str = Field(
        min_length=50,
        description=">= 50 chars or summarize_episode short-transcript skip "
                    "(episode_summarizer.py:130) returns None.",
    )
    gold_key_points: list[str] = Field(
        min_length=1,
        description="3-7 short factual claims the produced summary must surface.",
    )
    gold_summary_themes: list[str] = Field(
        default_factory=list,
        description="Higher-level themes (informational; not gated).",
    )
    question_type: str | None = Field(
        default=None,
        description="LongMemEval question_type for per-type breakdown "
                    "(knowledge-update, multi-session, single-session-{user,"
                    "assistant,preference}, temporal-reasoning).",
    )
    reviewed_by: str | None = None


class BackfillEntity(BaseModel):
    """One row in `tests/fixtures/handlers/backfill_corpus.jsonl`.

    Per F056 spec §C: 100 mixed entities total. PR #3 v1 simplifies to
    facts-only (entity_type="fact" for all rows) — extending to decisions/
    episodes/procedures adds 3 more seeding code paths and is deferred to
    F056.2 to keep v1 reviewable. The simplification is honest:
    GraphDensifier exercises ALL relation types regardless of seeded
    entity types because the spec's edge_precision metric is about
    LLM-judged semantic relatedness, not relation-type coverage.

    `is_orphan_intent` is informational only — in v1 ALL seeded facts
    start as orphans (no pre-seeded edges) so the densifier has work to
    do. Extending to non-orphan seeded entities (which would require
    pre-seeding inter-entity edges) is also F056.2.
    """

    row_id: str = Field(min_length=1)
    entity_type: str = Field(pattern="^(fact|decision|episode|procedure)$")
    content: str = Field(min_length=30, description=">= 30 chars per F038-1.2")
    is_orphan_intent: bool = True
    rationale: str | None = None
    reviewed_by: str | None = None
