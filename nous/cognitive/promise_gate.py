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
from nous.handlers.time_parser import parse_when

logger = logging.getLogger(__name__)

DETECTOR_VERSION = "stage1-v1"
SENTENCE_MAX = 500

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
    re.compile(r"\b(?:at|by|around|before)\s+\d{1,2}(?::\d{2})?\s*(?:am|pm|z|utc|gmt)?\b", re.I),
    re.compile(r"\b\d{1,2}:\d{2}\s*(?:z|utc|gmt|am|pm)?\b", re.I),
    re.compile(r"\b(?:tomorrow|tonight|this\s+(?:evening|afternoon|weekend)|next\s+(?:week|month))\b", re.I),
    re.compile(
        r"\b(?:by|on|this|next|until)\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday"
        r"|end\s+of\s+(?:day|week)|eod|eow)\b",
        re.I,
    ),
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
    re.compile(r"\bin\s+(?:\d+|an?|one|two|a\s+few|half\s+an)\s+(?:minute|min|hour|hr|day|week)s?\b", re.I),
    re.compile(r"\bwithin\s+(?:the|an?|\d+)\s*(?:minute|min|hour|hr|day)s?\b", re.I),
    re.compile(r"\bnext\s+step\b", re.I),
]
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
_QUOTED_RE = re.compile(r"\"[^\"\n]*\"|“[^”\n]*”")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")

# Due-time extraction for the cases the detector can resolve without guessing; anything else gets the default.
_REL_RE = re.compile(r"\bin\s+(\d+|an?|one|two|half\s+an)\s+(minute|min|hour|hr|day|week)s?\b", re.I)
_WITHIN_HOUR_RE = re.compile(r"\bwithin\s+(?:the|an?)\s+hour\b", re.I)
_CLOCK_RE = re.compile(r"\b(?:at|by|around|before)?\s*(\d{1,2}):(\d{2})\s*(z|utc|gmt)?\b", re.I)
_TOMORROW_RE = re.compile(r"\btomorrow\b", re.I)
_UNIT_NAMES = {"minute": "minute", "min": "minute", "hour": "hour", "hr": "hour", "day": "day", "week": "week"}
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
    """The deadline a sentence names, else ``now + default_minutes``. Relative times go through ``parse_when``, the
    resolver ``schedule_task`` uses; a clock time is read only with an explicit UTC marker (no owner zone is known
    to the code, and a guessed zone would move the deadline by hours)."""
    m = _REL_RE.search(sentence)
    if m:
        raw, unit = m.group(1).lower(), _UNIT_NAMES[m.group(2).lower()]
        if raw.startswith("half"):
            return now + timedelta(minutes=30), True
        amount = int(raw) if raw.isdigit() else _WORD_NUM.get(raw, 1)
        if amount > 0:
            return parse_when(f"in {amount} {unit}s"), True
    if _WITHIN_HOUR_RE.search(sentence):
        return now + timedelta(hours=1), True
    m = _CLOCK_RE.search(sentence)
    if m and m.group(3):
        hour, minute = int(m.group(1)), int(m.group(2))
        if hour < 24 and minute < 60:
            at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            return (at if at > now else at + timedelta(days=1)), True
    if _TOMORROW_RE.search(sentence):
        return now + timedelta(days=1), True
    return now + timedelta(minutes=default_minutes), False


class PromiseDetector:
    """Stage 1 (spec D.4): a sentence is a promise iff it has an agent-subject future form and a deferral marker
    (a strong one, or a weak one with an action verb). Questions, offers, code, quotes and quote blocks are not."""

    def __init__(self, default_due_minutes: int = 60) -> None:
        self._default_due_minutes = default_due_minutes

    def scan(self, response_text: str, now: datetime | None = None) -> list[Promise]:
        if not response_text:
            return []
        now = now or datetime.now(UTC)
        promises: list[Promise] = []
        for raw in _SENTENCE_SPLIT_RE.split(_strip_non_prose(response_text)):
            sentence = raw.strip(" \t-*•")
            if not sentence or sentence.endswith("?"):
                continue
            if not _FUTURE_RE.search(sentence) or _OFFER_RE.search(sentence):
                continue
            silence = _SILENCE_RE.search(sentence) is not None
            strong = silence or any(r.search(sentence) for r in _STRONG_RES)
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
# The argument whose words a backing call is matched on when the turn made more than one.
_MATCH_ARGS = ("what", "task", "intent", "name", "description", "goal", "prompt", "title", "summary", "check_prompt")
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

    One backing call in the turn backs every promise; with more, a promise is backed by a call whose argument words
    overlap the promise's by at least ``MIN_OVERLAP``. ``record_decision``, ``learn_fact``, ``create_censor``, reads,
    error results and results without a parseable id never back anything."""

    MIN_OVERLAP = 0.3

    def artifacts(self, tool_results: list[Any]) -> list[Artifact]:
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
            words = _words(" ".join(str(args.get(k, "")) for k in _MATCH_ARGS))
            found.append(Artifact(tool=name, kind=parsed[0], id=parsed[1], words=words))
        return found

    def split(
        self, promises: list[Promise], tool_results: list[Any]
    ) -> tuple[list[tuple[Promise, Artifact]], list[Promise]]:
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
        if len(artifacts) == 1:
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


def render_footer(labels: list[str], first: Promise, now: datetime) -> str:
    """One line, whatever the number of rows (spec: at most one footer per turn). No 'committed' wording: the
    claim verifier's vcs_commit check reads that word."""
    ids = ", ".join(labels)
    plural = "commitments" if len(labels) > 1 else "commitment"
    return (
        f"⏱ Recorded as {plural} {ids}, due {_due_phrase(first.due_at, now)}; nothing is scheduled for it yet. "
        f"/drop {labels[0]} to cancel."
    )


class PromiseGate:
    """Detect, resolve, record. Async because it writes; the caller bounds and guards it."""

    def __init__(self, store: CommitmentStore, settings: Any) -> None:
        self._store = store
        self._settings = settings
        self.detector = PromiseDetector(default_due_minutes=settings.promise_gate_default_due_minutes)
        self.resolver = BackingResolver()

    @property
    def mode(self) -> GateMode:
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
        mode = self.mode
        if mode == "off":
            return GateOutcome(outcome="off")
        now = now or datetime.now(UTC)
        started = time.perf_counter()
        promises = self.detector.scan(response_text, now=now)
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
            result.footer = render_footer([r.label for _, r in shown], shown[0][0], now)
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
