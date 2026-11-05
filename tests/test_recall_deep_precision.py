"""recall_deep precision controls (precision plan P1): a leg cut, a top-k with type floors, a section order.

All three are off by default, and then recall_deep's text on a seeded corpus is byte-identical to origin/main:
``tests/fixtures/recall_deep_p1_golden.txt`` was written by this module's ``render_corpus`` on f66d0204, before
these settings existed. Each setting then changes that text the way it says it does.

The corpus is inserted with fixed ids (the text prints them) under an agent of its own per test. Imports of
names this change adds sit inside the tests, so ``render_corpus`` also runs against origin/main.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from conftest import MockEmbeddingProvider
from sqlalchemy import text

GOLDEN = Path(__file__).parent / "fixtures" / "recall_deep_p1_golden.txt"
QUERY = "Grundfos"
# The filler rows' embeddings come from their text; SALT picks texts whose random vectors leave the keyed,
# keyed-hop, graph-memory and 1-hop rows outside the heart and brain cuts, so the golden shows every leg.
SALT = "d"


def _uid(n: int) -> UUID:
    return UUID(f"{n:08d}-0000-4000-8000-{n:012d}")


E1, E2 = _uid(101), _uid(102)
C1, C2 = _uid(201), _uid(202)
F1, F2, F3, F4, F5, F6, F7 = (_uid(300 + i) for i in range(1, 8))
FILLERS = [_uid(400 + i) for i in range(24)]
D1, D2, D3 = _uid(501), _uid(502), _uid(503)
FILLER_DECISIONS = [_uid(600 + i) for i in range(12)]
ALL_FACTS = [F1, F2, F3, F4, F5, F6, F7, *FILLERS]
ALL_DECISIONS = [D1, D2, D3, *FILLER_DECISIONS]

EPISODES = [
    (E1, "Orchard irrigation planning", "Planned the orchard irrigation schedule: drip lines, twice a week."),
    (E2, "Pump maintenance", "Talked about Grundfos pump maintenance before the irrigation season."),
]
CHUNKS = [
    (C1, E1, "user: when should the orchard irrigation run? assistant: twice a week in summer, early morning."),
    (C2, E2, "user: the Grundfos pump is leaking. assistant: replace the shaft seal before the irrigation season."),
]
# (id, content, subject, source episode, event date, entity keys)
FACTS = [
    (F1, "The orchard irrigation runs twice a week in summer.", "orchard irrigation", E1, None, []),
    (F2, "Drip lines were chosen for the orchard irrigation.", "orchard irrigation", E1, None, []),
    (F3, "The irrigation pump is a Grundfos unit.", "irrigation pump", E2, date(2026, 6, 1), ["grundfos"]),
    (
        F4,
        "Valley Supply services the Grundfos unit every spring.",
        "pump service",
        E2,
        None,
        ["grundfos", "valley supply"],
    ),
    (F5, "Valley Supply shuts early on Fridays.", "valley supply", None, None, ["valley supply"]),
    (F6, "Mulch keeps the soil moist between the rows.", "mulch", None, None, []),
    (F7, "The warranty on that unit runs until 2028.", "warranty", None, None, ["grundfos"]),
    *[
        (fid, f"Filler record {i}{SALT}: the barn roof needs paint.", None, None, None, [])
        for i, fid in enumerate(FILLERS)
    ],
]
# (id, description, category, stakes, confidence, outcome)
DECISIONS = [
    (D1, "Chose Grundfos drip irrigation for the orchard over sprinklers", "architecture", "medium", 0.8, "pending"),
    (D2, "Replace the Grundfos pump seal before the season", "process", "low", 0.7, "success"),
    (D3, "Budget the barn repairs for next year", "process", "low", 0.6, "pending"),
    *[
        (did, f"Filler decision {i}{SALT} about the fence line", "process", "low", 0.5, "pending")
        for i, did in enumerate(FILLER_DECISIONS)
    ],
]
# (source, source type, target, target type, relation, extraction method)
EDGES = [
    *[
        (src, st, D1, "decision", "supports", "deterministic")
        for src, st in ((F1, "fact"), (F2, "fact"), (F3, "fact"), (F4, "fact"), (E1, "episode"), (E2, "episode"))
    ],
    (D1, "decision", D3, "decision", "related_to", "deterministic"),
    (D2, "decision", D3, "decision", "related_to", "deterministic"),
    *[
        (src, st, F6, "fact", "co_occurred", "co_occurrence")
        for src, st in (
            (F1, "fact"),
            (F2, "fact"),
            (F3, "fact"),
            (F4, "fact"),
            (E1, "episode"),
            (E2, "episode"),
            (C1, "chunk"),
            (C2, "chunk"),
        )
    ],
]

# The prod-shaped switches recall_deep's paths read; the rest stay at code defaults.
BASE_SETTINGS = {
    "episode_chunks_enabled": True,
    "chunk_hybrid_search_enabled": True,
    "recall_include_parent_episodes": True,
    "heart_graph_all_types_enabled": True,
    "session_group_heart_section": True,
    "temporal_render_enabled": True,
    "keyed_fact_leg_enabled": True,
    "keyed_fact_leg_rounds": 2,
    "spreading_activation_enabled": "false",  # the 1-hop fallback: no density gate
    "cross_encoder_enabled": False,
    "query_expansion_enabled": False,
    "date_leg_enabled": False,
    "residual_activation_enabled": False,
}


def _vec(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


async def _clear(db, agent: str | None) -> None:
    fixed = [str(i) for i in (E1, E2, C1, C2, *ALL_FACTS, *ALL_DECISIONS)]
    async with db.session() as s:
        for table, col in (
            ("heart.fact_entity_keys", "fact_id"),
            ("brain.graph_edges", "source_id"),
            ("heart.episode_chunks", "id"),
            ("heart.facts", "id"),
            ("brain.decisions", "id"),
            ("heart.episodes", "id"),
        ):
            await s.execute(text(f"DELETE FROM {table} WHERE {col}::text = ANY(:ids)"), {"ids": fixed})
            if agent:
                await s.execute(text(f"DELETE FROM {table} WHERE agent_id = :a"), {"a": agent})
        await s.commit()


async def _seed(db, agent: str, emb: MockEmbeddingProvider) -> None:
    await _clear(db, None)
    async with db.session() as s:
        for eid, title, summary in EPISODES:
            await s.execute(
                text(
                    "INSERT INTO heart.episodes (id, agent_id, title, summary, embedding, active, ended_at) "
                    "VALUES (:id, :a, :t, :s, CAST(:v AS vector), false, now())"
                ),
                {"id": eid, "a": agent, "t": title, "s": summary, "v": _vec(await emb.embed(summary))},
            )
        for cid, eid, content in CHUNKS:
            await s.execute(
                text(
                    "INSERT INTO heart.episode_chunks "
                    "(id, agent_id, episode_id, chunk_index, content, embedding, source_kind) "
                    "VALUES (:id, :a, :e, 0, :c, CAST(:v AS vector), 'dialogue')"
                ),
                {"id": cid, "a": agent, "e": eid, "c": content, "v": _vec(await emb.embed(content))},
            )
        for did, desc, category, stakes, conf, outcome in DECISIONS:
            await s.execute(
                text(
                    "INSERT INTO brain.decisions "
                    "(id, agent_id, description, confidence, category, stakes, outcome, embedding) "
                    "VALUES (:id, :a, :d, :c, :cat, :st, :o, CAST(:v AS vector))"
                ),
                {
                    "id": did,
                    "a": agent,
                    "d": desc,
                    "c": conf,
                    "cat": category,
                    "st": stakes,
                    "o": outcome,
                    "v": _vec(await emb.embed(desc)),
                },
            )
        for fid, content, subject, ep, event, keys in FACTS:
            await s.execute(
                text(
                    "INSERT INTO heart.facts (id, agent_id, content, category, subject, source, source_episode_id, "
                    "event_date, embedding, active) "
                    "VALUES (:id, :a, :c, 'technical', :subj, 'user_direct', :ep, :ev, CAST(:v AS vector), true)"
                ),
                {
                    "id": fid,
                    "a": agent,
                    "c": content,
                    "subj": subject,
                    "ep": ep,
                    "ev": event,
                    "v": _vec(await emb.embed(content)),
                },
            )
            for key in keys:
                await s.execute(
                    text("INSERT INTO heart.fact_entity_keys (fact_id, entity_key, agent_id) VALUES (:f, :k, :a)"),
                    {"f": fid, "k": key, "a": agent},
                )
        for src, st, tgt, tt, rel, method in EDGES:
            await s.execute(
                text(
                    "INSERT INTO brain.graph_edges "
                    "(source_id, source_type, target_id, target_type, agent_id, relation, weight, extraction_method) "
                    "VALUES (:s, :st, :t, :tt, :a, :r, 0.8, :m)"
                ),
                {"s": src, "st": st, "t": tgt, "tt": tt, "a": agent, "r": rel, "m": method},
            )
        await s.commit()


def corpus_settings(agent: str, **overrides):
    """Settings for the corpus: BASE_SETTINGS plus ``overrides``, built by the validators (env-style values ok)."""
    from nous.config import Settings

    return Settings(_env_file=None, **{**BASE_SETTINGS, **overrides}).model_copy(update={"agent_id": agent})


async def render_corpus(db, settings, query: str = QUERY) -> tuple[str, list]:
    """Seed the corpus under ``settings.agent_id`` and return (recall_deep text, formatter emission order)."""
    import nous.api.tools as tools_mod
    from nous.brain.brain import Brain
    from nous.heart import Heart

    emb = MockEmbeddingProvider()
    agent = settings.agent_id
    await _seed(db, agent, emb)
    heart = Heart(db, settings, embedding_provider=emb)
    brain = Brain(database=db, settings=settings, embedding_provider=emb)
    emitted: list = []
    orig = tools_mod._format_pipeline_text

    def capture(*a, **kw):
        kw["emitted_out"] = kw.get("emitted_out") if kw.get("emitted_out") is not None else []
        out = orig(*a, **kw)
        emitted[:] = list(kw["emitted_out"])
        return out

    tools_mod._format_pipeline_text = capture
    try:
        tools = tools_mod.create_nous_tools(brain=brain, heart=heart, settings=settings)
        res = await tools["recall_deep"](query=query, limit=10)
        return res["content"][0]["text"], emitted
    finally:
        tools_mod._format_pipeline_text = orig
        await _clear(db, agent)
        await heart.close()
        await brain.close()


def _agent() -> str:
    return f"test-p1-{uuid4().hex[:8]}"


_ROW = re.compile(r"^\d+\. .*\(id: ([0-9a-f-]{36})", re.M)


def _row_ids(txt: str) -> list[str]:
    """The ids of the numbered rows the text shows, in text order."""
    return _ROW.findall(txt)


# ---------------------------------------------------------------------------
# Flags off: byte-identical to origin/main
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_flags_off_text_is_byte_identical_to_origin_main(db):
    txt, _ = await render_corpus(db, corpus_settings(_agent()))
    assert txt == GOLDEN.read_text(encoding="utf-8")


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_settings_spelled_out_at_their_defaults_change_nothing(db):
    s = corpus_settings(
        _agent(),
        recall_deep_disabled_legs="",
        recall_deep_max_items=0,
        recall_deep_type_min="decision:2",  # no effect without max_items
        recall_deep_section_order="heart,graph_decisions,brain_decisions",
    )
    txt, _ = await render_corpus(db, s)
    assert txt == GOLDEN.read_text(encoding="utf-8")


def test_golden_shows_every_part_the_flags_act_on():
    """The golden is only a guard where it holds what the settings change."""
    g = GOLDEN.read_text(encoding="utf-8")
    for part in (
        "=== Heart Memory ===",
        "-- Session ",
        "[chunk]",
        "[via keyed] ",
        "[via keyed-hop] ",
        "[via co_occurred] ",
        "=== Graph-Connected Decisions ===",
        "=== Brain Decisions ===",
        "[via graph: related_to]",
        "=== Parent Episode Context ===",
    ):
        assert part in g, part


# ---------------------------------------------------------------------------
# recall_deep_disabled_legs
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "leg,gone",
    [
        ("keyed_r2", ["[via keyed-hop] "]),
        ("keyed", ["[via keyed] ", "[via keyed-hop] "]),
        ("parent_episode", ["=== Parent Episode Context ==="]),
        ("graph_expanded", ["[via graph: related_to]"]),
        ("heart_graph", ["=== Graph-Connected Decisions ==="]),
        ("heart_graph_memory", ["[via co_occurred] "]),
        ("chunk", ["[chunk]"]),
    ],
)
async def test_a_disabled_leg_leaves_the_text(db, leg, gone):
    txt, _ = await render_corpus(db, corpus_settings(_agent(), recall_deep_disabled_legs=leg))
    for part in gone:
        assert part not in txt, part
    # The other legs still render.
    assert "=== Heart Memory ===" in txt
    assert "=== Brain Decisions ===" in txt


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_disabling_brain_keeps_its_graph_expansion(db):
    """brain still runs (it seeds Stage 4); only its own rows leave."""
    g = GOLDEN.read_text(encoding="utf-8")
    txt, _ = await render_corpus(db, corpus_settings(_agent(), recall_deep_disabled_legs="brain"))
    brain_rows = g.split("=== Brain Decisions ===")[1].split("\n\n")[0]
    assert "| architecture | medium |" in brain_rows
    assert "| architecture | medium |" not in txt
    assert "[via graph: related_to]" in txt


@pytest.mark.postgres_only
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "leg,existing",
    [
        ("keyed_r2", {"keyed_fact_leg_rounds": 1}),
        ("parent_episode", {"recall_include_parent_episodes": False}),
    ],
)
async def test_cutting_a_leg_matches_its_own_switch(db, leg, existing):
    """For the two legs that already had a switch, the cut is the same text."""
    cut, _ = await render_corpus(db, corpus_settings(_agent(), recall_deep_disabled_legs=leg))
    off, _ = await render_corpus(db, corpus_settings(_agent(), **existing))
    assert cut == off


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_legs_nothing_reads_are_not_run(db):
    """keyed, keyed_r2, exemplar and graph_expanded skip their work, not only their rows."""
    from nous.api.retrieval_pipeline import run_recall_pipeline
    from nous.brain.brain import Brain
    from nous.heart import Heart

    agent = _agent()
    emb = MockEmbeddingProvider()
    await _seed(db, agent, emb)
    try:
        for legs, check in (
            ("keyed_r2", lambda st: "keyed" in st.attempted_legs and "keyed_r2" not in st.attempted_legs),
            ("keyed", lambda st: not st.keyed_leg_used and "keyed_r2" not in st.attempted_legs),
            ("graph_expanded", lambda st: st.n_graph_expanded == 0 and "brain_graph" not in st.attempted_legs),
        ):
            settings = corpus_settings(agent, recall_deep_disabled_legs=legs)
            heart = Heart(db, settings, embedding_provider=emb)
            brain = Brain(database=db, settings=settings, embedding_provider=emb)
            try:
                _, on = await run_recall_pipeline(QUERY, heart, brain, corpus_settings(agent), limit=10)
                _, off = await run_recall_pipeline(QUERY, heart, brain, settings, limit=10)
            finally:
                await heart.close()
                await brain.close()
            assert not check(on), f"{legs}: the leg must run with the cut off"
            assert check(off), legs
    finally:
        await _clear(db, agent)


def test_disabled_leg_trace_says_disabled():
    """Each disabled leg is named in the trace with skip_reason='disabled', run or not."""
    import asyncio

    from nous.api.retrieval_pipeline import run_recall_pipeline
    from nous.observability.retrieval_trace import RetrievalTrace

    tr = RetrievalTrace(query="q", path="pipeline")
    heart, brain = _empty_heart_brain()
    settings = SimpleNamespace(
        graph_recall_enabled=False,
        cross_type_linking_enabled=False,
        contradiction_detection=False,
        recall_deep_disabled_legs=["heart_primary", "graph_expanded", "keyed"],
    )
    asyncio.run(run_recall_pipeline("q", heart, brain, settings, limit=10, trace=tr))
    legs = {lg["name"]: lg for lg in tr.to_dict()["legs"]}
    for name in ("heart_primary", "graph_expanded", "keyed"):
        assert legs[name]["skip_reason"] == "disabled", name
    assert legs["heart_primary"]["attempted"] is True  # it ran; its rows were left out
    assert legs["graph_expanded"]["attempted"] is False


def _empty_heart_brain():
    from unittest.mock import AsyncMock, MagicMock

    heart = MagicMock()
    heart.recall = AsyncMock(return_value=[])
    heart.date_window_parser = None
    brain = MagicMock()
    brain.query = AsyncMock(return_value=[])
    return heart, brain


# ---------------------------------------------------------------------------
# recall_deep_max_items / recall_deep_type_min
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_max_items_keeps_the_first_k_rows_in_pipeline_order(db):
    _, full = await render_corpus(db, corpus_settings(_agent(), recall_deep_section_order="ranked"))
    txt, kept = await render_corpus(
        db, corpus_settings(_agent(), recall_deep_section_order="ranked", recall_deep_max_items=6)
    )
    assert len(full) > 6
    assert kept == full[:6]
    assert len(_row_ids(txt)) == 6


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_type_floor_serves_decisions_that_rank_low(db):
    """Decisions below the first k rows are cut by k alone and kept by decision:2.

    On "irrigation" the corpus ranks five other rows above its first decision."""
    q = "irrigation"
    _, full = await render_corpus(db, corpus_settings(_agent(), recall_deep_section_order="ranked"), query=q)
    k = next(i for i, (_, t) in enumerate(full) if t == "decision")  # the rows before the first decision
    assert k >= 3, "the corpus must rank its decisions below several other rows"
    _, plain = await render_corpus(db, corpus_settings(_agent(), recall_deep_max_items=k), query=q)
    assert [t for _, t in plain].count("decision") == 0
    txt, floored = await render_corpus(
        db, corpus_settings(_agent(), recall_deep_max_items=k, recall_deep_type_min="decision:2"), query=q
    )
    assert [t for _, t in floored].count("decision") == 2
    assert len(floored) == k
    assert "=== Brain Decisions ===" in txt or "=== Graph-Connected Decisions ===" in txt


class _Trace:
    def __init__(self) -> None:
        self.dropped: list = []

    def drop(self, item_id, item_type, disposition, stage) -> None:
        self.dropped.append((item_id, disposition, stage))


def _rows(types: str) -> list:
    from nous.api.retrieval_pipeline import PipelineResult

    return [
        PipelineResult(id=_uid(900 + i), type=t, description=f"r{i}", score=1.0 - i / 100)
        for i, t in enumerate({"f": "fact", "d": "decision", "e": "episode", "c": "chunk"}[c] for c in types)
    ]


def _cut(rows, k, floors=None):
    from nous.api.retrieval_pipeline import _apply_max_items

    tr = _Trace()
    s = SimpleNamespace(recall_deep_max_items=k, recall_deep_type_min=floors or {})
    return _apply_max_items(rows, s, tr), tr


def test_max_items_off_or_short_list_is_the_same_list():
    rows = _rows("ffdd")
    assert _cut(rows, 0)[0] is rows
    assert _cut(rows, 4)[0] is rows
    assert _cut(rows, 0, {"decision": 1})[0] is rows  # floors alone do nothing


def test_max_items_cuts_by_place_not_by_score():
    from dataclasses import replace

    rows = _rows("fffd")
    rows[3] = replace(rows[3], score=9.0)  # a higher score on another scale does not move it up
    out, tr = _cut(rows, 2)
    assert out == rows[:2]
    assert [(i, d, st) for i, d, st in tr.dropped] == [
        (rows[2].id, "sliced_off", "recall_deep_max_items"),
        (rows[3].id, "sliced_off", "recall_deep_max_items"),
    ]


def test_floors_keep_the_first_n_of_a_type_and_survivors_keep_their_order():
    rows = _rows("fffefdcd")
    out, _ = _cut(rows, 4, {"decision": 2, "chunk": 1})
    # floors take d(5), c(6), d(7); one slot left goes to the first other row.
    assert out == [rows[0], rows[5], rows[6], rows[7]]


def test_a_floor_bigger_than_what_exists_keeps_what_exists():
    rows = _rows("ffffd")
    out, _ = _cut(rows, 2, {"decision": 3, "episode": 2})
    assert out == [rows[0], rows[4]]


def test_floors_beyond_k_win():
    rows = _rows("ffdddcc")
    out, _ = _cut(rows, 2, {"decision": 3, "chunk": 1})
    assert out == [rows[2], rows[3], rows[4], rows[5]]


# ---------------------------------------------------------------------------
# recall_deep_section_order
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_brain_decisions_first(db):
    _, base = await render_corpus(db, corpus_settings(_agent()))
    txt, emitted = await render_corpus(
        db, corpus_settings(_agent(), recall_deep_section_order="brain_decisions,heart,graph_decisions")
    )
    assert (
        txt.index("=== Brain Decisions ===")
        < txt.index("=== Heart Memory ===")
        < txt.index("=== Graph-Connected Decisions ===")
    )
    assert "\n\n=== Heart Memory ===" in txt  # a blank line before a section that is not first
    assert sorted(map(str, emitted)) == sorted(map(str, base))  # the same rows, reordered
    assert emitted[0][1] == "decision"
    assert [i for i, _ in emitted] == [UUID(i) for i in _row_ids(txt)]  # emission order is text order


@pytest.mark.postgres_only
@pytest.mark.asyncio
async def test_ranked_is_one_list_in_pipeline_order(db):
    from nous.api.retrieval_pipeline import run_recall_pipeline
    from nous.brain.brain import Brain
    from nous.heart import Heart

    agent = _agent()
    _, base = await render_corpus(db, corpus_settings(agent))
    txt, emitted = await render_corpus(db, corpus_settings(agent, recall_deep_section_order="ranked"))
    assert "=== Memory (ranked) ===" in txt
    assert "=== Heart Memory ===" not in txt and "=== Brain Decisions ===" not in txt
    assert sorted(map(str, emitted)) == sorted(map(str, base))
    assert [i for i, _ in emitted] == [UUID(i) for i in _row_ids(txt)]
    assert "[decision] [via supports] " in txt and "[decision] [via graph: related_to] " in txt

    # The order is the pipeline's own (recall_deep merges by score with chunks on).
    settings = corpus_settings(agent)
    emb = MockEmbeddingProvider()
    await _seed(db, agent, emb)
    heart = Heart(db, settings, embedding_provider=emb)
    brain = Brain(database=db, settings=settings, embedding_provider=emb)
    try:
        results, _ = await run_recall_pipeline(QUERY, heart, brain, settings, limit=10, rerank_by_score=True)
    finally:
        await _clear(db, agent)
        await heart.close()
        await brain.close()
    shown = {(i, t) for i, t in emitted}
    assert emitted == [(r.id, r.type) for r in results if (r.id, r.type) in shown]


@pytest.mark.parametrize(
    "env,error",
    [
        ({"recall_deep_disabled_legs": "keyd"}, "unknown legs"),
        ({"recall_deep_type_min": "decisions:2"}, "unknown type"),
        ({"recall_deep_type_min": "decision"}, "type:count"),
        ({"recall_deep_type_min": "decision:-1"}, ">= 0"),
        ({"recall_deep_section_order": "heart,brain_decisions"}, "permutation"),
        ({"recall_deep_section_order": "ranked,heart"}, "permutation"),
        ({"recall_deep_max_items": -1}, "greater than or equal"),
    ],
)
def test_a_typo_fails_at_load(env, error):
    from pydantic import ValidationError

    from nous.config import Settings

    with pytest.raises(ValidationError, match=error):
        Settings(_env_file=None, **env)


def test_env_values_parse(monkeypatch):
    from nous.config import Settings

    monkeypatch.setenv("NOUS_RECALL_DEEP_DISABLED_LEGS", " keyed_r2 , parent_episode,keyed_r2")
    monkeypatch.setenv("NOUS_RECALL_DEEP_MAX_ITEMS", "30")
    monkeypatch.setenv("NOUS_RECALL_DEEP_TYPE_MIN", "decision:2, episode:2,chunk:2")
    monkeypatch.setenv("NOUS_RECALL_DEEP_SECTION_ORDER", "ranked")
    s = Settings(_env_file=None)
    assert s.recall_deep_disabled_legs == ["keyed_r2", "parent_episode"]
    assert s.recall_deep_max_items == 30
    assert s.recall_deep_type_min == {"decision": 2, "episode": 2, "chunk": 2}
    assert s.recall_deep_section_order == ["ranked"]
    # Parsed values pass through again unchanged (the eval re-applies them as init values).
    again = Settings(
        _env_file=None,
        **{
            k: getattr(s, k) for k in ("recall_deep_disabled_legs", "recall_deep_type_min", "recall_deep_section_order")
        },
    )
    assert again.recall_deep_type_min == s.recall_deep_type_min
    assert again.recall_deep_section_order == ["ranked"]
    monkeypatch.setenv("NOUS_RECALL_DEEP_SECTION_ORDER", "")
    assert Settings(_env_file=None).recall_deep_section_order == ["heart", "graph_decisions", "brain_decisions"]
