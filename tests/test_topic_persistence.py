"""Tests for 007.2 spike: topic persistence via _resolve_focus_text."""

import pytest

from nous.cognitive.layer import CognitiveLayer


class _Stub(CognitiveLayer):
    """Stub that skips __init__ but inherits class attrs and methods."""

    def __init__(self):
        pass  # skip real init


@pytest.fixture
def resolve():
    stub = _Stub()
    return stub._resolve_focus_text


class TestTopicPersistence:
    """Verify that ambiguous/follow-up inputs preserve existing topic."""

    # --- Should PRESERVE topic (return None) ---

    @pytest.mark.parametrize(
        "input_text",
        [
            "yes",
            "ok",
            "no",
            "hm",
            "?",
            "",
            "   ",
        ],
    )
    def test_very_short_inputs_preserve_topic(self, resolve, input_text):
        assert resolve(input_text) is None

    @pytest.mark.parametrize(
        "input_text",
        [
            "it works",
            "that one",
            "this please",
            "them too",
        ],
    )
    def test_pronoun_inputs_preserve_topic(self, resolve, input_text):
        assert resolve(input_text) is None

    @pytest.mark.parametrize(
        "input_text",
        [
            "what about it?",
            "how about that?",
            "tell me more",
            "what else?",
            "anything else?",
            "go on",
            "continue",
            "keep going",
            "elaborate",
            "and what happened?",
        ],
    )
    def test_followup_phrases_preserve_topic(self, resolve, input_text):
        assert resolve(input_text) is None

    @pytest.mark.parametrize(
        "input_text",
        [
            "why?",
            "how?",
            "when?",
            "where?",
            "who?",
        ],
    )
    def test_bare_question_words_preserve_topic(self, resolve, input_text):
        assert resolve(input_text) is None

    def test_pronoun_question_preserves(self, resolve):
        assert resolve("what's that about?") is None

    def test_is_that_right_preserves(self, resolve):
        assert resolve("is that right?") is None

    # --- Should UPDATE topic (return text) ---

    @pytest.mark.parametrize(
        "input_text",
        [
            "tell me about cognition-engines",
            "let's talk about membrain",
            "what do you know about the attractor dynamics?",
            "explain the compaction algorithm",
            "how does the CSTP server work?",
            "I want to discuss the trading strategy",
        ],
    )
    def test_clear_topic_updates(self, resolve, input_text):
        result = resolve(input_text)
        assert result is not None
        assert result == input_text[:200]

    def test_tell_me_more_with_clear_object_updates(self, resolve):
        result = resolve("tell me more about the attractor dynamics in membrain")
        assert result is not None

    def test_more_about_short_object_updates(self, resolve):
        """'more about React' should update (threshold lowered to 3)."""
        result = resolve("more about React patterns")
        assert result is not None

    def test_truncation_at_200(self, resolve):
        long_input = "explain " + "x" * 300
        result = resolve(long_input)
        assert result is not None
        assert len(result) == 200

    def test_multiword_topic_updates(self, resolve):
        result = resolve("the compaction algorithm needs work")
        assert result is not None

    def test_how_does_with_object_updates(self, resolve):
        """'how does X work' has a clear topic — should update."""
        result = resolve("how does the CSTP server work?")
        assert result is not None
