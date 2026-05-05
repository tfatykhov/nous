"""F060 — abandoned-episode recovery validation against eval DB.

Synthesizes a fixture of N stuck-open episodes (active=true,
structured_summary=NULL, populated transcript, started_at > min_age_hours
ago) under a unique agent_id, then runs F060 directly against the eval
DB. Verifies that:

  - The phase finds the synthesized episodes
  - `summarize_episode` populates `structured_summary` for each
  - Episodes stay `active=true` (matching F057 contract)

Cost: ~$0.50 (3 LLM summarization calls). Re-runnable — agent_id is
namespaced with a timestamp to avoid stale-fixture interference.

Usage:
    NOUS_EVAL_DB_NAME=nous_eval_scratch \
      uv run python scripts/eval/validate_f060.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import text as sql_text

from nous.brain.brain import Brain
from nous.brain.embeddings import EmbeddingProvider
from nous.config import Settings
from nous.handlers.episode_summarizer import EpisodeSummarizer
from nous.handlers.sleep_handler import SleepHandler
from nous.heart.heart import Heart
from nous.storage.database import Database
from nous_eval.config import EvalSettings
from nous_eval.retrieval_runner import _settings_for_eval_db


_AGENT_ID = f"f060-validation-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}"

_FIXTURE_TRANSCRIPTS = [
    """User: I want to set up a Postgres staging database. What port should I use?
Assistant: For staging, I'd recommend a non-default port like 5433 to avoid conflicts with local dev databases that typically run on 5432.
User: OK, let's use 5433. Also, the connection string should use SSL mode=require since we're going across networks.
Assistant: Got it — staging Postgres on port 5433 with sslmode=require. I'll record that.
User: Perfect. The credentials are in the AWS secrets manager under 'staging-db-master'.
Assistant: Recorded: secret name staging-db-master.""",
    """User: Reviewed the auth bug from yesterday. Sarah said it's a CSRF token issue.
Assistant: Checking the issue — the session middleware was returning before adding the token to response headers.
User: Yeah, fix is in PR #482. We're rolling out tomorrow at 9am UTC.
Assistant: Acknowledged — PR #482 fixes the CSRF token issue, deploying 2026-05-06 09:00 UTC.
User: One more thing — make sure the deployment log goes to #ops-deploys, not #ops-general.
Assistant: Got it: deploy notifications route to #ops-deploys.""",
    """User: Need to plan the Q3 capacity. We're forecasting 2x traffic by August.
Assistant: Current peak is around 800 RPS — 2x would be 1600 RPS. The bottleneck is likely the single-region Postgres setup.
User: Right. Let's discuss read replicas. Daniel from infra suggested 2 replicas in us-west-2.
Assistant: Recorded: read replica plan — 2 replicas in us-west-2 region, owner Daniel from infra.
User: We'll revisit cost in 2 weeks. Send me a budget estimate by 2026-05-19.
Assistant: Acknowledged: budget estimate for read replicas due 2026-05-19.""",
]


async def _seed_fixture(db: Database, agent_id: str) -> list[uuid.UUID]:
    """Insert N stuck-open episodes (active=true, NULL summary, with transcript)."""
    started_24h_ago = datetime.now(UTC) - timedelta(hours=25)
    ids: list[uuid.UUID] = []
    async with db.session() as session:
        for i, transcript in enumerate(_FIXTURE_TRANSCRIPTS):
            ep_id = uuid.uuid4()
            await session.execute(sql_text("""
                INSERT INTO heart.episodes (
                    id, agent_id, session_id, summary, transcript,
                    structured_summary, active, outcome, started_at, ended_at
                ) VALUES (
                    :id, :agent_id, :session_id, :summary, :transcript,
                    NULL, true, NULL, :started_at, NULL
                )
            """), {
                "id": ep_id,
                "agent_id": agent_id,
                "session_id": f"f060-fixture-{i}",
                "summary": f"Fixture episode {i+1} (placeholder)",
                "transcript": transcript,
                "started_at": started_24h_ago,
            })
            ids.append(ep_id)
        await session.commit()
    return ids


