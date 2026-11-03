"""Bi-temporal facts, PR1: schema plus write path (migration 097, flag W).

Facts gain event time (``valid_from``/``valid_to``, the days a fact was true)
and statement time (``mentioned_at``, when it was said: the source episode's
start). ``learned_at`` stays ingestion time.

Flag W (``temporal_intervals_enabled``, default off) adds spans and states to
the extraction prompts. With W off the only changes are that ``mentioned_at``
is stored and that a dated row gets a one-day interval.
"""

from __future__ import annotations

import ast
import json
import logging
import re
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import select, text

from nous.config import Settings
from nous.heart.schemas import EpisodeInput, FactDetail, FactInput, FactSummary
from nous.storage.migrator import _split_sql_statements
from nous.storage.models import Fact

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "sql" / "migrations" / "097_fact_temporal_columns.sql"
TEMPORAL = ("valid_from", "valid_to", "mentioned_at")
EPISODE_START = datetime(2024, 4, 11, 9, 30, tzinfo=UTC)


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _code() -> str:
    return "\n".join(ln for ln in _sql().splitlines() if not ln.lstrip().startswith("--"))


# ---------------------------------------------------------------------------
# The FactInput validator
# ---------------------------------------------------------------------------


def test_an_event_date_becomes_a_one_day_interval():
    fi = FactInput(content="c", event_date="2024-04-10")
    assert (fi.event_date, fi.valid_from, fi.valid_to) == (date(2024, 4, 10),) * 3


def test_an_event_date_wins_over_a_different_interval(caplog):
    with caplog.at_level(logging.WARNING, logger="nous.heart.schemas"):
        fi = FactInput(content="c", event_date="2024-04-10", valid_from="2024-04-01", valid_to="2024-04-30")
    assert (fi.event_date, fi.valid_from, fi.valid_to) == (date(2024, 4, 10),) * 3
    assert "valid_from" in caplog.text


def test_a_span_never_derives_an_event_date():
    fi = FactInput(content="c", valid_from="2026-02-01", valid_to="2026-02-28")
    assert fi.event_date is None
    assert (fi.valid_from, fi.valid_to) == (date(2026, 2, 1), date(2026, 2, 28))


def test_an_open_state_keeps_valid_to_empty():
    fi = FactInput(content="c", valid_from="2026-01-01")
    assert (fi.event_date, fi.valid_from, fi.valid_to) == (None, date(2026, 1, 1), None)


def test_valid_to_before_valid_from_is_dropped_and_logged(caplog):
    with caplog.at_level(logging.WARNING, logger="nous.heart.schemas"):
        fi = FactInput(content="c", valid_from="2026-03-01", valid_to="2026-02-01")
    assert (fi.valid_from, fi.valid_to) == (date(2026, 3, 1), None)
    assert "valid_to" in caplog.text


def test_valid_to_without_valid_from_is_dropped(caplog):
    with caplog.at_level(logging.WARNING, logger="nous.heart.schemas"):
        fi = FactInput(content="c", valid_to="2026-02-01")
    assert (fi.valid_from, fi.valid_to) == (None, None)
    assert "valid_to" in caplog.text


@pytest.mark.parametrize("field", ["event_date", "valid_from", "valid_to"])
@pytest.mark.parametrize("bad", ["2024-3-10", "20240310", "2024-02-30", "Feb 2026"])
def test_one_parser_drops_a_bad_date_on_every_date_field(field, bad, caplog):
    with caplog.at_level(logging.WARNING, logger="nous.heart.schemas"):
        fi = FactInput(content="c", **{field: bad})
    assert getattr(fi, field) is None
    assert field in caplog.text


@pytest.mark.parametrize("field", ["event_date", "valid_from", "valid_to"])
def test_one_parser_reduces_a_datetime_to_its_date(field):
    kwargs = {field: datetime(2024, 4, 10, 23, 0, tzinfo=UTC)}
    if field == "valid_to":
        kwargs["valid_from"] = date(2024, 4, 1)
    assert getattr(FactInput(content="c", **kwargs), field) == date(2024, 4, 10)


def test_mentioned_at_is_a_plain_optional_timestamp():
    assert FactInput(content="c").mentioned_at is None
    assert FactInput(content="c", mentioned_at=EPISODE_START).mentioned_at == EPISODE_START


def test_a_naive_mentioned_at_is_taken_as_utc():
    naive = datetime(2024, 4, 11, 9, 30)
    fi = FactInput(content="c", mentioned_at=naive)
    assert fi.mentioned_at == EPISODE_START and fi.mentioned_at.tzinfo is UTC
    assert FactInput(content="c", mentioned_at="2024-04-11T09:30:00").mentioned_at == EPISODE_START
    # An aware value keeps its own offset.
    plus2 = datetime(2024, 4, 11, 11, 30, tzinfo=timezone(timedelta(hours=2)))
    assert FactInput(content="c", mentioned_at=plus2).mentioned_at.utcoffset() == timedelta(hours=2)


# ---------------------------------------------------------------------------
# DTOs, ORM and builders
# ---------------------------------------------------------------------------


