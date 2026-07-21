"""F086 write path: parse exemplar streams and store as embedded facts. Zero LLM."""

from __future__ import annotations

import logging
from uuid import UUID

from nous.heart.exemplars import parse_exemplars
from nous.heart.schemas import FactInput, FactRejected


async def ingest_exemplars(
    heart,
    settings,
    text: str,
    episode_id: UUID | None,
    agent_id: str,
    logger: logging.Logger,
) -> int:
    """Parse an exemplar stream and store each pair as an individually-embedded fact.

    Returns the count of ``heart.learn`` calls that did not come back as
    ``FactRejected`` (deduped against distinct new ids within this call). A
    dedup-confirm within the SAME batch is only counted once via
    ``seen_ids``; a confirm against a PRE-EXISTING fact from an earlier call
    still increments this count even though no new row was created -- the
    return value is telemetry only. Callers that need an authoritative
    count of newly stored facts must query the DB.
    """
    pairs = parse_exemplars(text)
    if not pairs:
        return 0
    cap = settings.exemplar_max_per_episode
    truncated = len(pairs) > cap
    if truncated:
        logger.warning(
            "F086 exemplar ingest: %d pairs exceed exemplar_max_per_episode=%d — "
            "coverage is TRUNCATED (truncated=true)",
            len(pairs),
            cap,
        )
        pairs = pairs[:cap]
    inputs = [
        FactInput(
            content=f"{p.text}\nlabel: {p.label}",
            subject=p.text[:200],
            subject_key=None,  # keeps D2/R2 same-slot machinery short-circuited
            attribute_key="label",
            category="exemplar",
            confidence=1.0,
            source="exemplar_extractor",
            source_episode_id=episode_id,
            source_text=f"{p.text}\nlabel: {p.label}",
            source_ordinal=p.ordinal,
            entity_keys=[],
            entity_extraction_complete=True,  # F085 backfill must skip these
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

    # Arch-review C1: heart.learn returns FactDetail | FactRejected, NEVER
    # None. A dedup-confirm also returns FactDetail, so count NEW rows by
    # unseen id (mirrors the R1 template's isinstance check,
    # enumerative_extractor.py:389-403).
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
    logger.info(
        "F086 exemplar ingest: parsed=%d stored=%d truncated=%s episode=%s",
        len(pairs),
        stored,
        truncated,
        episode_id,
    )
    return stored
