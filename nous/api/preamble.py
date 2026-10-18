"""The Claude Code preamble: system block 0 of every Messages request Nous sends.

Under an OAuth (subscription) token the API answers a request to any model
but Haiku 4.5 (Haiku 5.5 included) with a 429 unless system block 0 is this
text. Every request builder imports it from here instead of writing its own copy.
"""

CLAUDE_CODE_PREAMBLE = "You are Claude Code, Anthropic's official CLI for Claude."
