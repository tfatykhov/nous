"""Memory write health (precision plan P4): migration 099, the rollup, the rules, the hard-delete detector, the
acknowledgements and the check.

Every test that writes rows uses an agent of its own (``test-mwh-<uuid>``): the detector counts per agent, so another
test's rows can never move it.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import text

from nous.heartbeat.memory_write_health_check import MemoryWriteHealthCheck, findings_for
from nous.observability import memory_write_health as mwh
from nous.storage.migrator import _split_sql_statements
from nous.storage.models import Event, Fact, GraphEdge, MemoryWriteHealthDaily

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "sql" / "migrations" / "099_memory_write_health_daily.sql"
TODAY = date(2026, 11, 5)


# ---------------------------------------------------------------------------------------------------------------------
# Migration 099
# ---------------------------------------------------------------------------------------------------------------------


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _code() -> str:
    return "\n".join(ln for ln in _sql().splitlines() if not ln.lstrip().startswith("--"))


def test_the_migration_is_one_create_table_and_nothing_else():
    (statement,) = _split_sql_statements(_sql())
    assert statement.startswith("CREATE TABLE IF NOT EXISTS nous_system.memory_write_health_daily")
    code = _code().upper()
    for word in ("GRANT", "REVOKE", "OWNER", "ROLE", "ALTER", "DROP", "AUTHORIZATION"):
        assert not re.search(rf"\b{word}\b", code), word
    assert "/*" not in _sql() and "$$" not in _sql()
    assert all(";" not in ln for ln in _sql().splitlines() if ln.lstrip().startswith("--"))


def test_the_migration_columns_match_the_orm_model():
    body = _split_sql_statements(_sql())[0]
    sql_columns = set(re.findall(r"^\s{4}([a-z_]+)\s+(?:VARCHAR|JSONB|DATE|INT|TIMESTAMPTZ)", body, re.M))
    assert sql_columns == {c.name for c in MemoryWriteHealthDaily.__table__.columns}
    assert MemoryWriteHealthDaily.__table__.fullname == "nous_system.memory_write_health_daily"
    assert [c.name for c in MemoryWriteHealthDaily.__table__.primary_key.columns] == ["agent_id", "day"]


@pytest.mark.postgres_only
async def test_the_table_exists_with_its_primary_key(db):
    async with db.session() as s:
        pk = (
            await s.execute(
                text(
                    """SELECT array_agg(a.attname ORDER BY array_position(i.indkey, a.attnum))
                         FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
                        WHERE i.indrelid = 'nous_system.memory_write_health_daily'::regclass AND i.indisprimary"""
                )
            )
        ).scalar_one()
    assert list(pk) == ["agent_id", "day"]


# ---------------------------------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------------------------------


def _agent() -> str:
    return f"test-mwh-{uuid4().hex[:12]}"


def _row(**over) -> dict:
    row = mwh.empty_day()
    for key, value in over.items():
        row[key] = value
    return row


def _rows(n: int, fn, today: date = TODAY) -> dict[date, dict]:
    """``n`` days ending ``today``; ``fn(i)`` gives day ``today - i``'s overrides."""
    return {today - timedelta(days=i): _row(**fn(i)) for i in range(n)}


def _inflow(person: int = 0, preference: int = 0, rule: int = 0) -> dict:
    return {
        "tier1_inflow": {
            "person": {"all": person, "non_enum": person},
            "preference": {"all": preference, "non_enum": preference},
            "rule": {"all": rule, "non_enum": rule},
        }
    }


def _state(states, rule):
    return [s for s in states if s.id == rule]


def _one(states, rule):
    (s,) = _state(states, rule)
    return s


def _ack(rule: str, day: date, **extra) -> dict:
    return {"rule": rule, "day": day, "at": datetime.combine(day, datetime.min.time(), tzinfo=UTC), **extra}


async def _run(db, agent: str, **kw):
    return await mwh.run_once(db, agent, **kw)


async def _learn(db, agent: str, **fact) -> Fact:
    async with db.session() as s:
        row = Fact(id=uuid4(), agent_id=agent, content=fact.pop("content", f"fact {uuid4().hex}"), active=True, **fact)
        s.add(row)
        s.add(Event(agent_id=agent, event_type="fact_learned", data={"fact_id": str(row.id)}))
        await s.commit()
    return row


