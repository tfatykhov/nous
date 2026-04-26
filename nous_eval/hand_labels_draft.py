"""F051 hand-labels draft — Sonnet-drafted qrels, human-reviewed before use.

Drives a single Sonnet call per batch of 10 qrels (default 30 total = 3 batches)
to **draft** queries + memory-type guesses + relevance reasoning. Output rows
have ``reviewed_by: null`` so the harness's source-registry rejects them as
gate-eligible until a human bumps that field to a real string.

Why batched: at 30 qrels @ ~150 tokens each, a single call would be cheap but
batching limits per-call latency and gives the LLM a small enough context to
think clearly about each row. 3 batches × 10 = 30 in <60s on Sonnet.

Output JSONL schema (one row per draft):

    {
        "query": "How does Nous handle multi-session contradiction?",
        "gold_ids": [],                 # filled by human reviewer
        "memory_types": ["fact", "decision"],
        "source": "hand_labels",
        "notes": {
            "draft_model": "claude-sonnet-4-6",
            "draft_temperature": 0.4,
            "draft_batch_index": 0,
            "rationale": "Cross-session contradiction detection lives in F022 ..."
        },
        "reviewed_by": null
    }

Phase 1 ships the drafting code without invoking Sonnet — wired in Phase 2
when the operator runs ``python -m nous_eval.hand_labels_draft --n 30``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_BATCH_SIZE = 10
DEFAULT_N = 30
DEFAULT_TEMPERATURE = 0.4
OUT_DEFAULT = Path("tests/fixtures/hand_labels_qrels.jsonl")

# Prompt template. The model is asked for a JSON array — we parse defensively
# so a single bad row in a batch doesn't drop the rest.
SYSTEM_PROMPT = """\
You are drafting evaluation queries for a retrieval system over a Nous AI agent's
long-term memory. Memory contains facts, decisions, episodes, procedures, and
censors stored in PostgreSQL with pgvector embeddings.

For each query you draft:
  - It must be a realistic question a user might ask in everyday English.
  - It must be answerable from a single retrieved memory item (no multi-hop reasoning).
  - State the most likely memory_types ordering (most likely first).
  - Provide a short rationale for what the gold answer would look like.

