"""Tests for Telegram-specific formatting (#29, #39).

Tests cover:
- sanitize_telegram() converts tables, headers, HRs (plain text, intermediate streaming)
- format_telegram_html() converts markdown to Telegram HTML (final sends)
- _strip_html_tags() strips HTML for plain text fallback
- Platform param flows through to system prompt
"""

import pytest

from nous.telegram_bot import StreamingMessage, _strip_html_tags, format_telegram_html, sanitize_telegram


class TestSanitizeTelegram:
    """Tests for the sanitize_telegram() fallback sanitizer."""

    def test_table_to_bullets(self):
        """Markdown table rows converted to bullet points."""
        text = "| Feature | Status |\n|---|---|\n| Brain | Shipped |\n| Heart | Shipped |"
        result = sanitize_telegram(text)
        assert "|" not in result
        assert "• Brain — Shipped" in result
        assert "• Heart — Shipped" in result

    def test_table_separator_removed(self):
        """|---|---| separator rows are stripped."""
        text = "|---|---|\n|:-:|:-:|"
        result = sanitize_telegram(text)
        assert "---" not in result

    def test_single_column_table(self):
        """Single column table row becomes simple bullet."""
        text = "| Just one column |"
        result = sanitize_telegram(text)
        assert "• Just one column" in result

    def test_three_column_table(self):
        """Three+ column rows join with em dash."""
        text = "| A | B | C |"
        result = sanitize_telegram(text)
        assert "•" in result
        assert "|" not in result
        assert "A" in result
        assert "B" in result
        assert "C" in result

    def test_header_to_bold(self):
        """## headers converted to **bold**."""
        text = "## My Header\nSome text\n### Sub Header"
        result = sanitize_telegram(text)
        assert "##" not in result
        assert "**My Header**" in result
        assert "**Sub Header**" in result

    def test_horizontal_rule_removed(self):
        """--- horizontal rules stripped."""
        text = "Above\n---\nBelow"
        result = sanitize_telegram(text)
        assert "---" not in result
        assert "Above" in result
        assert "Below" in result

    def test_excessive_blank_lines_collapsed(self):
        """Multiple blank lines from removals collapsed to max 2."""
        text = "Above\n\n\n\n\nBelow"
        result = sanitize_telegram(text)
        assert "\n\n\n" not in result

    def test_plain_text_unchanged(self):
        """Normal text passes through unchanged."""
        text = "Hello, this is a normal message with **bold** and _italic_."
        result = sanitize_telegram(text)
        assert result == text

    def test_bullet_lists_unchanged(self):
        """Bullet lists are not affected."""
        text = "• Item one\n• Item two\n- Item three"
        result = sanitize_telegram(text)
        assert result == text

    def test_code_blocks_unchanged(self):
        """Code blocks pass through."""
        text = "```python\nprint('hello')\n```"
        result = sanitize_telegram(text)
        assert "```python" in result

    def test_table_inside_code_block_preserved(self):
        """Tables inside fenced code blocks are NOT sanitized."""
        text = "```\n| col1 | col2 |\n|------|------|\n| a    | b    |\n```"
        result = sanitize_telegram(text)
        assert "| col1 | col2 |" in result
        assert "|------|------|" in result

    def test_header_inside_code_block_preserved(self):
        """Headers inside fenced code blocks are NOT sanitized."""
        text = "```markdown\n## My Header\n```"
        result = sanitize_telegram(text)
        assert "## My Header" in result
        assert "**My Header**" not in result

    def test_mixed_code_and_tables(self):
        """Code blocks preserved while tables outside are sanitized."""
        text = (
            "## Status\n"
            "| Feature | Done |\n"
            "|---------|------|\n"
            "| Brain | Yes |\n\n"
            "```sql\n"
            "SELECT * FROM brain.decisions\n"
            "WHERE status = 'pending'\n"
            "```\n\n"
            "All good!"
        )
        result = sanitize_telegram(text)
        assert "**Status**" in result
        assert "• Brain — Yes" in result
        assert "SELECT * FROM brain.decisions" in result
        assert "```sql" in result

    def test_mixed_content(self):
        """Real-world message with tables + headers + normal text."""
        text = (
            "Here's the status:\n\n"
            "## Features\n"
            "| Feature | Status |\n"
            "|---------|--------|\n"
            "| Brain | Shipped |\n"
            "| Heart | Shipped |\n\n"
            "---\n\n"
            "That's everything!"
        )
        result = sanitize_telegram(text)
        assert "|" not in result
        assert "##" not in result
        assert "---" not in result
        assert "**Features**" in result
        assert "• Brain — Shipped" in result
        assert "That's everything!" in result

    def test_empty_string(self):
        """Empty string returns empty."""
        assert sanitize_telegram("") == ""

    def test_pipe_in_code_not_affected(self):
        """Pipes inside inline code should ideally not be affected.

        Note: The regex-based approach may catch some edge cases.
        This test documents current behavior.
        """
        text = "Use `a | b` for OR operations"
        result = sanitize_telegram(text)
        # Inline code pipes are not wrapped in |...|$ pattern, so safe
        assert "`a | b`" in result


