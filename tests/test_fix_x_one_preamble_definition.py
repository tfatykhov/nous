"""The Claude Code preamble is written once, in nous/api/preamble.py.

Every request builder puts it at system block 0; under an OAuth token any
model but Haiku 4.5 (Haiku 5.5 included) answers 429 without it. A builder that keeps its own
copy of the text is how three hand-built requests came to lack it.
"""

from __future__ import annotations

from pathlib import Path

import nous

_TEXT = "You are Claude Code, Anthropic's official CLI for Claude."


def test_the_preamble_text_has_one_definition():
    root = Path(nous.__file__).parent
    found = sorted(path.relative_to(root).as_posix() for path in root.rglob("*.py") if _TEXT in path.read_text("utf-8"))
    assert found == ["api/preamble.py"], "a module writes the preamble text instead of importing it"


def test_the_definition_is_the_text_the_api_expects():
    from nous.api.preamble import CLAUDE_CODE_PREAMBLE

    assert CLAUDE_CODE_PREAMBLE == _TEXT
