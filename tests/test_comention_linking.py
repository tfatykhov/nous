"""F076 — co-mention / shared-entity linking tests.

Entity-extraction unit tests (pure, no DB) covering the review's edge cases:
possessive, apostrophe connector (de'), Unicode initials, sentence boundaries,
sentence-initial stopwords, single-token exclusion, hub determinism.

The densifier DB integration (edges persist with extraction_method='co_mention',
caps, idempotency, reverse-dup guard) is exercised by the whole-system A/B on the
eval DB; these tests lock the deterministic surface that feeds it.
"""
from __future__ import annotations

from nous.brain.entity_extraction import extract_entities


def test_possessive_normalizes_to_bare_entity():
    # "Steve Hillage's" must match "Steve Hillage" (the orphan-bridge bug).
    a = extract_entities("Green is Steve Hillage's fourth studio album.")
    b = extract_entities("Miquette Giraudy worked with Steve Hillage on System 7.")
    assert "steve hillage" in a
    assert "steve hillage" in b
    assert a & b == {"steve hillage"}  # the shared mention that links them


def test_apostrophe_connector_de():
    # "Marie de' Medici" — the de' apostrophe must not break the phrase.
    ents = extract_entities("Marie de' Medici was the mother of Louis XIII.")
    assert "marie de medici" in ents
    assert "louis xiii" in ents


def test_unicode_initial():
    assert "etienne lenoir" in extract_entities("Étienne Lenoir built an engine.") or \
           "étienne lenoir" in extract_entities("Étienne Lenoir built an engine.")
    assert "luc besson" in extract_entities("Luc Besson directed the film.")


def test_sentence_boundary_not_crossed():
    # "...Paris. Later Steve..." must NOT yield a cross-sentence phrase.
    ents = extract_entities("He visited Paris. Later Steve Hillage arrived.")
    assert "paris later" not in ents
    assert not any("paris" in e and "steve" in e for e in ents)


def test_sentence_initial_stopword_dropped():
    # Leading discourse adverb must not splinter the entity (recall guard).
    ents = extract_entities("Later Steve Hillage formed System 7.")
    assert "steve hillage" in ents
    assert "later steve hillage" not in ents


def test_single_token_excluded():
    # Single surnames/words are intentionally not emitted (precision over recall).
    assert extract_entities("Mozart was a composer.") == set()
    assert extract_entities("Paris is a city.") == set()


def test_min_chars_floor():
    # Short multi-token caps below the floor are dropped.
    assert extract_entities("Al Bo met.", min_chars=6) == set()


def test_deterministic():
    text = "Grant Green recorded with Lou Donaldson and Ben Dixon for Blue Note."
    assert extract_entities(text) == extract_entities(text)


def test_empty_and_none():
    assert extract_entities("") == set()
    assert extract_entities(None) == set()  # type: ignore[arg-type]


def test_comma_separates_entities():
    # A comma+space is a segment boundary — must NOT fuse two names into a
    # phantom entity (which would create false co-mention edges).
    ents = extract_entities("Recorded with Lou Donaldson, Ben Dixon for Blue Note.")
    assert "lou donaldson" in ents
    assert "ben dixon" in ents
    assert not any("donaldson" in e and "ben" in e for e in ents)


def test_trailing_connector_trimmed():
    assert extract_entities("They met Steve Hillage of.") == {"steve hillage"}


def test_seed_score_scoring_composition():
    """fix-B _heart_graph_memory_to_pipeline scoring (pure): co_mention escapes the
    inferred penalty; a None seed_score falls back to legacy (NOT 0.0)."""
    from datetime import UTC, datetime
    from uuid import uuid4

    from nous.api.retrieval_pipeline import _heart_graph_memory_to_pipeline
    from nous.brain.schemas import NeighborResult
    from nous.config import Settings

    base = Settings().model_copy(update={
        "graph_neighbor_seed_score_enabled": True,
        "graph_recall_decay": 0.7,
        "graph_inferred_edge_penalty": 0.5,  # make the penalty visible
    })

    def nbr(method, seed):
        return NeighborResult(
            id=uuid4(), node_type="fact", description="x", edge_relation="related_to",
            edge_weight=0.85, created_at=datetime.now(UTC),
            extraction_method=method, seed_score=seed,
        )

    def score(n, s):
        return _heart_graph_memory_to_pipeline([n], s)[0].score

    # co_mention: seed_score * edge_weight, NO penalty
    assert abs(score(nbr("co_mention", 0.9), base) - 0.9 * 0.85) < 1e-6
    # inferred: penalty applies
    assert abs(score(nbr("inferred", 0.9), base) - 0.9 * 0.85 * 0.5) < 1e-6
    # None seed_score -> legacy fallback (edge_weight * decay), NOT 0.0
    assert abs(score(nbr("co_mention", None), base) - 0.85 * 0.7) < 1e-6
    # flag OFF -> legacy fallback
    off = base.model_copy(update={"graph_neighbor_seed_score_enabled": False})
    assert abs(score(nbr("co_mention", 0.9), off) - 0.85 * 0.7) < 1e-6