async def _count_summarized(db: Database, agent_id: str) -> int:
    async with db.session() as session:
        rows = await session.execute(sql_text("""
            SELECT COUNT(*) FROM heart.episodes
            WHERE agent_id = :agent_id AND structured_summary IS NOT NULL
        """), {"agent_id": agent_id})
        return rows.scalar() or 0


async def _cleanup(db: Database, agent_id: str) -> int:
    async with db.session() as session:
        result = await session.execute(sql_text(
            "DELETE FROM heart.episodes WHERE agent_id = :agent_id"
        ), {"agent_id": agent_id})
        await session.commit()
        return result.rowcount or 0


async def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    logging.basicConfig(level=logging.WARNING)

    eval_settings = EvalSettings()
    main_settings = Settings()
    settings = _settings_for_eval_db(eval_settings, main_settings).model_copy(
        update={"agent_id": _AGENT_ID}
    )

    if not (main_settings.anthropic_api_key or main_settings.anthropic_auth_token):
        print("ERROR: Anthropic creds required.", file=sys.stderr)
        return 2
    if not settings.openai_api_key:
        print("ERROR: OPENAI_API_KEY required.", file=sys.stderr)
        return 2

    db = Database(settings)
    await db.connect()

    embedder = EmbeddingProvider(
        api_key=settings.openai_api_key,
        model=settings.embedding_model,
        dimensions=settings.embedding_dimensions,
    )

    from nous.api.anthropic_client import create_client
    api_client = create_client(main_settings)
    await api_client.start()

    heart = Heart(database=db, settings=settings,
                  embedding_provider=embedder, owns_embeddings=False)
    brain = Brain(database=db, settings=settings, embedding_provider=embedder)

    try:
        async with heart, brain:
            print(f"\nSeeding fixture under agent_id={_AGENT_ID}...")
            seeded_ids = await _seed_fixture(db, _AGENT_ID)
            print(f"  Seeded {len(seeded_ids)} stuck-open episodes")

            before = await _count_summarized(db, _AGENT_ID)
            print(f"  Summarized BEFORE phase run: {before}")

            # Build the SleepHandler with EpisodeSummarizer wired
            class _StubBus:
                async def emit(self, *_a, **_k):  # pragma: no cover - stub
                    return None

                def on(self, *_a, **_k):  # pragma: no cover - stub
                    return None

            stub_bus = _StubBus()
            summarizer = EpisodeSummarizer(
                heart=heart, brain=brain, settings=settings, bus=None,
                llm_client=api_client,
            )
            sleep_handler = SleepHandler(
                brain=brain, heart=heart, settings=settings,
                bus=stub_bus, llm_client=api_client,
            )
            sleep_handler._episode_summarizer = summarizer

            sleep_stats: dict = {}
            print("\nRunning F060 phase...")
            ok = await sleep_handler._phase_recover_abandoned_episodes(sleep_stats)

            print(f"  Phase returned: {ok}")
            print(f"  Stats: {json.dumps(sleep_stats, indent=2)}")

            after = await _count_summarized(db, _AGENT_ID)
            print(f"\n  Summarized AFTER phase run: {after}")
            print(f"  Recovered this run: {after - before}")

            # Verify episodes still active=true
            async with db.session() as session:
                rows = await session.execute(sql_text(
                    "SELECT id, active, structured_summary IS NOT NULL "
                    "FROM heart.episodes WHERE agent_id=:aid"
                ), {"aid": _AGENT_ID})
                for ep_id, active, has_summary in rows.all():
                    print(
                        f"  Episode {str(ep_id)[:8]}... "
                        f"active={active} summarized={has_summary}"
                    )

            # Pass criteria
            recovered = after - before
            passed = (
                ok
                and recovered == len(_FIXTURE_TRANSCRIPTS)
                and sleep_stats.get("episodes_recovered") == len(_FIXTURE_TRANSCRIPTS)
            )
            print(f"\n  PASS = {passed}")

            return 0 if passed else 1
    finally:
        deleted = await _cleanup(db, _AGENT_ID)
        print(f"\nCleanup: deleted {deleted} fixture rows")
        await api_client.close()
        await db.disconnect()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
