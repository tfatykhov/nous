"""F113 fixed finish checks: two rules in code run over a background task's validated final report.

Pure functions over the report's text, the calls its session recorded (``Evidence`` items, as
``AgentRunner.session_evidence`` returns them) and whether a file exists. No model call, no database.

- ``named_file_missing``: a sentence of the report says a file under the workspace was written
  (created, wrote, written, saved, generated, exported) and that file does not exist.
- ``effects_all_failed``: the task tried to change something (a write, a send, a shell command
  that writes, an MCP tool), every such call failed or was refused, and the report does not say
  that anything failed.

``evaluate`` runs both and returns a ``FinishVerdict``; ``record_of`` turns it into the record kept on
the task's row (check names and labels only, never text of the report or of the calls).
"""

from __future__ import annotations

import logging
import os
import posixpath
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from nous.cognitive.claim_verifier import Evidence
from nous.cognitive.execution_ledger import EVIDENCE_TRUNCATED

logger = logging.getLogger(__name__)

NAMED_FILE_MISSING = "named_file_missing"
EFFECTS_ALL_FAILED = "effects_all_failed"
# The order check names are listed in a verdict.
CHECK_NAMES: tuple[str, ...] = (NAMED_FILE_MISSING, EFFECTS_ALL_FAILED)

RECORD_VERSION = 1
MAX_TEXT_CHARS = 4000  # how much of the report (summary, then findings) the checks read
MAX_PATHS = 10  # the first distinct paths a report names that are examined
MAX_DETAIL_CHARS = 200

Exists = Callable[[str], bool | None]

# A sentence ends at a full stop followed by a space or the end, at ! or ?, at a line break or at a bullet.
_SENTENCE_END = re.compile(r"(?<=\.)(?:\s+|$)|[!?\n\r]+|\s*\u2022\s*")
_BULLET = re.compile(r"^\s*(?:[-*\u2022]|\d+[.)])\s+")
_WRITE_VERB = re.compile(r"\b(?:created|wrote|written|saved|generated|exported)\b", re.IGNORECASE)
_HEDGE = re.compile(
    r"\b(?:no|not|never|will|would|should|could|may|might|need|needs|to\s+be|if|unless|instead"
    r"|removed|deleted|moved|renamed|cannot)\b|n['\u2019]t\b",
    re.IGNORECASE,
)
_BACKTICKED = re.compile(r"`([^`\s]+)`")
_PATH_SHAPE = re.compile(r"[^\s]*/[^\s/]*\.[A-Za-z0-9]{1,8}")
_NOT_A_PATH = re.compile(r"[~*?\[\]{}$<>`]|://")
_LEADING = "(\"'"
_TRAILING = ".,;:)\"'"

# Shell programs after which a file the report names may be gone for a reason the session
# recorded: they remove, move or rewrite files, or run commands this module does not read.
_REMOVERS = frozenset({"rm", "mv", "rmdir", "unlink", "shred", "git", "find", "rsync", "tar", "docker"})
_STRING_RUNNERS = frozenset({"bash", "sh", "zsh", "dash", "ksh", "su", "eval"})
_JOB_RUNNER_SUFFIX = "runner.sh"

