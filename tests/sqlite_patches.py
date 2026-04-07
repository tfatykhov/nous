"""SQLite compatibility patches for Postgres-specific operations in tests.

This module provides monkeypatching utilities to replace Postgres-only
behaviors with SQLite-compatible equivalents during offline test runs.

Usage in tests::

    from tests.sqlite_patches import patch_vector_search, patch_hybrid_search

    @pytest.fixture(autouse=True)
    def sqlite_compat(monkeypatch):
        patch_vector_search(monkeypatch)
        patch_hybrid_search(monkeypatch)

These patches are **only needed for NEEDS_MOCK tests** that use mock
fixtures but call into subsystems that issue PG-specific SQL. Tests
marked ``@pytest.mark.integration`` or ``@pytest.mark.postgres_only``
are skipped in offline mode and do not need these patches.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock


# ---------------------------------------------------------------------------
# Vector / embedding search patches
# ---------------------------------------------------------------------------


def patch_vector_search(monkeypatch) -> None:
    """Replace pgvector-based similarity searches with empty result stubs.

    The real implementation issues ``<->`` cosine-distance queries that
    SQLite cannot parse. Replacing the function with a stub means tests
    can verify the calling code path without exercising the SQL layer.
    """
    try:
        import nous.heart.search as search_mod

        monkeypatch.setattr(search_mod, "hybrid_search", AsyncMock(return_value=[]))
        monkeypatch.setattr(search_mod, "vector_search", AsyncMock(return_value=[]))
        monkeypatch.setattr(search_mod, "keyword_search", AsyncMock(return_value=[]))
    except (ImportError, AttributeError):
        pass


def patch_spreading_activation(monkeypatch) -> None:
    """Replace graph spreading activation (requires pgvector) with a no-op."""
    try:
        import nous.brain.spreading_activation as sa_mod

        monkeypatch.setattr(sa_mod, "spread", AsyncMock(return_value=[]))
    except (ImportError, AttributeError):
        pass


def patch_graph_linker(monkeypatch) -> None:
    """Replace cross-type graph linker (uses pgvector cosine) with a no-op."""
    try:
        import nous.brain.graph_linker as gl_mod

        monkeypatch.setattr(gl_mod, "GraphLinker.link", AsyncMock(return_value=None))
    except (ImportError, AttributeError):
        pass


# ---------------------------------------------------------------------------
# Embedding provider patch
# ---------------------------------------------------------------------------


def patch_embeddings(monkeypatch) -> None:
    """Replace the OpenAI embedding provider with a deterministic mock.

    Useful when a subsystem instantiates its own provider rather than
    accepting one via dependency injection.
    """
    import hashlib
    import random

    class _MockProvider:
        async def embed(self, text: str) -> list[float]:
            h = hashlib.sha256(text.encode()).hexdigest()
            rng = random.Random(h)
            vec = [rng.gauss(0, 1) for _ in range(1536)]
            norm = sum(x * x for x in vec) ** 0.5
            return [x / norm for x in vec]

        async def embed_batch(self, texts: list[str]) -> list[list[float]]:
            return [await self.embed(t) for t in texts]

        async def close(self) -> None:
            pass

    try:
        import nous.brain.embeddings as emb_mod

        monkeypatch.setattr(emb_mod, "EmbeddingProvider", _MockProvider)
    except (ImportError, AttributeError):
        pass


# ---------------------------------------------------------------------------
# Full-text search patch
# ---------------------------------------------------------------------------


def patch_fulltext_search(monkeypatch) -> None:
    """Replace tsvector/pg_trgm full-text search with a Python ``in`` check stub."""
    try:
        import nous.heart.search as search_mod

        async def _python_keyword_search(session, agent_id, query, limit=10, **kwargs):
            return []

        monkeypatch.setattr(search_mod, "keyword_search", _python_keyword_search)
    except (ImportError, AttributeError):
        pass


# ---------------------------------------------------------------------------
# Convenience: apply all patches at once
# ---------------------------------------------------------------------------


def apply_all_sqlite_patches(monkeypatch) -> None:
    """Apply every SQLite compatibility patch in one call.

    Call from a fixture::

        @pytest.fixture(autouse=True)
        def sqlite_compat(monkeypatch):
            from tests.sqlite_patches import apply_all_sqlite_patches
            apply_all_sqlite_patches(monkeypatch)
    """
    patch_vector_search(monkeypatch)
    patch_spreading_activation(monkeypatch)
    patch_graph_linker(monkeypatch)
    patch_fulltext_search(monkeypatch)