async def _delete_fact(db, fact_id) -> None:
    async with db.session() as s:
        await s.execute(text("DELETE FROM heart.facts WHERE id = :id"), {"id": fact_id})
        await s.commit()


async def _rows_of(db, agent: str) -> dict[date, dict]:
    async with db.session() as s:
        result = await s.execute(
            text("SELECT day, metrics FROM nous_system.memory_write_health_daily WHERE agent_id = :a ORDER BY day"),
            {"a": agent},
        )
        return {d: m for d, m in result.all()}


# ---------------------------------------------------------------------------------------------------------------------
# H1: the hard-delete detector (Postgres)
# ---------------------------------------------------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_a_deleted_fact_trips_h1_stays_tripped_and_clears_on_acknowledgement(db, mock_embeddings):
    from nous.config import Settings
    from nous.heart import Heart

    agent = _agent()
    heart = Heart(db, Settings(agent_id=agent), embedding_provider=mock_embeddings)
    try:
        from nous.heart.schemas import FactInput

        learned = await heart.learn(FactInput(content="The owner keeps bees in the garden.", category="person"))
    finally:
        await heart.close()

    first = await _run(db, agent)
    assert _one(first, "H1").state == "ok"  # the first run seeds the baseline
    await _delete_fact(db, learned.id)

    second = await _run(db, agent)
    h1 = _one(second, "H1")
    assert h1.state == "tripped"
    assert h1.detail["since_baseline"]["vanished_fact_ids"] == 1
    assert h1.detail["since_baseline"]["rows_dropped"] == 1
    assert [f.summary for f in findings_for(second)] == [h1.summary]
    assert _one(await _run(db, agent), "H1").state == "tripped"  # latched, not a one-run delta

    async with db.session() as s:
        await mwh.acknowledge(s, agent, "H1", datetime.now(UTC).date(), "deliberate cleanup", "owner")
        await s.commit()
    assert _one(await _run(db, agent), "H1").state == "ok"


@pytest.mark.postgres_only
async def test_a_deleted_episode_trips_h1(db, mock_embeddings):
    from nous.config import Settings
    from nous.heart import Heart
    from nous.heart.schemas import EpisodeInput

    agent = _agent()
    heart = Heart(db, Settings(agent_id=agent), embedding_provider=mock_embeddings)
    try:
        episode = await heart.start_episode(EpisodeInput(summary="a short chat"), dedup=False)
    finally:
        await heart.close()
    await _run(db, agent)
    async with db.session() as s:
        await s.execute(text("DELETE FROM heart.episodes WHERE id = :id"), {"id": episode.id})
        await s.commit()

    h1 = _one(await _run(db, agent), "H1")

    assert h1.state == "tripped"
    assert h1.detail["since_baseline"]["vanished_episode_ids"] == 1


@pytest.mark.postgres_only
async def test_ids_already_gone_before_the_first_run_are_the_baseline(db):
    agent = _agent()
    gone = await _learn(db, agent)
    await _delete_fact(db, gone.id)

    states = await _run(db, agent)

    assert _one(states, "H1").state == "ok"
    assert not [f for f in findings_for(states) if f.raw_data["rule"] == "H1"]
    async with db.session() as s:
        (seed,) = await mwh.load_acks(s, agent)
    assert seed["rule"] == "H1" and seed["actor"] == "system"
    assert seed["observed"]["vanished_fact_ids"] == 1


@pytest.mark.postgres_only
async def test_a_row_count_drop_trips_h1_with_no_ledger_event(db):
    agent = _agent()
    async with db.session() as s:
        row = Fact(id=uuid4(), agent_id=agent, content="written without an event", active=True)
        s.add(row)
        await s.commit()
    await _run(db, agent)
    await _delete_fact(db, row.id)

    h1 = _one(await _run(db, agent), "H1")

    assert h1.state == "tripped"
    assert h1.detail["since_baseline"]["vanished_fact_ids"] == 0
    assert h1.detail["since_baseline"]["rows_dropped"] == 1


