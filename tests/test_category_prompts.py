"""Drift-proofing for the shared Tier-1 category guidance (2026-07-24)."""
from nous.heart.category_prompts import TIER1_CATEGORY_GUIDANCE


def test_guidance_names_all_tier1_categories():
    for cat in ("person", "preference", "rule"):
        assert f'"{cat}"' in TIER1_CATEGORY_GUIDANCE


def test_guidance_has_negative_guards():
    for phrase in ("NEVER use", "session events", "dated one-offs", "If in doubt"):
        assert phrase in TIER1_CATEGORY_GUIDANCE


def test_all_definitional_prompts_embed_canonical_guidance():
    """The four LLM prompts that define tier-1 categories must embed the
    SHARED constant — hand-copied variants drift (2026-07-24 recon: three
    already had; sleep_handler is the fourth)."""
    from nous.handlers import (
        episode_summarizer, fact_extractor, knowledge_extractor, sleep_handler,
    )

    for mod in (episode_summarizer, fact_extractor, knowledge_extractor, sleep_handler):
        src_prompts = [
            v for k, v in vars(mod).items()
            if isinstance(v, str)
            and "category" in v.lower()
            and k.isupper()
            and v is not TIER1_CATEGORY_GUIDANCE  # correctness P2-2: the
            # imported constant trivially contains itself — exclude it or the
            # test passes without any prompt embedding the guidance
        ]
        assert any(TIER1_CATEGORY_GUIDANCE in p for p in src_prompts), (
            f"{mod.__name__} does not embed TIER1_CATEGORY_GUIDANCE"
        )


def test_coverage_addendum_does_not_route_session_events_to_person():
    """devil test-gap: the coverage-expansion block previously pushed session
    events to category person; pin the fix independently of the main prompt."""
    from nous.handlers import episode_summarizer

    addendum = episode_summarizer._COVERAGE_EXPANSION_INSTRUCTION
    assert "durable identity only" in addendum
    assert "category: event" in addendum
