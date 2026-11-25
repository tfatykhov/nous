"""Per-entity profiles (F119): a short, dated profile of each entity the agent's facts talk about.

The last sleep phase writes them (``EntityProfileWriter.run_sleep``) and the owner reads them on ``GET
/entity-profiles``; with ``NOUS_ENTITY_PROFILE_USE`` (``use_mode``) a turn whose message names a profiled entity reads
it (``match_profiled_keys``, ``EntityProfileStore.profiled_vocabulary`` and ``turn_profiles``; the context engine
renders it). Candidates are the keys of ``heart.fact_entity_keys`` with at
least ``entity_profile_min_facts`` active, unsuperseded facts. The keys ``user`` and ``assistant`` and the owner's own
keys (``entity_profile_owner_keys``) are stored as ``owner`` without a model call. For any other key one model call,
shown the key's newest facts, decides whether the key names an entity and writes its lines. A ``not_entity`` verdict is
stored and asked again only when the key's active facts have doubled or 30 days have passed (a reply that could not be
used: 7 days; a call that got no reply: 24 hours). Code then checks the lines with the profile card's
``settle_entries`` (1 to 3 citations of the facts shown; more than half dropped keeps the old profile), drops a line
that cites only the agent's own inferences or a fact that is no longer live, dates each line from its cited facts
(never from the model) and drops whole lines from the end until the rendered profile fits. A key that has a profile is
visited even when it falls below the threshold or past the cut, so its profile is written again when its facts
change, and emptied without a call when none is left; until it is written again, its lines that cite a fact no longer
live are dropped without a call, and the routes show only lines whose cited facts are all live. Each call is counted
before it is made (its row's ``written_at`` moves first), and a row written with a call stands for that call for 24
hours: the entity is not asked again until they have passed, so the rows written in the last 24 hours are the calls
made in them and ``entity_profile_max_calls_per_day`` bounds the sleep phase exactly. The owner's rebuild may ask
again about an entity written in that window.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from nous.config import Settings
from nous.handlers import LLMClient, call_background_llm_structured
from nous.heart.keys import extract_entity_candidates, normalize_key
from nous.heart.profile_card import (
    CHARS_PER_TOKEN,
    LINE_CHARS,
    _norm,
    _one_line,
    _render_fact,
    _session_scope,
    settle_entries,
)
from nous.heart.profile_card import source_hash as card_source_hash
from nous.heart.schemas import FACT_SOURCE_MARKERS
from nous.heart.temporal import as_date
from nous.storage.database import Database
from nous.storage.models import EntityProfile, Fact, FactEntityKey

logger = logging.getLogger(__name__)

TOOL_NAME = "store_entity_profile"
_TOOL_DESCRIPTION = "Store the verdict on the entity key and its whole profile. Every line cites its facts."
MAX_CITES = 3
OWNER_KEYS = ("user", "assistant")
KINDS = ("person", "place", "project", "device", "product", "organisation", "other")
NAME_CHARS = 100
# 8 lines of 160 characters with 3 citations each come to well under 1,000 output tokens; a reply cut off at
# max_tokens is discarded, so the cap leaves room above that.
LLM_MAX_TOKENS = 2000
# settle_entries gets no size bound: lines are counted and measured here, after the lines this module drops.
_NO_BOUND = 1_000_000
_DAY = timedelta(hours=24)
# A source row shows at most this much of a fact's subject and source (the card's row cuts content and tags), so a
# call's size has a bound.
SUBJECT_CHARS = 100
SOURCE_CHARS = 60
# A not_entity verdict is asked again once the key's active facts have doubled since the verdict, or after 30 days; a
# key whose first reply could not be used, after 7 days. The owner's rebuild asks at any time.
NOT_ENTITY_GROWTH = 2
NOT_ENTITY_REASK = timedelta(days=30)
FAILED_REASK = timedelta(days=7)
# GET /entity-profiles: rows a list answers with by default, and at most.
LIST_LIMIT_DEFAULT = 200
LIST_LIMIT_MAX = 1000
# The most fact ids one query looks up (one bind parameter each; asyncpg takes at most 32,767 a query).
ID_CHUNK = 5000
# What Postgres text and JSONB cannot store: NUL, and lone surrogates (not valid UTF-8).
_UNSTORABLE = re.compile("[\x00\ud800-\udfff]")

# A turn (PR 3, NOUS_ENTITY_PROFILE_USE): the modes, the most profiles one turn reads, the section's size in tokens
# (before the context engine's budget scaling) and the most matched keys the turn's record keeps.
USE_MODES = ("off", "trace", "on")
TURN_MAX_PROFILES = 3
TURN_MAX_TOKENS = 400
RECORD_MATCHED_MAX = 20
# The keys a turn can match, cached per agent in this process. Profiles change only when a writer writes: this
# process's writer clears the agent's entry on every write (``invalidate_profiled_vocabulary``), so its changes are seen
# on the next turn. A write by another process cannot clear it, so an entry lives at most 300 seconds, as the
# entity-key vocabulary's does (nous/heart/facts.py). A key whose last live line died since it was cached costs one
# read that shows nothing: the turn's read checks every line again.
VOCAB_TTL_SECONDS = 300.0
_vocab_cache: dict[str, tuple[frozenset[str], float]] = {}
# Bumped on every clear, so a read that started before a write does not store what it read.
_vocab_gen: dict[str, int] = {}
_warned_use_values: set[str] = set()

_SYSTEM_PROMPT = (
    "You write a short profile of one entity for an AI agent's memory: one specific person, place, project, device, "
    "product or organisation that the agent's facts talk about. You state only what the cited source facts say."
)

_PROFILE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["entity", "not_entity"],
            "description": "entity when the key names one specific entity the facts talk about, else not_entity.",
        },
        "kind": {"type": "string", "enum": list(KINDS)},
        "display_name": {
            "type": "string",
            "maxLength": NAME_CHARS,
            "description": "The entity's name as the facts write it.",
        },
        "lines": {
            "type": "array",
            "description": "The whole profile, most important line first. Empty for not_entity.",
            "items": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "maxLength": LINE_CHARS,
                        "description": f"One point about the entity, third person, at most {LINE_CHARS} characters.",
                    },
                    "fact_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": f"1 to {MAX_CITES} ids of the source facts this line states.",
                    },
                },
                "required": ["text", "fact_ids"],
            },
        },
    },
    # Only the verdict: a reply without a required key is discarded, and a not_entity reply may carry no lines.
    "required": ["verdict"],
}


def _storable(value: Any) -> Any:
    """``value`` (a model reply) with every character Postgres text and JSONB reject removed from its strings."""
    if isinstance(value, str):
        return _UNSTORABLE.sub("", value)
    if isinstance(value, list):
        return [_storable(v) for v in value]
    if isinstance(value, dict):
        return {k: _storable(v) for k, v in value.items()}
    return value


class _ReplySeen:
    """The LLM client, noting whether a reply came back. ``call_background_llm_structured`` answers None both for a
    call that got no reply (the API was down, rate limited or timed out) and for an unusable reply; ``replied`` tells
    the two apart."""

    def __init__(self, client: LLMClient) -> None:
        self._client = client
        self.replied = False

    @property
    def supports_call_context(self) -> bool:
        return getattr(self._client, "supports_call_context", False) is True

    async def call(self, payload: dict[str, Any], **kwargs: Any) -> Any:
        response = await self._client.call(payload, **kwargs)
        self.replied = True
        return response


def _source_row(fact: Fact) -> str:
    """The card's one-line source row, with the subject and source cut to their bounds, and the stated day."""
    shown = SimpleNamespace(
        id=fact.id,
        category=fact.category,
        subject=(fact.subject or "")[:SUBJECT_CHARS] or None,
        content=fact.content,
        tags=fact.tags,
        source=(fact.source or "")[:SOURCE_CHARS] or None,
        learned_at=fact.learned_at,
        overrides_prior=fact.overrides_prior,
    )
    return f"{_render_fact(shown)} | stated {_stated_on(fact) or 'unknown'}"