@pytest.mark.postgres_only
async def test_h1_still_trips_after_the_sleep_prune_removed_the_dangling_edge(db):
    from nous.config import Settings
    from nous.events import EventBus
    from nous.handlers.sleep_handler import SleepHandler

    agent = _agent()
    a = await _learn(db, agent)
    b = await _learn(db, agent)
    async with db.session() as s:
        s.add(
            GraphEdge(
                source_id=a.id,
                target_id=b.id,
                source_type="fact",
                target_type="fact",
                agent_id=agent,
                relation="related_to",
            )
        )
        await s.commit()
    await _run(db, agent)
    await _delete_fact(db, a.id)

    # The real prune runs between the delete and the next check, as a sleep cycle would.
    bus = MagicMock(spec=EventBus)
    handler = SleepHandler(MagicMock(), SimpleNamespace(db=db), Settings(agent_id=agent), bus)
    sleep_stats: dict = {}
    assert await handler._phase_prune_dead_edges(sleep_stats) is True
    assert sleep_stats["dead_edges_missing_endpoint"] == 1
    async with db.session() as s:  # what the bus persists for sleep_completed
        s.add(Event(agent_id=agent, event_type="sleep_completed", data={"phases_completed": [], **sleep_stats}))
        await s.commit()

    h1 = _one(await _run(db, agent), "H1")

    assert h1.state == "tripped"
    delta = h1.detail["since_baseline"]
    assert delta["vanished_fact_ids"] == 1 and delta["sleep_missing_endpoint"] == 1
    assert (await _rows_of(db, agent))[datetime.now(UTC).date()]["live"]["dangling_edges"] == 0


@pytest.mark.postgres_only
async def test_duplicate_fact_learned_events_count_once(db):
    agent = _agent()
    fact = await _learn(db, agent)
    async with db.session() as s:
        s.add(Event(agent_id=agent, event_type="fact_learned", data={"fact_id": str(fact.id)}))  # the bus copy
        s.add(Event(agent_id=agent, event_type="fact_learned", data={"fact_id": "not-a-uuid"}))
        await s.commit()
    await _delete_fact(db, fact.id)

    await _run(db, agent)

    assert (await _rows_of(db, agent))[datetime.now(UTC).date()]["live"]["vanished_fact_ids"] == 1


@pytest.mark.postgres_only
async def test_another_agents_delete_does_not_trip_this_agent(db):
    mine, theirs = _agent(), _agent()
    await _learn(db, mine)
    other = await _learn(db, theirs)
    await _run(db, mine)
    await _run(db, theirs)
    await _delete_fact(db, other.id)

    assert _one(await _run(db, mine), "H1").state == "ok"
    assert _one(await _run(db, theirs), "H1").state == "tripped"


@pytest.mark.postgres_only
async def test_a_restore_does_not_hide_a_later_delete(db):
    agent = _agent()
    gone = await _learn(db, agent)
    await _delete_fact(db, gone.id)
    await _run(db, agent)  # baseline: one id already gone
    async with db.session() as s:  # the owner restores it from a backup
        s.add(Fact(id=gone.id, agent_id=agent, content="restored", active=True))
        await s.commit()
    assert _one(await _run(db, agent), "H1").state == "ok"
    fresh = await _learn(db, agent)
    await _run(db, agent)
    await _delete_fact(db, fresh.id)

    assert _one(await _run(db, agent), "H1").state == "tripped"


# ---------------------------------------------------------------------------------------------------------------------
# The rollup (Postgres)
# ---------------------------------------------------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_sources_bucket_to_seven_labels_and_other_and_null_is_non_enumerative(db):
    agent = _agent()
    now = datetime.now(UTC)
    for source in ("enumerative_extractor", "user_direct", "https://example.com/a page", None, "fact_extractor"):
        await _learn(db, agent, source=source, category="person", learned_at=now)

    async with db.session() as s:
        day = (await mwh.compute_days(s, agent, now.date(), now.date()))[now.date()]

    assert day["facts_by_source"] == {"enumerative_extractor": 1, "user_direct": 1, "other": 3}
    assert day["tier1_inflow"]["person"] == {"all": 5, "non_enum": 4}  # NULL source counts as non-enumerative


@pytest.mark.postgres_only
async def test_days_are_utc_days_whatever_the_session_time_zone(db):
    agent = _agent()
    late = datetime(2026, 11, 3, 23, 30, tzinfo=UTC)
    await _learn(db, agent, learned_at=late)

    async with db.session() as s:
        await s.execute(text("SET LOCAL TIME ZONE 'Pacific/Kiritimati'"))  # UTC+14: local day is 4 Nov
        days = await mwh.compute_days(s, agent, date(2026, 11, 3), date(2026, 11, 4))

    assert days[date(2026, 11, 3)]["facts_by_source"] == {"other": 1}
    assert days[date(2026, 11, 4)]["facts_by_source"] == {}


