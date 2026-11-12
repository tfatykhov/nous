"""F111 PR A: the turn-close promise gate.

A reply that says "I'll do X later" leaves nothing behind unless the turn created an artifact. At turn close the gate
finds the reply's promises (``PromiseDetector``, deterministic, stage 1 of spec D.4), checks which ones an artifact
created in the same turn backs (``BackingResolver``: a ``schedule_task``, ``dag_create``, ``spawn_task``,
``propose_action``, ``commit`` or ``heartbeat_check_create`` result with a parseable id), writes one
``brain.commitments`` row per unbacked promise and one ``nous_system.promise_gate_log`` row per turn, and in ``warn``
returns one footer line for the caller to show (never to store).

What it never does: send anything, create a schedule, DAG or spawn, or act on parsed prose. ``enforce`` (re-entry
with a repair message) is PR B; here it is handled as ``warn``. The caller (``AgentRunner._promise_gate_step``) wraps
every call: an exception anywhere in it is logged at WARNING and the reply is delivered unchanged.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from nous.brain.commitments import MAX_DUE_DAYS, SOURCE_KIND_BY_CONTEXT, CommitmentStore, normalise_what

logger = logging.getLogger(__name__)

DETECTOR_VERSION = "stage1-v1"
SENTENCE_MAX = 500
# The detector reads the end of a reply's prose only (where a promise is made): it runs on the event loop, where the
# gate's timeout cannot stop it, so its cost must not grow with NOUS_MAX_TOKENS. The code, quotes and quote blocks are
# stripped from the whole reply first (linear patterns), so a cut never lands inside a fence.
SCAN_MAX_CHARS = 32_000

# ---------------------------------------------------------------------------
# Stage 1 detector (spec D.4)
# ---------------------------------------------------------------------------

_APOS = "['’]"
# An agent-subject future form. "Doing X" and "next step" count only at the start of a sentence.
_FUTURE_RE = re.compile(
    rf"\b(?:I{_APOS}ll|I\s+will|I{_APOS}m\s+going\s+to|I\s+am\s+going\s+to|I\s+plan\s+to|I{_APOS}m\s+planning\s+to"
    rf"|we{_APOS}ll|we\s+will|I\s+intend\s+to"
    r"|will\s+be\s+(?:re-?)?(?:launched|started|scheduled|merged|sent|run|queued|deployed|applied|removed|retried))\b"
    r"|^\s*(?:next\s+step\b|the\s+next\s+step\s+is\b|doing\s+\w+)",
    re.IGNORECASE,
)

# Strong deferral markers: any one suffices.
_STRONG_RES = [
    # A number is a time only with am/pm or a zone: "by 10 percent" and "at 3:1" are not deadlines.
    re.compile(r"\b(?:at|by|around|before)\s+\d{1,2}(?::\d{2})?\s*(?:am|pm|z|utc|gmt)\b", re.I),
    re.compile(r"\b\d{1,2}:\d{2}\s*(?:z|utc|gmt|am|pm)\b", re.I),
    re.compile(r"\b(?:tomorrow|tonight|this\s+(?:evening|afternoon|weekend)|next\s+(?:week|month))\b", re.I),
    re.compile(
        r"\b(?:by|on|this|next|until)\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday"
        r"|end\s+of\s+(?:day|week)|eod|eow)\b",
        re.I,
    ),
    re.compile(r"\b(?:on|by|before|until)\s+\d{4}-\d{2}-\d{2}\b", re.I),  # not "the 2026-11-10 snapshot"
    re.compile(r"\bin\s+(?:\d+|an?|one|two|a\s+few|half\s+an)\s+(?:minute|min|hour|hr|day|week)s?\b", re.I),
    re.compile(r"\bwithin\s+(?:the|an?|\d+)\s*(?:minute|min|hour|hr|day)s?\b", re.I),
    re.compile(r"\bnext\s+step\b", re.I),
]
# What may follow a future form and make it no promise of the agent's: a negation ("I will not restart it"), filler
# ("I'll be honest", "we'll see"), and for the passive form a third party ("will be sent by the vendor").
_NEGATED_RE = re.compile(r"\s+(?:not|never|no\s+longer)\b", re.I)
_FILLER_RE = re.compile(
    r"\s+(?:be\s+(?:honest|frank|candid|clear|brief|upfront)|bet)\b"
    r"|\s+see\b(?=\s*(?:[,.;:!]|$|(?:tomorrow|later|then|how|what|if|whether)\b))",  # not "see that it ships"
    re.I,
)
_THIRD_PARTY_RE = re.compile(
    r"\s+by\s+(?!(?:the\s+)?(?:end|close|start)\b|eod\b|eow\b|tomorrow|tonight|today|noon|midnight|next\b|this\b"
    r"|monday|tuesday|wednesday|thursday|friday|saturday|sunday|\d"
    r"|(?:me|us|e-?mail|mail|slack|telegram|ci|cron|the\s+pipeline)\b)\w",  # a means, or the agent itself
    re.I,
)
# "Next step" whose subject is the owner ("Next step: you approve the PR", "Next step is yours"): not a promise.
_NEXT_STEP_YOURS_RE = re.compile(
    r"^\s*(?:the\s+)?next\s+step\s*(?:is\b|[:,])?\s*(?:(?:for|on)\s+)?(?:you|yours|your)\b", re.I
)


def _agent_future_form(sentence: str) -> bool:
    """True when some future form in the sentence is the agent's own: not negated, not filler, and (passive) not
    done by someone else."""
    for m in _FUTURE_RE.finditer(sentence):
        rest = sentence[m.end() :]
        if _NEGATED_RE.match(rest) or _FILLER_RE.match(rest):
            continue
        if m.group(0).lower().startswith("will") and _THIRD_PARTY_RE.match(rest):
            continue
        return True
    return False


# Consent by silence: strong, and recorded as consent='silence'.
_SILENCE_RE = re.compile(
    r"\b(?:unless\s+you\s+(?:object|say\s+otherwise|prefer\s+otherwise|tell\s+me(?:\s+otherwise)?|stop\s+me"
    rf"|say\s+no|disagree)|if\s+you\s+don{_APOS}t\s+object|if\s+I\s+don{_APOS}t\s+hear"
    r"|absent\s+(?:an\s+)?objection)\b",
    re.IGNORECASE,
)
# Weak markers count only with an action verb.
_WEAK_RE = re.compile(r"\b(?:today|later|then|when|after|once|soon|shortly|afterwards|next)\b", re.I)
# The action verbs a weak marker needs (spec D.4; a module constant to tune from shadow data).
ACTION_VERBS: tuple[str, ...] = (
    "launch", "relaunch", "start", "restart", "schedule", "run", "re-run", "rerun", "merge", "send", "email", "build",
    "spawn", "queue", "kick off", "follow up", "check back", "retry", "apply", "remove", "delete", "create", "deploy",
    "fix", "draft", "resume", "look into",
)  # fmt: skip
_VERB_RE = re.compile(
    r"\b(?:"
    + "|".join(re.escape(v).replace(r"\ ", r"(?:s|es|ed|ing)?\s+") for v in sorted(ACTION_VERBS, key=len, reverse=True))
    + r")(?:s|es|ed|d|ing|ning)?\b",
    re.IGNORECASE,
)
# An offer to the owner is not a promise ("if you want, I can launch it").
_OFFER_RE = re.compile(
    rf"\b(?:if\s+you(?:{_APOS}d)?\s+(?:want|like|prefer|wish)|want\s+me\s+to|should\s+I|shall\s+I|would\s+you\s+like"
    r"|do\s+you\s+want)",
    re.IGNORECASE,
)
# The next move is the owner's: stored as kind='awaiting_user', never pinged (spec R3).
_AWAITING_USER_RE = re.compile(
    rf"\b(?:once|when|after|if|as\s+soon\s+as)\s+you(?:{_APOS}re|\s+are|{_APOS}ve|\s+have)?\s+"
    r"(?:confirm|approve|decide|ready|say|reply|answer|give|agree|ok|okay|sign|tell|let\s+me\s+know|green-?light"
    r"|review|send|choose|pick)",
    re.IGNORECASE,
)

_FENCE_RE = re.compile(r"```.*?(?:```|\Z)", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
# Neither quote class may contain an opening mark: "“[^”]*”" rescanned the line from every unclosed “ (quadratic).
_QUOTED_RE = re.compile(r"\"[^\"\n]*\"|“[^“”\n]*”")
# A sentence ends at . ! ? and a space, but not after an abbreviation ("Dr. Smith", "e.g. bash", "No. 3"): split
# there and neither half of the promise has both its future form and its marker.
_SENTENCE_SPLIT_RE = re.compile(
    r"(?<=[.!?])(?<!\bdr\.)(?<!\bmr\.)(?<!\bms\.)(?<!\bmrs\.)(?<!\bprof\.)(?<!\bst\.)(?<!\bvs\.)(?<!\be\.g\.)"
    r"(?<!\bi\.e\.)"
    r"(?<!\bno\.(?=\s\d))\s+|\n+",
    re.IGNORECASE,
)

# Due-time extraction for the cases the detector can resolve without guessing; anything else gets the default.
_REL_RE = re.compile(r"\bin\s+(\d+|an?|one|two|half\s+an)\s+(minute|min|hour|hr|day|week)s?\b", re.I)
_WITHIN_HOUR_RE = re.compile(r"\bwithin\s+(?:the|an?)\s+hour\b", re.I)
_CLOCK_RE = re.compile(r"\b(\d{1,2}):(\d{2})\s*(z|utc|gmt)?\b", re.I)
# A day word binds to a clock time anywhere in its sentence ("Tomorrow I'll relaunch at 18:15 UTC"); a possessive
# ("tomorrow's run") names a thing, not the day of the deadline.
_DAY_RE = re.compile(r"\b(today|tonight|tomorrow)\b(?!['’]s)", re.I)
# A time of day the parser does not read (am/pm, or a colon time without a zone): with one in the sentence, a day
# word alone does not make a parsed deadline ("at 6 PM UTC tomorrow" is not "now + 1 day").
_UNREAD_TIME_RE = re.compile(r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)|\b\d{1,2}:\d{2}\b", re.I)
_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
# A date this parser cannot bind to a clock time: with one in the sentence the deadline is the unparsed default,
# never a parsed "today" (a weekday, "next week", a month-day date, "the day after tomorrow").
# Whole month names or their abbreviations ("2 decision PRs" names no December).
_MONTH = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?"
    r"|nov(?:ember)?|dec(?:ember)?)\b"
)
_UNBOUND_DATE_RE = re.compile(
    r"\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|weekend|next\s+(?:week|month|year)"
    r"|end\s+of\s+(?:the\s+)?(?:week|month)|eow|day\s+after\s+tomorrow"
    rf"|{_MONTH}\.?\s+\d{{1,2}}(?:st|nd|rd|th)?\b|\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?{_MONTH}\b"
    r"|\d{1,2}/\d{1,2})",
    re.I,
)
_UNIT_MINUTES = {"minute": 1, "min": 1, "hour": 60, "hr": 60, "day": 1440, "week": 10080}
# A relative time is capped here (the gate clamps the row to MAX_DUE_DAYS anyway): "in 3000000 days" is no
# OverflowError, which would lose every promise of the reply.
_REL_MAX_MINUTES = (MAX_DUE_DAYS + 1) * 1440
_WORD_NUM = {"a": 1, "an": 1, "one": 1, "two": 2}


@dataclass(frozen=True)
class Promise:
    """One promise sentence."""

    text: str  # the sentence, at most SENTENCE_MAX characters
    what: str  # the normalised one-liner stored in brain.commitments.what
    due_at: datetime
    due_parsed: bool  # False: the default due (no time in the sentence the detector could resolve)
    consent: Literal["none", "silence"]
    kind: Literal["action", "awaiting_user"]


def _strip_non_prose(text: str) -> str:
    """The reply without code fences, inline code, quote-block lines and quoted strings."""
    text = _FENCE_RE.sub(" ", text)
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith(">")]
    text = "\n".join(lines)
    text = _INLINE_CODE_RE.sub(" ", text)
    return _QUOTED_RE.sub(" ", text)


def _due_at(sentence: str, now: datetime, default_minutes: int) -> tuple[datetime, bool]:
    """The deadline a sentence names, else ``now + default_minutes``. Relative times count from ``now`` (so a replay
    of a stored reply reads as it did then); a clock time is read only with an explicit UTC marker (no owner zone is
    known to the code, and a guessed zone would move the deadline by hours)."""
    m = _REL_RE.search(sentence)
    if m:
        raw, unit_minutes = m.group(1).lower(), _UNIT_MINUTES[m.group(2).lower()]
        if raw.startswith("half"):
            return now + timedelta(minutes=30), True
        amount = int(raw) if raw.isdigit() else _WORD_NUM.get(raw, 1)
        if amount > 0:
            return now + timedelta(minutes=min(amount * unit_minutes, _REL_MAX_MINUTES)), True
    if _WITHIN_HOUR_RE.search(sentence):
        return now + timedelta(hours=1), True
    default = (now + timedelta(minutes=default_minutes), False)
    if _UNBOUND_DATE_RE.search(sentence):
        return default
    iso = _ISO_DATE_RE.search(sentence)
    days = {d.group(1).lower().replace("tonight", "today") for d in _DAY_RE.finditer(sentence)}
    m = _CLOCK_RE.search(sentence)
    if m and m.group(3):
        hour, minute = int(m.group(1)), int(m.group(2))
        if hour >= 24 or minute >= 60:
            return default
        if iso:
            # An explicit date binds to the clock time ("on 2026-11-20 at 18:15 UTC").
            try:
                at = datetime(*(int(g) for g in iso.groups()), hour, minute, tzinfo=UTC)
            except ValueError:
                return default
            return (at, True) if at > now else default
        if len(days) > 1:
            return default  # "today" and "tomorrow" both: which one the time belongs to is a guess
        at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if days == {"tomorrow"}:
            return at + timedelta(days=1), True
        if at > now:
            return at, True
        if days == {"today"}:
            # "today" at a time already past is unreadable: the default, not tomorrow (a deadline a day late would
            # hide the miss).
            return default
        return at + timedelta(days=1), True  # a bare clock time is its next occurrence
    if iso or _UNREAD_TIME_RE.search(sentence):
        return default  # a date without a time it binds to, or a time it cannot read: either is a guess
    if days == {"tomorrow"}:
        return now + timedelta(days=1), True
    return default


class PromiseDetector:
    """Stage 1 (spec D.4): a sentence is a promise iff it has an agent-subject future form and a deferral marker
    (a strong one, or a weak one with an action verb). Questions, offers, code, quotes and quote blocks are not."""

    def __init__(self, default_due_minutes: int = 60) -> None:
        self._default_due_minutes = default_due_minutes

    def scan(self, response_text: str, now: datetime | None = None) -> list[Promise]:
        """The promises in the last ``SCAN_MAX_CHARS`` of a reply's prose, one per sentence, in order (empty for
        none)."""
        if not response_text:
            return []
        now = now or datetime.now(UTC)
        promises: list[Promise] = []
        for raw in _SENTENCE_SPLIT_RE.split(_strip_non_prose(response_text)[-SCAN_MAX_CHARS:]):
            sentence = raw.strip(" \t-*•")
            if not sentence or sentence.endswith("?"):
                continue
            probe = _NEXT_STEP_YOURS_RE.sub("", sentence)  # the owner's next step is no future form or marker
            if not _agent_future_form(probe) or _OFFER_RE.search(sentence):
                continue
            silence = _SILENCE_RE.search(sentence) is not None
            strong = silence or any(r.search(probe) for r in _STRONG_RES)
            awaiting = _AWAITING_USER_RE.search(sentence) is not None
            weak = _WEAK_RE.search(sentence) is not None or awaiting
            if not (strong or (weak and _VERB_RE.search(sentence))):
                continue
            due_at, parsed = _due_at(sentence, now, self._default_due_minutes)
            clipped = sentence[:SENTENCE_MAX]
            promises.append(
                Promise(
                    text=clipped,
                    what=normalise_what(clipped),
                    due_at=due_at,
                    due_parsed=parsed,
                    consent="silence" if silence else "none",
                    kind="awaiting_user" if awaiting and not silence else "action",
                )
            )
        return promises


# ---------------------------------------------------------------------------
# Backing (spec D.4): by artifact id only
# ---------------------------------------------------------------------------

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_HEX8 = r"[0-9a-f]{8}"
# The success text of each backing tool (read from the handlers; tests pin each shape).
_ID_PATTERNS: dict[str, tuple[str, list[re.Pattern[str]]]] = {
    "schedule_task": ("schedule", [re.compile(rf"Schedule created\.\s*\n\s*ID: ({_UUID})", re.I)]),
    "spawn_task": (
        "subtask",
        [re.compile(rf"Subtask spawned\.\s*\n\s*ID: ({_UUID})", re.I), re.compile(rf"\[Subtask ({_HEX8}) completed\]")],
    ),
    "dag_create": ("dag", [re.compile(rf"Created DAG '.*?' \(({_HEX8})\)", re.S)]),
    "propose_action": ("proposal", [re.compile(rf"Staged proposal ({_HEX8}) ")]),
    "heartbeat_check_create": ("check", [re.compile(rf"Created dynamic check: .*?\"id\": \"({_UUID})\"", re.S)]),
    "commit": ("commitment", [re.compile(r"Commitment (C-\d+) recorded")]),
}
BACKING_TOOLS: frozenset[str] = frozenset(_ID_PATTERNS)
# The arguments whose words a backing call is matched on. propose_action's are tool, arguments (an object: its values
# are read) and rationale (nous/handlers/continuation_runner.py PROPOSE_ACTION_SCHEMA).
_MATCH_ARGS = (
    "what", "task", "intent", "name", "description", "goal", "prompt", "title", "summary", "check_prompt",
    "tool", "arguments", "rationale",
)  # fmt: skip


def _match_text(args: dict[str, Any]) -> str:
    """The text of a backing call's matched arguments; a nested object (propose_action's ``arguments``) is read one
    level down."""
    parts: list[str] = []
    for key in _MATCH_ARGS:
        value = args.get(key)
        if isinstance(value, dict):
            parts.extend(str(v) for v in value.values())
        elif value:
            parts.append(str(value))
    return " ".join(parts)


_STOP = frozenset(
    "the and for with that this then when once after today tomorrow tonight unless you object later will ill i'll "
    "i'm going plan next step from into your our are was have has not nothing yet soon just also all any its it's "
    "there their them they what which who about over under before until within hour hours minute minutes".split()
)
_WORD_RE = re.compile(r"[a-z0-9][a-z0-9'_-]*")


@dataclass(frozen=True)
class Artifact:
    tool: str
    kind: str  # schedule | subtask | dag | proposal | check | commitment
    id: str
    words: frozenset[str]


def _words(text: str) -> frozenset[str]:
    out = set()
    for w in _WORD_RE.findall(text.lower()):
        w = w.strip("'-_")
        if len(w) < 3 or w in _STOP:
            continue
        out.add(w[:-1] if len(w) > 4 and w.endswith("s") else w)
    return frozenset(out)


def parse_artifact(tool_name: str, result: str | None) -> tuple[str, str] | None:
    """``(kind, id)`` from a backing tool's success text, or None when the text carries no id this parser knows."""
    spec = _ID_PATTERNS.get(tool_name)
    if spec is None or not result:
        return None
    kind, patterns = spec
    for pattern in patterns:
        m = pattern.search(result)
        if m:
            return kind, m.group(1)
    return None


