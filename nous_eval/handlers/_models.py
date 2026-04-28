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