@pytest.mark.postgres_only
async def test_a_stale_deactivated_person_fact_trips_s1_and_a_technical_one_does_not(db):
    from nous.heart.facts import mark_fact_inactive

    person, technical = _agent(), _agent()
    for agent, category in ((person, "person"), (technical, "technical")):
        fact = await _learn(db, agent, category=category)
        async with db.session() as s:
            row = await s.get(Fact, fact.id)
            mark_fact_inactive(row, "stale")
            await s.commit()

    assert _one(await _run(db, person), "S1").state == "tripped"
    assert _one(await _run(db, technical), "S1").state == "ok"


@pytest.mark.postgres_only
@pytest.mark.parametrize(("excluded", "expected"), [(None, "ok"), ([], "tripped")])
async def test_the_real_stale_scan_trips_s1_only_when_it_may_touch_tier1(db, excluded, expected):
    from nous.config import Settings
    from nous.events import EventBus
    from nous.handlers.sleep_handler import SleepHandler

    agent = _agent()
    old = datetime.now(UTC) - timedelta(days=200)
    for category in ("person", "technical"):
        await _learn(db, agent, category=category, created_at=old, learned_at=old)
    settings = Settings(agent_id=agent)
    if excluded is not None:  # the pre-#828 scan, which also aged out tier-1 facts
        settings = settings.model_copy(update={"stale_scan_excluded_categories": excluded})
    handler = SleepHandler(
        MagicMock(), SimpleNamespace(db=db, settings=settings, agent_id=agent), settings, MagicMock(spec=EventBus)
    )
    assert await handler._phase_stale_scan({}) is True

    assert _one(await _run(db, agent), "S1").state == expected


@pytest.mark.postgres_only
async def test_two_runs_leave_one_row_per_day_and_change_nothing(db):
    agent = _agent()
    await _learn(db, agent, source="user_direct")
    now = datetime.now(UTC)

    await _run(db, agent, now=now)
    first = await _rows_of(db, agent)
    await _run(db, agent, now=now + timedelta(minutes=5))
    second = await _rows_of(db, agent)

    assert len(first) == len(second) == mwh.BACKFILL_DAYS
    today = now.date()
    for day in first:
        if day != today:
            assert first[day] == second[day], day
    assert second[today]["counters"] == first[today]["counters"] == {k: 0 for k in mwh.H1_COUNTERS}
    assert second[today]["facts_by_source"] == {"user_direct": 1}
    async with db.session() as s:
        assert len(await mwh.load_acks(s, agent)) == 1  # one seed, not one per run


@pytest.mark.postgres_only
async def test_backfilled_days_have_no_point_in_time_keys(db):
    agent = _agent()
    await _run(db, agent)
    rows = await _rows_of(db, agent)
    today = datetime.now(UTC).date()
    assert set(rows[today]) >= {"live", "counters", "rules"}
    assert not any(key in rows[today - timedelta(days=1)] for key in ("live", "counters", "rules"))


@pytest.mark.postgres_only
async def test_a_failed_query_writes_no_row_and_raises(db, monkeypatch):
    agent = _agent()

    async def broken(session, agent_id, now):
        raise RuntimeError("db boom")

    monkeypatch.setattr(mwh, "observe_live", broken)
    with pytest.raises(RuntimeError):
        await MemoryWriteHealthCheck(db, agent).run()

    assert await _rows_of(db, agent) == {}
    async with db.session() as s:
        assert await mwh.load_acks(s, agent) == []


# ---------------------------------------------------------------------------------------------------------------------
# Acknowledgements (Postgres)
# ---------------------------------------------------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_an_acknowledgement_records_who_what_and_when(db):
    agent = _agent()
    await _run(db, agent)
    today = datetime.now(UTC).date()
    async with db.session() as s:
        data = await mwh.acknowledge(s, agent, "F1", today, "bulk import\x00 of notes", "dashboard:owner@example")
        await s.commit()
        acks = await mwh.load_acks(s, agent)

    assert data["note"] == "bulk import of notes" and data["actor"] == "dashboard:owner@example"
    assert [(a["rule"], a["day"], a["actor"]) for a in acks][-1] == ("F1", today, "dashboard:owner@example")