class BackingResolver:
    """Splits promises into backed and unbacked by the artifacts this turn created.

    One backing call in the turn backs every promise (a schedule, a DAG or a spawn may well do all of it), except a
    ``commit``, whose ``what`` names one obligation; otherwise a promise is backed by a call whose argument words
    overlap the promise's by at least ``MIN_OVERLAP``. ``record_decision``, ``learn_fact``, ``create_censor``, reads,
    error results and results without a parseable id never back anything."""

    MIN_OVERLAP = 0.3

    def artifacts(self, tool_results: list[Any]) -> list[Artifact]:
        """The artifacts this turn created: one per backing call with a non-error result and a parseable id."""
        found: list[Artifact] = []
        for tr in tool_results:
            name = getattr(tr, "tool_name", None)
            if name not in BACKING_TOOLS or getattr(tr, "error", None):
                continue
            parsed = parse_artifact(name, getattr(tr, "result", None))
            if parsed is None:
                logger.debug("promise gate: no id in the %s result; it backs nothing", name)
                continue
            args = getattr(tr, "arguments", None) or {}
            words = _words(_match_text(args))
            found.append(Artifact(tool=name, kind=parsed[0], id=parsed[1], words=words))
        return found

    def split(
        self, promises: list[Promise], tool_results: list[Any]
    ) -> tuple[list[tuple[Promise, Artifact]], list[Promise]]:
        """``(backed, unbacked)``: each backed promise with the artifact that backs it."""
        artifacts = self.artifacts(tool_results)
        backed: list[tuple[Promise, Artifact]] = []
        unbacked: list[Promise] = []
        for promise in promises:
            match = self._match(promise, artifacts)
            if match is None:
                unbacked.append(promise)
            else:
                backed.append((promise, match))
        return backed, unbacked

    def _match(self, promise: Promise, artifacts: list[Artifact]) -> Artifact | None:
        if not artifacts:
            return None
        if len(artifacts) == 1 and artifacts[0].tool != "commit":
            return artifacts[0]
        pw = _words(promise.what)
        if not pw:
            return None
        best, best_score = None, 0.0
        for art in artifacts:
            score = len(pw & art.words) / len(pw)
            if score > best_score:
                best, best_score = art, score
        return best if best_score >= self.MIN_OVERLAP else None


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

