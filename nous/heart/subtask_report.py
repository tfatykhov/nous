"""F061: SubtaskReport — payload submitted via the submit_final_report tool.

The report is the *only* legal way for a hardened subtask to terminate. Its
schema is mirrored at SUBMIT_FINAL_REPORT_SCHEMA in nous/api/subtask_tools.py
(the tool the model calls). Both must stay in sync.

Notes for future maintainers:

- ``extra="forbid"`` is intentionally a new pattern not used in
  ``nous/heart/schemas.py`` or ``nous/brain/schemas.py``. Validator-fronting
  schemas need it because the payload is adversarial — the model may invent
  fields like ``confidence_level`` instead of ``confidence``. Existing
  internal schemas remain bare ``BaseModel``.

- The summary length floor (default 50 chars) is enforced by the structural
  validator in ``nous/heart/subtask_validator.py``, NOT by this model. We
  keep ``min_length=1`` here so the validator owns the threshold and can be
  tuned via ``NOUS_SUBTASK_REPORT_MIN_SUMMARY_CHARS``.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SubtaskReport(BaseModel):
    """Validated payload an agent submits to terminate a subtask."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1)
    findings: list[str] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_refs: list[str] = Field(default_factory=list)
    incomplete: bool = False
    blocked_reason: str = ""
    # F062: optional schema-typed payload. Validation of `payload` against
    # the caller-supplied JSON Schema is done post-structural in
    # execute_hardened — this field is just the transport. Default None
    # keeps F061 callers unchanged (no payload, structural validation
    # still passes).
    payload: Any | None = None
