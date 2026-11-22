"""The status line: one short line of facts the model sees on each call of a background task turn.

``NOUS_STATUS_LINE_ENABLED`` (off by default). The line says which round of the tool loop this call is, how many tool
calls are used of the limit, what happens after the last round, and what the task has changed so far (from the
session's execution ledger). It states facts only, no advice.

It exists in the outgoing request only: the runner adds it as a text block after the tool results of the last user
message, after that message's cache breakpoint and with no ``cache_control`` of its own, so the cached prefix is the
same bytes with the setting on or off. The tool loop's message list, the saved conversation, pruning, compaction and
the claim verifier never see it.
"""

from __future__ import annotations

import unicodedata
from typing import Any

from nous.cognitive.execution_ledger import ExecutionLedger

# Background kinds under the round and tool-call limits of a task turn (ExecutionContext.for_subtask), and a
# continuation's decision turn. A person or another agent waits on interactive and mcp turns; the heartbeat, companion
# and DAG summary turns are short and bounded as a whole; an approved_action runs with no model.
STATUS_LINE_KINDS: frozenset[str] = frozenset({"subtask", "dag_node", "scheduled", "continuation"})
FILES_SHOWN = 5
PATH_CHARS = 80
_OUTSIDE = frozenset({"external", "irreversible"})
_PATH_ARGS = ("path", "file_path")


def _clean_path(path: str) -> str:
    """A model-written path made safe inside the line: control and format characters become spaces, brackets become
    parentheses (so a path cannot close the line or open a second one), runs of blanks one space, then the clip."""
    text = "".join(" " if unicodedata.category(ch).startswith("C") else ch for ch in path)
    text = " ".join(text.replace("[", "(").replace("]", ")").split())
    return text[:PATH_CHARS]


def changes_so_far(ledger: ExecutionLedger) -> str:
    """What the task changed so far: the calls that reported success and are not read-only, how many of them went
    outside the host, and the files written (newest five distinct paths, in the order written). ``none`` when there
    are none. A suppressed repeat of a call already sent records success again, so this counts reported successes."""
    changed = [a for a in ledger.actions if a.status == "success" and a.side_effect_type != "none"]
    if not changed:
        return "none"
    text = str(len(changed))
    outside = sum(1 for a in changed if a.side_effect_type in _OUTSIDE)
    if outside:
        text += f", {outside} sent outside"
    newest: list[str] = []
    for action in reversed(changed):
        if action.tool_name != "write_file":
            continue
        raw = next((action.key_args[k] for k in _PATH_ARGS if action.key_args.get(k)), None)
        path = _clean_path(raw) if raw else ""
        if path and path not in newest:
            newest.append(path)
    if newest:
        shown = list(reversed(newest[:FILES_SHOWN]))
        text += "; files written: " + ", ".join(shown)
        if len(newest) > FILES_SHOWN:
            text += f" and {len(newest) - FILES_SHOWN} more"
    return text


def render_status_line(
    *,
    round_number: int,
    max_rounds: int,
    tool_calls: int,
    max_tool_calls: int | None,
    ledger: ExecutionLedger | None,
) -> str:
    """The line for the call of round ``round_number`` (1-based) of a run of ``max_rounds``. A run is one ``run_turn``:
    a continuation may get a follow-up run, so the line never says the task's last. Rounds and tool calls are this
    run's; the changes are the whole session's ledger, which a retried attempt shares."""
    calls = f"Tool calls used: {tool_calls}" + (f" of {max_tool_calls}." if max_tool_calls else ".")
    if round_number >= max_rounds:
        limit = "This is the last round of this run that can call tools; after it comes one reply with no tools."
    elif max_tool_calls:
        limit = (
            f"After round {max_rounds}, or once the tool calls are used up, this run ends with one reply with no tools."
        )
    else:
        limit = f"After round {max_rounds} this run ends with one reply with no tools."
    parts = [f"Run status. Round {round_number} of {max_rounds}.", calls, limit]
    if ledger is not None:
        parts.append(f"Changes so far in this task: {changes_so_far(ledger)}.")
    return "[" + " ".join(parts) + "]"


def with_tail_note(message: dict[str, Any], note: str) -> dict[str, Any]:
    """A new message: ``message`` with one text block, ``note``, after its content blocks. Mutates nothing; the block
    has no ``cache_control``."""
    return {**message, "content": [*message["content"], {"type": "text", "text": note}]}
