"""Who cancelled a DAG or a Claude Code job: one record shape and its renderer.

A record is ``{"by": "owner" | "agent" | "harness", "via": <short label>, "at": <UTC ISO time>}``. A DAG keeps it
in ``execution_dags.postmortem`` under ``"cancellation"``; a job keeps it as JSON in its ``cancelled_by`` mark
(``nous.api.job_marks``), written after a lone ``runner.sh cancel <job id>`` that Nous ran succeeded. A record is
data: ``describe`` cleans ``via`` again and renders only its fixed sentences, so a garbled or forged record never
reaches a turn as written. No record makes no claim about who cancelled.
"""

from __future__ import annotations

import json
import posixpath
import re
import shlex
from datetime import UTC, datetime
from typing import Any

from nous.api.job_marks import JOB_ID_RE, read_job_mark, write_job_mark

BY_VALUES = ("owner", "agent", "harness")
NO_RECORD_TEXT = "no cancel recorded by Nous (cancelled outside Nous, or by a command Nous does not record)"
_VIA_MAX_CHARS = 60
_NOT_PRINTABLE_ASCII = re.compile(r"[^ -~]")
_TURN_LABELS = {"interactive": "chat turn", "continuation": "continuation turn", "mcp": "MCP turn"}
_CANCELLED_BY = "cancelled_by"
_CANCELLED_BY_MAX_BYTES = 512
_RUNNER_VERBS = frozenset({"cancel", "status", "result"})
_RELATIVE_RUNNERS = ("claude-jobs/runner.sh", "./claude-jobs/runner.sh")  # bash's cwd is the workspace
# Anything a shell would read as more than words: separators, redirections, expansions, quotes, globs, comments.
_SHELL_SPECIAL = re.compile(r"""[;&|<>$`()\\'"*?\[\]{}~#!\r\n]""")


def clean_via(value: object) -> str:
    """``value`` as one line of printable ASCII, whitespace collapsed, at most 60 characters; ``unknown`` when it is
    not a string or nothing is left."""
    if not isinstance(value, str):
        return "unknown"
    cleaned = " ".join(_NOT_PRINTABLE_ASCII.sub(" ", value).split())[:_VIA_MAX_CHARS].strip()
    return cleaned or "unknown"


def record(by: str, via: str) -> dict[str, str]:
    """A provenance record. Raises ValueError for a ``by`` outside ``BY_VALUES``."""
    if by not in BY_VALUES:
        raise ValueError(f"unknown canceller {by!r}")
    return {"by": by, "via": clean_via(via), "at": datetime.now(UTC).isoformat(timespec="seconds")}


def owner_via(actor: str | None) -> str:
    """The owner surface an actor string names: ``telegram:<id>``, ``companion:<who>``, ``rest`` (the REST routes'
    default, which the dashboard uses too), else the cleaned actor."""
    text = actor.strip() if isinstance(actor, str) else ""
    if not text:
        return "owner"
    if text.startswith("telegram:"):
        return "Telegram"
    if text == "companion" or text.startswith("companion:"):
        return "companion"
    if text == "rest":
        return "dashboard or REST"
    return clean_via(text)


def turn_label(kind: str | None) -> str:
    """The turn an execution context kind names: ``chat turn``, ``continuation turn``, ``MCP turn``, else
    ``<kind words> turn``."""
    if not kind:
        return "turn"
    return _TURN_LABELS.get(kind) or clean_via(f"{kind.replace('_', ' ')} turn")


def agent_record(kind: str | None) -> dict[str, str]:
    """The agent cancelled, in a turn of ``kind``."""
    return record("agent", turn_label(kind))


def context_actor(kind: str | None) -> dict[str, str]:
    """Who a cancel made by a tool call in a turn of ``kind`` belongs to: the owner for an approved proposal (the
    owner decided that exact call), else the agent's own turn."""
    if kind == "approved_action":
        return record("owner", "approved proposal")
    return agent_record(kind)