def test_the_three_fields_are_columns_and_on_every_fact_dto():
    from nous.api.tools import _LEGACY_FACT_KEYS
    from nous.heart.facts import FactManager

    for name in TEMPORAL:
        assert Fact.__table__.c[name].nullable, name
        for model in (FactInput, FactSummary, FactDetail):
            assert name in model.model_fields, (model.__name__, name)
        assert name in FactManager.LEGACY_SUMMARY_FIELDS
        assert _LEGACY_FACT_KEYS[name] is None


def test_every_fact_dto_builder_copies_the_temporal_fields():
    """Every FactSummary/FactDetail built in facts.py spreads _temporal_kwargs, so a
    builder added later without it fails here instead of reading None silently."""
    tree = ast.parse((ROOT / "nous" / "heart" / "facts.py").read_text(encoding="utf-8"))
    builders = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in ("FactSummary", "FactDetail")
    ]
    assert len(builders) == 6  # five FactSummary builders and _to_detail
    for call in builders:
        spreads = [
            kw.value.func.id
            for kw in call.keywords
            if kw.arg is None and isinstance(kw.value, ast.Call) and isinstance(kw.value.func, ast.Name)
        ]
        assert spreads == ["_temporal_kwargs"], f"line {call.lineno}"


def test_temporal_kwargs_reads_the_row():
    from nous.heart.facts import _temporal_kwargs

    row = SimpleNamespace(valid_from=date(2024, 4, 10), valid_to=None, mentioned_at=EPISODE_START)
    assert _temporal_kwargs(row) == {"valid_from": date(2024, 4, 10), "valid_to": None, "mentioned_at": EPISODE_START}


def test_fact_metadata_carries_iso_strings_and_the_pipeline_strips_none():
    from nous.api.retrieval_pipeline import _heart_results_to_pipeline
    from nous.heart.heart import Heart

    dated = FactSummary(
        id=uuid4(),
        content="c",
        category=None,
        subject=None,
        confidence=1.0,
        active=True,
        valid_from=date(2026, 1, 1),
        valid_to=date(2026, 6, 30),
        mentioned_at=EPISODE_START,
    )
    rr = Heart._to_recall_result(None, "fact", dated, 0.5)
    assert rr.metadata["valid_from"] == "2026-01-01"
    assert rr.metadata["valid_to"] == "2026-06-30"
    assert rr.metadata["mentioned_at"] == EPISODE_START.isoformat()

    undated = Heart._to_recall_result(None, "fact", dated.model_copy(update=dict.fromkeys(TEMPORAL)), 0.5)
    assert all(undated.metadata[k] is None for k in TEMPORAL)
    (piped,) = _heart_results_to_pipeline([undated])
    assert not set(TEMPORAL) & set(piped.metadata)
    (piped,) = _heart_results_to_pipeline([rr])
    assert piped.metadata["valid_from"] == "2026-01-01"


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def test_w_defaults_off():
    assert Settings.model_fields["temporal_intervals_enabled"].default is False


def test_w_without_temporal_extraction_is_forced_off(caplog):
    with caplog.at_level(logging.WARNING, logger="nous.config"):
        s = Settings(temporal_intervals_enabled=True, temporal_extraction_enabled=False)
    assert s.temporal_intervals_enabled is False
    assert "NOUS_TEMPORAL_INTERVALS_ENABLED" in caplog.text
    s = Settings(temporal_intervals_enabled=True, temporal_extraction_enabled=True)
    assert s.temporal_intervals_enabled is True


# ---------------------------------------------------------------------------
# Prompts: unchanged with W off
# ---------------------------------------------------------------------------


def _summarizer(**flags):
    from nous.handlers.episode_summarizer import EpisodeSummarizer

    s = EpisodeSummarizer.__new__(EpisodeSummarizer)
    s._settings = SimpleNamespace(
        extraction_input_hardening_enabled=False,
        extraction_coverage_broadened=False,
        episode_open_threads=False,
        **flags,
    )
    return s


def test_the_summarizer_prompt_is_unchanged_with_w_off():
    from nous.handlers.episode_summarizer import (
        _F075_EPISODE_TS_BLOCK,
        _F075_TEMPORAL_INSTRUCTION,
        _SUMMARY_PROMPT,
    )

    expected = (
        _F075_EPISODE_TS_BLOCK.format(iso=EPISODE_START.isoformat())
        + _SUMMARY_PROMPT.format(transcript="T", decision_context="")
        + _F075_TEMPORAL_INSTRUCTION
    )
    for w in (False, None):  # off, and a settings object that predates the flag
        flags = {"temporal_extraction_enabled": True}
        if w is not None:
            flags["temporal_intervals_enabled"] = w
        assert _summarizer(**flags)._build_summary_prompt("T", "", EPISODE_START) == expected


