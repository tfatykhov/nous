"""HaluMem data (huggingface.co/datasets/IAAR-Shanghai/HaluMem): one JSON object per user.

A user has ``uuid``, ``persona_info`` and ``sessions``; a session has ``start_time``,
``end_time``, ``dialogue`` (``role``/``content`` turns), ``memory_points`` and
optionally ``questions``. The raw dicts are kept: the adapter output copies them.
"""

from __future__ import annotations

import ast
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from nous_eval.beam.corpus import Message, Session

TIME_FORMAT = "%b %d, %Y, %H:%M:%S"  # "Sep 04, 2025, 18:42:18", as the reference adapters parse it
MIN_SESSION_LENGTH = timedelta(minutes=5)
_NAME_RE = re.compile(r"Name:\s*(.*?); Gender:")


def parse_time(value: str) -> datetime:
    return datetime.strptime(value, TIME_FORMAT).replace(tzinfo=UTC)


def user_name(persona_info: str) -> str:
    """The persona's name, extracted as the reference adapters extract it."""
    match = _NAME_RE.search(persona_info)
    if not match:
        raise ValueError("persona_info has no 'Name: ...; Gender:' field")
    return match.group(1).strip()


def load_users(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def as_session(raw: dict[str, Any], index: int) -> Session:
    """The session as the BEAM harness ingests one: both roles, in order, at its start time."""
    messages = tuple(Message(role=t["role"], content=t["content"]) for t in raw["dialogue"])
    return Session(batch=0, index=index, messages=messages, started_at=parse_time(raw["start_time"]))


def session_end(raw: dict[str, Any]) -> datetime:
    """``end_time``, or five minutes after the start when it is missing or not after the start."""
    start = parse_time(raw["start_time"])
    end = parse_time(raw["end_time"]) if raw.get("end_time") else start
    return end if end > start else start + MIN_SESSION_LENGTH


def is_update_point(point: dict[str, Any]) -> bool:
    """The reference adapters search memory for exactly these points (``is_update`` is a string)."""
    return point.get("is_update") != "False" and bool(point.get("original_memories"))


def load_string_constant(path: Path, name: str) -> str:
    """A module-level string constant of a HaluMem source file, read without running the file."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            value = ast.literal_eval(node.value)
            if isinstance(value, str):
                return value
    raise SystemExit(f"{path} has no string constant {name}")
