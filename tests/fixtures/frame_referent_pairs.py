"""F110 v2 referent rule: (previous assistant text, user reply, expected frame, expected match_method) pairs.

Scored against the seed frames in ``tests/test_frames.py`` with the frame-context flag on, a short reply of at
most 6 words and a switch threshold of 2 hits. A pair whose method is not ``referent`` expects exactly the
frame today's keyword selection picks. The first 30 pairs are the task's original set; the rest come from the
fix-round ruling (a reply with a ``?`` or a negation or deferral word is not affirmative-led, and a lone
``do`` is not an affirmative first word).

Five assistant texts are block lists flattened the way the runner's ``_extract_text`` flattens a tool-using
turn: text blocks joined with a newline, every other block dropped.
"""

from __future__ import annotations

from typing import Any


def _flatten(blocks: list[dict[str, Any]]) -> str:
    """Mirror of ``AgentRunner._extract_text``: join the text blocks with a newline, drop the rest."""
    return "\n".join(block["text"] for block in blocks if block.get("type") == "text")


def _tool_use(name: str) -> dict[str, Any]:
    return {"type": "tool_use", "id": f"toolu_{name}", "name": name, "input": {}}


_LONG_STATUS = (
    "Meanwhile, here is the status of the nightly jobs: the export finished at 02:10, the backfill at 02:40 "
    "and the cleanup at 03:05. All three wrote their rows, and the dashboard shows the counts for the last "
    "seven days without gaps. Nothing else changed overnight."
)

_FLAT_RERUN = _flatten(
    [
        {"type": "text", "text": "I checked the logs."},
        _tool_use("bash"),
        {"type": "text", "text": "The job failed at step 3. Want me to rerun it?"},
    ]
)
_FLAT_COMMIT = _flatten(
    [
        _tool_use("write_file"),
        {"type": "text", "text": "Done: 3 files updated."},
        {"type": "text", "text": "Shall I commit them?"},
    ]
)
_FLAT_PUSH = _flatten(
    [
        {"type": "text", "text": "Here is the diff."},
        _tool_use("bash"),
        {"type": "text", "text": "I can push it to a branch if that helps."},
    ]
)
_FLAT_DROP = _flatten(
    [
        {"type": "text", "text": "Two options: keep the cache or drop it."},
        _tool_use("db_query"),
        {"type": "text", "text": "I would drop it. Should I go ahead?"},
    ]
)
# The offer is more than 200 characters before the end: the tail holds no offer.
_FLAT_OFFER_TOO_EARLY = _flatten(
    [
        {"type": "text", "text": "Want me to rerun it?"},
        _tool_use("bash"),
        {"type": "text", "text": _LONG_STATUS},
    ]
)

WRITE_IT_UP = "Want me to write it up?"

FRAME_REFERENT_PAIRS: list[tuple[str, str, str, str]] = [
    # --- accepted offers: task / referent ---
    (WRITE_IT_UP, "yes do it", "task", "referent"),
    ("Shall I send the summary to your inbox?", "sure", "task", "referent"),
    ("Would you like a chart of last week's numbers?", "yep please", "task", "referent"),
    ("I found two stale branches. Delete them?", "go ahead", "task", "referent"),
    ("I can run the backfill tonight.", "proceed", "task", "referent"),
    ("Want me to add the env rows too?", "agreed", "task", "referent"),
    ("Would you like me to compare the two plans?", "please do", "task", "referent"),
    ("Ready to merge once CI is green. Should I merge?", "confirm", "task", "referent"),
    # one conversation hit: the ruling lets the referent rule override it
    ("Want me to file the issue?", "yes thanks", "task", "referent"),
    # one task hit: still the referent rule (confidence 0.6, not the pattern's 0.3)
    (WRITE_IT_UP, "yes, write it", "task", "referent"),
    # mixed case and punctuation
    ("Want me to rerun the job?", "YES, do it!", "task", "referent"),
    ("Shall I deploy it?", "Ok.", "task", "referent"),
    ("Want me to clean that up?", "Sure!! Go ahead.", "task", "referent"),
    # block-list-flattened assistant text
    (_FLAT_RERUN, "yeah", "task", "referent"),
    (_FLAT_COMMIT, "do it", "task", "referent"),
    (_FLAT_PUSH, "okay", "task", "referent"),
    (_FLAT_DROP, "yes, drop it", "task", "referent"),
    # --- not a referent: today's keyword result ---
    ("Here is the summary.", "yes", "conversation", "default"),
    (WRITE_IT_UP, "yes and also include the numbers from last week please", "conversation", "default"),
    (WRITE_IT_UP, "What if we redesign it?", "creative", "pattern"),
    # affirmative-led and short, but 2 hits: a clear new topic wins (rule 1)
    (WRITE_IT_UP, "yes, what if we redesign it", "creative", "pattern"),
    (WRITE_IT_UP, "yes, explain why", "question", "pattern"),
    # one hit for a frame other than task or conversation: the ruling keeps it
    (WRITE_IT_UP, "yes but why", "question", "pattern"),
    ("Should I retry the upload?", "ok but it is broken", "debug", "pattern"),
    ("Want me to pick one?", "yes, you choose", "decision", "pattern"),
    (WRITE_IT_UP, "no thanks", "conversation", "pattern"),
    ("Want me to rerun it?", "not now", "conversation", "default"),
    ("Shall I delete the branch?", "don't, keep it", "conversation", "default"),
    # "should include" is not "should i": the offer phrases match whole words
    ("The release notes should include the migration number.", "ok", "conversation", "default"),
    (_FLAT_OFFER_TOO_EARLY, "yes", "conversation", "default"),
    # --- fix round 1: more accepted offers ---
    ("I can draft the migration now if you want.", "ok go ahead", "task", "referent"),
    ("Want me to send it to the team?", "do that", "task", "referent"),
    # --- fix round 1: negations, deferrals and questions are not acceptances ---
    ("Shall I delete the branch?", "please don't", "conversation", "default"),
    ("Want me to rerun it?", "do not", "conversation", "default"),
    ("Want me to file the issue?", "yeah no", "conversation", "default"),
    ("I can run the backfill tonight.", "yes, later", "conversation", "default"),
    ("Should I merge?", "sure, but not today", "conversation", "default"),
    ("Want me to clean that up?", "ok, hold on", "conversation", "default"),
    ("Want me to clean that up?", "please stop", "conversation", "default"),
    (WRITE_IT_UP, "do you think so?", "conversation", "default"),
    # the review's "I can't" case: not an offer
    ("I can't run that tonight.", "ok", "conversation", "default"),
    # fix round 1 follow-up: skip, leave and away also veto
    ("Want me to clean that up?", "go away", "conversation", "default"),
    ("Shall I send the summary to your inbox?", "skip it", "conversation", "default"),
    ("Should I merge?", "leave it", "conversation", "default"),
]