def test_the_summarizer_prompt_under_w_asks_for_spans_and_states():
    from nous.handlers.episode_summarizer import (
        _ECHO_GUARD_TEMPLATES,
        _F075_TEMPORAL_INSTRUCTION,
        _TEMPORAL_INTERVALS_INSTRUCTION,
    )

    prompt = _summarizer(temporal_extraction_enabled=True, temporal_intervals_enabled=True)._build_summary_prompt(
        "T", "", EPISODE_START
    )
    assert prompt.startswith(f"EPISODE_START_TIMESTAMP: {EPISODE_START.isoformat()}")
    assert prompt.endswith(_TEMPORAL_INTERVALS_INSTRUCTION)
    assert _F075_TEMPORAL_INSTRUCTION not in prompt
    low = _TEMPORAL_INTERVALS_INSTRUCTION.lower()
    assert "never default to the 1st" not in low  # the month/year omit rule is replaced
    assert '"valid_from"' in low and '"valid_to"' in low and "since" in low and "until" in low
    assert "never emit event_date together with" in low
    assert "arxiv" in low and "never assume a prior year" in low  # the F075 guards stay
    assert _TEMPORAL_INTERVALS_INSTRUCTION in _ECHO_GUARD_TEMPLATES


def test_w_needs_temporal_extraction_in_the_summarizer_too():
    from nous.handlers.episode_summarizer import _SUMMARY_PROMPT

    s = _summarizer(temporal_extraction_enabled=False, temporal_intervals_enabled=True)
    assert s._build_summary_prompt("T", "", EPISODE_START) == _SUMMARY_PROMPT.format(
        transcript="T", decision_context=""
    )


def _enumerative_settings(**flags) -> SimpleNamespace:
    return SimpleNamespace(
        background_model="m",
        enumerative_density_threshold=0.0,
        episode_chunk_size=600,
        episode_chunk_overlap=80,
        episode_chunk_min_transcript_chars=10,
        enumerative_max_chunks_per_episode=200,
        enumerative_max_facts_per_episode=1000,
        enumerative_extraction_max_per_hour=1000,
        entity_keys_max_per_fact=8,
        entity_key_min_chars=3,
        **flags,
    )


_TRANSCRIPT = (
    "1. We deployed v2.1 to staging yesterday.\n"
    "2. Christina was in Lisbon in Feb 2026.\n"
    "3. Tim has lived in Berlin since January 2026.\n"
    "4. The API gateway runs on port 8080.\n"
    "5. The nightly job writes to the ledger.\n"
)

_LLM_FACTS = [
    {
        "content": "The team deployed v2.1 to staging on 2024-04-10.",
        "subject_key": "v2.1",
        "attribute_key": "deployment",
        "entities": [],
        "event_date": "2024-04-10",
    },
    {
        "content": "Christina was in Lisbon in February 2026.",
        "subject_key": "Christina",
        "attribute_key": "location",
        "entities": ["Lisbon"],
        "event_date": None,
        "valid_from": "2026-02-01",
        "valid_to": "2026-02-28",
    },
    {
        "content": "Tim has lived in Berlin since January 2026.",
        "subject_key": "Tim",
        "attribute_key": "residence",
        "entities": ["Berlin"],
        "valid_from": "2026-01-01",
    },
    {
        "content": "The API gateway runs on port 8080.",
        "subject_key": "API gateway",
        "attribute_key": "port",
        "entities": [],
    },
]


async def _run_enumerative(monkeypatch, flags, ref_time):
    from nous.handlers.enumerative_extractor import EnumerativeExtractor

    calls = []

    async def _llm(**kw):
        calls.append(kw)
        return {"facts": [dict(f) for f in _LLM_FACTS]}

    monkeypatch.setattr("nous.handlers.enumerative_extractor.call_background_llm_structured", _llm)
    heart = AsyncMock()
    heart.learn = AsyncMock(side_effect=lambda fi, **_kw: SimpleNamespace(id=uuid4()))
    ex = EnumerativeExtractor(heart=heart, settings=_enumerative_settings(**flags), llm_client=object(), embedder=None)
    stored = await ex.process_transcript(_TRANSCRIPT, uuid4(), ref_time=ref_time)
    assert len(stored) == len(_LLM_FACTS)
    return calls, [c.args[0] for c in heart.learn.call_args_list]


async def test_the_enumerative_prompt_and_schema_are_unchanged_with_w_off(monkeypatch):
    from nous.handlers.enumerative_extractor import _EXTRACTION_PROMPT, _EXTRACTION_SCHEMA
    from nous.heart.chunking import chunk_text

    calls, inputs = await _run_enumerative(
        monkeypatch, {"temporal_extraction_enabled": True, "temporal_intervals_enabled": False}, EPISODE_START
    )
    (chunk,) = chunk_text(_TRANSCRIPT, chunk_size=600, overlap=80, min_chars=10)
    assert calls[0]["user_message"] == _EXTRACTION_PROMPT.format(chunk=chunk)
    assert calls[0]["output_schema"] is _EXTRACTION_SCHEMA
    point, span, state, undated = inputs
    # W off: the point still becomes a one-day interval, the span and the state are ignored.
    assert (point.event_date, point.valid_from, point.valid_to) == (date(2024, 4, 10),) * 3
    assert (span.event_date, span.valid_from, span.valid_to) == (None, None, None)
    assert (state.valid_from, state.valid_to) == (None, None)
    assert all(fi.mentioned_at == EPISODE_START for fi in inputs)
    assert undated.valid_from is None


