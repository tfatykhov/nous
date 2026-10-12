"""F099 Phase 3a: card_text keeps model-authored text from opening or closing a fence on either renderer."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from nous.brain import continuation

ROOT = Path(__file__).resolve().parents[1]
MARKDOWN_TS = ROOT / "dashboard-app" / "src" / "companion" / "markdown.ts"
MARKDOWN_KT = (
    ROOT
    / "android"
    / "core"
    / "src"
    / "main"
    / "kotlin"
    / "us"
    / "fatykhov"
    / "nous"
    / "companion"
    / "core"
    / "Markdown.kt"
)

# The same vector is in the web test (Task 13); tests/test_f099_phase3a_card_text_golden.py ties the two together.
GOLDEN_IN = "ok\n```\n# Approve now\n[click](https://evil.example)\u202e\r\n   ```bash\nend"
GOLDEN_OUT = "ok\n\u00b7 ```\n# Approve now\n[click](https://evil.example)\\u202e\n" + "\u00b7 " + "   ```bash\nend"


def test_the_golden_vector():
    assert continuation.card_text(GOLDEN_IN, 4000) == GOLDEN_OUT


def test_unsafe_characters_are_shown_as_escapes():
    assert continuation.card_text("pay \u202eevil \u200b", 100) == "pay \\u202eevil \\u200b"


def test_a_clip_is_marked_and_never_inside_an_escape():
    # 6 units per escape; the marker is 12; limit 20 leaves room for exactly one escape
    assert continuation.card_text("\u202e" * 10, 20) == "\\u202e\n[truncated]"
    assert continuation.card_text("short", 100) == "short"


def test_a_lone_carriage_return_is_a_line_break_for_the_neutraliser():
    assert continuation.card_text("a\r```b", 50) == "a\n\u00b7 ```b"
    assert continuation.card_text("a\r\n```b", 50) == "a\n\u00b7 ```b"


def _renderer_fence() -> str:
    """The fence regex of BOTH renderers, read from their source: they must agree with each other and with the rule."""
    ts = re.search(r"const FENCE = /(.+)/;", MARKDOWN_TS.read_text(encoding="utf-8"))
    kt = re.search(r'val FENCE = JsRegex\.compile\("""(.+?)"""\)', MARKDOWN_KT.read_text(encoding="utf-8"))
    assert ts and kt, "the renderers' FENCE definitions moved"
    assert ts.group(1) == kt.group(1) == r"^\s*```(.*)$"
    return ts.group(1)


HOSTILE = [
    "```",
    "  ```js",
    "text\n```\n# Heading\n[link](https://evil.example)",
    "\u3000```",
    "\u00a0```",
    "\t```",
    "a\r```",
    "````",
    "```\n```\n```",
    "``` \n   ```   \nend",
    "no fence here",
    "",
]


@pytest.mark.parametrize("hostile", HOSTILE)
def test_inside_a_fence_no_model_line_can_open_or_close_one(hostile):
    """Wrap the shown text as the builders do and split it as BOTH renderers do (``\\r?\\n``): only the two outer
    lines may match the renderers' FENCE. Mutation: drop the middle-dot prefixing in card_text and this fails."""
    fence = re.compile(_renderer_fence())
    shown = "```\n" + continuation.card_text(hostile, 4000) + "\n```"
    lines = re.split(r"\r?\n", shown)
    assert [i for i, line in enumerate(lines) if fence.match(line)] == [0, len(lines) - 1]