class TestFormatTelegramHtml:
    """Tests for format_telegram_html() — markdown to Telegram HTML (#39)."""

    def test_html_entity_escaping(self):
        """HTML special chars are escaped to prevent injection."""
        text = "a < b & c > d"
        result = format_telegram_html(text)
        assert "a &lt; b &amp; c &gt; d" == result

    def test_header_to_html_bold(self):
        """## headers converted to <b>bold</b>."""
        text = "## My Header\nSome text\n### Sub Header"
        result = format_telegram_html(text)
        assert "##" not in result
        assert "<b>My Header</b>" in result
        assert "<b>Sub Header</b>" in result

    def test_bold_to_html(self):
        """**bold** converted to <b>bold</b>."""
        text = "This is **important** stuff"
        result = format_telegram_html(text)
        assert "**" not in result
        assert "This is <b>important</b> stuff" in result

    def test_inline_code_to_html(self):
        """`code` converted to <code>code</code>."""
        text = "Use `print()` to output"
        result = format_telegram_html(text)
        assert "`" not in result
        assert "<code>print()</code>" in result

    def test_inline_code_html_escaped(self):
        """HTML entities inside inline code are escaped."""
        text = "Use `a < b` in code"
        result = format_telegram_html(text)
        assert "<code>a &lt; b</code>" in result

    def test_fenced_code_block_with_language(self):
        """Fenced code blocks with language get <pre><code class=language-X>."""
        text = "```python\nprint('hello')\n```"
        result = format_telegram_html(text)
        assert "```" not in result
        assert '<pre><code class="language-python">' in result
        assert "print('hello')" in result
        assert "</code></pre>" in result

    def test_fenced_code_block_without_language(self):
        """Fenced code blocks without language get <pre>."""
        text = "```\nsome code\n```"
        result = format_telegram_html(text)
        assert "```" not in result
        assert "<pre>some code</pre>" in result

    def test_code_block_html_escaped(self):
        """HTML entities inside code blocks are escaped."""
        text = "```\na < b && c > d\n```"
        result = format_telegram_html(text)
        assert "a &lt; b &amp;&amp; c &gt; d" in result

    def test_link_to_html(self):
        """[text](url) converted to <a href=url>text</a>."""
        text = "Visit [Nous](https://example.com) for more"
        result = format_telegram_html(text)
        assert '<a href="https://example.com">Nous</a>' in result
        assert "[Nous]" not in result

    def test_table_to_bullets(self):
        """Tables converted to bullet lists (same as sanitize_telegram)."""
        text = "| Feature | Status |\n|---|---|\n| Brain | Shipped |"
        result = format_telegram_html(text)
        assert "• Brain — Shipped" in result
        assert "|" not in result

    def test_hr_removed(self):
        """--- horizontal rules removed."""
        text = "Above\n---\nBelow"
        result = format_telegram_html(text)
        assert "---" not in result
        assert "Above" in result
        assert "Below" in result

    def test_excessive_blank_lines_collapsed(self):
        """Multiple blank lines collapsed to max 2."""
        text = "Above\n\n\n\n\nBelow"
        result = format_telegram_html(text)
        assert "\n\n\n" not in result

    def test_plain_text_escaped(self):
        """Plain text without markdown passes through with HTML escaping."""
        text = "Hello, world!"
        result = format_telegram_html(text)
        assert result == "Hello, world!"

    def test_empty_string(self):
        """Empty string returns empty."""
        assert format_telegram_html("") == ""

    def test_table_inside_code_block_preserved(self):
        """Tables inside fenced code blocks are NOT converted to bullets."""
        text = "```\n| col1 | col2 |\n|------|\n| a    | b    |\n```"
        result = format_telegram_html(text)
        assert "| col1 | col2 |" in result
        assert "•" not in result

    def test_header_inside_code_block_preserved(self):
        """Headers inside code blocks are NOT converted to <b>."""
        text = "```markdown\n## My Header\n```"
        result = format_telegram_html(text)
        assert "## My Header" in result
        assert "<b>My Header</b>" not in result

    def test_mixed_content(self):
        """Real-world message with tables, headers, code, and bold."""
        text = (
            "## Status\n\n"
            "This is **important**.\n\n"
            "| Feature | Done |\n"
            "|---------|------|\n"
            "| Brain | Yes |\n\n"
            "```python\nx = 1\n```\n\n"
            "Use `code` here."
        )
        result = format_telegram_html(text)
        assert "<b>Status</b>" in result
        assert "<b>important</b>" in result
        assert "• Brain — Yes" in result
        assert '<pre><code class="language-python">x = 1</code></pre>' in result
        assert "<code>code</code>" in result

    def test_bold_with_html_entities_inside(self):
        """Bold text containing HTML special chars is properly escaped."""
        text = "**a < b**"
        result = format_telegram_html(text)
        assert "<b>a &lt; b</b>" in result

    def test_link_with_ampersand_in_url(self):
        """URLs with & are properly escaped in HTML."""
        text = "[search](https://example.com?a=1&b=2)"
        result = format_telegram_html(text)
        assert 'href="https://example.com?a=1&amp;b=2"' in result
        assert ">search</a>" in result


