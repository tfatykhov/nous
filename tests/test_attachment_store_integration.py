"""F024 Task H: DB-backed integration tests for attachment_store.

Exercises the persistence/recall path of nous/api/attachment_store.py against
a REAL Heart + Postgres (the `heart` fixture wires a MockEmbeddingProvider, so
no live OpenAI key is needed). Calls the store functions DIRECTLY rather than
through a full runner turn — the runner path is covered by the stubbed tests in
tests/test_attachments_integration.py.

Requires NOUS_TEST_DB=postgres (the eval scratch container is fine).
"""
from __future__ import annotations

import base64
import uuid

import pytest
from sqlalchemy import text

from nous.api import attachment_store
from nous.api.models import Attachment

pytestmark = [pytest.mark.postgres_only, pytest.mark.asyncio]


async def _insert_active_episode(db, agent_id: str, session_id: str) -> uuid.UUID:
    """Insert one active episode for the given agent + session."""
    episode_id = uuid.uuid4()
    async with db.session() as session:
        await session.execute(
            text(
                "INSERT INTO heart.episodes "
                "(id, agent_id, session_id, summary, started_at, active) "
                "VALUES (:i, :a, :s, 'attachment test ep', NOW(), true)"
            ),
            {"i": episode_id, "a": agent_id, "s": session_id},
        )
        await session.commit()
    return episode_id


async def _cleanup(db, agent_id: str) -> None:
    async with db.session() as session:
        await session.execute(
            text("DELETE FROM heart.episode_chunks WHERE agent_id = :a"),
            {"a": agent_id},
        )
        await session.execute(
            text("DELETE FROM heart.facts WHERE agent_id = :a"),
            {"a": agent_id},
        )
        await session.execute(
            text("DELETE FROM heart.episodes WHERE agent_id = :a"),
            {"a": agent_id},
        )
        await session.commit()


async def test_record_attachment_fact_is_recallable(db, heart):
    """record_attachment_fact stores a Heart fact whose filename + analysis
    are discoverable via heart.recall()."""
    agent_id = heart.agent_id
    # Unique filename so a prior run's leftover row can't satisfy the assertion.
    filename = f"login-error-{uuid.uuid4().hex[:8]}.png"
    att = Attachment(
        filename=filename,
        media_type="image/png",
        data_base64="",  # not needed for the fact path
        content_type="image",
        source="rest",
        workspace_path=f"/tmp/nous-workspace/attachments/sess/{filename}",
    )
    analysis = "A red login screen showing a 500 error"

    try:
        await attachment_store.record_attachment_fact(
            heart, att, agent_id=agent_id, source_episode_id=None, analysis=analysis,
        )

        hits = await heart.recall("login screen attachment", limit=5)

        assert hits, "recall returned no results for the attachment fact"
        assert any(
            ("login" in (r.summary or "").lower()) or (att.filename in (r.summary or ""))
            for r in hits
        ), f"no recall result referenced the filename or analysis: {[r.summary for r in hits]}"
    finally:
        await _cleanup(db, agent_id)


async def test_maybe_ingest_text_file_creates_document_chunk(db, heart):
    """maybe_ingest_text_file chunks a text/code body into heart.episode_chunks
    with source_kind='document' and source_ref == workspace_path."""
    agent_id = heart.agent_id
    session_id = f"attach-sess-{uuid.uuid4().hex[:8]}"

    # Settings: enable text-file ingest + a low chunk min so the body qualifies.
    settings = heart.settings.model_copy(
        update={
            "attachments_ingest_text_files": True,
            "document_ingest_enabled": True,
            "document_chunk_min_chars": 50,
        }
    )

    episode_id = await _insert_active_episode(db, agent_id, session_id)

    # A sizeable code body, comfortably above min_chars.
    body = (
        "def handle_login(request):\n"
        "    # Validate the incoming credentials and issue a session token.\n"
        "    user = authenticate(request.username, request.password)\n"
        "    if user is None:\n"
        "        raise Unauthorized('bad credentials')\n"
        "    return issue_token(user)\n"
    ) * 8
    workspace_path = f"/tmp/nous-workspace/attachments/{session_id}/login_handler.py"
    att = Attachment(
        filename="login_handler.py",
        media_type="text/x-python",
        data_base64=base64.b64encode(body.encode("utf-8")).decode("ascii"),
        content_type="text_file",
        source="rest",
        workspace_path=workspace_path,
    )

    try:
        await attachment_store.maybe_ingest_text_file(
            heart, settings, att, session_id=session_id, episode_id=str(episode_id),
        )

        async with db.session() as session:
            rows = await session.execute(
                text(
                    "SELECT source_kind, source_ref, length(content) AS clen "
                    "FROM heart.episode_chunks "
                    "WHERE agent_id = :a AND episode_id = :e AND source_kind = 'document' "
                    "ORDER BY chunk_index"
                ),
                {"a": agent_id, "e": episode_id},
            )
            chunks = rows.fetchall()

        assert len(chunks) >= 1, "expected at least one document chunk"
        for c in chunks:
            assert c.source_kind == "document"
            assert c.source_ref == workspace_path
            assert c.clen > 0
    finally:
        await _cleanup(db, agent_id)


async def test_record_attachment_fact_never_raises_on_learn_failure():
    """A learn() failure must be swallowed (the turn must never break).

    No DB needed — a tiny stub Heart whose .learn raises is sufficient.
    """
    class _RaisingHeart:
        async def learn(self, *args, **kwargs):
            raise RuntimeError("simulated learn failure")

    att = Attachment(
        filename="boom.png",
        media_type="image/png",
        data_base64="",
        content_type="image",
        source="rest",
        workspace_path="/tmp/nous-workspace/attachments/sess/boom.png",
    )

    # Must NOT raise.
    await attachment_store.record_attachment_fact(
        _RaisingHeart(), att, agent_id="nous-default",
        source_episode_id=None, analysis="anything",
    )
