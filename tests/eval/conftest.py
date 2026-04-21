"""Shared fixtures and markers for tests/eval/ (F051 Phase 1).

- Registers the ``eval`` marker via ``pytest_configure``.
- Provides ``mock_fixtures_dir`` — a tmpdir pre-populated with minimal JSONL
  files for smoke-mode tests of the source registry.
- Provides ``socket_preflight`` — a helper that returns True if the eval-db
  container is listening on ``127.0.0.1:5433``.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path
from uuid import UUID, uuid5, NAMESPACE_URL

import pytest


def pytest_configure(config: pytest.Config) -> None:
    """Register the `eval` marker so `-m eval` works and warnings stay clean."""
    config.addinivalue_line(
        "markers",
        "eval: F051 retrieval-eval-harness unit tests (no DB required)",
    )


# ---------------------------------------------------------------------------
# Socket preflight
# ---------------------------------------------------------------------------


def _eval_db_listening(host: str = "127.0.0.1", port: int = 5433, timeout: float = 0.5) -> bool:
    """Return True if something is listening on host:port.

    Used by integration tests to skip cleanly when the eval-db container is
    not running on the developer machine.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        return sock.connect_ex((host, port)) == 0
    finally:
        sock.close()


@pytest.fixture
def socket_preflight():
    """Expose `_eval_db_listening` to tests that want to skip on container-down."""
    return _eval_db_listening


# ---------------------------------------------------------------------------
# mock_fixtures_dir
# ---------------------------------------------------------------------------


def _smoke_uuid(i: int) -> UUID:
    """Match the UUID generation used by the smoke corpus fixture."""
    return uuid5(NAMESPACE_URL, f"nous-eval-smoke-{i}")


@pytest.fixture
def mock_fixtures_dir(tmp_path: Path) -> Path:
    """Populate a tmpdir with minimal qrels JSONL files for smoke tests.

    Creates three deterministic qrels files under ``tmp_path``:

    - ``qrels_longmemeval.jsonl``  (2 rows, gate_eligible)
    - ``qrels_ai_hand.jsonl``      (2 rows, not reviewed)
    - ``qrels_silver.jsonl``       (1 row)

    The gold IDs match UUIDs emitted by ``_smoke_uuid``.
    """
    # longmemeval
    lme = tmp_path / "qrels_longmemeval.jsonl"
    lme.write_text(
        "\n".join(
            json.dumps(
                {
                    "query": f"lme query {i}",
                    "gold_ids": [str(_smoke_uuid(i))],
                    "source": "longmemeval",
                    "confidence": "high",
                    "reasoning_type": "specific_lookup",
                }
            )
            for i in range(2)
        ),
        encoding="utf-8",
    )
    # ai_hand_labeled
    aih = tmp_path / "qrels_ai_hand.jsonl"
    aih.write_text(
        "\n".join(
            json.dumps(
                {
                    "query": f"ai query {i}",
                    "gold_ids": [str(_smoke_uuid(i))],
                    "source": "ai_hand_labeled",
                    "confidence": "medium",
                    "reviewed_by": None,
                }
            )
            for i in range(2)
        ),
        encoding="utf-8",
    )
    # silver
    silver = tmp_path / "qrels_silver.jsonl"
    silver.write_text(
        json.dumps(
            {
                "query": "silver query",
                "gold_ids": [str(_smoke_uuid(0))],
                "source": "silver_episodes",
                "confidence": "low",
            }
        ),
        encoding="utf-8",
    )
    return tmp_path
