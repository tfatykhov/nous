"""A retrieval failure must be distinguishable from "no results".

Each test pins a path where a failure was swallowed, aborted the whole
retrieval, or was logged without reaching the caller, so the retrieval trace
showed an empty leg (or nothing) where a leg had in fact failed.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import text
from test_date_leg_pipeline import _make_brain as _date_brain  # noqa: E402
from test_date_leg_pipeline import _make_heart as _date_heart  # noqa: E402
from test_date_leg_pipeline import _make_settings as _date_settings  # noqa: E402
from test_retrieval_pipeline import (  # noqa: E402  (test-local helper reuse)
    EPISODE_ID,
    FACT_ID,
    _make_brain,
    _make_heart,
    _make_recall_results,
    _make_settings,
)
from test_tiered_context import _frame, _fresh_engine, _session_profile_section, _tag_fact  # noqa: E402

from nous.api.retrieval_pipeline import run_recall_pipeline
from nous.heart import FactInput
from nous.observability.retrieval_trace import RetrievalTrace


def _legs(tr: RetrievalTrace) -> dict[str, dict]:
    return {leg["name"]: leg for leg in tr.to_dict()["legs"]}


class _FailingEmbeddings:
    """An embedding provider whose every call fails, as during an API outage."""

    async def embed(self, _text: str, *, fail_fast: bool = False) -> list[float]:
        raise RuntimeError("embedding API unavailable")

    async def embed_batch(self, texts: list[str], *, fail_fast: bool = False) -> list[list[float]]:
        raise RuntimeError("embedding API unavailable")

    async def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# A-1: brain.query was the one unguarded stage await
# ---------------------------------------------------------------------------


class TestBrainQueryFailure:
    @pytest.mark.asyncio
    async def test_heart_legs_survive_and_the_trace_names_the_failure(self):
        """A DB error in brain.query discarded every leg already fetched and
        recall_deep returned a tool error."""
        heart = _make_heart(recall_results=_make_recall_results())
        brain = _make_brain(neighbors_by_node={}, contradictions=[], decision_results=[])
        brain.query = AsyncMock(side_effect=RuntimeError("connection reset"))
        tr = RetrievalTrace(query="q")

        results, stats = await run_recall_pipeline(
            query="q",
            heart=heart,
            brain=brain,
            settings=_make_settings(graph_recall_enabled=False),
            limit=10,
            trace=tr,
        )

        assert {r.id for r in results} == {FACT_ID, EPISODE_ID}
        assert stats.n_stage_errors.get("brain") == 1
        legs = _legs(tr)
        # The error is on the brain leg itself, not on a second leg beside it.
        assert set(legs) >= {"brain"} and "brain_query" not in legs
        assert legs["brain"]["attempted"] is True
        assert legs["brain"]["n_returned"] == 0
        error = legs["brain"]["error"]
        assert error.startswith("1 error(s)")
        assert "RuntimeError: connection reset" in error


# ---------------------------------------------------------------------------
# F075 L3: the date parse ran outside every guard
# ---------------------------------------------------------------------------


class TestDateParseFailure:
    @pytest.mark.asyncio
    async def test_parse_raise_fails_open_with_a_trace_leg(self):
        parser = SimpleNamespace(parse=AsyncMock(side_effect=ValueError("bad window")))
        heart = _date_heart(parser=parser)
        tr = RetrievalTrace(query="q")

        results, _stats = await run_recall_pipeline(
            query="what happened last week",
            heart=heart,
            brain=_date_brain(),
            settings=_date_settings(date_leg_enabled=True),
            limit=10,
            trace=tr,
        )

        assert results, "the heart leg still runs without a date window"
        assert heart.recall.await_args.kwargs["date_window"] is None
        leg = _legs(tr)["date_window"]
        assert leg["attempted"] is True
        assert "ValueError: bad window" in leg["error"]


# ---------------------------------------------------------------------------
# C-2: a query expansion raise is counted
# ---------------------------------------------------------------------------


def _bare_heart(*, expander) -> object:
    """``Heart`` without a DB, as in the N1 tests (test_fixlist_n1_n3_n7)."""
    from nous.heart.heart import Heart

    heart = Heart.__new__(Heart)
    heart.settings = SimpleNamespace(
        heart_rrf_penalty_limit=None,
        query_expansion_enabled=True,
        mmr_enabled=False,
        mmr_skip_after_ce=True,
        mmr_diversity_weight=0.7,
        cross_encoder_enabled=False,
    )
    heart._query_expander = expander
    heart._embeddings = MagicMock()
    heart._residual_activator = None
    heart.agent_id = "test-agent"
    heart.facts = SimpleNamespace(search=AsyncMock(return_value=[]))
    return heart


class TestQueryExpansionFailure:
    @pytest.mark.asyncio
    async def test_expand_raise_is_counted(self):
        heart = _bare_heart(expander=SimpleNamespace(expand=AsyncMock(side_effect=RuntimeError("haiku down"))))
        stage_errors: dict[str, int] = {}

        results = await heart._recall(
            "q",
            10,
            ["fact"],
            MagicMock(),
            owns_session=True,
            stage_errors=stage_errors,
        )

        assert results == []
        assert stage_errors == {"query_expansion": 1}

    @pytest.mark.asyncio
    async def test_a_haiku_answer_ends_the_failure_streak(self, caplog):
        """Failures WARN 1-in-N; a success resets the count, so a sporadic
        failure after it WARNs again instead of waiting out the sample."""
        import logging

        from test_f050_query_expansion import _make_expander, _make_llm, _tool_use_response

        overloaded = RuntimeError("Anthropic API error (503): overloaded")
        llm = _make_llm(
            call_side_effect=[overloaded, overloaded, _tool_use_response(["variant one"]), overloaded],
        )
        expander = _make_expander(llm=llm)

        with caplog.at_level(logging.WARNING, logger="nous.heart.query_expansion"):
            for q in ("alpha beta gamma", "delta epsilon zeta", "eta theta iota", "kappa lambda mu"):
                await expander.expand(q, agent_id="a")

        warned = [r for r in caplog.records if r.levelno >= logging.WARNING and "503" in r.getMessage()]
        assert len(warned) == 2, [r.getMessage() for r in warned]


# ---------------------------------------------------------------------------
# D-2 / F-3: a failed query embedding silently switched to keyword-only
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
class TestEmbedFallbackCounted:
    @pytest.mark.asyncio
    async def test_fact_leg_returns_keyword_results_and_counts_the_fallback(
        self,
        heart,
        session,
        monkeypatch,
    ):
        fact = await heart.learn(
            FactInput(
                content="Aurora borealis keyword fallback probe",
                category="technical",
                subject="testing",
                confidence=0.9,
            ),
            session=session,
        )
        monkeypatch.setattr(
            heart.facts.embeddings, "embed", AsyncMock(side_effect=RuntimeError("embedding API unavailable"))
        )
        stage_errors: dict[str, int] = {}

        results = await heart.recall(
            "Aurora borealis keyword fallback probe",
            types=["fact"],
            session=session,
            stage_errors=stage_errors,
        )

        assert fact.id in {r.id for r in results}, "keyword-only search still finds it"
        assert stage_errors == {"fact_embed_fallback": 1}

    @pytest.mark.asyncio
    async def test_brain_query_counts_the_fallback_with_its_message(self, db, settings, session):
        from nous.brain.brain import Brain

        brain = Brain(database=db, settings=settings, embedding_provider=_FailingEmbeddings())
        try:
            stage_errors: dict[str, int] = {}
            msgs: dict[str, str] = {}
            await brain.query("anything", session=session, stage_errors=stage_errors, stage_error_msgs=msgs)
        finally:
            await brain.close()

        assert stage_errors == {"decision_embed_fallback": 1}
        assert msgs["decision_embed_fallback"] == "RuntimeError: embedding API unavailable"


# ---------------------------------------------------------------------------
# D-7: on a caller-owned session one failed leg aborted the transaction
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
class TestCallerSessionNoCascade:
    @pytest.mark.asyncio
    async def test_a_failed_first_leg_leaves_the_later_legs_working(
        self,
        heart,
        session,
        monkeypatch,
    ):
        fact = await heart.learn(
            FactInput(
                content="Cascade guard probe fact about lighthouses",
                category="technical",
                subject="testing",
                confidence=0.9,
            ),
            session=session,
        )

        async def _episode_leg_fails(_query, _limit, sess, **_kw):
            # A real SQL error on the caller's session, as a store a migration
            # behind raises. Without a savepoint it aborts the whole transaction.
            await sess.execute(text("SELECT 1/0"))

        monkeypatch.setattr(heart.episodes, "search", _episode_leg_fails)
        stage_errors: dict[str, int] = {}

        results = await heart.recall(
            "Cascade guard probe fact about lighthouses",
            types=["episode", "fact"],
            session=session,
            stage_errors=stage_errors,
        )

        assert stage_errors == {"heart_recall_episode": 1}, (
            "the fact leg must not inherit the episode leg's aborted transaction"
        )
        assert fact.id in {r.id for r in results}


# ---------------------------------------------------------------------------
# The error text: class and first line only, bounded
# ---------------------------------------------------------------------------


class TestShortError:
    def test_first_line_only_and_bounded(self):
        from nous.utils import short_error

        exc = RuntimeError("column x does not exist\n[SQL: SELECT secret]\n[parameters: ('q',)]")
        assert short_error(exc) == "RuntimeError: column x does not exist"
        assert len(short_error(ValueError("y" * 1000))) == 200

    def test_empty_message_gives_the_class(self):
        from nous.utils import short_error

        assert short_error(TimeoutError()) == "TimeoutError"


# ---------------------------------------------------------------------------
# E-5: the Session Profile leg was absent from the context trace
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
class TestSessionProfileLegTraced:
    @pytest.mark.asyncio
    async def test_served_facts_are_traced_as_rendered_and_kept_out_of_recalled_ids(
        self,
        db,
        mock_embeddings,
        settings,
    ):
        """The leg had no trace row, so a fact it put in the prompt was invisible
        to the trace. It is now registered and marked rendered, and still kept
        out of recalled_ids (procedure seeds, usage rows, next turn's working
        memory), which the leg never fed."""
        from unittest.mock import patch

        from nous.observability.retrieval_logger import RetrievalLogger

        engine, heart, s = await _fresh_engine(
            db,
            mock_embeddings,
            settings,
            identity_prompt="",
            # Core mode with one core-tagged fact: User Profile shows only that
            # one, so the untagged domain fact is left for the Session Profile
            # leg (as in test_tiered_context's TestSessionProfileLeg).
            settings_update={
                "profile_intent_leg_enabled": True,
                "profile_core_enabled": True,
                "profile_core_probation_days": 0,
            },
        )
        try:
            async with db.session() as session:
                core = await heart.learn(
                    FactInput(
                        content="Tim prefers Celsius wkrbcoretoken for temperatures",
                        category="preference",
                        subject="leg-served-core",
                    ),
                    session=session,
                )
                fact = await heart.learn(
                    FactInput(
                        content="Tim's trading rule qvmzplktoken trims losers early",
                        category="rule",
                        subject="leg-served",
                    ),
                    session=session,
                )
                await session.commit()
            await _tag_fact(db, core.id)
            tracing = RetrievalLogger(candidate_sample_rate=1.0)
            with patch("nous.cognitive.context.get_active_retrieval_logger", return_value=tracing):
                result = await engine.build(
                    agent_id=s.agent_id,
                    session_id="s-leg-served",
                    input_text="what about qvmzplktoken",
                    frame=_frame(),
                )
        finally:
            await heart.close()

        sp = _session_profile_section(result)
        assert sp is not None and "qvmzplktoken" in sp.content
        trace = result.retrieval_trace.to_dict()
        assert any(leg["name"] == "context_profile_intent" for leg in trace["legs"])
        traced = next(c for c in trace["candidates"] if c["id"] == str(fact.id))
        assert traced["disposition"] == "rendered"
        assert str(fact.id) not in result.recalled_ids["fact"]


# ---------------------------------------------------------------------------
# #827: the model sees a one-line note when a leg failed
# ---------------------------------------------------------------------------


class TestRecallDeepFailureNote:
    @pytest.mark.asyncio
    async def test_brain_query_failure_ends_the_tool_text_with_a_note(self):
        """The trace goes to retrieval_log, which the agent never reads, so a
        partly failed recall read as a complete one with fewer hits."""
        from nous.api.tools import create_nous_tools

        heart = _make_heart(recall_results=_make_recall_results())
        brain = _make_brain(neighbors_by_node={}, contradictions=[], decision_results=[])
        brain.query = AsyncMock(side_effect=RuntimeError("connection reset"))
        tools = create_nous_tools(brain=brain, heart=heart, settings=_make_settings(graph_recall_enabled=False))

        result = await tools["recall_deep"](query="anything", limit=5)

        text = result["content"][0]["text"]
        assert "fact summary one" in text, "the heart results are still served"
        assert text.endswith("\n\nNote: decision search failed (RuntimeError); results may be incomplete.")
        assert text.count("Note:") == 1

    @pytest.mark.asyncio
    async def test_no_errors_no_note(self):
        from nous.api.tools import create_nous_tools

        heart = _make_heart(recall_results=_make_recall_results())
        brain = _make_brain(neighbors_by_node={}, contradictions=[], decision_results=[])
        tools = create_nous_tools(brain=brain, heart=heart, settings=_make_settings(graph_recall_enabled=False))

        result = await tools["recall_deep"](query="anything", limit=5)

        assert "Note:" not in result["content"][0]["text"]

    def test_wording_for_failures_and_keyword_only_fallbacks(self):
        from nous.api.retrieval_pipeline import PipelineStats, failure_note

        stats = PipelineStats(
            n_stage_errors={"fact_embed_fallback": 1, "heart_recall_episode": 2, "decision_neighbors": 1},
            stage_error_msgs={"heart_recall_episode": "ProgrammingError: column x does not exist"},
        )
        assert failure_note(stats) == (
            "Note: episode search failed (ProgrammingError); graph expansion failed; "
            "fact search used keyword-only (embedding unavailable); results may be incomplete."
        )

    def test_a_failed_search_is_not_also_reported_as_keyword_only(self):
        from nous.api.retrieval_pipeline import PipelineStats, failure_note

        stats = PipelineStats(
            n_stage_errors={"fact_embed_fallback": 1, "heart_recall_fact": 1},
            stage_error_msgs={"heart_recall_fact": "OperationalError: server closed the connection"},
        )
        assert failure_note(stats) == "Note: fact search failed (OperationalError); results may be incomplete."

    def test_dedup_counts_and_harmless_steps_give_no_note(self):
        from nous.api.retrieval_pipeline import _NOTE_EXEMPT_STAGE_ERRORS, PipelineStats, failure_note

        assert failure_note(PipelineStats()) is None
        harmless = dict.fromkeys(_NOTE_EXEMPT_STAGE_ERRORS, 1)
        harmless["heart_graph_memory_duplicates"] = 9
        assert failure_note(PipelineStats(n_stage_errors=harmless)) is None


class TestPreTurnFailureNote:
    @pytest.mark.asyncio
    async def test_a_failed_context_leg_adds_one_dynamic_note(self):
        from test_preturn_fact_injection import _frame as _pt_frame
        from test_preturn_fact_injection import _make_engine, _stub_heart_for_build

        engine = _make_engine()
        _stub_heart_for_build(engine, [])
        engine._brain.query.side_effect = RuntimeError("connection reset")

        result = await engine.build(agent_id="a", session_id="s", input_text="what did we decide?", frame=_pt_frame())

        note = next(s for s in result.sections if s.label == "Retrieval Failures")
        assert note.content == "Note: decision recall failed (RuntimeError); results may be incomplete."
        assert note.tier == "dynamic"
        assert result.system_prompt.endswith("## Retrieval Failures\n\n" + note.content)
        assert "Retrieval Failures" not in result.sections_by_tier.get("static", "")
        assert "Retrieval Failures" not in result.sections_by_tier.get("semi_stable", "")

    @pytest.mark.asyncio
    async def test_no_failed_leg_no_note(self):
        from test_preturn_fact_injection import _frame as _pt_frame
        from test_preturn_fact_injection import _make_engine, _stub_heart_for_build

        engine = _make_engine()
        _stub_heart_for_build(engine, [])

        result = await engine.build(agent_id="a", session_id="s", input_text="what did we decide?", frame=_pt_frame())

        assert all(s.label != "Retrieval Failures" for s in result.sections)
        assert "Note:" not in result.system_prompt


# ---------------------------------------------------------------------------
# #826: a fail_fast skip (EmbeddingUnavailableError) is an embed fallback too
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
class TestEmbeddingUnavailableIsAFallback:
    @pytest.mark.asyncio
    async def test_fact_leg_counts_it_and_the_note_says_keyword_only(self, heart, session, monkeypatch):
        from nous.api.retrieval_pipeline import PipelineStats, failure_note
        from nous.brain.embeddings import EmbeddingUnavailableError

        monkeypatch.setattr(
            heart.facts.embeddings,
            "embed",
            AsyncMock(side_effect=EmbeddingUnavailableError("embeddings API failed recently; skipped on a read path")),
        )
        stage_errors: dict[str, int] = {}
        msgs: dict[str, str] = {}

        await heart.recall(
            "anything at all", types=["fact"], session=session, stage_errors=stage_errors, stage_error_msgs=msgs
        )

        assert stage_errors == {"fact_embed_fallback": 1}
        assert msgs["fact_embed_fallback"].startswith("EmbeddingUnavailableError: ")
        note = failure_note(PipelineStats(n_stage_errors=stage_errors, stage_error_msgs=msgs))
        assert note == "Note: fact search used keyword-only (embedding unavailable); results may be incomplete."

    @pytest.mark.asyncio
    async def test_chunk_hybrid_leg_counts_it(self, heart, monkeypatch):
        """#826's keyword-only fallback on the chunk leg records the same key shape."""
        from nous.api.retrieval_pipeline import _search_episode_chunks
        from nous.brain.embeddings import EmbeddingUnavailableError

        failing = SimpleNamespace(embed=AsyncMock(side_effect=EmbeddingUnavailableError("skipped on a read path")))
        monkeypatch.setattr(heart, "_embeddings", failing)
        stage_errors: dict[str, int] = {}
        msgs: dict[str, str] = {}

        await _search_episode_chunks(
            heart=heart,
            query="anything at all",
            agent_id=heart.agent_id,
            limit=5,
            settings=SimpleNamespace(chunk_hybrid_search_enabled=True, chunk_rrf_penalty_limit=None),
            stage_errors=stage_errors,
            stage_error_msgs=msgs,
        )

        assert stage_errors == {"chunk_embed_fallback": 1}
        assert msgs["chunk_embed_fallback"].startswith("EmbeddingUnavailableError")


