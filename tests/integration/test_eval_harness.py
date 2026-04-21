"""Integration smoke test for the F051 retrieval-eval harness (Phase 1).

Strategy:
- Socket-preflight on 127.0.0.1:5433. If nothing is listening, ``pytest.skip``
  with a clear hint pointing at ``docker compose --profile eval up -d``.
- When the container is up, run a tiny end-to-end pass through the harness:
  load smoke qrels, instantiate Heart+Brain against the eval DB, run one
  config, score, write a report.

Marked ``@pytest.mark.integration`` so default ``pytest`` runs skip it. Run via
``uv run pytest tests/integration/test_eval_harness.py --integration``.
"""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.eval]


# ---------------------------------------------------------------------------
# Socket preflight
# ---------------------------------------------------------------------------


EVAL_DB_HOST = "127.0.0.1"
EVAL_DB_PORT = 5433


def _eval_db_listening() -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.5)
    try:
        return sock.connect_ex((EVAL_DB_HOST, EVAL_DB_PORT)) == 0
    finally:
        sock.close()


def _skip_if_eval_db_down() -> None:
    if not _eval_db_listening():
        pytest.skip(
            f"nous-eval-db not reachable on {EVAL_DB_HOST}:{EVAL_DB_PORT}. "
            "Run `docker compose --profile eval up -d nous-eval-db` to enable."
        )


# ---------------------------------------------------------------------------
# Smoke fixtures path resolution
# ---------------------------------------------------------------------------


SMOKE_QRELS = Path(__file__).parent.parent / "fixtures" / "eval_smoke.jsonl"
SMOKE_PROBES = Path(__file__).parent.parent / "fixtures" / "eval_probes.jsonl"
SMOKE_CORPUS = Path(__file__).parent.parent / "fixtures" / "eval_smoke_corpus.jsonl"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_eval_harness_skips_cleanly_when_db_down() -> None:
    """Socket preflight must skip cleanly with a useful message — never crash."""
    if _eval_db_listening():
        pytest.skip("eval-db is up; this test verifies skip-on-down only")
    # If we get here, the skip already happened from the helper. Confirm via
    # an explicit call that the helper's behavior is a clean skip:
    with pytest.raises(pytest.skip.Exception):
        _skip_if_eval_db_down()


@pytest.mark.asyncio
async def test_smoke_corpus_fixture_present() -> None:
    """The committed smoke corpus must exist on disk; integration depends on it."""
    assert SMOKE_CORPUS.exists(), f"missing smoke corpus at {SMOKE_CORPUS}"
    # Should be a non-empty JSONL with at least 10 items
    lines = [
        line for line in SMOKE_CORPUS.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert len(lines) >= 10


@pytest.mark.asyncio
async def test_smoke_qrels_fixture_present() -> None:
    assert SMOKE_QRELS.exists(), f"missing smoke qrels at {SMOKE_QRELS}"
    lines = [
        line for line in SMOKE_QRELS.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert len(lines) >= 5


@pytest.mark.asyncio
async def test_eval_probes_fixture_present() -> None:
    assert SMOKE_PROBES.exists(), f"missing probes at {SMOKE_PROBES}"
    lines = [
        line for line in SMOKE_PROBES.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert len(lines) >= 10


@pytest.mark.asyncio
async def test_qrels_loader_reads_smoke_fixture() -> None:
    """Sanity check: the qrels_loader can parse the committed smoke fixture."""
    try:
        from nous.eval.qrels_loader import load_qrels
    except ImportError:
        pytest.skip("nous.eval.qrels_loader not yet available")
    qrels = load_qrels(SMOKE_QRELS)
    assert len(qrels) >= 5
    for q in qrels:
        assert q.query
        assert q.source


@pytest.mark.asyncio
async def test_end_to_end_smoke_run_when_db_up(tmp_path: Path) -> None:
    """End-to-end smoke: load smoke qrels, run one config, score, write report.

    Skipped unless the eval-db container is up.
    """
    _skip_if_eval_db_down()

    try:
        from nous.eval.config import EvalSettings
        from nous.eval.qrels_loader import load_qrels
        from nous.eval.report import render_json, render_markdown, write_reports
        from nous.eval.retrieval_runner import RetrievalConfig, run_matrix
    except ImportError:
        pytest.skip("nous.eval.* not yet available")

    from nous.config import Settings

    # eval settings -> point at the running container
    eval_settings = EvalSettings(
        db_host=EVAL_DB_HOST,
        db_port=EVAL_DB_PORT,
        agent_id=os.environ.get("NOUS_EVAL_AGENT_ID", "nous-eval-smoke"),
    )
    main_settings = Settings()

    qrels = load_qrels(SMOKE_QRELS)
    if not qrels:
        pytest.skip("smoke qrels empty")

    cfgs = [RetrievalConfig(name="baseline", flags={}, description="defaults")]

    run_results = await run_matrix(
        configs=cfgs,
        qrels=qrels[:5],  # tiny subset — we want fast smoke
        eval_settings=eval_settings,
        main_settings_template=main_settings,
        top_k=10,
    )
    assert len(run_results) == 1

    # report writes; we don't assert on metric values (corpus may not be ingested)
    md = render_markdown(
        run_results=run_results,
        resolved_sources=[],
        git_sha="smoke",
        fixture_version=eval_settings.fixture_version,
    )
    js = render_json(
        run_results=run_results,
        resolved_sources=[],
        git_sha="smoke",
        fixture_version=eval_settings.fixture_version,
    )
    md_path, json_path = write_reports(
        report_dir=tmp_path,
        md_content=md,
        json_content=js,
        config_names=[r.config.name for r in run_results],
    )
    assert md_path.exists() and json_path.exists()
