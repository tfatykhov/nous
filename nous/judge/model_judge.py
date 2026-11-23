"""F118: the built-in judge, a background model with fixed instructions.

The record is rendered as one JSON user message (``evidence.render_record``) and sent through
``call_background_llm_structured`` with the instructions of ``instructions.py`` as the system text, labelled
``outside_judge``, inside ``asyncio.wait_for``; the record is rendered in a thread. The payload sets no ``temperature``
and no ``thinking``. Input tokens are counted of every kind (uncached, cache reads, cache writes), and calls are the
requests sent (two when the model refuses a forced tool choice). The model is
``NOUS_OUTSIDE_JUDGE_MODEL``, or ``NOUS_BACKGROUND_MODEL`` when that is empty (a Claude model either way).

The answer is checked, never trusted: a verdict outside the set or not a string is an ``error``, unknown labels are
dropped, a confidence that is not a number from 0 to 1 is stored as none, and the reason is whitespace-collapsed,
redacted and cut to 200 characters. ``grade`` never raises; a cancellation passes through.
"""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Any

from nous.cognitive.ledger_store import redacted_head
from nous.config import Settings
from nous.handlers import call_background_llm_structured
from nous.judge import instructions
from nous.judge.evidence import render_record
from nous.judge.slot import JudgeInput, JudgeOutput, failed, register_provider

logger = logging.getLogger(__name__)

NAME = "background_model"
MAX_TOKENS = 400


_INPUT_KEYS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def _counts(usage: dict[str, int]) -> dict[str, int | None]:
    """The tokens and calls of one judgement: input tokens of every kind summed, output tokens, requests sent."""
    inputs = [usage[key] for key in _INPUT_KEYS if key in usage]
    return {
        "tokens_in": sum(inputs) if inputs else None,
        "tokens_out": usage.get("output_tokens"),
        "calls": usage.get("requests", 0),
    }


def _confidence(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) and 0.0 <= number <= 1.0 else None


def _labels(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    kept: list[str] = []
    for label in value:
        if isinstance(label, str) and label in instructions.DECISION_LABELS and label not in kept:
            kept.append(label)
    return tuple(kept[: instructions.MAX_LABELS])


def _reason(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    collapsed = " ".join(value.split())
    if not collapsed:
        return None
    return redacted_head(collapsed, instructions.REASON_MAX) or None


def parse_output(result: Any, *, model: str, usage: dict[str, int]) -> JudgeOutput:
    """The checked answer of one call (``result`` is the tool input the helper returned, or None)."""
    counts = _counts(usage)
    verdict = result.get("verdict") if isinstance(result, dict) else None
    if not isinstance(verdict, str) or verdict not in instructions.DECISION_VERDICTS:
        return failed("error", model, **counts)
    return JudgeOutput(
        status="ok",
        verdict=verdict,
        confidence=_confidence(result.get("confidence")),
        labels=_labels(result.get("labels")),
        reason=_reason(result.get("reason")),
        model=model,
        **counts,
    )


class BackgroundModelJudge:
    """The built-in judge: one model call per record."""

    name = NAME
    version = instructions.INSTRUCTION_VERSION

    def __init__(self, client: Any, model: str) -> None:
        self._client = client
        self.model = model

    async def grade(self, record: JudgeInput, *, timeout: float) -> JudgeOutput:
        try:
            # The second redaction of a full record is CPU work: off the event loop.
            message = await asyncio.to_thread(render_record, record)
        except Exception as exc:
            logger.warning("F118: the record of %s was not rendered (%s)", record.subject_id, type(exc).__name__)
            return failed("error", self.model, calls=0)
        usage: dict[str, int] = {}
        try:
            result = await asyncio.wait_for(
                call_background_llm_structured(
                    self._client,
                    self.model,
                    instructions.DECISION_INSTRUCTIONS,
                    message,
                    instructions.TOOL_NAME,
                    instructions.TOOL_DESCRIPTION,
                    instructions.OUTPUT_SCHEMA,
                    max_tokens=MAX_TOKENS,
                    purpose="outside_judge",
                    usage_out=usage,
                ),
                timeout=timeout,
            )
        except TimeoutError:
            logger.warning("F118: no verdict on %s within %s s", record.subject_id, timeout)
            return failed("timeout", self.model, **_counts(usage))
        except Exception as exc:
            logger.warning("F118: the judge call on %s failed (%s)", record.subject_id, type(exc).__name__)
            return failed("error", self.model, **_counts(usage))
        return parse_output(result, model=self.model, usage=usage)


def make_background_model_judge(settings: Settings, client: Any) -> BackgroundModelJudge:
    """The built-in judge over ``client``, with the judge's model or the background model."""
    return BackgroundModelJudge(client, settings.outside_judge_model or settings.background_model)


def register_background_model_judge(client: Any) -> None:
    """Register the built-in provider as a closure over ``client`` (``main.py``, with the judge on)."""
    register_provider(NAME, lambda settings: make_background_model_judge(settings, client))