async def test_the_enumerative_extractor_under_w_reads_spans_and_states(monkeypatch):
    calls, inputs = await _run_enumerative(
        monkeypatch, {"temporal_extraction_enabled": True, "temporal_intervals_enabled": True}, EPISODE_START
    )
    message, schema = calls[0]["user_message"], calls[0]["output_schema"]
    assert f"Observation date: {EPISODE_START.isoformat()}" in message
    assert message.index("Observation date") < message.index("<chunk>")
    # One rule for event_date: relative days resolve against the observation date,
    # and the W-off "explicit date only" line is gone, not contradicted.
    assert "resolved against the observation date" in message
    assert "ONLY when the statement itself carries an explicit" not in message
    assert message.count("Report event_date") == 1
    item = schema["properties"]["facts"]["items"]["properties"]
    assert {"valid_from", "valid_to"} <= set(item)
    assert "valid_from" not in schema["properties"]["facts"]["items"]["required"]
    point, span, state, undated = inputs
    assert (point.event_date, point.valid_from, point.valid_to) == (date(2024, 4, 10),) * 3
    assert (span.event_date, span.valid_from, span.valid_to) == (None, date(2026, 2, 1), date(2026, 2, 28))
    assert (state.event_date, state.valid_from, state.valid_to) == (None, date(2026, 1, 1), None)
    assert (undated.valid_from, undated.valid_to) == (None, None)
    assert all(fi.mentioned_at == EPISODE_START for fi in inputs)
    assert all(fi.event_date_classified_at is not None for fi in inputs)


async def test_the_enumerative_extractor_under_w_without_a_reference_time(monkeypatch):
    calls, inputs = await _run_enumerative(
        monkeypatch, {"temporal_extraction_enabled": True, "temporal_intervals_enabled": True}, None
    )
    message = calls[0]["user_message"]
    # No observation date: explicit dates only, and nothing refers to one.
    assert "observation date" not in message.lower()
    assert "ONLY when the statement itself carries an explicit single-day date" in message
    assert message.count("Report event_date") == 1 and "valid_from" in message
    assert all(fi.mentioned_at is None for fi in inputs)


async def test_a_malformed_span_leaves_the_row_backfill_eligible(monkeypatch):
    from nous.handlers.enumerative_extractor import EnumerativeExtractor

    ex = EnumerativeExtractor(
        heart=None,
        settings=_enumerative_settings(temporal_extraction_enabled=True, temporal_intervals_enabled=True),
        llm_client=None,
        embedder=None,
    )
    raw = [
        {
            "content": "Lisbon in Feb.",
            "subject_key": "x",
            "attribute_key": "y",
            "entities": [],
            "valid_from": "Feb 2026",
        }
    ]
    (fi,) = ex._to_fact_inputs(raw, 0, None)
    assert fi.valid_from is None and fi.event_date_classified_at is None


def test_the_merge_prompts_keep_dated_events_only_under_w():
    from nous.handlers.sleep_handler import (
        _CLUSTER_MERGE_SCHEMA,
        _cluster_merge_schema,
        _merge_prompt_tail,
    )

    off = SimpleNamespace(temporal_intervals_enabled=False)
    on = SimpleNamespace(temporal_intervals_enabled=True)
    assert _merge_prompt_tail(off) == "" and _merge_prompt_tail(SimpleNamespace()) == ""
    assert _cluster_merge_schema(off) is _CLUSTER_MERGE_SCHEMA
    assert "verbatim" not in json.dumps(_CLUSTER_MERGE_SCHEMA)
    assert "dated event verbatim" in _merge_prompt_tail(on)
    assert "dated event verbatim" in _cluster_merge_schema(on)["properties"]["should_merge"]["description"]


# ---------------------------------------------------------------------------
# The migration file
# ---------------------------------------------------------------------------


def test_the_migration_adds_three_nullable_columns_and_no_index():
    statements = _split_sql_statements(_sql())
    assert len(statements) == 7
    assert "ADD COLUMN IF NOT EXISTS valid_from DATE" in statements[0]
    assert "ADD COLUMN IF NOT EXISTS valid_to DATE" in statements[1]
    assert "ADD COLUMN IF NOT EXISTS mentioned_at TIMESTAMPTZ" in statements[2]
    assert all(s.startswith(("ALTER TABLE heart.facts", "UPDATE heart.facts")) for s in statements)
    assert "INDEX" not in _code().upper()


def test_the_backfill_and_only_the_backfill_runs_without_the_updated_at_trigger():
    statements = _split_sql_statements(_sql())
    assert statements[3] == "ALTER TABLE heart.facts DISABLE TRIGGER set_updated_at"
    assert [s.split()[0] for s in statements[4:6]] == ["UPDATE", "UPDATE"]
    assert statements[6] == "ALTER TABLE heart.facts ENABLE TRIGGER set_updated_at"
    # The exact name of the trigger init.sql puts on heart.facts.
    init_sql = (ROOT / "sql" / "init.sql").read_text(encoding="utf-8")
    assert "CREATE TRIGGER set_updated_at BEFORE UPDATE ON heart.facts" in init_sql


