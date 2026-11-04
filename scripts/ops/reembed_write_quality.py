"""Re-embed censors and procedures from the current embed-text settings. Dry run unless --apply.

NOUS_CENSOR_EMBED_TEXT_MODE and NOUS_PROCEDURE_EMBED_EVIDENCE decide the text a row is embedded from
when it is written. A row written before a change keeps its old vector until this script re-embeds it:

  --censors     every censor of the agent, active or not (an inactive one can be switched back on)
  --procedures  every active procedure of the agent (a restore re-embeds an inactive one)

A dry run prints the settings and how many rows --apply would re-embed, and writes nothing: an upper
bound, since a row whose embedding call fails keeps its old vector. --apply re-embeds them, one call per
row, and prints how many it re-embedded and how many failed.
Idempotent: a run embeds from the current settings, so a rollback is the old setting and another run.

Order for a change: set the setting in the deployed env and restart, then run with --apply, for
procedures between sleep cycles (the learner's covered probe and the create-time dedupe compare a new
lesson's text with the stored rows, so the two must be embedded the same way).

Usage::

    uv run python scripts/ops/reembed_write_quality.py --censors --procedures --agent-id nous-default
    uv run python scripts/ops/reembed_write_quality.py --censors --procedures --agent-id nous-default --apply
"""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from nous.brain.embeddings import EmbeddingProvider
from nous.config import Settings
from nous.heart import Heart
from nous.storage.database import Database

_COUNT_SQL = {
    "censors": text("SELECT count(*) FROM heart.censors WHERE agent_id = :agent_id"),
    "procedures": text("SELECT count(*) FROM heart.procedures WHERE agent_id = :agent_id AND active = true"),
}


async def reembed(
    heart: Heart, session: AsyncSession, *, censors: bool, procedures: bool, apply: bool,
) -> dict[str, int]:
    """Rows re-embedded (--apply) or that --apply would re-embed (dry run), by kind."""
    kinds = [kind for kind, wanted in (("censors", censors), ("procedures", procedures)) if wanted]
    if not apply:
        return {
            kind: int(await session.scalar(_COUNT_SQL[kind], {"agent_id": heart.agent_id}) or 0) for kind in kinds
        }
    counts: dict[str, int] = {}
    if censors:
        counts["censors"] = await heart.censors.reembed_all(session)
    if procedures:
        counts["procedures"] = await heart.reembed_procedures(session)
    return counts


def embedding_provider(settings: Settings) -> EmbeddingProvider | None:
    """The provider nous/main.py builds, so rows are embedded in the same space: the same key, model,
    dimensions and cache size. None without an API key, as there."""
    if not settings.openai_api_key:
        return None
    return EmbeddingProvider(
        api_key=settings.openai_api_key,
        model=settings.embedding_model,
        dimensions=settings.embedding_dimensions,
        cache_size=settings.embedding_cache_size,
    )


async def main_async(agent_id: str, *, censors: bool, procedures: bool, apply: bool) -> None:
    settings = Settings().model_copy(update={"agent_id": agent_id})
    print(
        f"agent {agent_id}: censor_embed_text_mode={settings.censor_embed_text_mode}, "
        f"procedure_embed_evidence={settings.procedure_embed_evidence}, "
        f"embedding_model={settings.embedding_model} ({settings.embedding_dimensions} dims)"
    )
    provider = embedding_provider(settings)
    if apply and provider is None:
        raise SystemExit("--apply needs OPENAI_API_KEY: without it nothing can be embedded")
    db = Database(settings)
    await db.connect()
    heart = Heart(db, settings, embedding_provider=provider)
    try:
        async with db.session() as session:
            planned = await reembed(heart, session, censors=censors, procedures=procedures, apply=False)
            if apply:
                done = await reembed(heart, session, censors=censors, procedures=procedures, apply=True)
                await session.commit()
    finally:
        await heart.close()
        await db.disconnect()
    for kind, count in planned.items():
        if apply:
            failed = count - done[kind]
            print(f"{kind}: re-embedded {done[kind]} of {count}; {failed} failed and kept their old vector")
        else:
            print(f"{kind}: would re-embed {count} (dry run, nothing written)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--censors", action="store_true", help="Re-embed every censor of the agent.")
    parser.add_argument("--procedures", action="store_true", help="Re-embed every active procedure of the agent.")
    parser.add_argument("--agent-id", default=None, help="Agent whose rows to re-embed (default: NOUS_AGENT_ID).")
    parser.add_argument("--apply", action="store_true", help="Write the new embeddings (default: dry run).")
    args = parser.parse_args()
    if not (args.censors or args.procedures):
        parser.error("name what to re-embed: --censors and/or --procedures")
    asyncio.run(main_async(
        args.agent_id or Settings().agent_id, censors=args.censors, procedures=args.procedures, apply=args.apply,
    ))


if __name__ == "__main__":
    main()