@pytest.mark.postgres_only
async def test_acknowledgements_are_refused_for_bad_input(db):
    agent = _agent()
    today = datetime.now(UTC).date()
    async with db.session() as s:
        with pytest.raises(mwh.AcknowledgeError):
            await mwh.acknowledge(s, agent, "H1", today, "", "owner")  # nothing observed yet
        with pytest.raises(mwh.AcknowledgeError):
            await mwh.acknowledge(s, agent, "Z9", today, "", "owner")
        with pytest.raises(mwh.AcknowledgeError):
            await mwh.acknowledge(s, agent, "F1", today + timedelta(days=1), "", "owner")
        with pytest.raises(mwh.AcknowledgeError):
            await mwh.acknowledge(s, agent, "F1", today - timedelta(days=mwh.ACK_MAX_AGE_DAYS + 1), "", "owner")


# ---------------------------------------------------------------------------------------------------------------------
# Rules on synthetic rows (no database)
# ---------------------------------------------------------------------------------------------------------------------


def test_f1_trips_on_a_flood_against_a_small_median_and_not_on_a_busy_source():
    rows = _rows(
        15,
        lambda i: {
            "facts_by_source": {"enumerative_extractor": 150 if i == 0 else 10, "user_direct": 150 if i == 0 else 120}
        },
    )

    tripped = [s for s in mwh.evaluate(rows, [], TODAY) if s.id == "F1"]

    assert [s.detail["source"] for s in tripped] == ["enumerative_extractor"]
    assert tripped[0].state == "tripped" and tripped[0].threshold == 100


def test_f1_needs_the_absolute_floor():
    rows = _rows(15, lambda i: {"facts_by_source": {"other": 99 if i == 0 else 0}})
    (f1,) = _state(mwh.evaluate(rows, [], TODAY), "F1")
    assert f1.state == "ok"


def test_f1_catches_a_flood_late_yesterday():
    rows = _rows(16, lambda i: {"facts_by_source": {"reflection": 400 if i == 1 else 5}})
    (f1,) = _state(mwh.evaluate(rows, [], TODAY), "F1")
    assert f1.state == "tripped" and f1.detail["day"] == (TODAY - timedelta(days=1)).isoformat()


def test_an_f1_acknowledgement_covers_that_day_only():
    rows = _rows(15, lambda i: {"facts_by_source": {"enumerative_extractor": 500 if i == 0 else 10}})
    (acked,) = _state(mwh.evaluate(rows, [_ack("F1", TODAY)], TODAY), "F1")
    assert acked.state == "acknowledged"
    (still,) = _state(mwh.evaluate(rows, [_ack("F1", TODAY - timedelta(days=1))], TODAY), "F1")
    assert still.state == "tripped"


def test_s1_counts_today_and_yesterday_after_the_acknowledged_day():
    rows = _rows(3, lambda i: {"deactivated_tier1_stale": 1 if i == 1 else 0})
    assert _one(mwh.evaluate(rows, [], TODAY), "S1").state == "tripped"
    assert _one(mwh.evaluate(rows, [_ack("S1", TODAY - timedelta(days=1))], TODAY), "S1").state == "acknowledged"
    rows[TODAY]["deactivated_tier1_stale"] = 2
    s1 = _one(mwh.evaluate(rows, [_ack("S1", TODAY - timedelta(days=1))], TODAY), "S1")
    assert s1.state == "tripped" and s1.value == 2


def _collapse_rows(prior_per_day: dict, current_per_day: dict, n: int = 80) -> dict[date, dict]:
    return _rows(n, lambda i: _inflow(**(current_per_day if i < 14 else prior_per_day)))


def test_p1_trips_at_the_ratio_and_not_above_it():
    # four prior windows of 14 a window (mean 14, threshold 5.6)
    tripped = _one(
        mwh.evaluate(_rows(80, lambda i: _inflow(person=1 if i >= 14 else (1 if i < 5 else 0))), [], TODAY), "P1"
    )
    assert tripped.state == "tripped" and tripped.value == 5 and tripped.threshold == 5.6
    ok = _one(mwh.evaluate(_rows(80, lambda i: _inflow(person=1 if i >= 14 else (1 if i < 6 else 0))), [], TODAY), "P1")
    assert ok.state == "ok" and ok.value == 6