class TestStripHtmlTags:
    """Tests for _strip_html_tags() plain text fallback."""

    def test_strips_bold_tags(self):
        text = "This is <b>bold</b> text"
        assert _strip_html_tags(text) == "This is bold text"

    def test_strips_code_tags(self):
        text = "Use <code>print()</code>"
        assert _strip_html_tags(text) == "Use print()"

    def test_strips_pre_tags(self):
        text = "<pre>code block</pre>"
        assert _strip_html_tags(text) == "code block"

    def test_unescapes_html_entities(self):
        text = "a &lt; b &amp; c &gt; d"
        assert _strip_html_tags(text) == "a < b & c > d"

    def test_combined_strip_and_unescape(self):
        text = "<b>a &lt; b</b>"
        assert _strip_html_tags(text) == "a < b"

    def test_plain_text_unchanged(self):
        text = "Hello, world!"
        assert _strip_html_tags(text) == "Hello, world!"


class TestDebugSystemPromptEncoding:
    """Tests for debug output HTML encoding of system prompts (#45).

    The debug path in _chat() uses html.escape(prompt, quote=False) inside
    <pre> tags. Quotes must NOT be escaped (they render as literal &quot;
    and &#x27; inside <pre>), but <, >, & must still be escaped for valid HTML.
    """

    def _encode_debug_prompt(self, prompt: str) -> str:
        """Replicate the debug encoding path: html.escape(prompt, quote=False)."""
        import html as html_module
        return f"<pre>{html_module.escape(prompt, quote=False)}</pre>"

    def test_single_quotes_not_escaped(self):
        """Single quotes must NOT be escaped to &#x27; in debug output."""
        result = self._encode_debug_prompt("You are an agent named 'Nous'")
        assert "&#x27;" not in result
        assert "'" in result

    def test_double_quotes_not_escaped(self):
        """Double quotes must NOT be escaped to &quot; in debug output."""
        result = self._encode_debug_prompt('Use the "recall_deep" tool')
        assert "&quot;" not in result
        assert '"' in result

    def test_angle_brackets_still_escaped(self):
        """< and > must still be escaped for valid HTML inside <pre>."""
        result = self._encode_debug_prompt("if a < b and c > d")
        assert "&lt;" in result
        assert "&gt;" in result
        assert "a < b" not in result  # raw < would break HTML

    def test_ampersand_still_escaped(self):
        """& must still be escaped for valid HTML inside <pre>."""
        result = self._encode_debug_prompt("foo & bar")
        assert "&amp;" in result
        assert "foo & bar" not in result  # raw & would break HTML entities

    def test_mixed_quotes_and_html_chars(self):
        """Prompt with both quotes and HTML-special chars encodes correctly."""
        prompt = """You are "Nous" — an agent that uses 'recall_deep' when x < 10 & y > 5."""
        result = self._encode_debug_prompt(prompt)
        # Quotes preserved literally
        assert '"Nous"' in result
        assert "'recall_deep'" in result
        # HTML-special chars escaped
        assert "&lt;" in result
        assert "&gt;" in result
        assert "&amp;" in result