def use_mode(settings: Any) -> str:
    """``NOUS_ENTITY_PROFILE_USE`` as it reads now: ``off``, ``trace`` or ``on``, case and spaces ignored. An empty
    value is off; any other value is off too, with one warning per value in this process."""
    raw = getattr(settings, "entity_profile_use", "off")
    mode = raw.strip().lower() if isinstance(raw, str) else ""
    if mode in USE_MODES:
        return mode
    if mode and mode not in _warned_use_values:
        _warned_use_values.add(mode)
        logger.warning("NOUS_ENTITY_PROFILE_USE=%r is not off, trace or on: entity profiles stay off", raw)
    return "off"


def invalidate_profiled_vocabulary(agent_id: str) -> None:
    """Drop the agent's cached profiled keys: the next turn reads them again."""
    _vocab_cache.pop(agent_id, None)
    _vocab_gen[agent_id] = _vocab_gen.get(agent_id, 0) + 1


def match_profiled_keys(text: str, vocab: frozenset[str]) -> list[str]:
    """The profiled keys ``text`` names, longest first and then by position, without a key whose words are a run of
    words inside a longer key also named ("the X repo" gives ``x repo``, not also ``x`` and ``repo``).

    The keyed fact leg's matcher (``extract_entity_candidates`` with ``vocab_only``): the message is normalised as
    entity keys are and only its first 1,000 characters are read.
    """
    found = extract_entity_candidates(text, vocab=vocab, max_candidates=None, vocab_only=True)
    words = {key: key.split() for key in found}

    def _inside(short: list[str], long: list[str]) -> bool:
        n = len(short)
        return n < len(long) and any(long[i : i + n] == short for i in range(len(long) - n + 1))

    return [key for key in found if not any(_inside(words[key], words[other]) for other in found)]


def owner_keys(settings: Settings) -> set[str]:
    """``user``, ``assistant`` and the owner's keys (``entity_profile_owner_keys``), normalised as entity keys are."""
    keys = set(OWNER_KEYS)
    for raw in settings.entity_profile_owner_keys:
        key = normalize_key(raw)
        if key:
            keys.add(key)
    return keys


def _stated(fact: Fact) -> tuple[datetime | None, str]:
    """When ``fact`` was stated, ``COALESCE(mentioned_at, learned_at)`` with a naive value read as UTC, and the column
    it came from. The context engine's ``_stated_at(fact, mentioned_first=True)`` rule: a test keeps the two equal
    (heart does not import nous.cognitive)."""
    mentioned = getattr(fact, "mentioned_at", None)
    stated, column = (mentioned, "mentioned_at") if mentioned else (getattr(fact, "learned_at", None), "learned_at")
    if not isinstance(stated, datetime):
        return None, column
    return (stated if stated.tzinfo is not None else stated.replace(tzinfo=UTC)), column


def _stated_on(fact: Fact) -> str | None:
    when, _column = _stated(fact)
    day = as_date(when)
    return day.isoformat() if day else None


def entity_source_hash(facts: list[Fact]) -> str:
    """The profile card's digest of the facts (every field the prompt shows, ``active`` and ``superseded_by``) plus
    each fact's ``mentioned_at``, which the prompt shows as the stated date."""
    mentioned = sorted((str(f.id), f.mentioned_at.isoformat() if f.mentioned_at else None) for f in facts)
    return hashlib.sha256((card_source_hash(facts) + json.dumps(mentioned)).encode()).hexdigest()


def render_profile(name: str, kind: str | None, lines: list[dict]) -> str:
    """A profile as a reader would show it: a heading, then each line with its date."""
    head = f"### {name} ({kind})" if kind else f"### {name}"
    return "\n".join([head] + [f"- ({line['said_on']}) {line['text']}" for line in lines])


def profile_tokens(name: str, kind: str | None, lines: list[dict]) -> int:
    """The rendered profile's size in the context engine's tokens (characters / 4), heading and dates included."""
    return len(render_profile(name, kind, lines)) // CHARS_PER_TOKEN


def _dated(line: dict, by_id: dict[str, Fact]) -> dict | None:
    """``line`` with ``said_on``, the latest stated day of its cited facts, and ``date_from``, the column that day came
    from; None when no cited fact has a date."""
    stamps = [_stated(by_id[i]) for i in line["fact_ids"] if i in by_id]
    stamps = [(when, column) for when, column in stamps if when is not None]
    if not stamps:
        return None
    when, column = max(stamps, key=lambda stamp: stamp[0])
    return {**line, "said_on": as_date(when).isoformat(), "date_from": column}


def settle_lines(
    raw: Any,
    facts: list[Fact],
    rejected: list[str],
    name: str,
    kind: str | None,
    settings: Settings,
) -> tuple[list[dict] | None, dict[str, int]]:
    """Check the model's lines; None means keep the old profile (fail closed).

    ``settle_entries`` drops a line with no text, over ``LINE_CHARS``, with more than 3 citations or one outside the
    facts shown (``dropped``), drops a rejected line and a repeat, and fails closed when none is left or more than half
    were dropped. Then a line whose cited facts are all the agent's own inferences is dropped (``inferred_dropped``),
    each line is dated from its cited facts, and whole lines are dropped from the end until the profile has at most
    ``entity_profile_max_lines`` lines and its rendered size fits ``entity_profile_max_tokens`` (``trimmed``).
    """
    lines, counts = settle_entries(raw, facts, set(), rejected, _NO_BOUND, _NO_BOUND, max_cites=MAX_CITES)
    counts["inferred_dropped"] = 0
    if lines is None:
        return None, counts
    by_id = {str(f.id): f for f in facts}
    kept: list[dict] = []
    for line in lines:
        cited = [by_id[i] for i in line["fact_ids"] if i in by_id]
        if cited and all(FACT_SOURCE_MARKERS.get(f.source or "") == "inferred" for f in cited):
            counts["inferred_dropped"] += 1
            continue
        dated = _dated(line, by_id)
        if dated is not None:
            kept.append(dated)
    max_lines, max_tokens = settings.entity_profile_max_lines, settings.entity_profile_max_tokens
    while kept and (len(kept) > max_lines or profile_tokens(name, kind, kept) > max_tokens):
        kept.pop()
        counts["trimmed"] += 1
    return (kept or None), counts


def build_prompt(key: str, facts: list[Fact], row: dict | None, settings: Settings) -> str:
    """The writer's one request for one key: the rules, the current lines, the source facts newest first, and the
    rejected lines."""
    key_text = _one_line(key)
    lines = [
        f'Write the profile of the entity key "{key_text}" from its source facts below, or say that the key is not '
        "an entity.",
        "",
        "Rules:",
        '1. verdict "entity" when the key names one specific person, place, project, device, product or '
        'organisation that the facts talk about. verdict "not_entity" when it names a category, a role, a '
        'document type, a step, a quantity or another common word (such as "report", "tool", "next step" or '
        '"link"); then write no lines.',
        f"2. kind: one of {', '.join(KINDS)}. display_name: the name as the facts write it.",
        f"3. At most {settings.entity_profile_max_lines} lines, each at most {LINE_CHARS} characters, third person, "
        f"one point a line. Every line cites 1 to {MAX_CITES} source fact ids (the 8 characters in brackets). Add "
        "no inference and nothing the cited facts do not say. Put the most important lines first: lines past the "
        "size bound are dropped from the end.",
        "4. Write no date in a line: each line's date is taken from the facts it cites.",
        "5. When two facts give different values for the same thing, write one line with the value of the fact "
        "stated later and cite both. Write the earlier value only when it is part of what happened, as "
        '"earlier, ...".',
        "6. A fact whose source is sleep_reflection or reflection is the agent's own inference, and one whose source "
        "is cluster_consolidation or contradiction_resolution merges other facts. Never state an inference as "
        "something the user said: a line that cites only inferences is dropped.",
    ]
    agent_key = normalize_key(settings.agent_name)
    if agent_key and key == agent_key:
        lines.append(
            f'7. "{key_text}" is also the agent\'s own name. Here it names the project of that name: write the '
            "profile of the project, not of the agent."
        )
    current = row["lines"] if row is not None and row["status"] == "profile" else []
    lines += ["", "CURRENT PROFILE:"]
    lines += [f"- {e['text']} (cites {', '.join(i[:8] for i in e['fact_ids'])})" for e in current] or ["(none)"]
    lines += ["", "SOURCE FACTS, newest first (id category | subject | content | tags | source | learned | stated):"]
    lines += [_source_row(f) for f in facts]
    rejected = row["rejected"] if row is not None else []
    if rejected:
        lines += ["", "REJECTED LINES (the owner deleted these; never write one again, or one that says the same):"]
        lines += [f"- {_one_line(t)}" for t in rejected]
    lines += ["", f"Call {TOOL_NAME} with the verdict and the whole profile."]
    return "\n".join(lines)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _cites(line: Any) -> set[str]:
    """The fact ids a stored line cites (strings only)."""
    ids = line.get("fact_ids") if isinstance(line, dict) else None
    return {i for i in ids if isinstance(i, str)} if isinstance(ids, list) else set()