def test_p1_needs_a_baseline_mean_of_eight():
    # prior windows of 7 (mean 7): a drop to 1 is not judged
    rows = _rows(80, lambda i: _inflow(person=(1 if i % 2 == 0 else 0) if i >= 14 else (1 if i == 0 else 0)))
    p1 = _one(mwh.evaluate(rows, [], TODAY), "P1")
    assert p1.detail["prior_mean"] == 7 and p1.state == "ok"


def test_p1_with_a_constant_baseline():
    steady = _one(mwh.evaluate(_collapse_rows({"person": 1}, {"person": 1}), [], TODAY), "P1")
    assert steady.state == "ok" and steady.detail["prior_sums"] == [14, 14, 14, 14]


def test_p1_trips_on_twenty_one_days_of_nothing_after_some_inflow():
    rows = _rows(80, lambda i: _inflow(preference=0 if i < 21 else (1 if i % 10 == 0 else 0)))
    p1 = _one(mwh.evaluate(rows, [], TODAY), "P1")
    assert p1.state == "tripped" and "no facts in 21 days" in p1.summary
    fresh = _one(mwh.evaluate(_rows(80, lambda i: _inflow()), [], TODAY), "P1")
    assert fresh.state == "ok"  # a new agent with no inflow ever is not a collapse


def test_steady_rule_inflow_does_not_mask_a_profile_collapse():
    rows = _collapse_rows({"person": 1, "rule": 3}, {"person": 0, "rule": 3})
    states = mwh.evaluate(rows, [], TODAY)
    assert _one(states, "P1").state == "tripped"
    assert _one(states, "R1").state == "ok"


def test_a_rule_collapse_trips_r1_only():
    rows = _collapse_rows({"person": 1, "rule": 3}, {"person": 1, "rule": 0})
    states = mwh.evaluate(rows, [], TODAY)
    assert _one(states, "R1").state == "tripped" and "rule learning" in _one(states, "R1").summary
    assert _one(states, "P1").state == "ok"


def test_a_collapse_acknowledgement_waits_for_a_full_window_after_it():
    rows = _collapse_rows({"person": 1}, {"person": 0})
    within = _one(mwh.evaluate(rows, [_ack("P1", TODAY - timedelta(days=13))], TODAY), "P1")
    assert within.state == "acknowledged"
    after = _one(mwh.evaluate(rows, [_ack("P1", TODAY - timedelta(days=14))], TODAY), "P1")
    assert after.state == "tripped"


def test_collapse_rules_have_no_data_without_seventy_days():
    assert _one(mwh.evaluate(_collapse_rows({"person": 1}, {"person": 0}, n=60), [], TODAY), "P1").state == "no_data"


def test_a1_trips_at_three_quarters_with_a_hundred_attempts():
    def rows(rejected: int, admitted: int) -> dict:
        return {TODAY: _row(admission_rejected=rejected, facts_by_source={"user_direct": admitted})}

    assert _one(mwh.evaluate(rows(300, 100), [], TODAY), "A1").state == "tripped"
    assert _one(mwh.evaluate(rows(299, 100), [], TODAY), "A1").state == "ok"
    assert _one(mwh.evaluate(rows(75, 24), [], TODAY), "A1").state == "ok"  # 99 attempts


def test_an_a1_acknowledgement_drops_the_days_up_to_it():
    rows = _rows(3, lambda i: {"admission_rejected": 0 if i == 0 else 300, "facts_by_source": {"user_direct": 50}})
    assert _one(mwh.evaluate(rows, [], TODAY), "A1").state == "tripped"
    assert _one(mwh.evaluate(rows, [_ack("A1", TODAY - timedelta(days=1))], TODAY), "A1").state == "ok"


def test_a1_compares_the_unrounded_rate():
    """1499 of 2000 is 0.7495: below 0.75, though it rounds to 0.75 at three places (Codex P2 on #858)."""

    def a1(rejected: int, admitted: int):
        rows = {TODAY: _row(admission_rejected=rejected, facts_by_source={"user_direct": admitted})}
        return _one(mwh.evaluate(rows, [], TODAY), "A1")

    below = a1(1499, 501)
    assert below.state == "ok" and below.value == 0.75  # shown rounded, judged unrounded
    assert a1(1500, 500).state == "tripped"


