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

- ``confidence`` is deliberately NOT required. It is soft, self-reported
  metadata with no load-bearing consumer (the only reader is the retry
  feedback prompt in ``nous/handlers/subtask_executor.py``). Making it
  fail-closed meant an omitted number discarded an otherwise complete run —
  the single largest source of ``validation_failed`` in production. It now
  defaults to ``DEFAULT_CONFIDENCE`` and sets ``confidence_reported=False``
  so the omission stays visible instead of masquerading as a real estimate.
  ``summary`` remains required: it IS the deliverable.

- The summary length floor (default 50 chars) is enforced by the structural
  validator in ``nous/heart/subtask_validator.py``, NOT by this model. We
  keep ``min_length=1`` here so the validator owns the threshold and can be
  tuned via ``NOUS_SUBTASK_REPORT_MIN_SUMMARY_CHARS``.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


#: Neutral fallback used when a report omits ``confidence``. 0.5 is
#: deliberately mid-scale: it asserts neither trust nor doubt, and pairs with
#: ``confidence_reported=False`` so downstream calibration can exclude it.
DEFAULT_CONFIDENCE = 0.5


class SubtaskReport(BaseModel):
    """Validated payload an agent submits to terminate a subtask."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1)
    findings: list[str] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)
    confidence: float = Field(default=DEFAULT_CONFIDENCE, ge=0.0, le=1.0)
    #: Derived, NOT model-supplied. True iff the payload actually carried a
    #: ``confidence`` key. Set by ``_derive_confidence_reported`` and always
    #: overwritten, so a caller cannot forge it. Lets calibration separate a
    #: real self-report from the neutral default above.
    confidence_reported: bool = False
    evidence_refs: list[str] = Field(default_factory=list)
    incomplete: bool = False
    blocked_reason: str = ""
    # F062: optional schema-typed payload. Validation of `payload` against
    # the caller-supplied JSON Schema is done post-structural in
    # execute_hardened — this field is just the transport. Default None
    # keeps F061 callers unchanged (no payload, structural validation
    # still passes).
    #
    # Note on fail-closed (Codex round-11 L45): we accept `payload` here
    # unconditionally, but the *fail-closed gate* lives one layer up at the
    # submit_final_report tool schema — when F062 is off,
    # build_submit_final_report_schema(False) leaves `payload` out of
    # input_schema.properties and additionalProperties: False rejects any
    # stray payload key at tool-dispatch time. SubtaskReport.model_validate
    # therefore never sees `payload` unless the tool layer allowed it
    # through. The Pydantic field is defense-in-depth for direct Python
    # callers, not the primary enforcement boundary.
    payload: Any | None = None

    @model_validator(mode="before")
    @classmethod
    def _derive_confidence_reported(cls, data: Any) -> Any:
        """Stamp ``confidence_reported`` from the *presence* of ``confidence``.

        Runs before field validation so it can see the raw payload. We always
        overwrite the flag rather than defaulting it, which makes the field
        derived-and-unspoofable: a direct Python caller cannot hand-set
        ``confidence_reported=True`` while omitting ``confidence``.

        Non-dict input (e.g. revalidating a model instance) is passed through
        untouched — there is no raw payload to inspect in that case.
        """
        if isinstance(data, dict):
            data = dict(data)
            data["confidence_reported"] = data.get("confidence") is not None
        return data