def test_the_migration_has_nothing_the_splitter_would_mangle_and_no_grants():
    sql = _sql()
    assert "/*" not in sql and "$$" not in sql
    assert not re.search(r"(?<!:):[A-Za-z_]", _code())  # text() reads :name as a bind parameter
    assert all(";" not in ln for ln in sql.splitlines() if ln.lstrip().startswith("--"))
    code = _code().upper()
    for word in ("GRANT", "REVOKE", "OWNER", "ROLE", "AUTHORIZATION"):
        assert not re.search(rf"\b{word}\b", code), word


def test_the_eval_preflight_probes_the_three_columns():
    from nous_eval.schema_preflight import migration_artifacts

    assert migration_artifacts(_sql()) == [("column", "heart", "facts", c) for c in TEMPORAL]


@pytest.mark.postgres_only
async def test_the_migration_backfills_and_a_second_run_changes_nothing(session):
    agent = f"test-097-backfill-{uuid4()}"
    learned = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    edited = datetime(2026, 6, 1, 8, 0, tzinfo=UTC)  # the rows' last real edit
    ep = uuid4()
    await session.execute(
        text("INSERT INTO heart.episodes (id, agent_id, summary, started_at) VALUES (:id, :a, 's', :t)"),
        {"id": ep, "a": agent, "t": EPISODE_START},
    )
    ids = {k: uuid4() for k in ("dated_ep", "undated_ep", "undated_noep", "merged_span")}
    rows = [
        (ids["dated_ep"], date(2024, 4, 10), None, None, ep),
        (ids["undated_ep"], None, None, None, ep),
        (ids["undated_noep"], None, None, None, None),
        # A row already carrying an interval is left alone.
        (ids["merged_span"], None, date(2026, 2, 1), date(2026, 2, 28), None),
    ]
    for fid, event_date, vf, vt, src in rows:
        await session.execute(
            text(
                "INSERT INTO heart.facts (id, agent_id, content, event_date, valid_from, valid_to, "
                "mentioned_at, source_episode_id, learned_at, updated_at) "
                "VALUES (:id, :a, 'c', :ed, :vf, :vt, NULL, :src, :la, :ua)"
            ),
            {"id": fid, "a": agent, "ed": event_date, "vf": vf, "vt": vt, "src": src, "la": learned, "ua": edited},
        )

    async def _state():
        res = await session.execute(
            text(
                "SELECT id, event_date, valid_from, valid_to, mentioned_at, updated_at FROM heart.facts "
                "WHERE agent_id = :a"
            ),
            {"a": agent},
        )
        return {r.id: (r.event_date, r.valid_from, r.valid_to, r.mentioned_at, r.updated_at) for r in res}

    statements = _split_sql_statements(_sql())
    for statement in statements:
        await session.execute(text(statement))
    first = await _state()
    assert first[ids["dated_ep"]] == (date(2024, 4, 10),) * 3 + (EPISODE_START, edited)
    assert first[ids["undated_ep"]] == (None, None, None, EPISODE_START, edited)
    # No episode, no statement time: the row is not rewritten, and readers use learned_at.
    assert first[ids["undated_noep"]] == (None, None, None, None, edited)
    assert first[ids["merged_span"]] == (None, date(2026, 2, 1), date(2026, 2, 28), None, edited)
    # None got a new updated_at: the backfill is not an edit.

    touched = [(s, (await session.execute(text(s))).rowcount) for s in statements]
    assert [n for s, n in touched if s.startswith("UPDATE")] == [0, 0]  # nothing left to fill, anywhere
    assert await _state() == first

    # The trigger is back on: a real edit after the migration still stamps updated_at.
    enabled = (
        await session.execute(
            text(
                "SELECT tgenabled::text FROM pg_trigger "
                "WHERE tgrelid = 'heart.facts'::regclass AND tgname = 'set_updated_at'"
            )
        )
    ).scalar_one()
    assert enabled == "O"
    await session.execute(text("UPDATE heart.facts SET content = 'edited' WHERE id = :id"), {"id": ids["dated_ep"]})
    assert (await _state())[ids["dated_ep"]][4] > edited

    cols = (
        await session.execute(
            text(
                "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
                "WHERE table_schema = 'heart' AND table_name = 'facts' "
                "AND column_name IN ('valid_from', 'valid_to', 'mentioned_at') ORDER BY column_name"
            )
        )
    ).all()
    assert [tuple(r) for r in cols] == [
        ("mentioned_at", "timestamp with time zone", "YES"),
        ("valid_from", "date", "YES"),
        ("valid_to", "date", "YES"),
    ]


# ---------------------------------------------------------------------------
# Persisting and reading back
# ---------------------------------------------------------------------------


async def _row(session, fact_id) -> Fact:
    row = await session.get(Fact, fact_id)
    await session.refresh(row)
    return row