# ---------------------------------------------------------------------------
# Codex P2 (#827): a failed base embed is a fallback only when the search that
# runs has no vector. The variant fusion carries its own vectors.
# ---------------------------------------------------------------------------


async def _recall_with_failed_base_embed(heart, session, mock_embeddings, monkeypatch, *, variants):
    """heart.recall over episodes/facts/procedures with the per-manager query
    embed failing and query expansion returning ``variants`` (embedded fine)."""
    from nous.brain.embeddings import EmbeddingUnavailableError

    query = "lighthouse keeper rota for the winter"
    vectors = [await mock_embeddings.embed(v) for v in variants]
    monkeypatch.setattr(heart.settings, "query_expansion_enabled", True)
    monkeypatch.setattr(heart, "_query_expander", SimpleNamespace(expand=AsyncMock(return_value=variants)))
    monkeypatch.setattr(mock_embeddings, "embed_batch", AsyncMock(return_value=vectors))
    monkeypatch.setattr(
        mock_embeddings, "embed", AsyncMock(side_effect=EmbeddingUnavailableError("skipped on a read path"))
    )
    stage_errors: dict[str, int] = {}
    msgs: dict[str, str] = {}
    await heart.recall(
        query,
        types=["episode", "fact", "procedure"],
        session=session,
        stage_errors=stage_errors,
        stage_error_msgs=msgs,
    )
    return stage_errors, msgs


