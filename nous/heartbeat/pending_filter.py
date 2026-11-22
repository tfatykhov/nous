"""F116: filters of the self-initiated check's pending items (docs/features/F116-stale-pending-findings.md).

Three layers, each with a mode: ``off`` (not evaluated), ``shadow`` (evaluated and recorded; the item still surfaces)
or ``on`` (the item is hidden).

- L1 kind: rule, preference and person facts, and triage or suppression notes, are not tasks.
- L4 age: a status line written by the enumerative extractor counts for ``snapshot_ttl_hours``, any other fact for
  ``fact_max_age_hours``, an ongoing episode for ``episode_max_age_hours``; a fact that ends with a question mark is a
  question, not a task.
- L3a supersession: a superseded or inactive fact is hidden. A newer active fact with the same subject and attribute
  keys is recorded in shadow only, whatever the mode.

The check calls the filter last, right before it would build a finding, so the filter only sees items that passed
every test the check already makes. Every failure keeps the item: a mode that does not parse is ``off``, a field of an
unexpected type does not fire the rule that needs it, an error while evaluating keeps the item and is counted, and a
failed read of the rows keeps every item a rule would need the row for.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import and_, exists, select, true
from sqlalchemy.orm import aliased

from nous.heartbeat.schemas import Finding, SuppressionReport

logger = logging.getLogger(__name__)

Mode = Literal["off", "shadow", "on"]
_MODES: frozenset[str] = frozenset({"off", "shadow", "on"})

# The source of the point-in-time status lines the snapshot TTL applies to.
SNAPSHOT_SOURCE = "enumerative_extractor"
# A stored actionability verdict at least this confident exempts a fact from the age cap.
EXEMPT_CONFIDENCE = 0.95
_KIND_CATEGORIES = frozenset({"rule", "preference", "person"})
_NOTE_PREFIX = re.compile(r"^\s*(HEARTBEAT FALSE-POSITIVE|Heartbeat triage\b|HEARTBEAT SUPPRESSION)", re.IGNORECASE)
_NOTE_TAG = "hb-note"
# The same leading filler the check strips before its question test on episodes (checks.py, _FILLER_PREFIX).
_FILLER_PREFIX = re.compile(r"^\s*(?:(?:hey|ok|okay)[\s,!.:-]+)+", re.IGNORECASE)
# Reasons the daily digest line counts.
AGE_REASONS = frozenset({"age_snapshot", "age_fact", "age_ongoing"})
# Seconds one batched read of the rows may take. After a failed read no other read runs in that check run.
READ_TIMEOUT_SECONDS = 2.0

# fact id -> (last_confirmed, a newer active fact has the same subject and attribute keys)
Rows = dict[str, tuple[datetime | None, bool]]
FetchRows = Callable[[list[str]], Awaitable[Rows]]


def parse_mode(value: object) -> Mode:
    """``off``, ``shadow`` or ``on`` from a setting; anything else (any other text, blank, not text) is ``off``."""
    if isinstance(value, str):
        word = value.strip().lower()
        if word in _MODES:
            return word  # type: ignore[return-value]
    return "off"


def _hours(value: object, default: float) -> float:
    if isinstance(value, int | float) and not isinstance(value, bool) and value > 0:
        return float(value)
    return default


@dataclass(frozen=True)
class PendingFilterConfig:
    """The three modes and the three age limits (hours)."""

    kind: Mode = "off"
    age: Mode = "off"
    supersede: Mode = "off"
    snapshot_ttl_hours: float = 336.0
    fact_max_age_hours: float = 720.0
    episode_max_age_hours: float = 720.0

    @classmethod
    def from_settings(cls, settings: object) -> PendingFilterConfig:
        """Read the ``heartbeat_pending_*`` settings; a missing or unusable value gives the default."""
        d = cls()

        def setting(name: str) -> object:
            return getattr(settings, f"heartbeat_pending_{name}", None)

        return cls(
            kind=parse_mode(setting("kind_mode")),
            age=parse_mode(setting("age_mode")),
            supersede=parse_mode(setting("supersede_mode")),
            snapshot_ttl_hours=_hours(setting("snapshot_ttl_hours"), d.snapshot_ttl_hours),
            fact_max_age_hours=_hours(setting("fact_max_age_hours"), d.fact_max_age_hours),
            episode_max_age_hours=_hours(setting("episode_max_age_hours"), d.episode_max_age_hours),
        )


@dataclass
class Candidate:
    """A fact or episode the check is about to raise, with the fields the rules read (None: unknown)."""

    kind: Literal["fact", "episode"]
    id: str
    text: str
    summary: str  # exactly the summary the finding would carry
    source_label: str  # the finding's source: "facts" or "episodes"
    category: str | None = None
    source: str | None = None
    tags: frozenset[str] = frozenset()
    actionable_confidence: float | None = None
    learned_at: datetime | None = None
    event_date: date | None = None
    active: bool | None = None
    superseded_by: str | None = None
    outcome: str | None = None
    started_at: datetime | None = None
    # From the batched read: whether the row was read, and what it said.
    rows_known: bool = False
    last_confirmed: datetime | None = None
    newer_same_key: bool | None = None


@dataclass(frozen=True)
class Verdict:
    decision: Literal["keep", "drop", "shadow_drop"]
    reason: str | None = None


KEEP = Verdict("keep", None)


# --- Building a candidate (defensive: a value of an unexpected type is None) ---


def _str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return None


def _date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    return value if isinstance(value, date) else None


def _number(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _tags(value: object) -> frozenset[str]:
    if isinstance(value, list | tuple | set | frozenset):
        return frozenset(t.lower() for t in value if isinstance(t, str))
    return frozenset()


def _ref(value: object) -> str | None:
    if isinstance(value, uuid.UUID):
        return str(value)
    return value if isinstance(value, str) and value else None


def fact_candidate(fact: Any) -> Candidate:
    """A candidate from a fact search result (``FactSummary``, or any object with some of its fields)."""
    content = getattr(fact, "content", None)
    return Candidate(
        kind="fact",
        id=str(fact.id),
        text=content if isinstance(content, str) else "",
        summary=f"Pending action: {fact.content[:100]}",
        source_label="facts",
        category=_str(getattr(fact, "category", None)),
        source=_str(getattr(fact, "source", None)),
        tags=_tags(getattr(fact, "tags", None)),
        actionable_confidence=_number(getattr(fact, "actionable_confidence", None)),
        learned_at=_datetime(getattr(fact, "learned_at", None)),
        event_date=_date(getattr(fact, "event_date", None)),
        active=_bool(getattr(fact, "active", None)),
        superseded_by=_ref(getattr(fact, "superseded_by", None)),
    )


def episode_candidate(ep: Any) -> Candidate:
    """A candidate from an episode search result (``EpisodeSummary``)."""
    summary_text = getattr(ep, "summary", None) or getattr(ep, "title", "")
    return Candidate(
        kind="episode",
        id=str(ep.id),
        text=summary_text if isinstance(summary_text, str) else "",
        summary=f"Unresolved commitment: {summary_text[:100]}",
        source_label="episodes",
        outcome=_str(getattr(ep, "outcome", None)),
        started_at=_datetime(getattr(ep, "started_at", None)),
    )


# --- The rules ---


def _kind_reason(c: Candidate) -> str | None:
    if c.kind != "fact":
        return None
    if (c.category or "").strip().lower() in _KIND_CATEGORIES:
        return "kind_rule"
    if _NOTE_PREFIX.match(c.text) or _NOTE_TAG in c.tags:
        return "kind_note"
    return None


def _age_reason(c: Candidate, cfg: PendingFilterConfig, now: datetime) -> str | None:
    if c.kind == "episode":
        if c.outcome == "ongoing" and c.started_at is not None:
            if now - c.started_at > timedelta(hours=cfg.episode_max_age_hours):
                return "age_ongoing"
        return None
    if _FILLER_PREFIX.sub("", c.text).strip().endswith("?"):
        return "question_form"
    if c.learned_at is None:
        return None
    if c.event_date is not None and c.event_date >= now.astimezone(UTC).date():
        return None
    if c.actionable_confidence is not None and c.actionable_confidence >= EXEMPT_CONFIDENCE:
        return None
    snapshot = c.source == SNAPSHOT_SOURCE
    limit = timedelta(hours=cfg.snapshot_ttl_hours if snapshot else cfg.fact_max_age_hours)
    if now - c.learned_at <= limit:
        return None
    # Past its limit by learned_at: a recent confirmation keeps it, and without the row it is kept.
    if not c.rows_known:
        return None
    if c.last_confirmed is not None and now - c.last_confirmed <= limit:
        return None
    return "age_snapshot" if snapshot else "age_fact"


def _supersede_reason(c: Candidate) -> str | None:
    if c.kind != "fact":
        return None
    if c.superseded_by is not None:
        return "superseded"
    if c.active is False:
        return "inactive"
    if c.rows_known and c.newer_same_key is True:
        return "newer_same_key"
    return None


def evaluate(c: Candidate, cfg: PendingFilterConfig, now: datetime) -> Verdict:
    """The verdict on one candidate. Pure: no I/O, ``now`` is passed in (a naive one is read as UTC).

    Layers in the order kind, age, supersession; a layer whose mode is off is not evaluated. The first hit of a layer
    that is ``on`` hides the item (``drop``); otherwise the first hit of a layer in ``shadow`` records it
    (``shadow_drop``). ``newer_same_key`` is recorded in shadow only, whatever the mode.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    hits: list[tuple[Mode, str]] = []
    if cfg.kind != "off" and (reason := _kind_reason(c)):
        hits.append((cfg.kind, reason))
    if cfg.age != "off" and (reason := _age_reason(c, cfg, now)):
        hits.append((cfg.age, reason))
    if cfg.supersede != "off" and (reason := _supersede_reason(c)):
        hits.append(("shadow" if reason == "newer_same_key" else cfg.supersede, reason))
    for mode, reason in hits:
        if mode == "on":
            return Verdict("drop", reason)
    for mode, reason in hits:
        if mode == "shadow":
            return Verdict("shadow_drop", reason)
    return KEEP