def test_the_collapse_rules_compare_the_unrounded_threshold(monkeypatch):
    """With a ratio whose threshold rounds up across the window sum, the unrounded one decides."""
    monkeypatch.setitem(mwh.THRESHOLDS["P1"], "ratio", 0.4996)  # 0.4996 x mean 10 = 4.996, rounded 5.0
    rows = _rows(80, lambda i: _inflow(person=10 if i >= 14 and i % 14 == 0 else (5 if i == 0 else 0)))

    p1 = _one(mwh.evaluate(rows, [], TODAY), "P1")

    assert p1.detail["prior_sums"] == [10, 10, 10, 10] and p1.value == 5
    assert p1.state == "ok" and p1.threshold == 5.0


def test_the_stored_rule_states_keep_every_f1_label():
    rows = _rows(15, lambda i: {"facts_by_source": {"user_direct": 150, "other": 150} if i == 0 else {}})
    states = mwh.evaluate(rows, [], TODAY)

    stored = mwh.rule_states(states)

    assert stored["F1:user_direct"] == stored["F1:other"] == "tripped"
    assert {k for k in stored if not k.startswith("F1")} == {"H1", "S1", "P1", "R1", "A1"}
    quiet = mwh.rule_states(mwh.evaluate(_rows(15, lambda i: {}), [], TODAY))
    assert [k for k in quiet if k.startswith("F1")] and all(v != "tripped" for v in quiet.values())


@pytest.mark.postgres_only
async def test_a_run_stores_both_labels_flooding_on_one_day(db):
    agent = _agent()
    async with db.session() as s:
        await s.execute(
            text(
                """INSERT INTO heart.facts (agent_id, content, source, active)
                   SELECT :a, 'flood ' || g, CASE WHEN g % 2 = 0 THEN 'user_direct' ELSE 'https://example.com' END, true
                     FROM generate_series(1, 300) g"""
            ),
            {"a": agent},
        )
        await s.commit()

    await _run(db, agent)

    rules = (await _rows_of(db, agent))[datetime.now(UTC).date()]["rules"]
    assert rules["F1:user_direct"] == rules["F1:other"] == "tripped"


def test_h1_is_measured_against_the_latest_acknowledgement():
    counters = {
        "vanished_fact_ids": 3,
        "vanished_episode_ids": 0,
        "rows_dropped": 3,
        "dangling_edges": 0,
        "sleep_missing_endpoint": 0,
    }
    rows = {TODAY: _row(counters=counters)}
    assert _one(mwh.evaluate(rows, [], TODAY), "H1").state == "tripped"
    old = _ack("H1", TODAY - timedelta(days=3), counters={**counters, "vanished_fact_ids": 1})
    new = _ack("H1", TODAY, counters=counters)
    assert _one(mwh.evaluate(rows, [old], TODAY), "H1").state == "tripped"
    assert _one(mwh.evaluate(rows, [old, new], TODAY), "H1").state == "ok"


def test_counters_add_rises_only():
    prev = {
        "facts_total": 10,
        "episodes_total": 4,
        "vanished_fact_ids": 5,
        "vanished_episode_ids": 1,
        "dangling_edges": 2,
        "sleep_missing_endpoint_total": 99,
    }
    now = {**prev, "facts_total": 9, "vanished_fact_ids": 3, "dangling_edges": 4, "sleep_missing_endpoint_total": 101}
    counters = mwh.advance_counters(prev, {k: 1 for k in mwh.H1_COUNTERS}, now)
    assert counters == {
        "vanished_fact_ids": 1,
        "vanished_episode_ids": 1,
        "rows_dropped": 2,
        "dangling_edges": 3,
        "sleep_missing_endpoint": 3,
    }
    assert mwh.advance_counters(None, None, now) == {k: 0 for k in mwh.H1_COUNTERS}


# ---------------------------------------------------------------------------------------------------------------------
# Findings and the check
# ---------------------------------------------------------------------------------------------------------------------