def describe(rec: object) -> str | None:
    """``cancelled by ...`` for a record, or None for anything that is not one."""
    if not isinstance(rec, dict) or rec.get("by") not in BY_VALUES:
        return None
    via = clean_via(rec.get("via"))
    if rec["by"] == "owner":
        return f"cancelled by the owner ({via})"
    if rec["by"] == "agent":
        return f"cancelled by your own {via}"
    return f"cancelled by the harness ({via})"


def dag_postmortem(rec: dict[str, str]) -> dict[str, Any]:
    """The ``postmortem`` value that stores ``rec`` on a DAG."""
    return {"cancellation": rec}


def dag_description(dag: object) -> str | None:
    """``cancelled by ...`` from a DAG's ``postmortem``, or None when it holds no record. Never raises."""
    postmortem = getattr(dag, "postmortem", None)
    return describe(postmortem.get("cancellation") if isinstance(postmortem, dict) else None)


def dag_cancel_line(dag: Any) -> str | None:
    """``DAG <id8> was cancelled by ...`` for a cancelled DAG that holds a record, else None."""
    if getattr(dag, "status", None) != "cancelled":
        return None
    description = dag_description(dag)
    return f"DAG {str(dag.id)[:8]} was {description}" if description else None


def parse_runner_call(command: str, workspace_dir: str) -> tuple[str, str] | None:
    """``(verb, job id)`` when ``command`` is exactly one ``runner.sh cancel|status|result <job id>``, else None.

    The runner is ``<workspace>/claude-jobs/runner.sh``, or ``claude-jobs/runner.sh`` / ``./claude-jobs/runner.sh``
    (bash runs in the workspace), optionally after a plain ``bash``. Any shell metacharacter, quote or glob refuses
    the command. This is the record side only: whether a turn may run the command is ``tool_policy``'s
    (``_runner_allowed``), and the claim verifier reads runner calls for evidence on its own. A form one of them
    starts to accept must be added here in the same change for its cancels to be recorded."""
    if not isinstance(command, str) or _SHELL_SPECIAL.search(command):
        return None
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    if words[:1] == ["bash"]:
        words = words[1:]
    if len(words) != 3:
        return None
    runner, verb, job_id = words
    runners = (posixpath.join(workspace_dir, "claude-jobs", "runner.sh"), *_RELATIVE_RUNNERS)
    if runner not in runners or verb not in _RUNNER_VERBS or JOB_ID_RE.fullmatch(job_id) is None:
        return None
    return verb, job_id


def write_job_cancelled_by(workspace_dir: str, job_id: str, rec: dict[str, str]) -> bool:
    """Store ``rec`` as the job's ``cancelled_by`` mark, replacing an older one. Whether it was written. Never
    raises."""
    if describe(rec) is None:
        return False
    try:
        text = json.dumps(rec, sort_keys=True) + "\n"
    except (TypeError, ValueError):
        return False
    return write_job_mark(workspace_dir, job_id, _CANCELLED_BY, text, overwrite=True)


def describe_job_cancellation(workspace_dir: str, job_id: str) -> str:
    """``cancelled by ...`` from the job's ``cancelled_by`` mark; ``NO_RECORD_TEXT`` when there is none or it is
    not a record as ``write_job_cancelled_by`` writes one (a ``via`` that cleaning would change was not)."""
    raw = read_job_mark(workspace_dir, job_id, _CANCELLED_BY, max_bytes=_CANCELLED_BY_MAX_BYTES)
    if raw is None:
        return NO_RECORD_TEXT
    try:
        rec = json.loads(raw)
    except ValueError:
        return NO_RECORD_TEXT
    if not isinstance(rec, dict) or clean_via(rec.get("via")) != rec.get("via"):
        return NO_RECORD_TEXT
    return describe(rec) or NO_RECORD_TEXT