# --- One check run: the ledger, and the filter the check holds ---


@dataclass
class Ledger:
    """The state of one check run: verdicts by item, the rows read, and the report being built."""

    now: datetime = field(default_factory=lambda: datetime.now(UTC))
    report: SuppressionReport = field(default_factory=SuppressionReport)
    verdicts: dict[tuple[str, str], Verdict] = field(default_factory=dict)
    rows: Rows = field(default_factory=dict)
    requested: set[str] = field(default_factory=set)  # fact ids a read was attempted for
    age_hidden: list[str] = field(default_factory=list)  # ids hidden by an age rule (the daily digest)
    read_failed: bool = False
    logged_error: bool = False
    logged_read_failure: bool = False


class PendingFilter:
    """The filters as the self-initiated check uses them: one instance per check, one ``Ledger`` per run.

    ``enabled`` is False when every mode is off: the check then builds no ledger and calls nothing here.
    ``needs_rows`` is True when a layer that reads ``last_confirmed`` or the slot keys is not off.
    ``fetch_rows`` replaces the batched read (tests).
    """

    def __init__(
        self,
        cfg: PendingFilterConfig,
        *,
        db: Any = None,
        agent_id: str = "",
        check_name: str = "self_initiated",
        fetch_rows: FetchRows | None = None,
    ) -> None:
        self.cfg = cfg
        self.enabled = cfg.kind != "off" or cfg.age != "off" or cfg.supersede != "off"
        self.needs_rows = cfg.age != "off" or cfg.supersede != "off"
        self._db = db
        self._agent_id = agent_id
        self._check_name = check_name
        self._fetch_rows: FetchRows | None = fetch_rows or (self._read_rows if db is not None else None)

    def new_ledger(self) -> Ledger:
        return Ledger()

    async def prefetch(self, facts: Iterable[Any], ledger: Ledger) -> None:
        """Read ``last_confirmed`` and the same-key flag of one search result list, in one bounded read.

        Never raises (a cancellation passes through). On a failure or a timeout no further read runs this run, the
        items of the list stay without their row (a rule that needs it does not fire) and one error is counted.
        """
        if not self.needs_rows or ledger.read_failed or self._fetch_rows is None:
            return
        try:
            ids: list[str] = []
            for fact in facts:
                fid = str(fact.id)
                if fid in ledger.requested:
                    continue
                try:
                    uuid.UUID(fid)
                except ValueError:
                    continue
                ledger.requested.add(fid)
                ids.append(fid)
            if not ids:
                return
            ledger.rows.update(await asyncio.wait_for(self._fetch_rows(ids), READ_TIMEOUT_SECONDS))
        except Exception:
            ledger.read_failed = True
            ledger.report.errors += 1
            if not ledger.logged_read_failure:
                ledger.logged_read_failure = True
                logger.warning(
                    "F116: the pending filter could not read the fact rows; items that need them surface this run",
                    exc_info=True,
                )

    def screen_fact(self, fact: Any, ledger: Ledger) -> Verdict:
        """The verdict on a fact the check is about to raise, recorded once per run. Never raises."""
        try:
            key = ("fact", str(fact.id))
            cached = ledger.verdicts.get(key)
            if cached is not None:
                return cached
            c = fact_candidate(fact)
            row = ledger.rows.get(c.id)
            if row is not None:
                c.rows_known = True
                c.last_confirmed = _datetime(row[0])
                c.newer_same_key = bool(row[1])
            return self._decide(c, key, ledger)
        except Exception:
            return self._failed(ledger)

    def screen_episode(self, ep: Any, ledger: Ledger) -> Verdict:
        """The verdict on an episode the check is about to raise, recorded once per run. Never raises."""
        try:
            key = ("episode", str(ep.id))
            cached = ledger.verdicts.get(key)
            if cached is not None:
                return cached
            return self._decide(episode_candidate(ep), key, ledger)
        except Exception:
            return self._failed(ledger)

    def _decide(self, c: Candidate, key: tuple[str, str], ledger: Ledger) -> Verdict:
        verdict = evaluate(c, self.cfg, ledger.now)
        if verdict.decision != "keep" and verdict.reason is not None:
            fingerprint = Finding(source=c.source_label, summary=c.summary, check_name=self._check_name).fingerprint()
            ledger.report.add(
                verdict.reason,
                shadow=verdict.decision == "shadow_drop",
                fingerprint=fingerprint,
                kind=c.kind,
                item_id=c.id,
            )
            if verdict.decision == "drop" and verdict.reason in AGE_REASONS:
                ledger.age_hidden.append(c.id)
        ledger.verdicts[key] = verdict
        return verdict

    @staticmethod
    def _failed(ledger: Ledger) -> Verdict:
        ledger.report.errors += 1
        if not ledger.logged_error:
            ledger.logged_error = True
            logger.error("F116: the pending filter could not evaluate an item; it surfaces", exc_info=True)
        return KEEP

    async def _read_rows(self, ids: list[str]) -> Rows:
        """One read: ``last_confirmed`` of each fact and whether a newer active, not superseded fact of the same
        agent has the same non-null subject and attribute keys (a pair linked by ``contradiction_of`` does not
        count). Named columns only; no write."""
        from nous.storage.models import Fact

        newer = aliased(Fact)
        newer_same_key = and_(
            Fact.subject_key.is_not(None),
            Fact.attribute_key.is_not(None),
            exists().where(
                newer.agent_id == Fact.agent_id,
                newer.subject_key == Fact.subject_key,
                newer.attribute_key == Fact.attribute_key,
                newer.subject_key.is_not(None),
                newer.active == true(),
                newer.superseded_by.is_(None),
                newer.id != Fact.id,
                newer.learned_at > Fact.learned_at,
                newer.contradiction_of.is_distinct_from(Fact.id),
                Fact.contradiction_of.is_distinct_from(newer.id),
            ),
        ).label("newer_same_key")
        stmt = select(Fact.id, Fact.last_confirmed, newer_same_key).where(
            Fact.agent_id == self._agent_id,
            Fact.id.in_([uuid.UUID(i) for i in ids]),
        )
        async with self._db.session() as session:
            result = await session.execute(stmt)
            return {str(row.id): (row.last_confirmed, bool(row.newer_same_key)) for row in result}