def test_no_finding_asks_the_agent_to_act_and_only_h1_f1_s1_are_high():
    states = [mwh.RuleState(rule, "tripped", summary=f"{rule} tripped") for rule in mwh.RULES]
    states.append(mwh.RuleState("F1", "acknowledged"))

    findings = findings_for(states)

    assert [f.raw_data["rule"] for f in findings] == list(mwh.RULES)
    assert all(f.needs_action is False for f in findings)  # off chat: tests/test_heartbeat_chat_delivery.py
    assert {f.raw_data["rule"] for f in findings if f.urgency == "high"} == {"H1", "F1", "S1"}


def test_the_check_runs_hourly_in_quiet_hours_and_off_chat():
    check = MemoryWriteHealthCheck(database=object(), agent_id="test-mwh-attrs")
    assert (check.name, check.interval, check.timeout, check.urgent_override, check.chat_delivery) == (
        "memory_write_health",
        3600,
        120,
        True,
        False,
    )


@pytest.mark.postgres_only
async def test_every_tripped_summary_says_the_owner_decides(db):
    agent = _agent()
    fact = await _learn(db, agent, category="person")
    await _run(db, agent)
    await _delete_fact(db, fact.id)
    states = await _run(db, agent)
    for f in findings_for(states):
        assert "owner decides" in f.summary
        assert not re.search(r"\b(DELETE|psql|UPDATE|INSERT)\b", f.summary)


# ---------------------------------------------------------------------------------------------------------------------
# The ledger depends on nous_system.events never being pruned
# ---------------------------------------------------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_the_retention_sweeps_never_delete_a_ledger_event(db, monkeypatch):
    """Run every retention sweep the app has against a 400-day-old fact_learned event; it must survive."""
    import asyncio

    import nous.main as main_mod
    from nous.config import Settings
    from nous.handlers.consolidation_audit import ConsolidationAuditor

    agent = _agent()
    event_id = uuid4()
    async with db.session() as s:
        await s.execute(
            text(
                """INSERT INTO nous_system.events (id, agent_id, event_type, data, created_at)
                   VALUES (:id, :a, 'fact_learned', CAST(:d AS jsonb), now() - interval '400 days')"""
            ),
            {"id": event_id, "a": agent, "d": f'{{"fact_id": "{uuid4()}"}}'},
        )
        await s.commit()
    settings = Settings(
        agent_id=agent, retrieval_telemetry_retention_days=1, context_log_retention_days=1, tool_call_retention_days=1
    )

    class _Counting:
        """The real database, with a signal once a loop's sweep has closed its session."""

        def __init__(self) -> None:
            self.swept = asyncio.Event()

        def session(self):
            outer = self

            class _Ctx:
                async def __aenter__(self):
                    self._cm = db.session()
                    return await self._cm.__aenter__()

                async def __aexit__(self, *exc):
                    out = await self._cm.__aexit__(*exc)
                    outer.swept.set()
                    return out

            return _Ctx()

    monkeypatch.setattr(main_mod, "_RETENTION_SWEEP_INTERVAL_SECONDS", 0)
    for loop_fn in (
        main_mod._context_log_retention_loop,
        main_mod._retrieval_log_retention_loop,
        main_mod._tool_call_retention_loop,
    ):
        counting = _Counting()
        stop = {"now": False}
        monkeypatch.setattr(main_mod, "cancel_requested", lambda: stop["now"])
        task = asyncio.create_task(loop_fn(settings, counting))
        await asyncio.wait_for(counting.swept.wait(), timeout=30)
        stop["now"] = True
        task.cancel()
        await asyncio.wait_for(task, timeout=30)

    from nous.cognitive.ledger_store import LedgerStore

    await LedgerStore(db, agent).prune(retention_days=1, detail_retention_days=1)
    await ConsolidationAuditor(db, agent).prune_old_actions(1)

    async with db.session() as s:
        survived = (
            await s.execute(text("SELECT count(*) FROM nous_system.events WHERE id = :id"), {"id": event_id})
        ).scalar_one()
    assert survived == 1


def test_no_code_path_deletes_events():
    """A second line behind the behaviour pin: no DELETE, TRUNCATE or ORM delete of nous_system.events in nous/."""
    pattern = re.compile(
        r"DELETE\s+FROM\s+nous_system\.events\b|TRUNCATE[^;\n]*nous_system\.events\b"
        r"|delete\(\s*(?:Event|EventRow|EventModel)\s*\)",
        re.I,
    )
    offenders = [
        str(path.relative_to(ROOT))
        for path in (ROOT / "nous").rglob("*.py")
        if pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []
