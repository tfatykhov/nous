"""Unit tests for nous.eval.retrieval_runner (F051 Phase 1).

Strategy: patch ``nous.eval.retrieval_runner.run_recall_pipeline`` (not Heart
directly — the runner calls the pipeline, not Heart). A FakePipeline callable
stands in for ``run_recall_pipeline`` so tests execute without a DB.

Covered silent-failure paths (per plan):
- Pipeline raises on a specific qrel -> QrelResult.error is captured; MRR not
  forced to zero for the rest of the run.
- RuntimeConfig is reset between configs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

pytestmark = pytest.mark.eval

try:
    from nous.api.retrieval_pipeline import PipelineResult, PipelineStats
    from nous.eval.qrels_loader import Qrel
    from nous.eval.retrieval_runner import (
        QrelResult,
        RetrievalConfig,
        RunResult,
        run_matrix,
    )
except ImportError:
    pytest.skip(
        "nous.eval.retrieval_runner (+deps) not yet available",
        allow_module_level=True,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mk_qrel(query: str, gold_ids: list, source: str = "probes") -> Qrel:
    # Qrel requires at least one gold_id (min_length=1)
    if not gold_ids:
        gold_ids = [uuid4()]
    return Qrel(
        query=query,
        gold_ids=gold_ids,
        source=source,  # pydantic coerces str -> QrelSource enum
        confidence="high",
    )


def _pr(uid, type_: str = "fact", score: float = 0.8) -> PipelineResult:
    return PipelineResult(
        id=uid, type=type_, description="desc", score=score, source="heart"
    )


async def _fake_pipeline_hit(query, heart, brain, settings, limit, memory_types):
    """Return a hit at rank 1 for any query."""
    uid = uuid4()
    return [_pr(uid)], PipelineStats()


async def _fake_pipeline_miss(query, heart, brain, settings, limit, memory_types):
    return [], PipelineStats()


async def _fake_pipeline_raises(query, heart, brain, settings, limit, memory_types):
    raise RuntimeError("synthetic failure from fake pipeline")


# ---------------------------------------------------------------------------
# Direct _run_one (if exposed) or exercise via run_matrix-level mocks
# ---------------------------------------------------------------------------


class _StubEvalSettings:
    agent_id = "nous-eval-corpus"
    db_host = "127.0.0.1"
    db_port = 5999  # unreachable — run_matrix should be fully mocked anyway
    db_user = "x"
    db_password = "x"
    db_name = "x"
    db_pool_size = 1
    db_max_overflow = 0

    @property
    def db_url(self) -> str:
        return f"postgresql+asyncpg://{self.db_user}:{self.db_password}@{self.db_host}:{self.db_port}/{self.db_name}"


class _StubSettings:
    """Minimal Settings stand-in supporting ``model_copy(update=...)``."""

    def __init__(self, **kw) -> None:
        self.__dict__.update(kw)

    def model_copy(self, update: dict[str, Any] | None = None) -> "_StubSettings":
        new = _StubSettings(**self.__dict__)
        if update:
            new.__dict__.update(update)
        return new


# ---------------------------------------------------------------------------
# run_matrix with an injected-pipeline strategy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_matrix_builds_per_config_run_result() -> None:
    """run_matrix runs each config once; output has one RunResult per config."""
    cfgs = [
        RetrievalConfig(name="baseline", flags={}),
        RetrievalConfig(name="ce_off", flags={"cross_encoder_enabled": False}),
    ]
    qrels = [_mk_qrel("q1", [uuid4()]), _mk_qrel("q2", [uuid4()])]

    with (
        patch(
            "nous.eval.retrieval_runner.run_recall_pipeline",
            side_effect=_fake_pipeline_hit,
        ),
        patch(
            "nous.eval.retrieval_runner._build_heart_for_eval"
        ) as mock_build_heart,
        patch(
            "nous.eval.retrieval_runner._build_brain_for_eval"
        ) as mock_build_brain,
        patch("nous.eval.retrieval_runner.Database") as mock_db_cls,
    ):
        # Heart async context manager
        mock_heart = AsyncMock()
        mock_heart.embeddings = None
        mock_build_heart.return_value.__aenter__.return_value = mock_heart
        mock_build_heart.return_value.__aexit__.return_value = None

        mock_build_brain.return_value = AsyncMock()

        mock_db = AsyncMock()
        mock_db.engine.dispose = AsyncMock()
        mock_db_cls.return_value = mock_db

        results = await run_matrix(
            configs=cfgs,
            qrels=qrels,
            eval_settings=_StubEvalSettings(),
            main_settings_template=_StubSettings(),
            top_k=10,
        )

    assert len(results) == 2
    assert results[0].config.name == "baseline"
    assert results[1].config.name == "ce_off"
    for rr in results:
        assert len(rr.per_qrel) == 2


@pytest.mark.asyncio
async def test_qrel_exception_captured_not_zero_scored() -> None:
    """If the pipeline raises on a qrel, the error is captured in QrelResult.error
    — the runner does NOT silently return zeros."""
    cfgs = [RetrievalConfig(name="baseline", flags={})]
    qrels = [_mk_qrel("boom", [uuid4()])]

    with (
        patch(
            "nous.eval.retrieval_runner.run_recall_pipeline",
            side_effect=_fake_pipeline_raises,
        ),
        patch(
            "nous.eval.retrieval_runner._build_heart_for_eval"
        ) as mock_build_heart,
        patch(
            "nous.eval.retrieval_runner._build_brain_for_eval"
        ) as mock_build_brain,
        patch("nous.eval.retrieval_runner.Database") as mock_db_cls,
    ):
        mock_heart = AsyncMock()
        mock_heart.embeddings = None
        mock_build_heart.return_value.__aenter__.return_value = mock_heart
        mock_build_heart.return_value.__aexit__.return_value = None
        mock_build_brain.return_value = AsyncMock()
        mock_db = AsyncMock()
        mock_db.engine.dispose = AsyncMock()
        mock_db_cls.return_value = mock_db

        results = await run_matrix(
            configs=cfgs,
            qrels=qrels,
            eval_settings=_StubEvalSettings(),
            main_settings_template=_StubSettings(),
            top_k=10,
        )

    assert len(results) == 1
    q = results[0].per_qrel[0]
    assert q.error is not None
    assert "synthetic failure" in q.error or "RuntimeError" in q.error


@pytest.mark.asyncio
async def test_runtime_config_reset_between_configs() -> None:
    """Per plan §Silent-failure-coverage: RuntimeConfig.reset() MUST be called
    before each config so leftover overrides from a previous config don't leak."""
    cfgs = [
        RetrievalConfig(name="baseline", flags={}),
        RetrievalConfig(name="ce_off", flags={"cross_encoder_enabled": False}),
    ]
    qrels = [_mk_qrel("q", [uuid4()])]

    with (
        patch(
            "nous.eval.retrieval_runner.run_recall_pipeline",
            side_effect=_fake_pipeline_hit,
        ),
        patch(
            "nous.eval.retrieval_runner._build_heart_for_eval"
        ) as mock_build_heart,
        patch(
            "nous.eval.retrieval_runner._build_brain_for_eval"
        ) as mock_build_brain,
        patch("nous.eval.retrieval_runner.Database") as mock_db_cls,
        patch(
            "nous.eval.retrieval_runner.RuntimeConfig"
        ) as mock_runtime_config,
    ):
        mock_heart = AsyncMock()
        mock_heart.embeddings = None
        mock_build_heart.return_value.__aenter__.return_value = mock_heart
        mock_build_heart.return_value.__aexit__.return_value = None
        mock_build_brain.return_value = AsyncMock()
        mock_db = AsyncMock()
        mock_db.engine.dispose = AsyncMock()
        mock_db_cls.return_value = mock_db

        await run_matrix(
            configs=cfgs,
            qrels=qrels,
            eval_settings=_StubEvalSettings(),
            main_settings_template=_StubSettings(),
            top_k=10,
        )

        # reset() must be called at least once per config (2 configs -> 2+ calls)
        assert mock_runtime_config.reset.call_count >= 2


@pytest.mark.asyncio
async def test_score_rank_first_gold_match() -> None:
    """Sanity check: when retrieved order hits gold at rank 2, rank_of_first_gold=2."""
    gold = uuid4()
    other = uuid4()

    async def _pipeline(query, heart, brain, settings, limit, memory_types):
        return [_pr(other), _pr(gold), _pr(uuid4())], PipelineStats()

    cfgs = [RetrievalConfig(name="baseline", flags={})]
    qrels = [_mk_qrel("q", [gold])]

    with (
        patch(
            "nous.eval.retrieval_runner.run_recall_pipeline", side_effect=_pipeline
        ),
        patch(
            "nous.eval.retrieval_runner._build_heart_for_eval"
        ) as mock_build_heart,
        patch(
            "nous.eval.retrieval_runner._build_brain_for_eval"
        ) as mock_build_brain,
        patch("nous.eval.retrieval_runner.Database") as mock_db_cls,
    ):
        mock_heart = AsyncMock()
        mock_heart.embeddings = None
        mock_build_heart.return_value.__aenter__.return_value = mock_heart
        mock_build_heart.return_value.__aexit__.return_value = None
        mock_build_brain.return_value = AsyncMock()
        mock_db = AsyncMock()
        mock_db.engine.dispose = AsyncMock()
        mock_db_cls.return_value = mock_db

        results = await run_matrix(
            configs=cfgs,
            qrels=qrels,
            eval_settings=_StubEvalSettings(),
            main_settings_template=_StubSettings(),
            top_k=10,
        )

    q = results[0].per_qrel[0]
    assert q.rank_of_first_gold == 2
    assert q.n_gold_in_top_k == 1
    assert q.error is None
