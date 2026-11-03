"""The bounded profile card: a short list of durable profile lines, each citing the facts it states.

One model call rewrites the whole card from the agent's tier-1 facts (the sleep phase, or ``POST
/profile-card/rebuild``). Code then checks the reply: every cited id must be a source fact, every ``profile_core``
fact keeps a line, a line the owner deleted never comes back, and whole lines are dropped from the end to fit the
size bound. A bad reply keeps the old card. The card is one row per agent (``nous_system.profile_card``).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.cognitive.context import PROFILE_CORE_TAG, TIER1_FACT_CATEGORIES, ContextEngine
from nous.config import Settings
from nous.handlers import LLMClient, call_background_llm_structured
from nous.storage.database import Database
from nous.storage.models import Episode, Fact, ProfileCard

logger = logging.getLogger(__name__)

TOOL_NAME = "store_profile_card"
LINE_CHARS = 160
MAX_CITES = 5
REJECTED_CAP = 20
FACT_CHARS = 200
TAG_CHARS = 120
EPISODE_CHARS = 300
# 25 lines of 160 characters with 5 citations each come to about 2,000 output tokens; a reply cut off at max_tokens
# is discarded, so the cap leaves room above that.
LLM_MAX_TOKENS = 3000

_SYSTEM_PROMPT = (
    "You maintain a bounded profile card for an AI agent: the durable facts about its user and the people around "
    "them. You state only what the cited source facts say."
)

_CARD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "entries": {
            "type": "array",
            "description": "The whole card, most important line first.",
            "items": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "maxLength": LINE_CHARS,
                        "description": f"One durable profile fact, third person, at most {LINE_CHARS} characters.",
                    },
                    "fact_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": f"1 to {MAX_CITES} ids of the source facts this line states.",
                    },
                },
                "required": ["text", "fact_ids"],
            },
        }
    },
    "required": ["entries"],
}


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _norm(text: str) -> str:
    return _one_line(text).casefold()


def card_tokens(entries: list[dict]) -> int:
    """The card's size as the reader renders it (``- text`` lines), in the context engine's tokens."""
    return len("\n".join(f"- {e['text']}" for e in entries)) // ContextEngine.CHARS_PER_TOKEN


def source_hash(facts: list[Fact]) -> str:
    """Digest of what the card is written from. ``updated_at`` is left out: a recall touch moves it."""
    rows = sorted(
        (
            str(f.id),
            hashlib.sha256(f.content.encode()).hexdigest(),
            sorted(f.tags or []),
            bool(f.active),
            str(f.superseded_by) if f.superseded_by else None,
        )
        for f in facts
    )
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()


@asynccontextmanager
async def _session_scope(database: Database, session: AsyncSession | None) -> AsyncIterator[AsyncSession]:
    """The caller's session (the caller commits), or a new one committed on a clean exit."""
    if session is not None:
        yield session
        return
    async with database.session() as own:
        yield own
        await own.commit()