GateMode = Literal["off", "shadow", "warn", "enforce"]


@dataclass
class GateOutcome:
    """What the gate decided for one turn. ``footer`` is for display only: the caller never stores it."""

    outcome: Literal["clean", "footer", "error", "off"] = "clean"
    detected: int = 0
    backed: int = 0
    unbacked: int = 0
    footer: str | None = None
    labels: list[str] = field(default_factory=list)  # C-ids of the rows written this turn
    latency_ms: int = 0  # stage 1 (scan and resolve) only


def source_kind_for(path: str, context_kind: str | None) -> str:
    """The ledger's ``source_kind`` for a turn: its background kind, else the chat path it ran on."""
    if context_kind in SOURCE_KIND_BY_CONTEXT:
        return SOURCE_KIND_BY_CONTEXT[context_kind]
    return "chat_stream" if path == "stream" else "chat_turn"


def _due_phrase(due_at: datetime, now: datetime) -> str:
    """'in 60 min (20:15 UTC)': relative first, so it reads right in any zone."""
    minutes = max(0, round((due_at - now).total_seconds() / 60))
    rel = f"in {minutes} min" if minutes < 120 else f"in {round(minutes / 60)} h"
    if minutes >= 1440:
        return f"{due_at.astimezone(UTC):%Y-%m-%d %H:%M} UTC"
    return f"{rel} ({due_at.astimezone(UTC):%H:%M} UTC)"