@pytest.mark.postgres_only
class TestFallbackOnlyWithoutVectorInput:
    @pytest.mark.asyncio
    async def test_variants_embedded_means_no_fallback_and_no_note(self, heart, session, mock_embeddings, monkeypatch):
        from nous.api.retrieval_pipeline import PipelineStats, failure_note

        stage_errors, msgs = await _recall_with_failed_base_embed(
            heart,
            session,
            mock_embeddings,
            monkeypatch,
            variants=["lighthouse keeper rota for the winter", "winter schedule for the lighthouse keepers"],
        )

        assert stage_errors == {}, "the variant fusion ran with vectors: not keyword-only"
        assert failure_note(PipelineStats(n_stage_errors=stage_errors, stage_error_msgs=msgs)) is None

    @pytest.mark.asyncio
    async def test_no_variants_means_the_fallback_is_counted(self, heart, session, mock_embeddings, monkeypatch):
        from nous.api.retrieval_pipeline import PipelineStats, failure_note

        stage_errors, msgs = await _recall_with_failed_base_embed(
            heart,
            session,
            mock_embeddings,
            monkeypatch,
            variants=["lighthouse keeper rota for the winter"],
        )

        assert stage_errors == {
            "episode_embed_fallback": 1,
            "fact_embed_fallback": 1,
            "procedure_embed_fallback": 1,
        }
        assert failure_note(PipelineStats(n_stage_errors=stage_errors, stage_error_msgs=msgs)) == (
            "Note: episode search used keyword-only (embedding unavailable); "
            "fact search used keyword-only (embedding unavailable); "
            "procedure search used keyword-only (embedding unavailable); results may be incomplete."
        )


class TestHasVectorInput:
    def test_single_path_reads_the_base_embedding(self):
        from nous.heart.search import has_vector_input

        assert has_vector_input(False, [0.1], [("a", None), ("b", None)]) is True
        assert has_vector_input(False, None, [("a", [0.1]), ("b", [0.2])]) is False

    def test_variant_path_reads_only_the_variants(self):
        from nous.heart.search import has_vector_input

        assert has_vector_input(True, None, [("a", [0.1]), ("b", None)]) is True
        assert has_vector_input(True, [0.1], [("a", None), ("b", [])]) is False
