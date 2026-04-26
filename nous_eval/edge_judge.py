"""F052 — LLM-judge for edge precision in density-eval reports.

Loads a cached operator-editable prompt from
``nous_eval/templates/edge_precision_prompt.md``, batches edges in chunks
of ``BATCH_SIZE``, calls Sonnet via the existing OAT-supporting
``AnthropicClient``, parses the JSON-array response, and returns one
:class:`EdgeJudgment` per edge in input order.

The harness is intentionally minimal — no caching, no retries, no
streaming. Operators run it ad-hoc against a sampled edge set after a
density-eval pass to estimate precision (YES / WEAK / NO ratios).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nous.api.anthropic_client import create_client
from nous.config import Settings

logger = logging.getLogger(__name__)


BATCH_SIZE = 30  # ≤30 edges per Sonnet call to keep prompts well under 8k tokens
_TEMPLATE_PATH = Path(__file__).parent / "templates" / "edge_precision_prompt.md"


@dataclass(frozen=True)
class EdgeJudgment:
    """One judge verdict per edge."""

    source_id: str
    target_id: str
    relation: str
    verdict: str  # "YES" | "WEAK" | "NO" | "PARSE_ERROR"
    reasoning: str


def _load_prompt_template() -> str:
    """Read the operator-editable prompt template once per call.

    Re-read on every call (rather than module-load) so operators can edit
    the file between judge runs without restarting. The file is small
    (<2KB) so the syscall cost is negligible.
    """
    return _TEMPLATE_PATH.read_text(encoding="utf-8")


def _format_edge_payload(edges: list[dict[str, Any]]) -> str:
    """Serialize an edge batch as a JSON array for the prompt body."""
    return json.dumps(edges, ensure_ascii=False, indent=2)


def _parse_response(content: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract the JSON array from a Sonnet text response.

    Sonnet returns a list of content blocks; we concatenate ``text`` blocks
    and parse the result as JSON. Anything else (tool-use, image) is
    skipped. Raises ``ValueError`` if no JSON array is found.
    """
    text_parts: list[str] = []
    for block in content:
        # Both dict-style and attribute-style content blocks are tolerated
        # so this works against either AnthropicClient backend.
        btype = block.get("type") if isinstance(block, dict) else getattr(block, "type", None)
        if btype != "text":
            continue
        bt = block.get("text") if isinstance(block, dict) else getattr(block, "text", "")
        if bt:
            text_parts.append(bt)
    raw = "".join(text_parts).strip()
    if not raw:
        raise ValueError("edge_judge: empty Sonnet response")
    # Trim possible Markdown code fences
    if raw.startswith("```"):
        raw = raw.strip("`")
        # Strip an optional leading "json\n" tag
        if raw.lower().startswith("json"):
            raw = raw[4:]
    parsed = json.loads(raw)
    if not isinstance(parsed, list):
        raise ValueError(
            f"edge_judge: expected JSON array, got {type(parsed).__name__}"
        )
    return parsed


async def judge_edges(
    edges: list[dict[str, Any]],
    settings: Settings,
    model: str = "claude-sonnet-4-6",
) -> list[EdgeJudgment]:
    """Judge a list of edges. Each edge dict is shaped::

        {
            "source_id": str,
            "target_id": str,
            "source_content": str,
            "target_content": str,
            "relation": str,
            "weight": float,    # optional
        }

    Returns one :class:`EdgeJudgment` per input edge in the same order.
    Edges in batches that fail to parse are returned with
    ``verdict="PARSE_ERROR"`` so the operator can spot-check rather than
    silently lose precision data.
    """
    if not edges:
        return []

    template = _load_prompt_template()
    client = create_client(settings)
    await client.start()

    judgments: list[EdgeJudgment] = []
    try:
        for batch_start in range(0, len(edges), BATCH_SIZE):
            batch = edges[batch_start : batch_start + BATCH_SIZE]
            prompt_body = template + "\n\n" + _format_edge_payload(batch)

            try:
                resp = await client.call(
                    model=model,
                    system=[{"type": "text", "text": "You are a careful precision judge. Return JSON only."}],
                    messages=[{"role": "user", "content": prompt_body}],
                    max_tokens=2048,
                    temperature=0.0,
                )
                parsed = _parse_response(resp.content)
            except Exception as exc:
                logger.warning(
                    "edge_judge: batch %d-%d failed (%s); marking PARSE_ERROR",
                    batch_start, batch_start + len(batch), exc,
                )
                for e in batch:
                    judgments.append(
                        EdgeJudgment(
                            source_id=str(e.get("source_id", "")),
                            target_id=str(e.get("target_id", "")),
                            relation=str(e.get("relation", "")),
                            verdict="PARSE_ERROR",
                            reasoning=str(exc),
                        )
                    )
                continue

            for src_edge, verdict_obj in zip(batch, parsed):
                judgments.append(
                    EdgeJudgment(
                        source_id=str(src_edge.get("source_id", "")),
                        target_id=str(src_edge.get("target_id", "")),
                        relation=str(src_edge.get("relation", "")),
                        verdict=str(verdict_obj.get("verdict", "PARSE_ERROR")).upper(),
                        reasoning=str(verdict_obj.get("reasoning", "")),
                    )
                )
    finally:
        await client.close()

    return judgments