@pytest.mark.postgres_only
async def test_learn_persists_the_fields_and_every_reader_returns_them(heart, session):
    detail = await heart.learn(
        FactInput(
            content="The canary rollout of build 4417 finished on the tenth.",
            event_date="2024-04-10",
            mentioned_at=EPISODE_START,
        ),
        session=session,
    )
    row = await _row(session, detail.id)
    assert (row.event_date, row.valid_from, row.valid_to) == (date(2024, 4, 10),) * 3
    assert row.mentioned_at == EPISODE_START
    assert (detail.valid_from, detail.valid_to, detail.mentioned_at) == (
        date(2024, 4, 10),
        date(2024, 4, 10),
        EPISODE_START,
    )

    legacy = (await heart.facts.fetch_legacy_fields([detail.id], session=session))[detail.id]
    assert legacy["valid_from"] == legacy["valid_to"] == "2024-04-10"
    assert legacy["mentioned_at"] == EPISODE_START.isoformat()

    # learn_fact and every caller that sets no statement time: NULL, readers fall back to learned_at.
    plain = await heart.learn(FactInput(content="Ledger rows are archived after ninety days."), session=session)
    row = await _row(session, plain.id)
    assert (row.valid_from, row.valid_to, row.mentioned_at) == (None, None, None)


AUDIT = "The quarterly security audit of the payments service closed with no findings."
LISBON = "Christina worked from the Lisbon office during the product launch."


@pytest.mark.postgres_only
async def test_confirm_duplicate_fills_the_interval_with_the_date(heart, session):
    undated = await heart.learn(FactInput(content=AUDIT), session=session)
    dupe = await _row(session, undated.id)
    await heart.facts._confirm_duplicate(dupe, FactInput(content=AUDIT, event_date="2024-04-10"), session)
    row = await _row(session, undated.id)
    assert (row.event_date, row.valid_from, row.valid_to) == (date(2024, 4, 10),) * 3


@pytest.mark.postgres_only
async def test_confirm_duplicate_never_dates_a_row_that_carries_a_span(heart, session):
    span = await heart.learn(
        FactInput(content=LISBON, valid_from="2026-02-01", valid_to="2026-02-28"),
        session=session,
    )
    dupe = await _row(session, span.id)
    await heart.facts._confirm_duplicate(dupe, FactInput(content=LISBON, event_date="2026-02-10"), session)
    row = await _row(session, span.id)
    assert (row.event_date, row.valid_from, row.valid_to) == (None, date(2026, 2, 1), date(2026, 2, 28))


# ---------------------------------------------------------------------------
# Merge inheritance
# ---------------------------------------------------------------------------


async def _merged(heart, session, sources, *, w: bool, inherit_temporal: bool = True, replacement=None):
    """Learn the sources and a bare replacement, inherit, and return the replacement row."""
    heart.facts._settings.temporal_intervals_enabled = w
    source_ids = []
    for i, (kwargs, mentioned) in enumerate(sources):
        d = await heart.learn(
            FactInput(content=f"Source fact {i} {uuid4()} about the Lisbon office.", mentioned_at=mentioned, **kwargs),
            session=session,
        )
        source_ids.append(d.id)
    rep = await heart.learn(
        FactInput(content=f"Merged fact {uuid4()} about the Lisbon office.", **(replacement or {})), session=session
    )
    await heart.facts.inherit_conflict_slot_keys(rep.id, source_ids, session, inherit_temporal=inherit_temporal)
    await session.flush()
    return await _row(session, rep.id)


T1 = datetime(2024, 4, 1, tzinfo=UTC)
T2 = datetime(2024, 5, 1, tzinfo=UTC)


@pytest.mark.postgres_only
async def test_a_merge_of_two_same_day_points_is_that_point(heart, session):
    row = await _merged(
        heart, session, [({"event_date": "2024-04-10"}, T1), ({"event_date": "2024-04-10"}, T2)], w=True
    )
    assert (row.event_date, row.valid_from, row.valid_to) == (date(2024, 4, 10),) * 3
    assert row.mentioned_at == T2


@pytest.mark.postgres_only
async def test_a_merge_of_two_points_is_their_span_without_an_event_date(heart, session):
    row = await _merged(
        heart, session, [({"event_date": "2024-04-10"}, T1), ({"event_date": "2024-04-12"}, T2)], w=True
    )
    assert (row.event_date, row.valid_from, row.valid_to) == (None, date(2024, 4, 10), date(2024, 4, 12))


@pytest.mark.postgres_only
async def test_a_merge_with_an_open_state_stays_open(heart, session):
    row = await _merged(
        heart, session, [({"event_date": "2024-04-10"}, T1), ({"valid_from": "2024-01-01"}, T2)], w=True
    )
    assert (row.event_date, row.valid_from, row.valid_to) == (None, date(2024, 1, 1), None)


@pytest.mark.postgres_only
async def test_a_merge_with_an_undated_source_keeps_the_shared_date_as_a_point(heart, session):
    row = await _merged(heart, session, [({"event_date": "2024-04-10"}, T1), ({}, T2)], w=True)
    assert (row.event_date, row.valid_from, row.valid_to) == (date(2024, 4, 10),) * 3


@pytest.mark.postgres_only
async def test_a_merge_of_spans_with_an_undated_source_gets_no_interval(heart, session):
    row = await _merged(
        heart, session, [({"valid_from": "2026-02-01", "valid_to": "2026-02-28"}, T1), ({}, None)], w=True
    )
    assert (row.event_date, row.valid_from, row.valid_to) == (None, None, None)
    # The max over COALESCE(mentioned_at, learned_at): the second source has no
    # statement time, and it was learned now, after T1.
    assert row.mentioned_at > T1 + timedelta(days=365)


@pytest.mark.postgres_only
async def test_a_merge_with_w_off_inherits_only_the_statement_time(heart, session):
    row = await _merged(
        heart, session, [({"event_date": "2024-04-10"}, T1), ({"event_date": "2024-04-10"}, T2)], w=False
    )
    assert (row.event_date, row.valid_from, row.valid_to) == (None, None, None)
    assert row.mentioned_at == T2
    assert row.event_date_classified_at is None  # still eligible for the F075.1 backfill


@pytest.mark.postgres_only
async def test_an_inherited_interval_is_stamped_classified(heart, session):
    """Unstamped, the F075.1 backfill would re-ask the LLM about the merged text
    and overwrite the inherited span with whatever single day it answers."""
    row = await _merged(
        heart,
        session,
        [({"valid_from": "2026-02-01", "valid_to": "2026-02-28"}, T1), ({"event_date": "2026-02-10"}, T2)],
        w=True,
    )
    assert (row.event_date, row.valid_from, row.valid_to) == (None, date(2026, 2, 1), date(2026, 2, 28))
    assert row.event_date_classified_at is not None


@pytest.mark.postgres_only
async def test_supersede_inherits_no_time(heart, session):
    row = await _merged(heart, session, [({"valid_from": "2024-01-01"}, T1)], w=True, inherit_temporal=False)
    assert (row.event_date, row.valid_from, row.valid_to, row.mentioned_at) == (None, None, None, None)


@pytest.mark.postgres_only
async def test_a_dated_replacement_keeps_its_own_time(heart, session):
    row = await _merged(
        heart,
        session,
        [({"event_date": "2024-04-10"}, T1)],
        w=True,
        replacement={"event_date": "2024-06-01", "mentioned_at": T2},
    )
    assert (row.event_date, row.valid_from, row.valid_to, row.mentioned_at) == (
        date(2024, 6, 1),
        date(2024, 6, 1),
        date(2024, 6, 1),
        T2,
    )


# ---------------------------------------------------------------------------
# The extractor round trip: summarizer (stub LLM) -> FactExtractor -> rows
# ---------------------------------------------------------------------------


_SUMMARY_TRANSCRIPT = (
    "user: We shipped release 4.2 of the billing service to production yesterday.\n\n"
    "user: Christina was in Lisbon in Feb 2026 for the conference.\n\n"
    "assistant: Noted both."
)

_CANDIDATES = [
    {
        "subject": "billing release 4.2",
        "content": "The team shipped billing release 4.2 to production on 2024-04-10.",
        "category": "event",
        "event_date": "2024-04-10",
    },
    {
        "subject": "Lisbon trip",
        "content": "Christina was in Lisbon in February 2026.",
        "category": "event",
        "valid_from": "2026-02-01",
        "valid_to": "2026-02-28",
    },
]


@pytest.mark.postgres_only
@pytest.mark.parametrize("w", [True, False])
async def test_the_summarizer_round_trip_stores_event_and_statement_time(db, mock_embeddings, monkeypatch, w):
    """A stub LLM tests the plumbing, not date resolution: the prompt must carry the
    episode start, and what the model returns for it must land on the row."""
    from nous.handlers.episode_summarizer import EpisodeSummarizer
    from nous.handlers.fact_extractor import FactExtractor
    from nous.heart.heart import Heart

    agent = f"test-097-rt-{uuid4()}"
    settings = Settings().model_copy(
        update={
            "agent_id": agent,
            "temporal_extraction_enabled": True,
            "temporal_intervals_enabled": w,
            "admission_control_enabled": False,
        }
    )
    heart = Heart(db, settings, embedding_provider=mock_embeddings)
    prompts: list[str] = []

    async def _llm(_client, **kw):
        prompts.append(kw["user_message"])
        return json.dumps({"summary": "Deploy and travel.", "key_points": [], "candidate_facts": _CANDIDATES})

    monkeypatch.setattr("nous.handlers.episode_summarizer.call_background_llm", _llm)
    try:
        ep = await heart.start_episode(EpisodeInput(summary="deploy and travel"))
        async with db.session() as s:
            await s.execute(
                text("UPDATE heart.episodes SET started_at = :t WHERE id = :i"), {"t": EPISODE_START, "i": ep.id}
            )
            await s.commit()
        summary = await EpisodeSummarizer(heart, None, settings, None, llm_client=object()).summarize_episode(
            ep.id, _SUMMARY_TRANSCRIPT, agent_id=agent
        )
        assert summary is not None
        assert f"EPISODE_START_TIMESTAMP: {EPISODE_START.isoformat()}" in prompts[0]
        assert ('"valid_from"' in prompts[0]) is w

        extractor = FactExtractor(heart, settings, None, llm_client=None, dedup_via_search=False)
        ids = await extractor.extract_and_store(summary=summary, episode_id=str(ep.id), transcript=_SUMMARY_TRANSCRIPT)
        assert len(ids) == 2
        async with db.session() as s:
            rows = {r.content: r for r in (await s.execute(select(Fact).where(Fact.agent_id == agent))).scalars().all()}
        point = rows[_CANDIDATES[0]["content"]]
        span = rows[_CANDIDATES[1]["content"]]
        assert (point.event_date, point.valid_from, point.valid_to) == (date(2024, 4, 10),) * 3
        if w:
            assert (span.event_date, span.valid_from, span.valid_to) == (None, date(2026, 2, 1), date(2026, 2, 28))
        else:
            assert (span.event_date, span.valid_from, span.valid_to) == (None, None, None)
        assert point.mentioned_at == span.mentioned_at == EPISODE_START
        assert point.learned_at - EPISODE_START > timedelta(days=365)  # ingestion time stays now
    finally:
        async with db.session() as s:
            await s.execute(text("DELETE FROM heart.facts WHERE agent_id = :a"), {"a": agent})
            await s.execute(text("DELETE FROM heart.episodes WHERE agent_id = :a"), {"a": agent})
            await s.commit()
        await heart.close()


async def test_the_reference_time_lookup_never_raises(caplog):
    from nous.handlers.fact_extractor import FactExtractor

    fx = FactExtractor.__new__(FactExtractor)
    ep = str(uuid4())

    # An unexpected failure is a WARNING: the episode's facts lose their statement time.
    fx._heart = MagicMock()  # get_episode is not awaitable
    with caplog.at_level(logging.DEBUG, logger="nous.handlers.fact_extractor"):
        assert await fx._episode_ref_time(ep) is None
    assert [r.levelno for r in caplog.records] == [logging.WARNING]
    caplog.clear()
    # A missing episode is only DEBUG.
    fx._heart = AsyncMock()
    fx._heart.get_episode = AsyncMock(side_effect=ValueError("Episode not found"))
    with caplog.at_level(logging.DEBUG, logger="nous.handlers.fact_extractor"):
        assert await fx._episode_ref_time(ep) is None
    assert [r.levelno for r in caplog.records] == [logging.DEBUG]
    fx._heart.get_episode = AsyncMock(return_value=SimpleNamespace(started_at=MagicMock()))
    assert await fx._episode_ref_time(ep) is None
    fx._heart.get_episode = AsyncMock(return_value=SimpleNamespace(started_at=EPISODE_START))
    assert await fx._episode_ref_time(ep) == EPISODE_START
    assert await fx._episode_ref_time("?") is None


async def test_the_fallback_extractor_sets_only_the_statement_time():
    from nous.handlers.fact_extractor import FactExtractor

    captured = []
    fx = FactExtractor.__new__(FactExtractor)
    fx._heart = MagicMock()
    fx._heart.learn = AsyncMock(side_effect=lambda fi, **_kw: captured.append(fi) or SimpleNamespace(id=uuid4()))
    fx._settings = SimpleNamespace(
        fact_dedup_threshold=0.92,
        candidate_facts_event_limit=30,
        temporal_extraction_enabled=True,
        temporal_intervals_enabled=True,
    )
    fx._dedup_via_search = False
    await fx._store_extracted_facts(
        [{"content": "Christina was in Lisbon.", "confidence": 0.9, "valid_from": "2026-02-01"}],
        "?",
        None,
        ref_time=EPISODE_START,
    )
    (fi,) = captured
    assert fi.mentioned_at == EPISODE_START
    assert fi.valid_from is None  # the fallback prompt asks for no interval


# ---------------------------------------------------------------------------
# The F075.1 backfill script keeps the row invariant
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_the_temporal_backfill_writes_the_interval_with_the_date(session, monkeypatch):
    import scripts.backfill_temporal_facts as backfill

    agent = f"test-097-f075bf-{uuid4()}"
    ids = {k: uuid4() for k in ("gets_date", "undated_state", "loses_date")}
    for fid, ed, vf, vt in (
        (ids["gets_date"], None, None, None),
        (ids["undated_state"], None, date(2026, 1, 1), None),
        (ids["loses_date"], date(2024, 1, 5), date(2024, 1, 5), date(2024, 1, 5)),
    ):
        await session.execute(
            text(
                "INSERT INTO heart.facts (id, agent_id, content, event_date, valid_from, valid_to) "
                "VALUES (:id, :a, 'c', :ed, :vf, :vt)"
            ),
            {"id": fid, "a": agent, "ed": ed, "vf": vf, "vt": vt},
        )
    verdict = {ids["gets_date"]: date(2024, 4, 10), ids["undated_state"]: None, ids["loses_date"]: None}

    async def _classify(_client, _model, row, _ctx):
        return verdict[row["id"]], True

    monkeypatch.setattr(backfill, "_classify_event_date", _classify)
    for reclassify in (False, True):
        await backfill._process_batch(
            session, agent, 10, backfill.BudgetTracker(10), client=None, model="m", cursor=None, reclassify=reclassify
        )

    async def _get(fid):
        r = (
            await session.execute(
                text("SELECT event_date, valid_from, valid_to FROM heart.facts WHERE id = :id"), {"id": fid}
            )
        ).one()
        return tuple(r)

    assert await _get(ids["gets_date"]) == (date(2024, 4, 10),) * 3
    assert await _get(ids["undated_state"]) == (None, date(2026, 1, 1), None)
    assert await _get(ids["loses_date"]) == (None, None, None)