def _as_dict(row: EntityProfile) -> dict:
    return {
        "key": row.entity_key,
        "status": row.status,
        "display_name": row.display_name,
        "kind": row.kind,
        "lines": list(row.lines),
        "previous_lines": list(row.previous_lines),
        "rejected": list(row.rejected),
        "source_hash": row.source_hash,
        "fact_count": row.fact_count,
        "source_count": row.source_count,
        "tokens": row.tokens,
        "active": row.active,
        "written_at": _iso(row.written_at),
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


# How many active facts name each key in their content or subject (the facts' full-text vector, English stemming)
# without carrying it as an entity key: a rough measure of what a profile cannot see. A key of stop words only counts 0.
_UNKEYED_SQL = """
SELECT k.key AS key, count(f.id) AS n
FROM unnest(CAST(:keys AS text[])) AS k(key)
JOIN heart.facts f
  ON f.agent_id = :agent AND f.active = true AND f.superseded_by IS NULL
 AND f.search_tsv @@ phraseto_tsquery('english', k.key)
WHERE NOT EXISTS (
    SELECT 1 FROM heart.fact_entity_keys ek WHERE ek.fact_id = f.id AND ek.entity_key = k.key
)
GROUP BY k.key
"""


# A turn's two reads. A stored line is ``{text, fact_ids, ...}``; anything else (a line that is not an object,
# ``fact_ids`` that is not an array, an id that is not a UUID) reads as citing no live fact, never as an error.
def _array_or_empty(expr: str) -> str:
    """``expr`` when it is a JSONB array, else an empty one (so the set-returning functions never raise)."""
    return f"CASE WHEN jsonb_typeof({expr}) = 'array' THEN {expr} ELSE CAST('[]' AS jsonb) END"


_LINES = _array_or_empty("p.lines")
_CITED = _array_or_empty("l.line -> 'fact_ids'")
# Lines cite ``str(fact.id)``, so an id counts only in that form, the one ``CAST(f.id AS text)`` gives back.
_LIVE_FACT = (
    "f.agent_id = :agent AND f.active = true AND f.superseded_by IS NULL AND f.id = CASE WHEN c.fid ~ "
    "'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN CAST(c.fid AS uuid) END"
)
# The keys of the agent's active profile rows that have at least one line citing only live facts.
_VOCAB_SQL = f"""
SELECT p.entity_key
FROM nous_system.entity_profile p
WHERE p.agent_id = :agent AND p.active = true AND p.status = 'profile'
  AND EXISTS (
    SELECT 1
    FROM jsonb_array_elements({_LINES}) AS l(line)
    WHERE jsonb_array_length({_CITED}) > 0
      AND NOT EXISTS (
        SELECT 1 FROM jsonb_array_elements_text({_CITED}) AS c(fid)
        WHERE NOT EXISTS (SELECT 1 FROM heart.facts f WHERE {_LIVE_FACT})
      )
  )
"""
# The active profile rows of ``keys``, one row per cited id with the fact's statement times when it is live (NULL
# when not), and one row with no id for a profile that cites none.
_TURN_SQL = f"""
WITH p AS (
    SELECT entity_key, display_name, kind, lines
    FROM nous_system.entity_profile
    WHERE agent_id = :agent AND active = true AND status = 'profile' AND entity_key = ANY(CAST(:keys AS text[]))
), cited AS (
    SELECT DISTINCT p.entity_key, c.fid
    FROM p
    CROSS JOIN LATERAL jsonb_array_elements({_LINES}) AS l(line)
    CROSS JOIN LATERAL jsonb_array_elements_text({_CITED}) AS c(fid)
)
SELECT p.entity_key, p.display_name, p.kind, p.lines, CAST(f.id AS text) AS fact_id, f.mentioned_at, f.learned_at
FROM p
LEFT JOIN cited c ON c.entity_key = p.entity_key
LEFT JOIN heart.facts f ON {_LIVE_FACT}
"""


class EntityProfileStore:
    """Read and write one agent's entity profile rows."""

    def __init__(self, database: Database, agent_id: str) -> None:
        self._db = database
        self.agent_id = agent_id

    async def _row(self, session: AsyncSession, key: str, *, lock: bool) -> EntityProfile | None:
        stmt = select(EntityProfile).where(EntityProfile.agent_id == self.agent_id, EntityProfile.entity_key == key)
        if lock:
            stmt = stmt.with_for_update()
        stmt = stmt.execution_options(populate_existing=True)
        return (await session.execute(stmt)).scalar_one_or_none()

    async def get(self, key: str, session: AsyncSession | None = None) -> dict | None:
        """One key's row, or None."""
        async with _session_scope(self._db, session) as s:
            row = await self._row(s, key, lock=False)
            return _as_dict(row) if row is not None else None

    async def rows(self, keys: list[str], session: AsyncSession | None = None) -> dict[str, dict]:
        """The rows of ``keys``, by key."""
        if not keys:
            return {}
        async with _session_scope(self._db, session) as s:
            stmt = (
                select(EntityProfile)
                .where(EntityProfile.agent_id == self.agent_id, EntityProfile.entity_key.in_(keys))
                .execution_options(populate_existing=True)
            )
            return {row.entity_key: _as_dict(row) for row in (await s.execute(stmt)).scalars()}

    async def list(self, limit: int | None = None, session: AsyncSession | None = None) -> list[dict]:
        """The active rows, the keys with the most facts first (at most ``limit``)."""
        async with _session_scope(self._db, session) as s:
            stmt = (
                select(EntityProfile)
                .where(EntityProfile.agent_id == self.agent_id, EntityProfile.active.is_(True))
                .order_by(EntityProfile.fact_count.desc(), EntityProfile.entity_key)
                .limit(limit)
                .execution_options(populate_existing=True)
            )
            return [_as_dict(row) for row in (await s.execute(stmt)).scalars()]

    async def live_ids(self, ids: set[str], session: AsyncSession | None = None) -> set[str]:
        """The ids among ``ids`` of this agent's active, unsuperseded facts (an id that is not a UUID is not live)."""
        parsed = []
        for i in ids:
            try:
                parsed.append(uuid.UUID(i))
            except (TypeError, ValueError):
                continue
        live: set[str] = set()
        if not parsed:
            return live
        async with _session_scope(self._db, session) as s:
            # In chunks: each id is one bind parameter, and the driver takes at most 32,767 in one query.
            for start in range(0, len(parsed), ID_CHUNK):
                stmt = select(Fact.id).where(
                    Fact.agent_id == self.agent_id,
                    Fact.id.in_(parsed[start : start + ID_CHUNK]),
                    Fact.active.is_(True),
                    Fact.superseded_by.is_(None),
                )
                live |= {str(i) for i in (await s.execute(stmt)).scalars()}
        return live

    async def with_live_lines(self, rows: list[dict], session: AsyncSession | None = None) -> list[dict]:
        """``rows`` with only the lines whose cited facts are all active and unsuperseded now (one query)."""
        cited = {i for row in rows for line in row["lines"] for i in _cites(line)}
        live = await self.live_ids(cited, session=session)
        return [
            {**row, "lines": [line for line in row["lines"] if _cites(line) and _cites(line) <= live]} for row in rows
        ]

    async def profiled_keys(self, session: AsyncSession | None = None) -> list[str]:
        """The keys of the active rows with status ``profile``."""
        async with _session_scope(self._db, session) as s:
            stmt = select(EntityProfile.entity_key).where(
                EntityProfile.agent_id == self.agent_id,
                EntityProfile.active.is_(True),
                EntityProfile.status == "profile",
            )
            return list((await s.execute(stmt)).scalars())

    async def profiled_vocabulary(self) -> frozenset[str]:
        """The keys a turn can match: those of the active ``profile`` rows with at least one line whose cited facts
        are all live. One query in a session of its own, cached for ``VOCAB_TTL_SECONDS`` (see there)."""
        cached = _vocab_cache.get(self.agent_id)
        now = time.monotonic()
        if cached is not None and now - cached[1] < VOCAB_TTL_SECONDS:
            return cached[0]
        gen = _vocab_gen.get(self.agent_id, 0)
        async with self._db.session() as s:
            keys = frozenset((await s.execute(text(_VOCAB_SQL), {"agent": self.agent_id})).scalars())
        # Stored only when no write cleared the entry while the query ran (no await between the test and the store).
        if gen == _vocab_gen.get(self.agent_id, 0):
            _vocab_cache[self.agent_id] = (keys, now)
        return keys

    async def turn_profiles(self, keys: list[str], session: AsyncSession | None = None) -> dict[str, dict]:
        """The active ``profile`` rows of ``keys`` in one query: per key its ``display_name``, ``kind`` and stored
        ``lines``, and ``facts``, the live facts its lines cite (by id, each with ``mentioned_at`` and
        ``learned_at``). A cited fact missing from ``facts`` is not live: inactive, superseded or another agent's."""
        if not keys:
            return {}
        out: dict[str, dict] = {}
        async with _session_scope(self._db, session) as s:
            result = await s.execute(text(_TURN_SQL), {"agent": self.agent_id, "keys": list(keys)})
            for row in result:
                profile = out.setdefault(
                    row.entity_key,
                    {"display_name": row.display_name, "kind": row.kind, "lines": row.lines, "facts": {}},
                )
                if row.fact_id is not None:
                    profile["facts"][row.fact_id] = SimpleNamespace(
                        mentioned_at=row.mentioned_at, learned_at=row.learned_at
                    )
        return out

    async def written_last_day(self, session: AsyncSession | None = None) -> int:
        """Rows written with a model call in the last 24 hours: every row but ``owner`` rows, whose ``written_at``
        moves on each verdict, profile and failed reply."""
        async with _session_scope(self._db, session) as s:
            stmt = select(func.count()).where(
                EntityProfile.agent_id == self.agent_id,
                EntityProfile.status != "owner",
                EntityProfile.written_at > datetime.now(UTC) - _DAY,
            )
            return int((await s.execute(stmt)).scalar_one())

    async def unkeyed_mentions(self, keys: list[str], session: AsyncSession | None = None) -> dict[str, int]:
        """Per key, the active facts that name it in their content or subject but carry no key row (approximate)."""
        if not keys:
            return {}
        async with _session_scope(self._db, session) as s:
            result = await s.execute(text(_UNKEYED_SQL), {"keys": list(keys), "agent": self.agent_id})
            return {row.key: int(row.n) for row in result}

    async def write(
        self,
        key: str,
        status: str,
        digest: str,
        *,
        fact_count: int,
        source_count: int,
        lines: list[dict],
        display_name: str | None = None,
        kind: str | None = None,
        called: bool = True,
        session: AsyncSession | None = None,
    ) -> dict:
        """Replace the row's lines (the old ones become ``previous_lines``) and its status; ``rejected`` is kept.

        The row is locked and a rejected line is dropped here, so a line deleted while the model was writing does
        not come back. ``written_at`` moves only for a write that followed a model call (``called``).
        """
        async with _session_scope(self._db, session) as s:
            row = await self._row(s, key, lock=True)
            banned = {_norm(t) for t in (row.rejected if row is not None else [])}
            lines = [line for line in lines if _norm(line["text"]) not in banned]
            tokens = profile_tokens(display_name or key, kind, lines) if lines else 0
            now = datetime.now(UTC)
            if row is None:
                row = EntityProfile(
                    agent_id=self.agent_id,
                    entity_key=key,
                    previous_lines=[],
                    rejected=[],
                    active=True,
                    created_at=now,
                )
                s.add(row)
            else:
                row.previous_lines = list(row.lines)
            row.status = status
            row.display_name = display_name
            row.kind = kind
            row.lines = lines
            row.source_hash = digest
            row.fact_count = fact_count
            row.source_count = source_count
            row.tokens = tokens
            if called or row.written_at is None:
                row.written_at = now
            row.updated_at = now
            await s.flush()
            # The run clears it again once its writes are committed (run_sleep, rebuild).
            invalidate_profiled_vocabulary(self.agent_id)
            return _as_dict(row)

    async def count_call(
        self, key: str, *, fact_count: int, source_count: int, session: AsyncSession | None = None
    ) -> dict:
        """A call about to be made: only ``written_at`` moves, so the call counts and the key is held for 24 hours.

        Everything else stays until a reply is written: the status, the lines, the digest, ``fact_count`` (a
        ``not_entity`` row's baseline) and ``updated_at`` (when the last reply was judged, which the rests are
        measured from). So a call that gets no reply changes nothing else. A key with no row, or an ``owner`` row (a
        key no longer named an owner key), becomes an ``asked`` row with an empty digest: a call made, no reply judged.
        """
        async with _session_scope(self._db, session) as s:
            row = await self._row(s, key, lock=True)
            now = datetime.now(UTC)
            if row is None:
                row = EntityProfile(
                    agent_id=self.agent_id,
                    entity_key=key,
                    lines=[],
                    previous_lines=[],
                    rejected=[],
                    fact_count=fact_count,
                    source_count=source_count,
                    active=True,
                    created_at=now,
                    updated_at=now,
                )
                s.add(row)
            if row.status in (None, "owner"):
                row.status = "asked"
                row.source_hash = ""
            row.written_at = now
            await s.flush()
            return _as_dict(row)

    async def mark_failed(self, key: str, session: AsyncSession | None = None) -> None:
        """An unusable reply to a key with no judged reply yet (``asked``) or an unusable one before (``failed``): the
        row becomes ``failed`` and its rest starts now. A profile or a verdict is left as it was."""
        async with _session_scope(self._db, session) as s:
            row = await self._row(s, key, lock=True)
            if row is not None and row.status in ("asked", "failed"):
                row.status = "failed"
                row.source_hash = ""
                row.updated_at = datetime.now(UTC)
                await s.flush()


def _new_stats() -> dict[str, Any]:
    return {
        "outcome": "",
        "candidates": 0,
        "asked": 0,
        "written": 0,
        "not_entity": 0,
        "owner": 0,
        "failed_closed": 0,
        "no_reply": 0,
        "unchanged": 0,
        "cleared": 0,
        "held": 0,
        "not_entity_resting": 0,
        "failed_resting": 0,
        "skipped_cap": 0,
        "stale_dropped": 0,
        "inactive": 0,
        "errors": 0,
        "dropped": 0,
        "rejected_dropped": 0,
        "inferred_dropped": 0,
        "dead_dropped": 0,
        "trimmed": 0,
    }


class EntityProfileWriter:
    """Writes the profiles whose facts changed. One run at a time: the sleep phase and a rebuild share a lock."""

    def __init__(
        self,
        database: Database,
        settings: Settings,
        llm_client: LLMClient | None,
        agent_id: str | None = None,
    ) -> None:
        self._db = database
        self._settings = settings
        self._llm = llm_client
        self.agent_id = agent_id or settings.agent_id
        self.store = EntityProfileStore(database, self.agent_id)
        self._lock = asyncio.Lock()

    @property
    def busy(self) -> bool:
        """True while a run holds the lock."""
        return self._lock.locked()

    async def _candidates(self, session: AsyncSession) -> list[tuple[str, int]]:
        """Keys with at least ``entity_profile_min_facts`` active, unsuperseded facts, the most facts first (at most
        ``entity_profile_max_profiles``)."""
        n = func.count()
        stmt = (
            select(FactEntityKey.entity_key, n)
            .join(Fact, Fact.id == FactEntityKey.fact_id)
            .where(
                FactEntityKey.agent_id == self.agent_id,
                Fact.agent_id == self.agent_id,
                Fact.active.is_(True),
                Fact.superseded_by.is_(None),
            )
            .group_by(FactEntityKey.entity_key)
            .having(n >= self._settings.entity_profile_min_facts)
            .order_by(n.desc(), FactEntityKey.entity_key)
            .limit(self._settings.entity_profile_max_profiles)
        )
        return [(key, int(count)) for key, count in (await session.execute(stmt)).all()]

    async def _fact_count(self, session: AsyncSession, key: str) -> int:
        stmt = (
            select(func.count())
            .select_from(FactEntityKey)
            .join(Fact, Fact.id == FactEntityKey.fact_id)
            .where(
                FactEntityKey.agent_id == self.agent_id,
                FactEntityKey.entity_key == key,
                Fact.agent_id == self.agent_id,
                Fact.active.is_(True),
                Fact.superseded_by.is_(None),
            )
        )
        return int((await session.execute(stmt)).scalar_one())

    async def _source_facts(self, session: AsyncSession, key: str) -> list[Fact]:
        """The key's newest ``entity_profile_source_limit`` active, unsuperseded facts, by stated time."""
        stated = func.coalesce(Fact.mentioned_at, Fact.learned_at)
        stmt = (
            select(Fact)
            .join(FactEntityKey, FactEntityKey.fact_id == Fact.id)
            .where(
                FactEntityKey.agent_id == self.agent_id,
                FactEntityKey.entity_key == key,
                Fact.agent_id == self.agent_id,
                Fact.active.is_(True),
                Fact.superseded_by.is_(None),
            )
            .order_by(stated.desc(), Fact.id)
            .limit(self._settings.entity_profile_source_limit)
            .execution_options(populate_existing=True)
        )
        return list((await session.execute(stmt)).scalars().all())

    async def _prune(self, key: str, row: dict | None, stats: dict[str, Any], session: AsyncSession | None) -> None:
        """No new lines for a profile whose facts changed: drop the stored lines that cite a fact no longer live,
        without a call and without moving ``written_at`` (the digest is kept, so the profile stays due)."""
        if row is None or row["status"] != "profile" or not row["lines"]:
            return
        async with _session_scope(self._db, session) as s:
            live = await self.store.live_ids({i for line in row["lines"] for i in _cites(line)}, session=s)
            kept = [line for line in row["lines"] if _cites(line) and _cites(line) <= live]
            if len(kept) == len(row["lines"]):
                return
            await self.store.write(
                key,
                "profile",
                row["source_hash"],
                fact_count=row["fact_count"],
                source_count=row["source_count"],
                lines=kept,
                display_name=row["display_name"],
                kind=row["kind"],
                called=False,
                session=s,
            )
        stats["stale_dropped"] += len(row["lines"]) - len(kept)

    async def _save_owner(self, key: str, fact_count: int, session: AsyncSession | None) -> None:
        async with _session_scope(self._db, session) as s:
            await self.store.write(key, "owner", "", fact_count=fact_count, source_count=0, lines=[], session=s)

    async def _clear(self, key: str, digest: str, row: dict | None, session: AsyncSession | None) -> None:
        name, kind = (row["display_name"], row["kind"]) if row is not None else (None, None)
        async with _session_scope(self._db, session) as s:
            await self.store.write(
                key,
                "profile",
                digest,
                fact_count=0,
                source_count=0,
                lines=[],
                display_name=name,
                kind=kind,
                called=False,
                session=s,
            )

    async def run_sleep(
        self, interrupted: Callable[[], bool] | None = None, session: AsyncSession | None = None
    ) -> dict:
        """The sleep phase's run: owner keys are stored without a call, a key whose facts are unchanged is skipped,
        and at most the day's remaining calls are made. An interrupted sleep stops between entities.

        An entity whose row was written with a call in the last 24 hours (a profile, a verdict or a reply that could
        not be used) is not visited again until they have passed, whatever changed, so the rows written in the last 24
        hours are the calls made in them. Only emptying a profile none of whose facts is left, which makes no call and
        moves no ``written_at``, is done at any time. A ``not_entity`` row waits until the key's active facts have
        doubled or 30 days have passed since its verdict, a ``failed`` row 7 days since its unusable reply (both from
        ``updated_at``, which a call that gets no reply does not move); an ``asked`` row only its 24 hours. A profile
        whose facts changed but which gets no new lines (held, or past the day's calls) loses the lines that cite a
        fact no longer live, without a call.
        """
        interrupted = interrupted or (lambda: False)
        async with self._lock:
            stats = _new_stats()
            if self._llm is None:
                stats["outcome"] = "no_llm"
                return stats
            now = datetime.now(UTC)
            since = now - _DAY
            async with _session_scope(self._db, session) as s:
                candidates = await self._candidates(s)
                # A key with a written profile is visited even below the threshold or past the cut, so a profile whose
                # cited facts died is written again (or emptied when none of its facts is left).
                listed = {key for key, _ in candidates}
                for key in await self.store.profiled_keys(session=s):
                    if key not in listed:
                        candidates.append((key, await self._fact_count(s, key)))
                rows = await self.store.rows([key for key, _ in candidates], session=s)
                used = await self.store.written_last_day(session=s)
            stats["candidates"] = len(candidates)
            calls_left = max(0, self._settings.entity_profile_max_calls_per_day - used)
            owners = owner_keys(self._settings)

            def order(item: tuple[str, int]) -> tuple:
                # Keys never written first, then changed profiles, then not_entity verdicts to ask again; within each,
                # the most facts first.
                key, count = item
                row = rows.get(key)
                return (row is not None, row is not None and row["status"] == "not_entity", -count, key)

            for key, count in sorted(candidates, key=order):
                if interrupted():
                    stats["outcome"] = "interrupted"
                    break
                row = rows.get(key)
                try:
                    if row is not None and not row["active"]:
                        stats["inactive"] += 1
                        continue
                    written = datetime.fromisoformat(row["written_at"]) if row is not None else None
                    # When the last reply was judged: a call that got no reply does not move it.
                    judged = datetime.fromisoformat(row["updated_at"]) if row is not None else None
                    # Counted by written_last_day: the row stands for one call until 24 hours have passed.
                    held = row is not None and row["status"] != "owner" and written > since
                    if key in owners:
                        if held:
                            stats["held"] += 1
                        elif row is None or row["status"] != "owner":
                            await self._save_owner(key, count, session)
                            stats["owner"] += 1
                        continue
                    async with _session_scope(self._db, session) as s:
                        facts = await self._source_facts(s, key)
                    digest = entity_source_hash(facts)
                    # A not_entity verdict is asked again after its rest or once its facts have doubled, whether or
                    # not the facts shown changed (the digest covers only the newest ones).
                    verdict_due = (
                        row is not None
                        and row["status"] == "not_entity"
                        and (count >= NOT_ENTITY_GROWTH * row["fact_count"] or judged <= now - NOT_ENTITY_REASK)
                    )
                    if (
                        row is not None
                        and row["status"] != "owner"
                        and row["source_hash"] == digest
                        and not verdict_due
                    ):
                        stats["unchanged"] += 1
                        continue
                    if not facts:
                        # Every fact of a profiled key is gone: empty the profile without a call (its lines move to
                        # previous_lines), held or not.
                        await self._clear(key, digest, row, session)
                        stats["cleared"] += 1
                        continue
                    if held:
                        stats["held"] += 1
                        await self._prune(key, row, stats, session)
                        continue
                    if (
                        row is not None
                        and row["status"] == "not_entity"
                        and count < NOT_ENTITY_GROWTH * row["fact_count"]
                        and judged > now - NOT_ENTITY_REASK
                    ):
                        stats["not_entity_resting"] += 1
                        continue
                    if row is not None and row["status"] == "failed" and judged > now - FAILED_REASK:
                        stats["failed_resting"] += 1
                        continue
                    if calls_left <= 0:
                        stats["skipped_cap"] += 1
                        await self._prune(key, row, stats, session)
                        continue
                    calls_left -= 1
                    await self._ask(key, count, facts, digest, row, stats, session)
                except Exception:
                    # One entity's failure never stops the others (each writes in its own transaction).
                    logger.warning("Entity profile: one entity failed", exc_info=True)
                    stats["errors"] += 1
            # Each write cleared the cached keys before its commit; a turn that read them in between saw the old rows.
            invalidate_profiled_vocabulary(self.agent_id)
            stats["outcome"] = stats["outcome"] or "done"
            return stats

    async def rebuild(self, key: str, session: AsyncSession | None = None) -> dict:
        """The owner's run for one key: asks the model even when the key's facts are unchanged, within the daily limit
        (``at_limit``: no call). An entity written in the last 24 hours may be asked again: its row already counts, so
        this call is the one the count does not see. An owner key whose row was written with a call in the last 24
        hours stays as it is until they have passed (``held``). The result carries the row as written (``profile``).
        """
        async with self._lock:
            stats = _new_stats()
            norm = normalize_key(key) if isinstance(key, str) else None
            stats["key"] = norm
            if not norm:
                stats["outcome"] = "no_key"
                return stats
            async with _session_scope(self._db, session) as s:
                facts = await self._source_facts(s, norm)
                count = await self._fact_count(s, norm)
                row = await self.store.get(norm, session=s)
                used = await self.store.written_last_day(session=s)
            held = (
                row is not None
                and row["status"] != "owner"
                and datetime.fromisoformat(row["written_at"]) > datetime.now(UTC) - _DAY
            )
            if row is not None and not row["active"]:
                stats["outcome"] = "inactive"
            elif norm in owner_keys(self._settings) and held:
                # The row stands for a call made in the last 24 hours: it becomes an owner row once they have
                # passed, so the call never leaves the day's count early.
                stats["held"] = 1
                stats["outcome"] = "held"
            elif norm in owner_keys(self._settings):
                await self._save_owner(norm, count, session)
                stats["owner"] = 1
                stats["outcome"] = "owner"
            elif not facts:
                stats["outcome"] = "no_facts"
            elif self._llm is None:
                stats["outcome"] = "no_llm"
            elif used >= self._settings.entity_profile_max_calls_per_day:
                stats["outcome"] = "at_limit"
            else:
                stats["outcome"] = await self._ask(norm, count, facts, entity_source_hash(facts), row, stats, session)
            invalidate_profiled_vocabulary(self.agent_id)  # as at the end of run_sleep
            written = await self.store.get(norm, session=session)
            # As the GET routes do: only the lines whose cited facts are all live now.
            stats["profile"] = (await self.store.with_live_lines([written], session=session))[0] if written else None
            return stats

    async def _ask(
        self,
        key: str,
        fact_count: int,
        facts: list[Fact],
        digest: str,
        row: dict | None,
        stats: dict[str, Any],
        session: AsyncSession | None,
    ) -> str:
        """One model call for ``key`` and its write: ``written``, ``not_entity``, ``failed_closed`` or ``no_reply``.

        The call is counted before it is made: the row's ``written_at`` moves in a transaction of its own first (a new
        key gets an ``asked`` row), so a crash, a cancellation or a failed result write still leaves the call counted
        and the key held for 24 hours. If that write fails, no call is made. A call that gets no reply (the API was
        down, rate limited or timed out) leaves the row as reserved; only an unusable reply makes an ``asked`` row
        ``failed``, to rest 7 days.
        """
        sources = len(facts)
        async with _session_scope(self._db, session) as s:
            await self.store.count_call(key, fact_count=fact_count, source_count=sources, session=s)
        stats["asked"] += 1
        client = _ReplySeen(self._llm)
        reply = _storable(
            await call_background_llm_structured(
                client=client,
                model=self._settings.background_model,
                system_prompt=_SYSTEM_PROMPT,
                user_message=build_prompt(key, facts, row, self._settings),
                tool_name=TOOL_NAME,
                tool_description=_TOOL_DESCRIPTION,
                output_schema=_PROFILE_SCHEMA,
                max_tokens=LLM_MAX_TOKENS,
                purpose="entity_profile",
            )
        )
        verdict = reply.get("verdict") if isinstance(reply, dict) else None
        if not client.replied:
            # No reply to judge: the row stays as count_call left it, counted and held for 24 hours.
            stats["no_reply"] += 1
            logger.warning("Entity profile: no reply; the call is counted and the key is asked again after 24 hours")
            return "no_reply"
        async with _session_scope(self._db, session) as s:
            if verdict == "not_entity":
                await self.store.write(
                    key, "not_entity", digest, fact_count=fact_count, source_count=sources, lines=[], session=s
                )
                stats["not_entity"] += 1
                return "not_entity"
            lines, name, kind = None, key, "other"
            if verdict == "entity":
                raw_name = reply.get("display_name")
                if isinstance(raw_name, str) and raw_name.strip():
                    name = _one_line(raw_name)[:NAME_CHARS]
                if reply.get("kind") in KINDS:
                    kind = reply["kind"]
                lines, counts = settle_lines(
                    reply.get("lines"), facts, row["rejected"] if row is not None else [], name, kind, self._settings
                )
                for k, v in counts.items():
                    stats[k] = stats.get(k, 0) + v
            if lines:
                # A fact retired while the model was writing: every cited fact must still be live.
                live = await self.store.live_ids({i for line in lines for i in line["fact_ids"]}, session=s)
                kept = [line for line in lines if set(line["fact_ids"]) <= live]
                stats["dead_dropped"] += len(lines) - len(kept)
                lines = kept
            if not lines:
                # An unusable reply: the call was counted before it was made. A key with no judged reply yet becomes
                # failed and rests 7 days; an old profile is kept, without its dead lines; a verdict stays.
                stats["failed_closed"] += 1
                logger.warning(
                    "Entity profile: reply not used, old profile kept (reply=%s)", "none" if reply is None else verdict
                )
                await self.store.mark_failed(key, session=s)
                await self._prune(key, row, stats, s)
                return "failed_closed"
            await self.store.write(
                key,
                "profile",
                digest,
                fact_count=fact_count,
                source_count=sources,
                lines=lines,
                display_name=name,
                kind=kind,
                session=s,
            )
            stats["written"] += 1
            return "written"