# Tools that change something outside the conversation. Memory and bookkeeping tools are not counted.
_EFFECT_TOOLS = frozenset({"write_file", "bash", "run_python", "send_email", "send_file"})
_FAILURE_WORDS = re.compile(
    r"\b(?:fail|unable|could not|couldn't|cannot|can't|can not|block|refus|reject|denied|deny|not allowed"
    r"|not permitted|permission|approval|approve|error|did not|didn't|was not|wasn't|not able|unsuccessful"
    r"|timed out|timeout|no access)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Finding:
    """One check that found a mismatch, with a short description (never stored on the row)."""

    check: str
    detail: str


@dataclass(frozen=True)
class FinishVerdict:
    """``clean``, ``flagged`` (``failed`` names the checks) or ``not_checked`` (``why`` says why)."""

    verdict: str
    failed: tuple[str, ...] = ()
    why: str | None = None
    findings: tuple[Finding, ...] = ()


def finish_checks_mode_of(settings: Any) -> str:
    """``NOUS_FINISH_CHECKS_MODE``; settings without the field (test namespaces) read as ``off``."""
    return getattr(settings, "finish_checks_mode", "off")


def fs_exists(path: str) -> bool | None:
    """Whether ``path`` exists, without following a link or reading anything; None when it cannot be told."""
    try:
        os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except (OSError, ValueError):
        return None
    return True


# ---------------------------------------------------------------------------
# named_file_missing
# ---------------------------------------------------------------------------


def _sentences(text: str) -> list[str]:
    return [_BULLET.sub("", part).strip() for part in _SENTENCE_END.split(text) if part and part.strip()]


def _clean_token(token: str) -> str | None:
    token = token.lstrip(_LEADING).rstrip(_TRAILING)
    if not _PATH_SHAPE.fullmatch(token) or _NOT_A_PATH.search(token):
        return None
    return token


def _written_paths(text: str) -> list[str]:
    """The distinct path tokens of sentences that say a file was written, in order, at most ``MAX_PATHS``."""
    found: list[str] = []
    for sentence in _sentences(text):
        tokens = [m.group(1) for m in _BACKTICKED.finditer(sentence)]
        prose = _BACKTICKED.sub(" ", sentence)
        words = prose.split()
        tokens += words
        paths = [p for p in (_clean_token(t) for t in tokens) if p is not None]
        # The verb and the hedge are read from the words around the paths, not from inside them.
        rest = " ".join(w for w in words if _clean_token(w) is None)
        if not paths or not _WRITE_VERB.search(rest) or _HEDGE.search(rest):
            continue
        for path in paths:
            if path not in found:
                found.append(path)
                if len(found) == MAX_PATHS:
                    return found
    return found


def _under(path: str, workspace: str) -> bool:
    prefix = workspace.rstrip("/") + "/"
    return path.startswith(prefix)


def _write_file_paths(evidence: Sequence[Evidence]) -> set[str]:
    paths = set()
    for item in evidence:
        if item.tool_name != "write_file":
            continue
        for key in ("path", "file_path"):
            value = item.args.get(key)
            if value:
                paths.add(posixpath.normpath(value))
    return paths


def _program_base(program: str) -> str:
    name = program.replace("\\", "/").rsplit("/", 1)[-1]
    return name[:-4].lower() if name.lower().endswith(".exe") else name


def _could_remove(item: Evidence) -> bool:
    """Whether a recorded call may have removed, moved or hidden a file."""
    if item.tool_name == "run_python":
        return True
    if item.tool_name != "bash":
        return False
    command = item.args.get("command") or item.args.get("cmd") or ""
    runs = item.runs
    if runs is None or EVIDENCE_TRUNCATED in command:
        return True
    for program, _args, _certain in runs:
        if "$" in program or "`" in program:  # a program the parser could not read
            return True
        base = _program_base(program)
        if base in _REMOVERS or base in _STRING_RUNNERS or base.endswith(_JOB_RUNNER_SUFFIX):
            return True
    return False


def named_file_missing(text: str, evidence: Sequence[Evidence], workspace_dir: str, exists: Exists) -> list[Finding]:
    """Files the report says were written under the workspace that do not exist.

    A path is checked when it is absolute and under the workspace, or when it is relative and equal
    to the path of a ``write_file`` call of the session (then resolved against the workspace, as
    ``write_file`` resolves it). Nothing is reported when any call of the session may have removed
    or moved a file (``_could_remove``), nor when ``exists`` cannot tell.
    """
    paths = _written_paths(text)
    if not paths or any(_could_remove(item) for item in evidence):
        return []
    workspace = posixpath.normpath(workspace_dir)
    written = _write_file_paths(evidence)
    findings: list[Finding] = []
    for named in paths:
        if named.startswith("/"):
            resolved = posixpath.normpath(named)
        elif posixpath.normpath(named) in written:
            resolved = posixpath.normpath(posixpath.join(workspace, named))
        else:
            continue
        if not _under(resolved, workspace):
            continue
        if exists(resolved) is False:
            detail = f"the report says {named} was written; it does not exist"
            findings.append(Finding(NAMED_FILE_MISSING, detail[:MAX_DETAIL_CHARS]))
    return findings


# ---------------------------------------------------------------------------
# effects_all_failed
# ---------------------------------------------------------------------------


def _is_effect_call(item: Evidence) -> bool:
    return item.side_effect != "none" and (item.tool_name in _EFFECT_TOOLS or item.tool_name.startswith("mcp__"))


def effects_all_failed(text: str, evidence: Sequence[Evidence]) -> list[Finding]:
    """Every call that tried to change something failed or was refused, and the report names no failure."""
    effects = [item for item in evidence if _is_effect_call(item)]
    if not effects or any(item.ok for item in effects):
        return []
    if _FAILURE_WORDS.search(text.replace("\u2019", "'")):
        return []
    detail = f"{len(effects)} attempts to change something all failed or were refused; the report does not say so"
    return [Finding(EFFECTS_ALL_FAILED, detail[:MAX_DETAIL_CHARS])]


# ---------------------------------------------------------------------------
# The verdict and its record
# ---------------------------------------------------------------------------


def evaluate(text: str, evidence: Sequence[Evidence], workspace_dir: str, exists: Exists) -> FinishVerdict:
    """Run both checks over the first ``MAX_TEXT_CHARS`` of ``text``.

    ``clean`` when neither finds anything, ``flagged`` otherwise. A check that raises makes the
    verdict ``not_checked`` / ``check_error``; the error is logged by its class name only.
    """
    text = text[:MAX_TEXT_CHARS]
    findings: list[Finding] = []
    try:
        findings += named_file_missing(text, evidence, workspace_dir, exists)
        findings += effects_all_failed(text, evidence)
    except Exception as exc:
        logger.warning("F113: a finish check failed (%s)", type(exc).__name__)
        return FinishVerdict("not_checked", why="check_error")
    if not findings:
        return FinishVerdict("clean")
    failed = tuple(name for name in CHECK_NAMES if any(f.check == name for f in findings))
    return FinishVerdict("flagged", failed=failed, findings=tuple(findings))


def record_of(verdict: FinishVerdict, mode: str) -> dict[str, Any]:
    """The ``finish_check`` record kept in ``heart.subtasks.metadata``: labels only."""
    return {
        "v": RECORD_VERSION,
        "mode": mode,
        "verdict": verdict.verdict,
        "failed": list(verdict.failed),
        "why": verdict.why,
    }


def finish_check_counts(records: Iterable[Any], mode: str) -> dict[str, Any]:
    """Counts of ``finish_check`` records, for a dashboard block over completed tasks.

    ``records`` are the ``metadata->'finish_check'`` values of the completed rows to count (None for a
    row that has none, which is counted nowhere). ``checked`` = verdict ``clean`` or ``flagged``;
    ``flagged`` = verdict ``flagged``; ``not_checked`` = verdict ``not_checked``.
    """
    checked = flagged = not_checked = 0
    for record in records:
        verdict = record.get("verdict") if isinstance(record, Mapping) else None
        if verdict in ("clean", "flagged"):
            checked += 1
            flagged += verdict == "flagged"
        elif verdict == "not_checked":
            not_checked += 1
    return {"checked": checked, "flagged": flagged, "not_checked": not_checked, "mode": mode}
