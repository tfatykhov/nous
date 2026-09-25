"""Harness dashboard visibility (spec docs/superpowers/specs/2026-09-25-
harness-dashboard-visibility-design.md): read-only views of the durable
execution ledger (§3.2), the warn-mode rule and claim-check events (§3.3), and
the counts the Overview and the nav badges show (§3.4).

Everything is agent-scoped and read-only. Queries are typed ORM selects and
aggregation happens in Python, so SQLite (tests) and Postgres (prod) agree;
timestamps leave as aware UTC ISO strings.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, get_args
from uuid import UUID

from sqlalchemy import Text, and_, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from nous.api.execution_context import ContextKind
from nous.api.idempotency import is_keyed_tool
from nous.cognitive.ledger_store import KEY_HOLDING_STATUSES, REFUSAL_CODES
from nous.dag.approval import as_utc
from nous.storage.models import DAGNode, Event, ExecutionDAG, ExecutionLedgerEntry

WINDOWS: dict[str, timedelta] = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}
LEDGER_STATUSES = ("pending", "success", "error", "blocked", "unknown")
EFFECTS = ("write", "external", "irreversible")
ATTENTION_CAP = 20
MAX_Q = 100


def _iso(ts: datetime | None) -> str | None:
    return as_utc(ts).isoformat() if ts else None


def window_start(window: str, now: datetime | None = None) -> datetime:
    if window not in WINDOWS:
        raise ValueError(f"window must be one of {', '.join(WINDOWS)}")
    return (now or datetime.now(UTC)) - WINDOWS[window]


def _refusal_code(status: str, summary: str | None) -> str | None:
    """A blocked row stores 'refused by <code>' (ledger_store.record_blocked)."""
    if status != "blocked" or not summary or not summary.startswith("refused by "):
        return None
    code = summary.removeprefix("refused by ").strip()
    return code if code in REFUSAL_CODES else None


def _is_tombstone(row: Any) -> bool:
    """Retention keeps a key-holding row but clears what it held (ledger_store.prune).

    Decided by the empty ``key_args`` alone: a keyed send always stores its
    recipients or chat (durable_key_args), so only retention empties them —
    while ``result_summary`` is written again by the Ledger's own release
    statements (concat_ws), so it cannot tell a trimmed row apart."""
    return bool(
        row.idempotency_key
        and row.status in ("success", "unknown")
        and not row.key_args
    )


def _parse_before(before: str | None) -> tuple[datetime, UUID] | None:
    if not before:
        return None
    try:
        ts_s, id_s = before.rsplit(",", 1)
        ts = datetime.fromisoformat(ts_s)
        return (as_utc(ts), UUID(id_s))
    except ValueError as exc:
        raise ValueError("before must be '<iso timestamp>,<row id>'") from exc


def _escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _stats(rows: list[Any]) -> dict[str, int]:
    stats = {
        "calls": 0, "sends": 0, "external": 0, "repeat_sends_refused": 0, "blocked": 0,
        "unknown": 0, "unknown_keyed": 0, "errors": 0, "pending": 0,
    }
    for r in rows:
        stats["calls"] += 1
        stats["sends"] += is_keyed_tool(r.tool_name)  # the tools send de-duplication keys
        stats["external"] += r.side_effect_type in ("external", "irreversible")
        if r.status == "blocked":
            if _refusal_code(r.status, r.result_summary) == "duplicate":
                stats["repeat_sends_refused"] += 1
            else:
                stats["blocked"] += 1
        elif r.status == "unknown":
            stats["unknown"] += 1
            stats["unknown_keyed"] += bool(r.idempotency_key)
        elif r.status == "error":
            stats["errors"] += 1
        elif r.status == "pending":
            stats["pending"] += 1
    return stats


def attention_filter(agent_id: str):
    """The ONE predicate for a send in doubt (spec §3.2): only a KEYED row
    holds anything — an unkeyed or non-send unknown (a timed-out script, a
    swept orphan) has nothing for a person to settle."""
    L = ExecutionLedgerEntry
    return and_(L.agent_id == agent_id, L.status == "unknown", L.idempotency_key.is_not(None))


async def in_doubt(session: AsyncSession, agent_id: str, limit: int, *,
                   ledger_persisted: bool) -> tuple[int, list[Any]]:
    """Every send in doubt counted, and the newest ``limit`` of them — the
    Ledger callout and the nav badge both show THIS count, so they agree
    however many there are (a page length would cap at the page).

    With ledger persistence off no LedgerStore is installed (main.py), so no
    retry is refused: a keyed unknown row written earlier holds nothing, and
    reporting it as a held send would be false. It stays in the ledger table
    as history, and holds again only if persistence is switched back on."""
    if not ledger_persisted:
        return 0, []
    L = ExecutionLedgerEntry
    total = (await session.execute(
        select(func.count()).select_from(L).where(attention_filter(agent_id))
    )).scalar_one()
    rows = (await session.execute(
        select(L).where(attention_filter(agent_id))
        .order_by(L.created_at.desc(), L.id.desc()).limit(limit)
    )).scalars().all()
    return total, list(rows)


async def _names(session: AsyncSession, agent_id: str, rows: list[Any]) -> tuple[dict, dict]:
    dag_ids = {r.dag_id for r in rows if r.dag_id}
    node_ids = {r.dag_node_id for r in rows if r.dag_node_id}
    dags: dict[UUID, str] = {}
    nodes: dict[UUID, str] = {}
    if dag_ids:
        res = await session.execute(
            select(ExecutionDAG.id, ExecutionDAG.name)
            .where(ExecutionDAG.agent_id == agent_id, ExecutionDAG.id.in_(dag_ids))
        )
        dags = {r.id: r.name for r in res}
    if node_ids and dags:
        res = await session.execute(
            select(DAGNode.id, DAGNode.name)
            .where(DAGNode.id.in_(node_ids), DAGNode.dag_id.in_(list(dags)))
        )
        nodes = {r.id: r.name for r in res}
    return dags, nodes


async def _holders(session: AsyncSession, agent_id: str, rows: list[Any]) -> dict[tuple, Any]:
    """The row currently holding each (tool, key) seen on this page."""
    L = ExecutionLedgerEntry
    pairs = {(r.tool_name, r.idempotency_key) for r in rows if r.idempotency_key}
    if not pairs:
        return {}
    res = await session.execute(
        select(L.id, L.tool_name, L.idempotency_key, L.status, L.created_at, L.external_ref,
               L.session_id, L.turn)
        .where(
            L.agent_id == agent_id,
            L.status.in_(KEY_HOLDING_STATUSES),
            L.idempotency_key.in_({k for _, k in pairs}),
        )
    )
    held: dict[tuple, Any] = {}
    for h in res:
        key = (h.tool_name, h.idempotency_key)
        if key in pairs and (key not in held or as_utc(h.created_at) > as_utc(held[key].created_at)):
            held[key] = h
    return held


def _row_view(r: Any, dags: dict, nodes: dict, holders: dict) -> dict:
    holder = holders.get((r.tool_name, r.idempotency_key)) if r.idempotency_key else None
    return {
        "id": str(r.id),
        "created_at": _iso(r.created_at),
        "completed_at": _iso(r.completed_at),
        "dispatched_at": _iso(r.dispatched_at),
        "tool_name": r.tool_name,
        "context_kind": r.context_kind,
        "side_effect_type": r.side_effect_type,
        "status": r.status,
        "refusal_code": _refusal_code(r.status, r.result_summary),
        "result_summary": r.result_summary,
        "key_args": r.key_args or {},
        "idempotency_key": r.idempotency_key,
        "external_ref": r.external_ref,
        "session_id": r.session_id,
        "parent_session_id": r.parent_session_id,
        "subtask_id": str(r.subtask_id) if r.subtask_id else None,
        "dag_id": str(r.dag_id) if r.dag_id else None,
        "dag_name": dags.get(r.dag_id),
        "dag_node_id": str(r.dag_node_id) if r.dag_node_id else None,
        "node_name": nodes.get(r.dag_node_id),
        "turn": r.turn,
        "tombstone": _is_tombstone(r),
        "held_by": (
            {"id": str(holder.id), "status": holder.status, "created_at": _iso(holder.created_at),
             "external_ref": holder.external_ref, "session_id": holder.session_id, "turn": holder.turn}
            if holder is not None and holder.id != r.id else None
        ),
    }


async def get_execution_data(
    session: AsyncSession,
    agent_id: str,
    *,
    modes: dict[str, Any],
    window: str = "24h",
    context: str | None = None,
    status: str | None = None,
    effect: str | None = None,
    q: str | None = None,
    limit: int = 50,
    before: str | None = None,
) -> dict[str, Any]:
    """GET /dashboard/execution (spec §3.2). Raises ValueError on bad input."""
    since = window_start(window)
    if context and context not in get_args(ContextKind):
        raise ValueError(f"unknown context {context!r}")
    if status and status not in LEDGER_STATUSES:
        raise ValueError(f"unknown status {status!r}")
    if effect and effect not in EFFECTS:
        raise ValueError(f"unknown effect {effect!r}")
    if not 1 <= limit <= 200:
        raise ValueError("limit must be between 1 and 200")
    if q is not None and len(q) > MAX_Q:
        raise ValueError(f"q must be at most {MAX_Q} characters")
    cursor = _parse_before(before)

    L = ExecutionLedgerEntry
    in_window = and_(L.agent_id == agent_id, L.created_at >= since)

    stat_rows = (await session.execute(
        select(L.tool_name, L.side_effect_type, L.status, L.result_summary, L.idempotency_key)
        .where(in_window)
    )).all()

    attention_total, attention = await in_doubt(
        session, agent_id, ATTENTION_CAP, ledger_persisted=bool(modes.get("persist")))

    stmt = select(L).where(in_window)
    if context:
        stmt = stmt.where(L.context_kind == context)
    if status:
        stmt = stmt.where(L.status == status)
    if effect:
        stmt = stmt.where(L.side_effect_type == effect)
    if q:
        pattern = f"%{_escape_like(q.lower())}%"
        stmt = stmt.where(or_(
            func.lower(L.tool_name).like(pattern, escape="\\"),
            func.lower(func.coalesce(L.idempotency_key, "")).like(pattern, escape="\\"),
            func.lower(func.coalesce(L.external_ref, "")).like(pattern, escape="\\"),
            func.lower(cast(L.key_args, Text)).like(pattern, escape="\\"),
        ))
    if cursor:
        ts, row_id = cursor
        stmt = stmt.where(or_(L.created_at < ts, and_(L.created_at == ts, L.id < row_id)))
    page = (await session.execute(
        stmt.order_by(L.created_at.desc(), L.id.desc()).limit(limit + 1)
    )).scalars().all()
    has_more = len(page) > limit
    page = page[:limit]

    both = [*page, *attention]
    dags, nodes = await _names(session, agent_id, both)
    holders = await _holders(session, agent_id, both)
    last = page[-1] if page else None
    return {
        "modes": modes,
        "stats": _stats(stat_rows),
        "attention": [_row_view(r, dags, nodes, holders) for r in attention],
        "attention_total": attention_total,
        "rows": [_row_view(r, dags, nodes, holders) for r in page],
        "next_before": f"{_iso(last.created_at)},{last.id}" if has_more and last else None,
    }


# ── §3.3 Harness: what each rule flagged ──────────────────────────────────

OFFERED = "harness_unoffered_tool_call"
POLICY = "harness_context_policy_violation"
CLAIMS = "f026_claim_verification"
_RULE_OF = {OFFERED: "offered_set", POLICY: "context_policy", CLAIMS: "claims"}
PATTERN_CAP = 20


def _claims_measured_from(evidence_since: datetime | None, first_claim: datetime | None,
                          legacy: dict[str, int]) -> datetime | None:
    """When claim evidence levels are known from. Every turn writes a claim
    event, so with no pre-2c event in the window every turn in it carried
    evidence levels (a day with none had no turns): measured since the first
    claim event ever. With pre-2c events in view, only from the first
    post-2c one."""
    if legacy["events"]:
        return evidence_since
    return as_utc(first_claim)


def _blank_unmeasured(days: dict[str, dict], persisted: bool,
                      series: dict[str, tuple[str | None, datetime | None]]) -> None:
    """A quiet day the record cannot vouch for is a gap (None), never a zero —
    a chart must not draw "nothing happened" over "nothing was measured".

    ``series`` maps each key to (its rule's mode NOW, the first moment the
    record shows it running). For the two flag-only rules that moment is the
    first flag: they write nothing on a clean day, so a quiet day before it
    may be clean or may predate the rule (a fresh deploy) — it cannot be
    told apart, so it stays a gap. The claim check writes every turn, so its
    first event is when recording began. A rule that is off now leaves its
    quiet days blank; a day with a count keeps it whatever the mode is now.
    Modes are not stored per day, so a rule switched off today blanks last
    week's quiet days too — the trade for never inventing a clean day.
    """
    for day in days.values():
        for key, (mode, start) in series.items():
            quiet = not day[key]
            before = start is None or day["date"] < start.date().isoformat()
            if not persisted or (quiet and (before or mode == "off")):
                day[key] = None


def _ranked(counter: dict[str, int]) -> list[dict[str, Any]]:
    return [{"key": k, "count": n} for k, n in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))]


def _bump(counter: dict, key: Any) -> None:
    counter[key] = counter.get(key, 0) + 1


async def get_harness_data(
    session: AsyncSession,
    agent_id: str,
    *,
    modes: dict[str, str],
    events_persisted: bool,
    window: str = "7d",
) -> dict[str, Any]:
    """GET /dashboard/harness (spec §3.3).

    Never sums across rules: one call in warn mode can be flagged by the
    offered-tool rule AND the context policy (runner._authorize_tool_call),
    so each rule reports its own events, grouped by the mode recorded ON the
    event (a total after a flip must not mix warn with enforce). Claim events
    written before harness 2c carry no claims[]; they are counted apart.
    """
    now = datetime.now(UTC)
    since = window_start(window, now)
    types = (OFFERED, POLICY, CLAIMS)
    # Persistence off: the page says nothing was measured, so no event —
    # not even one written before the switch — may reach any section of it.
    first: dict[str, Any] = {} if not events_persisted else {
        r.event_type: r.first
        for r in (await session.execute(
            select(Event.event_type, func.min(Event.created_at).label("first"))
            .where(Event.agent_id == agent_id, Event.event_type.in_(types))
            .group_by(Event.event_type)
        ))
    }
    events = [] if not events_persisted else (await session.execute(
        select(Event.event_type, Event.data, Event.session_id, Event.created_at)
        .where(Event.agent_id == agent_id, Event.event_type.in_(types), Event.created_at >= since)
        .order_by(Event.created_at)
    )).all()

    by_mode: dict[str, dict] = {OFFERED: {}, POLICY: {}}
    by_context: dict[str, dict] = {OFFERED: {}, POLICY: {}}
    by_tool: dict[str, int] = {}
    by_violation: dict[str, int] = {}
    evidence = {"exact": 0, "plausible": 0, "none": 0}
    none_by_mode: dict[str, int] = {}  # a correction was queued only under enforce
    claims_by_mode: dict[str, int] = {}
    turns_with_claims = 0
    legacy = {"events": 0, "violations": 0}
    evidence_since: datetime | None = None
    days = {}
    day = since.date()
    while day <= now.date():
        days[day.isoformat()] = {"date": day.isoformat(), "offered_set": 0, "context_policy": 0, "claims_none": 0}
        day += timedelta(days=1)
    groups: dict[tuple, dict] = {}

    def pattern(rule, mode, context, tool, violation, at, session_id, snippet=None):
        key = (rule, mode, context, tool, violation)
        g = groups.setdefault(key, {"rule": rule, "mode": mode, "context": context, "tool": tool,
                                    "violation": violation, "count": 0, "last_seen": None,
                                    "latest_session": None, "snippet": None})
        g["count"] += 1
        g["last_seen"], g["latest_session"] = _iso(at), session_id
        if snippet:
            g["snippet"] = snippet

    for ev in events:
        data = ev.data if isinstance(ev.data, dict) else {}
        at = as_utc(ev.created_at)
        bucket = days.get(at.date().isoformat())
        mode = data.get("mode") or "unknown"
        if ev.event_type in (OFFERED, POLICY):
            _bump(by_mode[ev.event_type], mode)
            _bump(by_context[ev.event_type], data.get("context_kind") or "unknown")
            violation = "not offered" if ev.event_type == OFFERED else (data.get("violation") or "unknown")
            if ev.event_type == OFFERED:
                _bump(by_tool, data.get("tool_name") or "unknown")
            else:
                _bump(by_violation, violation)
            if bucket:
                bucket[_RULE_OF[ev.event_type]] += 1
            pattern(_RULE_OF[ev.event_type], mode, data.get("context_kind"), data.get("tool_name"),
                    violation, at, ev.session_id)
            continue
        claims = data.get("claims")
        if not isinstance(claims, list):  # written before harness 2c
            legacy["events"] += 1
            legacy["violations"] += int(data.get("violation_count") or 0)
            continue
        evidence_since = evidence_since or at
        _bump(claims_by_mode, mode)
        if claims:
            turns_with_claims += 1
        for c in claims:
            level = c.get("evidence") if isinstance(c, dict) else None
            if level in evidence:
                evidence[level] += 1
            if level == "none":
                _bump(none_by_mode, mode)
                if bucket:
                    bucket["claims_none"] += 1
                pattern("claims", mode, None, None, "no evidence", at, ev.session_id,
                        snippet=(c.get("text") or None))

    patterns = sorted(groups.values(), key=lambda g: (-g["count"], g["last_seen"] or ""))
    _blank_unmeasured(days, events_persisted, {
        "offered_set": (modes.get("offered_set"), as_utc(first.get(OFFERED))),
        "context_policy": (modes.get("context_policy"), as_utc(first.get(POLICY))),
        "claims_none": (modes.get("claim_verification"),
                        _claims_measured_from(evidence_since, first.get(CLAIMS), legacy)),
    })
    return {
        "window": window,
        "events_persisted": events_persisted,
        "rules": {
            "offered_set": {
                "mode": modes.get("offered_set"), "first_event_at": _iso(first.get(OFFERED)),
                "by_mode": by_mode[OFFERED], "by_context": _ranked(by_context[OFFERED]),
                "by_tool": _ranked(by_tool),
            },
            "context_policy": {
                "mode": modes.get("context_policy"), "first_event_at": _iso(first.get(POLICY)),
                "by_mode": by_mode[POLICY], "by_violation": _ranked(by_violation),
                "by_context": _ranked(by_context[POLICY]),
            },
            "claims": {
                "mode": modes.get("claim_verification"), "first_event_at": _iso(first.get(CLAIMS)),
                "evidence_since": _iso(evidence_since), "by_evidence": evidence,
                "by_mode": claims_by_mode, "none_by_mode": none_by_mode,
                "turns_with_claims": turns_with_claims, "legacy": legacy,
            },
        },
        "daily": list(days.values()),
        "patterns": patterns[:PATTERN_CAP],
    }


# ── §3.4 Attention: the Overview strip and the nav badges ──────────────────


def question_waiting_filter(agent_id: str):
    """An approval step waiting on a person — the SAME predicate the DAG tab
    applies in dashboard_queries._attach_approvals (awaiting_input, in a live
    DAG); a node stranded in an ended DAG is not a question."""
    from nous.dag.store import LIVE_DAG_STATUSES

    return and_(
        ExecutionDAG.agent_id == agent_id,
        ExecutionDAG.status.in_(sorted(LIVE_DAG_STATUSES)),
        DAGNode.node_type == "approval",
        DAGNode.status == "awaiting_input",
    )


def _recipients(key_args: dict) -> list[str]:
    out: list[str] = []
    for field in ("to", "cc", "chat_id"):
        value = (key_args or {}).get(field)
        if isinstance(value, list):
            out += [str(v) for v in value]
        elif value:
            out.append(str(value))
    return out


async def get_attention_data(
    session: AsyncSession,
    agent_id: str,
    *,
    modes: dict[str, str],
    events_persisted: bool,
    ledger_persisted: bool,
) -> dict[str, Any]:
    """GET /dashboard/attention (spec §3.4) — counts that must agree with
    the tabs they link to."""
    from nous.dag.approval import label_of
    questions = (await session.execute(
        select(DAGNode.name, DAGNode.answer_deadline, DAGNode.approval_spec, ExecutionDAG.name.label("dag_name"))
        .join(ExecutionDAG, ExecutionDAG.id == DAGNode.dag_id)
        .where(question_waiting_filter(agent_id))
    )).all()
    questions.sort(key=lambda r: (r.answer_deadline is None, as_utc(r.answer_deadline) or datetime.max))
    nxt = questions[0] if questions else None

    sends_in_doubt, newest = await in_doubt(session, agent_id, 1, ledger_persisted=ledger_persisted)
    latest = newest[0] if newest else None
    dags, _ = await _names(session, agent_id, [latest] if latest else [])

    since = datetime.now(UTC) - timedelta(days=7)
    warn = {OFFERED: 0, POLICY: 0}
    refused = {OFFERED: 0, POLICY: 0}
    for ev in (await session.execute(
        select(Event.event_type, Event.data)
        .where(Event.agent_id == agent_id, Event.event_type.in_((OFFERED, POLICY)), Event.created_at >= since)
    )):
        mode = ev.data.get("mode") if isinstance(ev.data, dict) else None
        if mode == "warn":
            warn[ev.event_type] += 1
        elif mode == "enforce":
            refused[ev.event_type] += 1

    return {
        "questions_waiting": len(questions),
        "next": {
            "dag_name": nxt.dag_name, "node_name": nxt.name, "deadline": _iso(nxt.answer_deadline),
            "default_label": label_of(nxt.approval_spec or {}, (nxt.approval_spec or {}).get("default_option")),
        } if nxt else None,
        "sends_in_doubt": sends_in_doubt,
        "latest_in_doubt": {
            "tool_name": latest.tool_name, "recipients": _recipients(latest.key_args),
            "created_at": _iso(latest.created_at), "dag_name": dags.get(latest.dag_id),
            "tombstone": _is_tombstone(latest),
        } if latest else None,
        "ledger_persisted": ledger_persisted,
        "harness": {
            "events_persisted": events_persisted,
            "offered_set": {"mode": modes.get("offered_set"), "warn_7d": warn[OFFERED],
                            "refused_7d": refused[OFFERED]},
            "context_policy": {"mode": modes.get("context_policy"), "warn_7d": warn[POLICY],
                               "refused_7d": refused[POLICY]},
        },
    }