def render_footer(labels: list[str], due_at: datetime, now: datetime) -> str:
    """One line, whatever the number of rows (spec: at most one footer per turn). ``due_at`` is the first row's, as
    stored (a far promise is clamped). No 'committed' wording: the claim verifier's vcs_commit check reads that
    word."""
    ids = ", ".join(labels)
    plural = "commitments" if len(labels) > 1 else "commitment"
    return (
        f"⏱ Recorded as {plural} {ids}, due {_due_phrase(due_at, now)}; nothing is scheduled for it yet. "
        f"/drop {labels[0]} to cancel."
    )


# The report argument of each terminal tool (TERMINAL_EXTRA_TOOLS in nous/api/runner.py). A turn that ends with one
# returns a fixed marker ("Report submitted.") as its text, so the gate reads the report the call carried.
_REPORT_ARGS: dict[str, str] = {"submit_final_report": "summary", "resolve_intention": "note"}


def turn_text(response_text: str, tool_results: list[Any]) -> str:
    """What the gate scans for a turn: the report of the turn's last successful terminal-tool call (a subtask's
    ``submit_final_report`` summary, a continuation's ``resolve_intention`` note), else the reply text."""
    for tr in reversed(tool_results):
        key = _REPORT_ARGS.get(getattr(tr, "tool_name", None) or "")
        if key is None or getattr(tr, "error", None):
            continue
        report = (getattr(tr, "arguments", None) or {}).get(key)
        if isinstance(report, str) and report.strip():
            return report
    return response_text