def _as_dict(row: ProfileCard) -> dict:
    return {
        "entries": list(row.entries),
        "previous_entries": list(row.previous_entries),
        "source_hash": row.source_hash,
        "rejected": list(row.rejected),
        "tokens": row.tokens,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


class ProfileCardStore:
    """Read and change one agent's card row."""

    def __init__(self, database: Database, agent_id: str) -> None:
        self._db = database
        self.agent_id = agent_id

    async def _row(self, session: AsyncSession, *, lock: bool) -> ProfileCard | None:
        stmt = select(ProfileCard).where(ProfileCard.agent_id == self.agent_id)
        if lock:
            stmt = stmt.with_for_update()
        stmt = stmt.execution_options(populate_existing=True)
        return (await session.execute(stmt)).scalar_one_or_none()

    async def get(self, session: AsyncSession | None = None) -> dict | None:
        """The card, or None when none was written."""
        async with _session_scope(self._db, session) as s:
            row = await self._row(s, lock=False)
            return _as_dict(row) if row is not None else None

    async def write(self, entries: list[dict], digest: str, session: AsyncSession | None = None) -> dict:
        """Replace the entries (the old ones become ``previous_entries``); ``rejected`` is kept.

        The row is locked and lines in ``rejected`` are dropped here, so a line deleted while the model was writing
        does not come back.
        """
        async with _session_scope(self._db, session) as s:
            row = await self._row(s, lock=True)
            banned = {_norm(t) for t in (row.rejected if row is not None else [])}
            entries = [e for e in entries if _norm(e["text"]) not in banned]
            now = datetime.now(UTC)
            if row is None:
                row = ProfileCard(
                    agent_id=self.agent_id,
                    entries=entries,
                    previous_entries=[],
                    source_hash=digest,
                    rejected=[],
                    tokens=card_tokens(entries),
                    created_at=now,
                    updated_at=now,
                )
                s.add(row)
            else:
                row.previous_entries = list(row.entries)
                row.entries = entries
                row.source_hash = digest
                row.tokens = card_tokens(entries)
                row.updated_at = now
            await s.flush()
            return _as_dict(row)

    async def delete_entry(self, entry_id: str, session: AsyncSession | None = None) -> bool:
        """Remove one line now and remember its text in ``rejected`` (newest 20). False when there is no such line."""
        async with _session_scope(self._db, session) as s:
            row = await self._row(s, lock=True)
            hit = next((e for e in (row.entries if row is not None else []) if e.get("id") == entry_id), None)
            if row is None or hit is None:
                return False
            row.entries = [e for e in row.entries if e.get("id") != entry_id]
            row.rejected = (list(row.rejected) + [hit["text"]])[-REJECTED_CAP:]
            row.tokens = card_tokens(row.entries)
            row.updated_at = datetime.now(UTC)
            await s.flush()
            return True


def _resolve(cited: Any, ids: list[str]) -> str | None:
    """A cited id (a full id, or a prefix of at least 8 characters) as the one source id it names, else None."""
    if not isinstance(cited, str):
        return None
    prefix = cited.strip().strip("[]").lower()
    if len(prefix) < 8:
        return None
    matches = [i for i in ids if i.startswith(prefix)]
    return matches[0] if len(matches) == 1 else None


def settle_entries(
    raw: Any,
    facts: list[Fact],
    pinned: set[str],
    rejected: list[str],
    max_entries: int,
    max_tokens: int,
) -> tuple[list[dict] | None, dict[str, int]]:
    """Check the model's entries against the source set; None means keep the old card (fail closed).

    In order: drop an entry with no text, a line over ``LINE_CHARS`` or a citation outside the source set
    (``dropped``); drop a rejected line (``rejected_dropped``) and a repeat of a kept line; fail closed when no entry
    is left or more than half were ``dropped``; add a line (the fact's text cut to ``LINE_CHARS``) for each pinned
    fact no entry cites (``pinned_added``); drop whole lines from the end, unpinned first, until the card fits
    (``trimmed``).
    """
    counts = {"dropped": 0, "rejected_dropped": 0, "pinned_added": 0, "trimmed": 0}
    if not isinstance(raw, list) or not raw:
        return None, counts
    ids = [str(f.id) for f in facts]
    banned = {_norm(t) for t in rejected}
    seen: set[str] = set()
    kept: list[dict] = []
    for item in raw:
        item = item if isinstance(item, dict) else {}
        text = _one_line(item["text"]) if isinstance(item.get("text"), str) else ""
        cited = item.get("fact_ids")
        resolved = [_resolve(c, ids) for c in cited] if isinstance(cited, list) and 1 <= len(cited) <= MAX_CITES else []
        if not text or len(text) > LINE_CHARS or not resolved or None in resolved:
            counts["dropped"] += 1
            continue
        if _norm(text) in banned:
            counts["rejected_dropped"] += 1
            continue
        if _norm(text) in seen:
            continue
        seen.add(_norm(text))
        kept.append({"id": uuid.uuid4().hex[:12], "text": text, "fact_ids": list(dict.fromkeys(resolved))})
    if not kept or counts["dropped"] * 2 > len(raw):
        return None, counts

    cited_ids = {i for e in kept for i in e["fact_ids"]}
    for fact in facts:
        fid = str(fact.id)
        text = _one_line(fact.content)[:LINE_CHARS]
        if fid in pinned and fid not in cited_ids and _norm(text) not in banned:
            kept.append({"id": uuid.uuid4().hex[:12], "text": text, "fact_ids": [fid]})
            counts["pinned_added"] += 1

    while kept and (len(kept) > max_entries or card_tokens(kept) > max_tokens):
        unpinned = [i for i, e in enumerate(kept) if not pinned.intersection(e["fact_ids"])]
        kept.pop(unpinned[-1] if unpinned else -1)
        counts["trimmed"] += 1
    return (kept or None), counts


def _render_fact(fact: Fact) -> str:
    """One source row. Every agent-writable field is put on one line, so none can start a forged row."""
    tags = ", ".join(_one_line(t) for t in fact.tags or [])[:TAG_CHARS]
    parts = [
        f"[{str(fact.id)[:8]}] {fact.category}",
        _one_line(fact.subject or "-"),
        _one_line(fact.content)[:FACT_CHARS],
        "tags: " + (tags or "-"),
        f"source: {_one_line(fact.source or 'unknown')}",
        f"learned {fact.learned_at:%Y-%m-%d}",
    ]
    if fact.overrides_prior:
        parts.append("overrides_prior")
    return " | ".join(parts)


def build_prompt(
    facts: list[Fact],
    pinned: set[str],
    card: dict | None,
    episodes: list[str],
    settings: Settings,
) -> str:
    """The writer's one request: rules, the current card, what changed, the sources, episodes and rejected lines."""
    max_tokens = settings.profile_card_max_tokens
    entries = card["entries"] if card else []
    source_ids = {str(f.id) for f in facts}
    cited = {i for e in entries for i in e["fact_ids"]}
    new = [i[:8] for i in source_ids - cited]
    gone = [i[:8] for i in cited - source_ids]
    lines = [
        "Rewrite the whole profile card from the source facts below.",
        "",
        "Rules:",
        f"1. A durable profile: third person, one fact per line, at most {LINE_CHARS} characters a line.",
        f"2. Every line cites 1 to {MAX_CITES} source fact ids (the 8 characters in brackets). Add no inference and "
        "nothing the cited facts do not say.",
        "3. Recent episodes only tell you what is current or important. Never cite them or take a fact from them.",
        "4. When facts conflict, the later learned date wins, and a fact marked overrides_prior wins.",
        "5. Keep a line for every fact tagged profile_core. Never write a rejected line, or one that says the same.",
        f"6. At most {settings.profile_card_max_entries} lines and {max_tokens} tokens in all. Put the most important "
        "lines first: lines past the bound are dropped from the end.",
    ]
    if card and card["tokens"] >= 0.9 * max_tokens:
        lines.append(
            f"The card is at {100 * card['tokens'] // max_tokens}% of its {max_tokens}-token budget: merge or drop "
            "lines until it is under 70%."
        )
    lines += ["", "CURRENT CARD:"]
    lines += [f"- {e['text']} (cites {', '.join(i[:8] for i in e['fact_ids'])})" for e in entries] or ["(empty)"]
    lines += [
        "",
        "CHANGES SINCE THE CURRENT CARD:",
        f"New facts not on the card: {', '.join(sorted(new)) or 'none'}",
        f"Cited facts no longer in the source set: {', '.join(sorted(gone)) or 'none'}",
        f"Pinned (profile_core) facts: {', '.join(sorted(i[:8] for i in pinned)) or 'none'}",
        "",
        "SOURCE FACTS (id category | subject | content | tags | source | learned):",
    ]
    lines += [_render_fact(f) for f in facts]
    if episodes:
        lines += ["", "RECENT EPISODES (for currency only, never a source):"] + [f"- {s}" for s in episodes]
    rejected = card["rejected"] if card else []
    lines += ["", "REJECTED LINES (the owner deleted these):"] + ([f"- {t}" for t in rejected] or ["(none)"])
    lines += ["", f"Call {TOOL_NAME} with the whole card."]
    return "\n".join(lines)


class ProfileCardWriter:
    """Rewrites the card when its source facts change. One run at a time: the sleep phase and a rebuild share a lock."""

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
        self.store = ProfileCardStore(database, self.agent_id)
        self._lock = asyncio.Lock()

    @property
    def busy(self) -> bool:
        """True while a run holds the lock."""
        return self._lock.locked()

    async def run_sleep(
        self, interrupted: Callable[[], bool] | None = None, session: AsyncSession | None = None
    ) -> dict:
        """The sleep phase's run: no model call when the source set is unchanged or the sleep was interrupted."""
        return await self._run(force=False, interrupted=interrupted or (lambda: False), session=session)

    async def rebuild(self, session: AsyncSession | None = None) -> dict:
        """The owner's run: rewrites even when the source set is unchanged. The result lists the added and removed
        lines."""
        return await self._run(force=True, interrupted=lambda: False, session=session)

    async def _source_facts(self, session: AsyncSession) -> tuple[list[Fact], set[str]]:
        """Active tier-1 facts by confidence then recency (limited), plus every ``profile_core`` fact.

        Rule facts from ``profile_exclude_sources`` are left out, as in the User Profile section.
        """
        base = select(Fact).where(
            Fact.agent_id == self.agent_id,
            Fact.category.in_(TIER1_FACT_CATEGORIES),
            Fact.active.is_(True),
        )
        exclude = self._settings.profile_exclude_sources
        if exclude:
            base = base.where(or_(Fact.source.is_(None), Fact.source.notin_(exclude), Fact.category != "rule"))
        base = base.execution_options(populate_existing=True)
        top = (
            (
                await session.execute(
                    base.order_by(Fact.confidence.desc(), Fact.learned_at.desc(), Fact.id).limit(
                        self._settings.profile_card_source_limit
                    )
                )
            )
            .scalars()
            .all()
        )
        tagged = (
            (await session.execute(base.where(Fact.tags.any(PROFILE_CORE_TAG)).order_by(Fact.learned_at, Fact.id)))
            .scalars()
            .all()
        )
        seen = {f.id for f in top}
        return list(top) + [f for f in tagged if f.id not in seen], {str(f.id) for f in tagged}

    async def _episodes(self, session: AsyncSession) -> list[str]:
        limit = self._settings.profile_card_episodes
        if limit <= 0:
            return []
        rows = (
            (
                await session.execute(
                    select(Episode.summary)
                    .where(
                        Episode.agent_id == self.agent_id,
                        Episode.ended_at.is_not(None),
                        Episode.active.is_not(False),
                    )
                    .order_by(Episode.ended_at.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return [_one_line(s)[:EPISODE_CHARS] for s in rows if s]

    async def _run(self, *, force: bool, interrupted: Callable[[], bool], session: AsyncSession | None) -> dict:
        async with self._lock:
            stats: dict[str, Any] = {
                "outcome": "",
                "rewritten": 0,
                "sources": 0,
                "dropped": 0,
                "rejected_dropped": 0,
                "pinned_added": 0,
                "trimmed": 0,
                "entries": 0,
                "tokens": 0,
            }
            async with _session_scope(self._db, session) as s:
                facts, pinned = await self._source_facts(s)
                card = await self.store.get(session=s)
                episodes = await self._episodes(s) if facts else []
            stats["sources"] = len(facts)
            if card is not None:
                stats["entries"], stats["tokens"] = len(card["entries"]), card["tokens"]
            digest = source_hash(facts)

            if not facts:
                stats["outcome"] = "empty_source"
            elif not force and card is not None and card["source_hash"] == digest:
                stats["outcome"] = "unchanged"
            elif interrupted():
                stats["outcome"] = "interrupted"
            elif self._llm is None:
                stats["outcome"] = "no_llm"
            if stats["outcome"]:
                return stats

            reply = await call_background_llm_structured(
                client=self._llm,
                model=self._settings.background_model,
                system_prompt=_SYSTEM_PROMPT,
                user_message=build_prompt(facts, pinned, card, episodes, self._settings),
                tool_name=TOOL_NAME,
                tool_description="Store the whole profile card. Every line cites the source facts it states.",
                output_schema=_CARD_SCHEMA,
                max_tokens=LLM_MAX_TOKENS,
            )
            entries, counts = settle_entries(
                reply.get("entries") if reply else None,
                facts,
                pinned,
                card["rejected"] if card else [],
                self._settings.profile_card_max_entries,
                self._settings.profile_card_max_tokens,
            )
            stats.update(counts)
            if entries is None:
                stats["outcome"] = "failed_closed"
                logger.warning(
                    "Profile card: reply not used, old card kept (reply=%s, dropped=%d)",
                    "none" if reply is None else len(reply.get("entries") or []),
                    counts["dropped"],
                )
                return stats

            async with _session_scope(self._db, session) as s:
                written = await self.store.write(entries, digest, session=s)
            old = {e["text"] for e in (card["entries"] if card else [])}
            new = {e["text"] for e in written["entries"]}
            stats.update(
                outcome="written",
                rewritten=1,
                entries=len(written["entries"]),
                tokens=written["tokens"],
                added=sorted(new - old),
                removed=sorted(old - new),
            )
            return stats
