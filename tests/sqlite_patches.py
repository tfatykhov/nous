"""Monkey-patches for PG-specific methods to work with SQLite.

Each patch replaces a method that uses raw PG SQL (vector operators,
full-text search, etc.) with a pure-Python equivalent.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from sqlite_compat import cosine_similarity, keyword_match_score, _parse_embedding


# ============================================================================
# hybrid_search replacement (nous.heart.search)
# ============================================================================


async def sqlite_hybrid_search(
    session: AsyncSession,
    table: str,
    embedding: list[float] | None,
    query_text: str,
    agent_id: str,
    extra_where: str = "",
    extra_params: dict | None = None,
    limit: int = 10,
    vector_weight: float | None = None,
) -> list[tuple[UUID, float]]:
    """Pure-Python hybrid search replacement for SQLite tests."""
    from nous.heart.search import _rrf_merge

    if vector_weight is None:
        vector_weight = 0.7

    model = _table_to_model(table)
    if model is None:
        return []

    query = select(model).where(
        model.agent_id == agent_id,
        model.active == True,  # noqa: E712
    )

    if extra_where and extra_params:
        if "category" in extra_params and "category" in extra_where:
            query = query.where(model.category == extra_params["category"])

    result = await session.execute(query)
    rows = result.scalars().all()

    if not rows:
        return []

    vector_results: list[tuple[UUID, float]] = []
    if embedding is not None:
        for row in rows:
            row_emb = _parse_embedding(row.embedding)
            if row_emb:
                sim = cosine_similarity(embedding, row_emb)
                if sim > 0:
                    vector_results.append((row.id, sim))
        vector_results.sort(key=lambda x: x[1], reverse=True)
        vector_results = vector_results[:limit * 3]

    keyword_results: list[tuple[UUID, float]] = []
    for row in rows:
        searchable = _get_searchable_text(row)
        score = keyword_match_score(query_text, searchable)
        if score > 0:
            keyword_results.append((row.id, score))
    keyword_results.sort(key=lambda x: x[1], reverse=True)
    keyword_results = keyword_results[:limit * 3]

    if embedding is None:
        return keyword_results[:limit]

    return _rrf_merge(vector_results, keyword_results, 60, vector_weight, limit)


def _table_to_model(table: str):
    from nous.storage.models import Episode, Fact, Procedure, Censor
    mapping = {
        "heart.episodes": Episode,
        "heart.facts": Fact,
        "heart.procedures": Procedure,
        "heart.censors": Censor,
    }
    return mapping.get(table)


def _get_searchable_text(row) -> str:
    parts = []
    for attr in ("content", "summary", "description", "title", "name",
                 "trigger_pattern", "reason"):
        val = getattr(row, attr, None)
        if val:
            parts.append(str(val))
    tags = getattr(row, "tags", None)
    if tags:
        if isinstance(tags, str):
            try:
                tags = json.loads(tags)
            except (json.JSONDecodeError, ValueError):
                tags = []
        if isinstance(tags, list):
            parts.extend(str(t) for t in tags)
    return " ".join(parts)


# ============================================================================
# batch_fetch_embeddings replacement
# ============================================================================


async def sqlite_batch_fetch_embeddings(
    session: AsyncSession,
    type_ids: dict[str, list[UUID]],
    agent_id: str,
) -> dict[UUID, list[float]]:
    from nous.storage.models import Episode, Fact, Procedure, Censor

    type_to_model = {
        "fact": Fact,
        "episode": Episode,
        "procedure": Procedure,
        "censor": Censor,
    }

    embeddings: dict[UUID, list[float]] = {}
    for mem_type, ids in type_ids.items():
        model = type_to_model.get(mem_type)
        if not model or not ids:
            continue
        result = await session.execute(
            select(model.id, model.embedding).where(
                model.id.in_(ids),
                model.agent_id == agent_id,
            )
        )
        for row in result.all():
            emb = _parse_embedding(row.embedding)
            if emb:
                embeddings[row.id] = emb

    return embeddings


# ============================================================================
# FactManager patches — signatures match production exactly
# ============================================================================


async def sqlite_find_duplicate(
    self,
    embedding: list[float],
    exclude_ids: list[UUID],
    session: AsyncSession,
):
    """Pure-Python duplicate finding using cosine similarity."""
    from nous.storage.models import Fact

    query = select(Fact).where(
        Fact.agent_id == self.agent_id,
        Fact.active == True,  # noqa: E712
    )
    if exclude_ids:
        query = query.where(Fact.id.notin_(exclude_ids))

    result = await session.execute(query)
    facts = result.scalars().all()

    for fact in facts:
        fact_emb = _parse_embedding(fact.embedding)
        if fact_emb:
            sim = cosine_similarity(embedding, fact_emb)
            if sim > 0.95:
                return fact
    return None


async def sqlite_find_contradiction(
    self,
    embedding: list[float],
    new_content: str,
    exclude_ids: list[UUID],
    session: AsyncSession,
):
    """Pure-Python contradiction detection."""
    from nous.storage.models import Fact
    from nous.heart.schemas import ContradictionWarning

    if not embedding:
        return None

    query = select(Fact).where(
        Fact.agent_id == self.agent_id,
        Fact.active == True,  # noqa: E712
    )
    if exclude_ids:
        query = query.where(Fact.id.notin_(exclude_ids))

    result = await session.execute(query)
    facts = result.scalars().all()

    for fact in facts:
        fact_emb = _parse_embedding(fact.embedding)
        if fact_emb:
            sim = cosine_similarity(embedding, fact_emb)
            if self.CONTRADICTION_SIMILARITY_MIN < sim <= self.CONTRADICTION_SIMILARITY_MAX:
                return ContradictionWarning(
                    existing_fact_id=fact.id,
                    existing_content=fact.content[:500],
                    similarity=sim,
                    message=f"Potential contradiction detected (similarity {sim:.2f}). "
                    f"Existing fact: '{fact.content[:100]}' — review and resolve.",
                )
    return None


async def sqlite_find_max_similarity(
    self,
    embedding: list[float],
    exclude_ids: list[UUID],
    session: AsyncSession,
) -> float | None:
    """Pure-Python max similarity search."""
    from nous.storage.models import Fact

    if not embedding:
        return None

    query = select(Fact).where(
        Fact.agent_id == self.agent_id,
        Fact.active == True,  # noqa: E712
    )
    if exclude_ids:
        query = query.where(Fact.id.notin_(exclude_ids))

    result = await session.execute(query)
    facts = result.scalars().all()

    max_sim = None
    for fact in facts:
        fact_emb = _parse_embedding(fact.embedding)
        if fact_emb:
            sim = cosine_similarity(embedding, fact_emb)
            if max_sim is None or sim > max_sim:
                max_sim = sim
    return max_sim


async def sqlite_search_all(
    self,
    query: str,
    embedding: list[float] | None,
    limit: int,
    category: str | None,
    session: AsyncSession,
):
    """Pure-Python fact search replacement (for _search_all including inactive)."""
    from nous.storage.models import Fact
    from nous.heart.schemas import FactSummary
    from nous.heart.search import _rrf_merge

    stmt = select(Fact).where(Fact.agent_id == self.agent_id)
    if category:
        stmt = stmt.where(Fact.category == category)

    result = await session.execute(stmt)
    facts = result.scalars().all()

    if not facts:
        return []

    vector_results: list[tuple[UUID, float]] = []
    if embedding is not None:
        for fact in facts:
            fact_emb = _parse_embedding(fact.embedding)
            if fact_emb:
                sim = cosine_similarity(embedding, fact_emb)
                if sim > 0:
                    vector_results.append((fact.id, sim))
        vector_results.sort(key=lambda x: x[1], reverse=True)

    keyword_results: list[tuple[UUID, float]] = []
    for fact in facts:
        score = keyword_match_score(query, fact.content or "")
        if score > 0:
            keyword_results.append((fact.id, score))
    keyword_results.sort(key=lambda x: x[1], reverse=True)

    if embedding is None:
        ranked = keyword_results[:limit]
    else:
        ranked = _rrf_merge(vector_results, keyword_results, 60, 0.7, limit)

    if not ranked:
        return []

    ids = [r[0] for r in ranked]
    scores = {r[0]: r[1] for r in ranked}

    fact_result = await session.execute(select(Fact).where(Fact.id.in_(ids)))
    fact_map = {f.id: f for f in fact_result.scalars().all()}

    results = []
    for fid in ids:
        f = fact_map.get(fid)
        if not f:
            continue
        tags = f.tags
        if isinstance(tags, str):
            try:
                tags = json.loads(tags)
            except (json.JSONDecodeError, ValueError):
                tags = []
        results.append(FactSummary(
            id=f.id,
            content=f.content,
            category=f.category,
            subject=f.subject,
            confidence=f.confidence if f.confidence is not None else 1.0,
            active=f.active if f.active is not None else True,
            score=scores.get(fid, 0),
            tags=tags or [],
            # F047: propagate actionable verdict so SQLite-based tests see
            # the same heartbeat behavior as Postgres (embedding + keyword
            # paths both consult the persisted flag).
            actionable=getattr(f, "actionable", None),
            actionable_confidence=getattr(f, "actionable_confidence", None),
        ))
    return results


async def sqlite_get_current(self, fact_id, session):
    """Follow superseded_by chain using ORM instead of recursive CTE."""
    from nous.storage.models import Fact

    current_id = fact_id
    seen = set()
    for _ in range(10):
        if current_id in seen:
            break
        seen.add(current_id)
        result = await session.execute(
            select(Fact).where(
                Fact.id == current_id,
                Fact.agent_id == self.agent_id,
            )
        )
        fact = result.scalars().first()
        if fact is None:
            raise ValueError(f"Fact {fact_id} not found")
        if fact.superseded_by is None:
            return self._to_detail(fact)
        current_id = fact.superseded_by

    raise ValueError(f"Supersede chain too deep for fact {fact_id}")


async def sqlite_find_contradiction_candidates(self, limit, session):
    """Pure-Python contradiction candidate finder."""
    from nous.storage.models import Fact

    result = await session.execute(
        select(Fact).where(
            Fact.agent_id == self.agent_id,
            Fact.active == True,  # noqa: E712
            Fact.subject.isnot(None),
        )
    )
    facts = result.scalars().all()

    candidates = []
    for i, f1 in enumerate(facts):
        f1_emb = _parse_embedding(f1.embedding)
        if not f1_emb:
            continue
        for f2 in facts[i + 1:]:
            if not f2.subject or not f1.subject:
                continue
            if f1.subject.lower() != f2.subject.lower():
                continue
            f2_emb = _parse_embedding(f2.embedding)
            if not f2_emb:
                continue
            sim = cosine_similarity(f1_emb, f2_emb)
            if 0.75 < sim < 0.95:
                candidates.append({
                    "fact1_id": f1.id,
                    "fact2_id": f2.id,
                    "content1": f1.content,
                    "content2": f2.content,
                    "date1": f1.created_at,
                    "date2": f2.created_at,
                    "subject": f1.subject,
                    "category": f1.category,
                    "similarity": round(sim, 4),
                })
    candidates.sort(key=lambda x: x["similarity"], reverse=True)
    return candidates[:limit]


async def sqlite_check_domain_threshold(
    self,
    category: str,
    session: AsyncSession,
) -> None:
    """Pure ORM domain threshold check (no raw SQL)."""
    from nous.storage.models import Fact

    result = await session.execute(
        select(func.count()).select_from(Fact).where(
            Fact.agent_id == self.agent_id,
            Fact.category == category,
            Fact.active == True,  # noqa: E712
        )
    )
    count = result.scalar() or 0

    if count <= self.DOMAIN_COMPACTION_THRESHOLD:
        return

    excess = count - self.DOMAIN_COMPACTION_THRESHOLD
    if excess == 1 or excess % self.DOMAIN_COMPACTION_INTERVAL == 0:
        await self._emit_event(
            session,
            "fact_threshold_exceeded",
            {
                "category": category,
                "count": count,
                "threshold": self.DOMAIN_COMPACTION_THRESHOLD,
            },
        )


# ============================================================================
# CensorManager patches
# ============================================================================


async def sqlite_censor_semantic_search(
    self,
    embedding: list[float],
    limit: int,
    domain: str | None,
    session: AsyncSession,
):
    """Pure-Python censor semantic search."""
    from nous.storage.models import Censor
    from nous.heart.schemas import CensorMatch
    from sqlalchemy import or_

    query = select(Censor).where(
        Censor.agent_id == self.agent_id,
        Censor.active == True,  # noqa: E712
    )
    if domain:
        query = query.where(or_(Censor.domain == domain, Censor.domain.is_(None)))

    result = await session.execute(query)
    censors = result.scalars().all()

    scored = []
    for c in censors:
        c_emb = _parse_embedding(c.embedding)
        if c_emb:
            sim = cosine_similarity(embedding, c_emb)
            if sim > 0.7:
                scored.append((c, sim))

    scored.sort(key=lambda x: x[1], reverse=True)
    scored = scored[:limit]

    return [
        CensorMatch(
            id=c.id,
            trigger_pattern=c.trigger_pattern,
            reason=c.reason,
            action=c.action or "warn",
            domain=c.domain,
            similarity=sim,
        )
        for c, sim in scored
    ]


# ============================================================================
# EpisodeManager patches
# ============================================================================


async def sqlite_vector_temporal_search(self, query_embedding, hours, limit, session):
    """Pure-Python temporal vector search for episodes."""
    from nous.storage.models import Episode
    from sqlite_compat import ensure_aware

    cutoff = datetime.now(UTC) - timedelta(hours=hours)

    result = await session.execute(
        select(Episode).where(
            Episode.agent_id == self.agent_id,
            Episode.active == True,  # noqa: E712
        )
    )
    episodes = result.scalars().all()

    scored = []
    for ep in episodes:
        started = ensure_aware(ep.started_at)
        if started and started > cutoff:
            ep_emb = _parse_embedding(ep.embedding)
            if ep_emb:
                sim = cosine_similarity(query_embedding, ep_emb)
                if sim > 0:
                    scored.append((ep.id, sim))

    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:limit]


# ============================================================================
# EpisodeManager duration patch (timezone-aware subtraction)
# ============================================================================


def patch_episode_duration():
    """Patch Episode.end_episode to handle naive datetimes from SQLite."""
    import nous.heart.episodes as ep_mod

    original_end = ep_mod.EpisodeManager._end

    async def _end_tz_safe(self, episode_id, outcome, lessons, surprise_level, transcript, session):
        """Wrapper that ensures timezone-aware datetimes before calling original."""
        from nous.storage.models import Episode
        from sqlite_compat import ensure_aware

        # Ensure started_at is timezone-aware before the original method tries subtraction
        result = await session.execute(
            select(Episode).where(
                Episode.id == episode_id,
                Episode.agent_id == self.agent_id,
            )
        )
        ep = result.scalars().first()
        if ep and ep.started_at:
            ep.started_at = ensure_aware(ep.started_at)
            await session.flush()

        return await original_end(self, episode_id, outcome, lessons, surprise_level, transcript, session)

    ep_mod.EpisodeManager._end = _end_tz_safe


# ============================================================================
# pg_insert patch for working_memory
# ============================================================================


def patch_pg_insert():
    """Patch modules that use pg_insert ON CONFLICT to use ORM upsert logic."""
    import nous.heart.working_memory as wm_mod
    from nous.storage.models import WorkingMemory

    original_get_or_create = wm_mod.WorkingMemoryManager._get_or_create

    async def _sqlite_get_or_create(self, session_id, session):
        """SQLite-compatible upsert for working_memory."""
        # Check if exists first
        wm = await self._get_wm_orm(session_id, session)
        if wm is None:
            # Create new
            wm = WorkingMemory(
                agent_id=self.agent_id,
                session_id=session_id,
                items=[],
                open_threads=[],
            )
            session.add(wm)
            await session.flush()
            wm = await self._get_wm_orm(session_id, session)

        if wm is None:
            raise RuntimeError(f"Failed to create working memory for session {session_id}")
        return self._to_state(wm)

    wm_mod.WorkingMemoryManager._get_or_create = _sqlite_get_or_create

    # Also patch graph edge creation in facts.py (uses pg_insert)
    import nous.heart.facts as facts_mod
    from nous.storage.models import GraphEdge

    async def _sqlite_create_graph_edge(
        self, source_id, target_id, source_type, target_type, relation, weight, session
    ):
        """SQLite-compatible graph edge creation (no pg_insert)."""
        try:
            async with session.begin_nested():
                edge = GraphEdge(
                    source_id=source_id,
                    target_id=target_id,
                    source_type=source_type,
                    target_type=target_type,
                    agent_id=self.agent_id,
                    relation=relation,
                    weight=weight,
                    auto_linked=True,
                )
                session.add(edge)
                await session.flush()
        except Exception:
            pass  # Ignore conflicts

    facts_mod.FactManager._create_graph_edge = _sqlite_create_graph_edge


# ============================================================================
# Brain patches
# ============================================================================


def patch_brain():
    """Patch Brain methods that use raw PG SQL."""
    import nous.brain.brain as brain_mod
    from nous.storage.models import Decision, GraphEdge

    # Patch _query to use pure-Python search
    if hasattr(brain_mod.Brain, '_query'):
        original_query = brain_mod.Brain._query

        async def _sqlite_query_inner(self, query_text, limit, category, stakes, outcome, bridge_side, session):
            """Pure-Python decision search replacing PG vector + text search."""
            from nous.brain.schemas import DecisionSummary

            # Generate embedding
            query_embedding = None
            if self.embeddings:
                try:
                    query_embedding = await self.embeddings.embed(query_text)
                except Exception:
                    pass

            result = await session.execute(
                select(Decision).where(Decision.agent_id == self.agent_id)
            )
            decisions = result.scalars().all()

            # Apply filters
            filtered = []
            for d in decisions:
                if category and d.category != category:
                    continue
                if stakes and d.stakes != stakes:
                    continue
                if outcome and d.outcome != outcome:
                    continue
                elif not outcome:
                    # Exclude abandoned (outcome='failure', confidence=0.0)
                    if d.outcome == "failure" and d.confidence == 0.0:
                        continue
                filtered.append(d)

            if not filtered:
                return []

            # Score by combination of keyword match and vector similarity
            scored = []
            for d in filtered:
                searchable = f"{d.description or ''} {d.context or ''} {d.pattern or ''}"
                kw_score = keyword_match_score(query_text, searchable)
                vec_score = 0.0
                if query_embedding:
                    d_emb = _parse_embedding(d.embedding)
                    if d_emb:
                        vec_score = cosine_similarity(query_embedding, d_emb)
                combined = max(kw_score, vec_score)
                if combined > 0:
                    scored.append((d, combined))

            scored.sort(key=lambda x: x[1], reverse=True)
            top = scored[:limit]

            # Fetch tags
            results = []
            for d, score in top:
                from nous.storage.models import DecisionTag
                tag_result = await session.execute(
                    select(DecisionTag.tag).where(DecisionTag.decision_id == d.id)
                )
                tags = [row[0] for row in tag_result.all()]
                results.append(DecisionSummary(
                    id=d.id,
                    description=d.description,
                    context=d.context,
                    category=d.category,
                    stakes=d.stakes,
                    confidence=d.confidence,
                    outcome=d.outcome,
                    pattern=d.pattern,
                    created_at=d.created_at,
                    tags=tags,
                    score=score,
                ))
            return results

        brain_mod.Brain._query = _sqlite_query_inner

    # Patch _auto_link to avoid pg_insert and <=> operator
    if hasattr(brain_mod.Brain, '_auto_link'):
        original_auto_link = brain_mod.Brain._auto_link

        async def _sqlite_auto_link(self, decision_id, session, threshold=0.7, max_links=5):
            """Pure-Python auto-linking without PG vector operators."""
            # Get the decision
            result = await session.execute(
                select(Decision).where(
                    Decision.id == decision_id,
                    Decision.agent_id == self.agent_id,
                )
            )
            decision = result.scalars().first()
            if not decision or not decision.embedding:
                return []

            d_emb = _parse_embedding(decision.embedding)
            if not d_emb:
                return []

            # Find similar decisions
            all_result = await session.execute(
                select(Decision).where(
                    Decision.agent_id == self.agent_id,
                    Decision.id != decision_id,
                    Decision.embedding.isnot(None),
                )
            )
            all_decisions = all_result.scalars().all()

            similar = []
            for other in all_decisions:
                other_emb = _parse_embedding(other.embedding)
                if other_emb:
                    sim = cosine_similarity(d_emb, other_emb)
                    if sim >= threshold:
                        similar.append((other.id, sim))

            similar.sort(key=lambda x: x[1], reverse=True)
            similar = similar[:max_links]

            edges = []
            for other_id, sim in similar:
                src, tgt = decision_id, other_id
                if str(src) > str(tgt):
                    src, tgt = tgt, src
                try:
                    async with session.begin_nested():
                        edge = GraphEdge(
                            source_id=src,
                            target_id=tgt,
                            source_type="decision",
                            target_type="decision",
                            agent_id=self.agent_id,
                            relation="related_to",
                            weight=float(sim),
                            auto_linked=True,
                        )
                        session.add(edge)
                        await session.flush()
                        edges.append(edge.id)
                except Exception:
                    pass  # Ignore duplicates

            return edges

        brain_mod.Brain._auto_link = _sqlite_auto_link

    # Patch _delete_inner (raw SQL with schema refs)
    if hasattr(brain_mod.Brain, '_delete_inner'):
        original_delete = brain_mod.Brain._delete_inner

        async def _sqlite_delete_inner(self, decision_id, session):
            """SQLite-compatible decision deletion."""
            from nous.storage.models import Fact, Censor, DecisionReason, DecisionTag, DecisionBridge
            from sqlalchemy import update, delete

            # Unlink facts
            await session.execute(
                update(Fact).where(Fact.source_decision_id == decision_id).values(source_decision_id=None)
            )
            # Unlink censors
            await session.execute(
                update(Censor).where(Censor.learned_from_decision == decision_id).values(learned_from_decision=None)
            )
            # Delete child records
            await session.execute(delete(DecisionTag).where(DecisionTag.decision_id == decision_id))
            await session.execute(delete(DecisionReason).where(DecisionReason.decision_id == decision_id))
            await session.execute(delete(DecisionBridge).where(
                (DecisionBridge.source_id == decision_id) | (DecisionBridge.target_id == decision_id)
            ))
            # Delete the decision
            await session.execute(delete(Decision).where(Decision.id == decision_id))
            await session.flush()

        brain_mod.Brain._delete_inner = _sqlite_delete_inner


# ============================================================================
# Patch installer
# ============================================================================


def install_all_patches():
    """Monkey-patch all PG-specific methods with SQLite-compatible versions."""
    import nous.heart.search as search_mod
    import nous.heart.facts as facts_mod
    import nous.heart.censors as censors_mod
    import nous.heart.episodes as episodes_mod
    import nous.heart.procedures as procedures_mod

    # Patch hybrid_search in ALL modules that imported it
    search_mod.hybrid_search = sqlite_hybrid_search
    facts_mod.hybrid_search = sqlite_hybrid_search
    episodes_mod.hybrid_search = sqlite_hybrid_search
    procedures_mod.hybrid_search = sqlite_hybrid_search

    # Patch batch_fetch_embeddings
    search_mod.batch_fetch_embeddings = sqlite_batch_fetch_embeddings

    # Patch FactManager methods
    facts_mod.FactManager._find_duplicate = sqlite_find_duplicate
    facts_mod.FactManager._find_contradiction = sqlite_find_contradiction
    facts_mod.FactManager._find_max_similarity = sqlite_find_max_similarity
    facts_mod.FactManager._search_all = sqlite_search_all
    facts_mod.FactManager._get_current = sqlite_get_current
    facts_mod.FactManager._find_contradiction_candidates = sqlite_find_contradiction_candidates
    facts_mod.FactManager._check_domain_threshold = sqlite_check_domain_threshold

    # Patch CensorManager methods
    censors_mod.CensorManager._semantic_search = sqlite_censor_semantic_search

    # Patch EpisodeManager methods
    episodes_mod.EpisodeManager._vector_temporal_search = sqlite_vector_temporal_search

    # Patch duration calculation for timezone safety
    patch_episode_duration()

    # Patch pg_insert usage
    patch_pg_insert()

    # Patch Brain
    patch_brain()
