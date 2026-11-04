"""Read one BEAM conversation from the upstream checkout.

Layout (upstream ``chats/``)::

    chats/<bucket>/<conv_id>/chat.json
    chats/<bucket>/<conv_id>/probing_questions/probing_questions.json

``chat.json`` is a list of batches ``{"batch_number", "turns": [[message, ...], ...]}``.
The 10M bucket wraps the batches in plans: ``[{"plan-1": [batch, ...]}, ...]``.
Each element of ``turns`` is one exchange (a user question, the answer and any
follow-ups); BEAM's questions call it a session, and the harness stores each as
one Nous episode. The first user message of a batch carries the batch's date as
``time_anchor`` (e.g. ``March-15-2024``).

``probing_questions.json`` maps each ability to a list of questions, each with a
``rubric`` list that BEAM's judge scores against. Question order inside a list
is the index the upstream evaluator matches answers on.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

# The ten abilities, in upstream report order (src/evaluation/report_results.py).
ABILITIES = (
    "abstention",
    "contradiction_resolution",
    "event_ordering",
    "information_extraction",
    "instruction_following",
    "knowledge_update",
    "multi_session_reasoning",
    "preference_following",
    "summarization",
    "temporal_reasoning",
)

# Sessions of one batch share its date; they are spaced this far apart (or
# closer, so a batch fits in its day) to keep their order in the timestamps.
SESSION_SPACING = timedelta(minutes=10)


@dataclass(frozen=True)
class Message:
    role: str
    content: str


@dataclass(frozen=True)
class Session:
    """One BEAM exchange: the unit the harness stores as one episode."""

    batch: int
    index: int  # position inside the batch
    messages: tuple[Message, ...]
    started_at: datetime


@dataclass(frozen=True)
class Question:
    ability: str
    index: int  # position inside the ability's list
    question: str
    rubric: tuple[str, ...]


@dataclass(frozen=True)
class Conversation:
    bucket: str
    conv_id: str
    sessions: tuple[Session, ...]
    batch_ends: tuple[int, ...]  # index into sessions after which each batch ends
    questions: tuple[Question, ...]

    @property
    def key(self) -> str:
        return f"{self.bucket}/{self.conv_id}"


def parse_anchor(value: str) -> datetime:
    """``March-15-2024`` -> 2024-03-15 00:00 UTC."""
    return datetime.strptime(value, "%B-%d-%Y").replace(tzinfo=UTC)


def list_conversations(chats_dir: Path, bucket: str) -> list[str]:
    """Numeric conversation ids that have a chat.json, in numeric order."""
    root = chats_dir / bucket
    ids = [p.name for p in root.iterdir() if p.name.isdigit() and (p / "chat.json").is_file()]
    return sorted(ids, key=int)


def _batches(raw: list) -> list[dict]:
    out: list[dict] = []
    for item in raw:
        if isinstance(item, dict) and "turns" in item:
            out.append(item)
        elif isinstance(item, dict) and all(k.startswith("plan-") for k in item):
            for plan in item.values():
                out.extend(plan)
        else:
            raise ValueError(f"unexpected chat.json element: {type(item).__name__} with keys {list(item)[:5]}")
    return out


def _anchor(batch: dict) -> datetime | None:
    for turn in batch["turns"]:
        for msg in turn:
            if msg.get("time_anchor"):
                return parse_anchor(msg["time_anchor"])
    return None


def _anchors(batches: list[dict]) -> list[datetime]:
    """Each batch's date. A batch without one (one in the whole corpus, 10M/7) is
    dated a day after the previous batch, or a day before the next one."""
    found = [_anchor(b) for b in batches]
    if not any(found):
        raise ValueError("no batch has a time_anchor")
    out: list[datetime] = []
    for i, a in enumerate(found):
        if a is None:
            a = out[-1] + timedelta(days=1) if out else next(x for x in found[i:] if x) - timedelta(days=1)
        out.append(a)
    return out


def load_conversation(chats_dir: Path, bucket: str, conv_id: str) -> Conversation:
    conv_dir = chats_dir / bucket / conv_id
    raw = json.loads((conv_dir / "chat.json").read_text(encoding="utf-8"))
    sessions: list[Session] = []
    batch_ends: list[int] = []
    batches = _batches(raw)
    for b, anchor in zip(batches, _anchors(batches), strict=True):
        turns = b["turns"]
        spacing = min(SESSION_SPACING, timedelta(days=1) / max(len(turns), 1))
        for i, turn in enumerate(turns):
            msgs = tuple(Message(m["role"], m["content"]) for m in turn if m.get("content"))
            if msgs:
                sessions.append(Session(int(b["batch_number"]), i, msgs, anchor + i * spacing))
        batch_ends.append(len(sessions) - 1)

    pq = json.loads((conv_dir / "probing_questions" / "probing_questions.json").read_text(encoding="utf-8"))
    questions: list[Question] = []
    for ability, items in pq.items():
        for i, q in enumerate(items):
            rubric = q.get("rubric")
            if not rubric:
                raise ValueError(f"{bucket}/{conv_id} {ability}[{i}] has no rubric")
            questions.append(Question(ability, i, q["question"], tuple(rubric)))
    return Conversation(bucket, conv_id, tuple(sessions), tuple(batch_ends), tuple(questions))