class PromiseGate:
    """Detect, resolve, record. Async because it writes; the caller bounds and guards it."""

    def __init__(self, store: CommitmentStore, settings: Any) -> None:
        self._store = store
        self._settings = settings
        self.detector = PromiseDetector(default_due_minutes=settings.promise_gate_default_due_minutes)
        self.resolver = BackingResolver()

    @property
    def mode(self) -> GateMode:
        """The effective mode: ``off`` whenever the gate is disabled."""
        if not self._settings.promise_gate_enabled:
            return "off"
        return self._settings.promise_gate_mode

    async def run(
        self,
        response_text: str,
        tool_results: list[Any],
        *,
        path: Literal["stream", "run_turn"],
        session_id: str | None,
        turn_id: str | None,
        context_kind: str | None,
        now: datetime | None = None,
    ) -> GateOutcome:
        """Detect, resolve and record one turn (rows and log row in one transaction); the footer in warn."""
        mode = self.mode
        if mode == "off":
            return GateOutcome(outcome="off")
        now = now or datetime.now(UTC)
        started = time.perf_counter()
        promises = self.detector.scan(turn_text(response_text, tool_results), now=now)
        # The same sentence twice is one promise: the store would write one row for it (one dedupe key), and the log
        # and the footer must not count or name it twice.
        unique: dict[str, Promise] = {}
        for promise in promises:
            unique.setdefault(promise.what.lower(), promise)
        promises = list(unique.values())
        backed, unbacked = self.resolver.split(promises, tool_results)
        result = GateOutcome(
            detected=len(promises),
            backed=len(backed),
            unbacked=len(unbacked),
            latency_ms=round((time.perf_counter() - started) * 1000),
        )
        if mode == "enforce":
            # PR A has no re-entry: enforce acts as warn (spec scope decision 8).
            logger.info("promise gate: mode enforce is handled as warn until the re-entry ships (PR B)")
        source_kind = source_kind_for(path, context_kind)
        latest = now + timedelta(days=MAX_DUE_DAYS) - timedelta(minutes=1)  # a far promise is clamped, not lost
        specs = [
            {
                "source_kind": source_kind,
                "detected_by": "gate",
                "promise_text": promise.text,
                "what": promise.what,
                "due_at": min(promise.due_at, latest),
                "session_id": session_id,
                "turn_id": turn_id,
                "kind": promise.kind,
                "consent": promise.consent,
                "grace_seconds": self._settings.promise_gate_grace_seconds,
                "detector_version": DETECTOR_VERSION,
                "evidence": {"due_parsed": promise.due_parsed},
                "now": now,
            }
            for promise in unbacked
        ]
        footer_wanted = mode in ("warn", "enforce") and any(p.kind == "action" for p in unbacked)
        result.outcome = "footer" if footer_wanted else "clean"
        rows = await self._store.record_gate_turn(
            specs,
            {
                "path": path,
                "mode": mode,
                "detected": result.detected,
                "backed": result.backed,
                "unbacked": result.unbacked,
                "outcome": result.outcome,
                "session_id": session_id,
                "turn_id": turn_id,
                "context_kind": context_kind,
                "latency_ms": result.latency_ms,
            },
        )
        result.labels = [r.label for r in rows if r is not None]
        shown = [(p, r) for p, r in zip(unbacked, rows, strict=True) if r is not None and p.kind == "action"]
        if footer_wanted and shown:
            result.footer = render_footer([r.label for _, r in shown], shown[0][1].due_at, now)
        return result

    async def log_error(
        self, *, path: str, session_id: str | None, turn_id: str | None, context_kind: str | None
    ) -> None:
        """Best effort: the gate failed for this turn; the row says so. Never raises."""
        try:
            await self._store.log_gate_turn(
                path=path,
                mode=self.mode,
                detected=0,
                backed=0,
                unbacked=0,
                outcome="error",
                session_id=session_id,
                turn_id=turn_id,
                context_kind=context_kind,
            )
        except Exception:
            logger.warning("promise gate: could not log the error row either", exc_info=True)
