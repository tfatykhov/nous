"""F086 write path: parse exemplar streams and store as embedded facts. Zero LLM."""

from __future__ import annotations

import logging
from uuid import UUID

from nous.heart.exemplars import ExemplarPair, parse_exemplars
from nous.heart.schemas import FactInput, FactRejected


async def _embed_and_store_pairs(
    heart,
    settings,
    pairs: list[ExemplarPair],
    episode_id: UUID | None,
    logger: logging.Logger,
    *,
    log_prefix: str,
) -> tuple[int, int]:
    """Cap + embed + learn an already-ordinaled list of exemplar pairs.

    Shared by ``ingest_exemplars`` below (single-transcript, live write
    path -- pairs come straight from one ``parse_exemplars`` call) and
    ``scripts/backfill_exemplar_facts.py``'s ``_store_episode_pairs``
    (multi-chunk backfill path -- pairs are assembled across an episode's
    chunks with continuing ordinals via ``build_episode_pairs``). The two
    callers differ only in HOW the pair list gets assembled, never in how
    it gets capped, embedded, or stored -- this is the one shared
    cap+embed+learn implementation.

    Returns ``(stored, skipped_no_embedding)``. ``stored`` is the count of
    ``heart.learn`` calls that did not come back as ``FactRejected`` (deduped
    against distinct new ids within this call). A dedup-confirm within the
    SAME batch is only counted once via ``seen_ids``; a confirm against a
    PRE-EXISTING fact from an earlier call still increments it even though no
    new row was created -- ``stored`` is telemetry only. ``skipped_no_embedding``
    counts pairs dropped because no embedding could be produced (codex r3 --
    an exemplar row must never persist with a NULL embedding). Callers that
    need an authoritative count of newly stored facts must query the DB.
    """
    cap = settings.exemplar_max_per_episode
    if len(pairs) > cap:
        logger.warning(
            "%s: %d pairs exceed exemplar_max_per_episode=%d for episode=%s — coverage is TRUNCATED (truncated=true)",
            log_prefix,
            len(pairs),
            cap,
            episode_id,
        )
        pairs = pairs[:cap]
    if not pairs:
        return 0, 0  # (stored, skipped_no_embedding) — callers unpack a tuple (codex r4)

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
            logger.warning(
                "%s: batch embed failed for episode=%s; falling back to per-fact",
                log_prefix,
                episode_id,
                exc_info=True,
            )
            vectors = [None] * len(inputs)

    # Arch-review C1: heart.learn returns FactDetail | FactRejected, NEVER
    # None. A dedup-confirm also returns FactDetail, so count NEW rows by
    # unseen id (mirrors the R1 template's isinstance check,
    # enumerative_extractor.py:389-403).
    stored = 0
    skipped_no_embedding = 0
    seen_ids: set = set()
    for fi, vec in zip(inputs, vectors):
        # Codex r3: an exemplar row must NEVER reach heart.learn without a
        # non-None precomputed embedding. A NULL-embedding row is invisible to
        # BOTH fetch_exemplars_by_vector (embedding IS NOT NULL) and cosine
        # dedup, so a backfill rerun silently duplicates it. On a batch miss,
        # retry once per-pair; if it is STILL None (or no embedder at all),
        # SKIP the pair loudly rather than persist an unembeddable exemplar.
        if vec is None and embedder is not None:
            try:
                vec = await embedder.embed(fi.content)
            except Exception:
                logger.warning(
                    "%s: per-pair embed retry failed for episode=%s ordinal=%s",
                    log_prefix,
                    episode_id,
                    fi.source_ordinal,
                    exc_info=True,
                )
                vec = None
        if vec is None:
            skipped_no_embedding += 1
            continue
        try:
            result = await heart.learn(fi, precomputed_embedding=vec)
            if not isinstance(result, FactRejected) and result.id not in seen_ids:
                seen_ids.add(result.id)
                stored += 1
        except Exception:
            logger.warning(
                "%s: learn failed for episode=%s ordinal=%s",
                log_prefix,
                episode_id,
                fi.source_ordinal,
                exc_info=True,
            )
    if skipped_no_embedding:
        logger.warning(
            "%s: SKIPPED %d exemplar pair(s) with no embedding for episode=%s "
            "(unembeddable — not stored; a NULL-embedding row would be invisible to retrieval + dedup)",
            log_prefix,
            skipped_no_embedding,
            episode_id,
        )
    return stored, skipped_no_embedding


async def ingest_exemplars(
    heart,
    settings,
    text: str,
    episode_id: UUID | None,
    agent_id: str,
    logger: logging.Logger,
) -> int:
    """Parse an exemplar stream and store each pair as an individually-embedded fact.

    See ``_embed_and_store_pairs`` for the storage-count/telemetry contract.
    """
    pairs = parse_exemplars(text)
    if not pairs:
        return 0
    truncated = len(pairs) > settings.exemplar_max_per_episode
    stored, skipped_no_embedding = await _embed_and_store_pairs(
        heart,
        settings,
        pairs,
        episode_id,
        logger,
        log_prefix="F086 exemplar ingest",
    )
    logger.info(
        "F086 exemplar ingest: parsed=%d stored=%d skipped_no_embedding=%d truncated=%s episode=%s",
        len(pairs),
        stored,
        skipped_no_embedding,
        truncated,
        episode_id,
    )
    return stored