CRITICAL — instance-agnostic vocabulary only:
  - DO NOT use feature codes (F001, F022, F050, etc.), PR numbers (#123), commit
    SHAs, or any identifier that only makes sense inside one specific Nous
    deployment. Those are internal jargon, not what real users say.
  - DO NOT reference internal-only file paths, module names, or class names
    (e.g. "QueryExpander", "Heart._recall", "nous/heart/search.py").
  - DO use conceptual/functional terminology a Nous user or operator would
    actually say: "cross-encoder reranking", "diversity reranker", "session
    cleanup", "graph-augmented recall", "memory contradiction detection",
    "context pruning", "hybrid search", etc.

Return STRICT JSON: a list of {query, memory_types, rationale} objects, length N.
Do NOT include anything outside the JSON array.
"""

USER_PROMPT_TEMPLATE = """\
Draft {n} retrieval evaluation queries.

Topic seed: {seed}

Output schema:
[
  {{"query": "...", "memory_types": ["fact"|"decision"|"episode"|"procedure"|"censor", ...], "rationale": "..."}}
  ... (length {n})
]
"""


@dataclass
class HLDConfig:
    n: int = DEFAULT_N
    batch_size: int = DEFAULT_BATCH_SIZE
    model: str = DEFAULT_MODEL
    temperature: float = DEFAULT_TEMPERATURE
    out_path: Path = OUT_DEFAULT
    seed: str = "Nous shipped features F010-F050; cover memory recall, decision review, heartbeat, eval harness, identity, sleep cycles."
    dry_run: bool = False


# ---------------------------------------------------------------------------
# Sonnet call (lazy — Anthropic SDK is already a Nous dep)
# ---------------------------------------------------------------------------


async def _draft_batch(config: HLDConfig, batch_index: int) -> list[dict[str, Any]]:
    """Invoke Sonnet for a single batch of ``config.batch_size`` drafts.

    On dry_run, returns deterministic placeholder rows so the rest of the
    pipeline is testable without an API key.
    """
    n_this_batch = min(
        config.batch_size, config.n - batch_index * config.batch_size
    )
    if n_this_batch <= 0:
        return []

    if config.dry_run:
        return [
            {
                "query": f"[DRY RUN] draft #{batch_index * config.batch_size + i}",
                "memory_types": ["fact"],
                "rationale": "Placeholder — dry-run mode skips the API call.",
            }
            for i in range(n_this_batch)
        ]

    # Use Nous's existing AnthropicClient (HttpxAnthropicClient under the hood)
    # so OAT auth + beta headers + Bearer-token + Stainless headers all match
    # what prod uses. Vanilla `anthropic.AsyncAnthropic()` rejects OAT with
    # `OAuth authentication is currently not supported` — Nous's wrapper adds
    # the `anthropic-dangerous-direct-browser-access` + `oauth-2025-04-20`
    # beta headers that make OAT actually work. See
    # `nous/api/anthropic_client.py::HttpxAnthropicClient` lines 367-396.
    from nous.api.anthropic_client import create_client
    from nous.config import Settings

    settings = Settings()
    client = create_client(settings)
    await client.start()
    try:
        user_prompt = USER_PROMPT_TEMPLATE.format(n=n_this_batch, seed=config.seed)
        # OAT auth requires Block 0 to be the Claude Code preamble — without it
        # Anthropic returns 429 (not 401, despite being an auth-shape problem).
        # Mirrors nous/api/runner.py:488-495 prod usage.
        payload = {
            "model": config.model,
            "max_tokens": 4096,
            "temperature": config.temperature,
            "system": [
                {
                    "type": "text",
                    "text": "You are Claude Code, Anthropic's official CLI for Claude.",
                    "cache_control": {"type": "ephemeral"},
                },
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                },
            ],
            "messages": [{"role": "user", "content": user_prompt}],
        }
        # Retry on 429 (rate-limit) with exponential backoff. Prod uses the
        # same OAT actively (heartbeat, subtasks) and short bursts can collide.
        # 5 attempts: 0s, 4s, 12s, 28s, 60s gaps.
        last_exc = None
        for attempt in range(5):
            try:
                resp = await client.call(payload)
                break
            except RuntimeError as exc:
                if "429" not in str(exc) or attempt == 4:
                    raise
                wait = (2 ** (attempt + 1)) * 2
                logger.warning(
                    "hand_labels_draft: 429 from Anthropic on attempt %d/5; "
                    "retrying in %ds", attempt + 1, wait,
                )
                await asyncio.sleep(wait)
                last_exc = exc
        else:
            assert last_exc is not None
            raise last_exc
        # AnthropicClient response shape: resp.content is a list of dicts
        # with `type` and `text` fields (matches the QueryExpander pattern
        # in nous/heart/query_expansion.py:309-315).
        raw_chunks = []
        for block in resp.content or []:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    raw_chunks.append(block.get("text", ""))
            else:  # SDK-style typed object
                if getattr(block, "type", "") == "text":
                    raw_chunks.append(getattr(block, "text", ""))
        raw = "".join(raw_chunks)
        return _parse_batch_response(raw, batch_index)
    finally:
        try:
            await client.close()
        except Exception:
            logger.debug("hand_labels_draft: client.close raised", exc_info=True)


def _parse_batch_response(raw: str, batch_index: int) -> list[dict[str, Any]]:
    """Parse the model's JSON array; tolerate trailing text after the array.

    Returns an empty list (with a WARN log) on parse failure rather than
    raising — keeps the overall draft pipeline resilient to a single bad batch.
    """
    raw = raw.strip()
    # Find the first `[` and matching `]` — handles models that prepend prose.
    start = raw.find("[")
    end = raw.rfind("]")
    if start == -1 or end == -1 or end <= start:
        logger.warning("[eval.hand_labels] batch %d: no JSON array found", batch_index)
        return []
    try:
        parsed = json.loads(raw[start : end + 1])
    except json.JSONDecodeError as exc:
        logger.warning("[eval.hand_labels] batch %d: JSON parse failed: %s", batch_index, exc)
        return []
    if not isinstance(parsed, list):
        logger.warning("[eval.hand_labels] batch %d: top-level is not a list", batch_index)
        return []
    return parsed


# ---------------------------------------------------------------------------
# Format + write
# ---------------------------------------------------------------------------


def _to_qrel_row(draft: dict[str, Any], batch_index: int, model: str, temperature: float) -> dict[str, Any]:
    """Convert one Sonnet draft dict to the on-disk qrel JSON shape.

    Defensive: missing keys default to safe values; bad memory_types entries
    are dropped.
    """
    raw_types = draft.get("memory_types") or ["fact"]
    valid_types = [
        t for t in raw_types
        if isinstance(t, str) and t in {"fact", "decision", "episode", "procedure", "censor"}
    ]
    if not valid_types:
        valid_types = ["fact"]
    return {
        "query": str(draft.get("query", "")).strip(),
        "gold_ids": [],
        "memory_types": valid_types,
        "source": "hand_labels",
        "notes": {
            "draft_model": model,
            "draft_temperature": temperature,
            "draft_batch_index": batch_index,
            "rationale": str(draft.get("rationale", "")).strip(),
        },
        "reviewed_by": None,  # MUST be set by a human before harness uses these
    }


async def run(config: HLDConfig) -> int:
    """Drive batches end-to-end + write the JSONL output.

    Returns the number of rows written.
    """
    config.out_path.parent.mkdir(parents=True, exist_ok=True)
    n_batches = (config.n + config.batch_size - 1) // config.batch_size

    rows: list[dict[str, Any]] = []
    for batch_idx in range(n_batches):
        drafts = await _draft_batch(config, batch_idx)
        for d in drafts:
            row = _to_qrel_row(d, batch_idx, config.model, config.temperature)
            if row["query"]:
                rows.append(row)
        logger.info(
            "[eval.hand_labels] batch %d/%d -> %d rows", batch_idx + 1, n_batches, len(drafts)
        )

    with config.out_path.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    logger.info("[eval.hand_labels] wrote %d rows -> %s", len(rows), config.out_path)
    return len(rows)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None) -> HLDConfig:
    p = argparse.ArgumentParser(prog="python -m nous_eval.hand_labels_draft")
    p.add_argument("--n", type=int, default=DEFAULT_N)
    p.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE)
    p.add_argument("--out", dest="out_path", type=Path, default=OUT_DEFAULT)
    p.add_argument(
        "--seed",
        default="Nous shipped features F010-F050; cover memory recall, decision review, heartbeat, eval harness, identity, sleep cycles.",
        help="Topic seed text injected into the user prompt to bias drafts.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip the Anthropic call; emit deterministic placeholder rows for testing.",
    )
    ns = p.parse_args(argv)
    return HLDConfig(
        n=ns.n,
        batch_size=ns.batch_size,
        model=ns.model,
        temperature=ns.temperature,
        out_path=ns.out_path,
        seed=ns.seed,
        dry_run=ns.dry_run,
    )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    config = _parse_args(argv)
    asyncio.run(run(config))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