class TestStreamingMessageThinking:
    """Tests for thinking indicator display in StreamingMessage (#48, spec 007.1)."""

    def _make_streamer(self):
        """Create a StreamingMessage with a mock bot for testing display logic."""
        from unittest.mock import AsyncMock, MagicMock

        bot = MagicMock()
        bot._send = AsyncMock(return_value={"message_id": 1})
        bot._tg = AsyncMock(return_value={})
        streamer = StreamingMessage(bot, chat_id=123)
        # Override interval so tests don't skip edits
        streamer._min_interval = 0
        return streamer

    def test_initial_thinking_state(self):
        """Thinking state starts at zero/empty."""
        streamer = self._make_streamer()
        assert streamer._thinking_text == ""
        assert streamer._thinking_count == 0
        assert streamer._thinking_displayed is False

    @pytest.mark.asyncio
    async def test_start_thinking_increments_count(self):
        """start_thinking increments count and resets text."""
        streamer = self._make_streamer()
        await streamer.start_thinking()
        assert streamer._thinking_count == 1
        assert streamer._thinking_text == ""
        assert streamer._thinking_displayed is False

    @pytest.mark.asyncio
    async def test_append_thinking_accumulates(self):
        """append_thinking accumulates text."""
        streamer = self._make_streamer()
        await streamer.start_thinking()
        await streamer.append_thinking("Let me ")
        await streamer.append_thinking("analyze this")
        assert streamer._thinking_text == "Let me analyze this"

    @pytest.mark.asyncio
    async def test_thinking_not_displayed_under_threshold(self):
        """Thinking preview not shown until 50 chars accumulated."""
        streamer = self._make_streamer()
        await streamer.start_thinking()
        await streamer.append_thinking("Short text")  # 10 chars
        assert streamer._thinking_displayed is False

    @pytest.mark.asyncio
    async def test_thinking_displayed_over_threshold(self):
        """Thinking preview shown after 50+ chars accumulated."""
        streamer = self._make_streamer()
        await streamer.start_thinking()
        await streamer.append_thinking("A" * 60)
        assert streamer._thinking_displayed is True

    def test_build_display_single_thinking(self):
        """Single thinking block shows preview without count."""
        streamer = self._make_streamer()
        streamer._thinking_count = 1
        streamer._thinking_text = "Let me analyze the current state of the system"
        result = streamer._build_display_text()
        assert "\U0001f4ad" in result
        assert "Let me analyze the current state of the system" in result
        assert "Thinking (" not in result  # no count for single block

    def test_build_display_multiple_thinking(self):
        """Multiple thinking blocks show count."""
        streamer = self._make_streamer()
        streamer._thinking_count = 3
        streamer._thinking_text = "Final thinking block content here"
        result = streamer._build_display_text()
        assert "Thinking (3):" in result

    def test_build_display_thinking_truncated(self):
        """Long thinking text truncated at 100 chars with ellipsis."""
        streamer = self._make_streamer()
        streamer._thinking_count = 1
        streamer._thinking_text = "A" * 150
        result = streamer._build_display_text()
        assert "A" * 100 + "..." in result

    def test_build_display_thinking_with_text(self):
        """Thinking indicator appears before base text."""
        streamer = self._make_streamer()
        streamer._thinking_count = 1
        streamer._thinking_text = "Analyzing the problem carefully here for testing"
        streamer._base_text = "Here is my response."
        result = streamer._build_display_text()
        thinking_pos = result.find("\U0001f4ad")
        response_pos = result.find("Here is my response.")
        assert thinking_pos < response_pos

    def test_build_display_no_thinking(self):
        """No thinking state means no thinking indicator."""
        streamer = self._make_streamer()
        streamer._base_text = "Just a response"
        result = streamer._build_display_text()
        assert "\U0001f4ad" not in result
        assert result == "Just a response"

    def test_build_display_thinking_newlines_replaced(self):
        """Newlines in thinking text replaced with spaces for preview."""
        streamer = self._make_streamer()
        streamer._thinking_count = 1
        streamer._thinking_text = "Line one\nLine two\nLine three"
        result = streamer._build_display_text()
        assert "\n" not in result.split("\n\n")[0]  # thinking part has no internal newlines

    @pytest.mark.asyncio
    async def test_finalize_with_thinking_summary(self):
        """Finalize includes thinking summary before response text."""
        streamer = self._make_streamer()
        streamer._thinking_count = 1
        streamer._thinking_text = "Let me analyze the procedures table and check results"
        streamer._base_text = "The answer is 42."
        streamer._usage = {"input_tokens": 8200, "output_tokens": 943}
        await streamer.finalize()
        # Thinking should be in the final text
        assert "\U0001f4ad" in streamer.text
        assert "The answer is 42." in streamer.text
        assert "8.2K in" in streamer.text

    @pytest.mark.asyncio
    async def test_finalize_redacted_thinking(self):
        """Finalize shows redacted message when thinking has no content."""
        streamer = self._make_streamer()
        streamer._thinking_count = 1
        streamer._thinking_text = ""  # redacted — no content
        streamer._base_text = "Response text."
        await streamer.finalize()
        assert "redacted" in streamer.text

    @pytest.mark.asyncio
    async def test_finalize_with_tools_and_thinking(self):
        """Finalize shows thinking summary + tool count + response."""
        streamer = self._make_streamer()
        streamer._thinking_count = 2
        streamer._thinking_text = "Second thinking block about the search results analysis"
        streamer._base_text = "Found the answer."
        streamer._tool_counts = {"web_search": 2, "recall_deep": 1}
        await streamer.finalize()
        assert "\U0001f4ad" in streamer.text
        assert "Ran 3 tools" in streamer.text
        assert "Found the answer." in streamer.text

    @pytest.mark.asyncio
    async def test_multiple_thinking_blocks_reset(self):
        """Each start_thinking resets text for the new block."""
        streamer = self._make_streamer()
        await streamer.start_thinking()
        await streamer.append_thinking("First block content")
        await streamer.start_thinking()
        assert streamer._thinking_count == 2
        assert streamer._thinking_text == ""  # reset for new block
        await streamer.append_thinking("Second block content")
        assert streamer._thinking_text == "Second block content"
