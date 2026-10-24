"""Procedure management — K-lines with level-bands (how to do things).

Manages procedural memory: storing, activating, recording outcomes.
All methods follow Brain's session injection pattern (P1-1).
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from nous.brain.embeddings import EmbeddingProvider
from nous.heart.schemas import (
    JOB_LESSON_KIND,
    STRATEGY_CARD_KIND,
    EvolutionCandidate,
    JobLesson,
    ProcedureChangeSource,
    ProcedureDetail,
    ProcedureInput,
    ProcedureOutcome,
    ProcedureSummary,
    ProcedureVersionDetail,
    is_job_lesson,
    is_strategy_card,
)
from nous.heart.search import cosine_similarity, hybrid_search, hybrid_search_multi
from nous.storage.database import Database
from nous.storage.models import Event, Procedure, ProcedureTaskAffinity, ProcedureVersion

logger = logging.getLogger(__name__)

# The list fields a version snapshot holds and a dedupe merge unions.
_LIST_FIELDS = ("goals", "core_patterns", "core_tools", "core_concepts", "implementation_notes", "tags")

# The auto-learned how-to population of the semantic dedupe (scripts/diag/procedure_dedupe_report.py
# reports on the same one): an ``auto:*`` tag, never a skill, a strategy card or a job lesson.
LEARNED_WHERE = (
    "kind IS DISTINCT FROM :strategy_kind AND kind IS DISTINCT FROM :job_lesson_kind"
    " AND EXISTS (SELECT 1 FROM unnest(tags) AS tag WHERE tag LIKE :learned_tag)"
    " AND NOT ('skill' = ANY(coalesce(tags, '{}')))"
)
LEARNED_PARAMS = {"strategy_kind": STRATEGY_CARD_KIND, "job_lesson_kind": JOB_LESSON_KIND, "learned_tag": "auto:%"}


class ProcedureConflictError(ValueError):
    """A procedure write refused because the stored state is not what it assumed (F102).

    Raised for a stale ``expected_version``, an idempotency key reused with a different
    request, and a restore to a name another active procedure holds. Nothing is written.
    """


def _normalize_name(name: str) -> str:
    """The name the create-time name lock compares: casefolded, '-'/'_' as spaces, whitespace collapsed."""
    return " ".join(name.casefold().replace("-", " ").replace("_", " ").split())


def _is_skill(tags: list[str] | None) -> bool:
    return "skill" in (tags or [])


def _is_learned(tags: list[str] | None) -> bool:
    """Auto-learned by the K-line learner (an ``auto:*`` tag), not authored by hand."""
    return any(tag.startswith("auto:") for tag in tags or [])


def _in_dedupe_population(procedure: object) -> bool:
    """Auto-learned how-to (LEARNED_WHERE in Python), for a row or an input: never a skill, a card or a job lesson."""
    tags = getattr(procedure, "tags", None)
    if is_strategy_card(procedure) or is_job_lesson(procedure):
        return False
    return _is_learned(tags) and not _is_skill(tags)


# Evidence lines one procedure collects (the K-line learner's covered path and a dedupe merge).
MAX_EVIDENCE_NOTES = 10


def new_evidence_notes(notes: list[str], incoming: list[str]) -> list[str]:
    """The ``evidence:`` lines of ``incoming`` to append to ``notes``: each cited ID once, up to the cap.

    The line's second word is the ID of the input item it cites.
    """
    evidence = [note for note in notes if note.startswith("evidence: ")]
    cited = {note.split(" ")[1] for note in evidence}
    return [
        note for note in incoming
        if note.startswith("evidence: ") and note.split(" ")[1] not in cited
    ][: max(0, MAX_EVIDENCE_NOTES - len(evidence))]


def _union(existing: list[str] | None, incoming: list[str]) -> list[str]:
    """``existing`` in its order, then each item of ``incoming`` it does not hold yet."""
    merged = list(existing or [])
    for item in incoming:
        if item not in merged:
            merged.append(item)
    return merged


def _check_version(procedure: Procedure, expected_version: int | None) -> None:
    """Refuse a write that assumed a version the row has moved past (optimistic concurrency)."""
    if expected_version is not None and procedure.version != expected_version:
        raise ProcedureConflictError(
            f"Procedure {procedure.id} is at version {procedure.version}, not the expected {expected_version}"
        )


def _is_postgres(session: AsyncSession) -> bool:
    # pgvector and advisory locks are Postgres-only; the SQLite test backend runs serially.
    return session.bind is not None and session.bind.dialect.name == "postgresql"


# The request fields a replay must repeat: a fixed list, so a field added to ProcedureInput
# later does not turn the retries in flight during a deploy into conflicts.
_FINGERPRINT_FIELDS = {"name", "domain", "description", *_LIST_FIELDS, "active", "runtime_metadata", "kind"}


def _fingerprint(input: ProcedureInput) -> str:
    """The sha256 of a create request without its key: a replay must send the same request.

    Keys sorted, so a runtime_metadata dict built in another order is the same request.
    """
    request = json.dumps(
        input.model_dump(mode="json", include=_FINGERPRINT_FIELDS), sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(request.encode()).hexdigest()


def _build_embed_text(
    name: str,
    description: str | None,
    core_patterns: list[str] | None,
    goals: list[str] | None,
    core_tools: list[str] | None,
    core_concepts: list[str] | None,
    implementation_notes: list[str] | None,
) -> str:
    """Build the text used for procedure embedding (issue #197).

    Includes all body fields, not just metadata, for full-body search accuracy.
    """
    text = (
        f"{name} {description or ''} "
        f"{' '.join(core_patterns or [])} "
        f"{' '.join(goals or [])} "
        f"{' '.join(core_tools or [])} "
        f"{' '.join(core_concepts or [])} "
        f"{' '.join(implementation_notes or [])}"
    ).strip()
    # Bound the EMBEDDING input only — OpenAI text-embedding-3 caps at 8191 tokens;
    # full skill bodies (F081) can exceed that and would 400 → NULL embedding →
    # invisible to vector search. The STORED body stays full; metadata
    # (name/description) leads so the body never crowds out the routing signal.
    # Conservative CHAR bound (tiktoken is not a dependency): realistic skill
    # markdown (English prose + code) runs >=2 chars/token, so 16000 chars stays
    # under 8191 tokens. _embed_with_retry NULLs+logs on a pathological (e.g.
    # CJK-dense) overflow as the backstop — but skill bodies are English markdown.
    _EMBED_CHAR_CAP = 16000
    return text[:_EMBED_CHAR_CAP] if len(text) > _EMBED_CHAR_CAP else text


class ProcedureManager:
    """Manages procedural memory — how to do things (K-lines with level-bands)."""

    def __init__(
        self,
        db: Database,
        embeddings: EmbeddingProvider | None,
        agent_id: str,
        *,
        utility_boost: bool = True,
        utility_alpha: float = 0.15,
        affinity_beta: float = 0.10,
        min_activations_for_boost: int = 5,
        dedupe_enabled: bool = True,
        dedupe_threshold: float = 0.85,
    ) -> None:
        self.db = db
        self.embeddings = embeddings
        self.agent_id = agent_id
        self._utility_boost = utility_boost
        self._utility_alpha = utility_alpha
        self._affinity_beta = affinity_beta
        self._min_activations_for_boost = min_activations_for_boost
        self._dedupe_enabled = dedupe_enabled
        self._dedupe_threshold = dedupe_threshold

    # ------------------------------------------------------------------
    # Event helper
    # ------------------------------------------------------------------

    async def _emit_event(self, session: AsyncSession, event_type: str, data: dict) -> None:
        """Insert event in same session (P2-1)."""
        event = Event(
            agent_id=self.agent_id,
            event_type=event_type,
            data=data,
        )
        session.add(event)

    # ------------------------------------------------------------------
    # store()
    # ------------------------------------------------------------------

    async def store(self, input: ProcedureInput, session: AsyncSession | None = None) -> ProcedureDetail:
        """Store a new procedure, or return the one it duplicates (F102).

        An ``idempotency_key`` already used returns the procedure that request wrote
        (ProcedureConflictError for the same key with a different request). With dedupe
        enabled, an active auto-learned input (an ``auto:*`` tag; never a skill, a strategy
        card or a job lesson) is merged into the active auto-learned procedure of the same
        normalized name when their cosine is at or above the threshold too (the name alone
        when there is no embedding to compare), or else into the nearest one at or above it.
        A merge writes the existing row a new version (change_source ``dedupe_merge``, see
        _merged_body) and returns that row.
        """
        if session is None:
            async with self.db.session() as session:
                result = await self._store(input, session)
                await session.commit()
                return result
        return await self._store(input, session)

    async def _embed_with_retry(
        self,
        embed_text: str,
        *,
        attempts: int = 2,
        on_failure: str = "storing procedure with NULL embedding (invisible to vector search/dedup until reembed_all)",
    ) -> list[float] | None:
        """Embed with one retry; on final failure log ERROR (not a quiet warning), with ``on_failure``.

        A NULL embedding makes the row invisible to vector search and the dedup
        cosine gate (audit bug 8), so the failure must be loud, not swallowed.
        """
        if not self.embeddings:
            return None
        for attempt in range(1, attempts + 1):
            try:
                return await self.embeddings.embed(embed_text)
            except Exception:
                if attempt >= attempts:
                    logger.error("Embedding generation failed after %d attempts; %s", attempts, on_failure)
                    return None
                logger.warning("Embedding attempt %d/%d failed for procedure, retrying", attempt, attempts)
        return None

    async def _store(self, input: ProcedureInput, session: AsyncSession) -> ProcedureDetail:
        # Generate embedding from all body fields (issue #197). Before the create lock: it
        # depends on the input alone, and the lock's holder should not wait on the provider.
        embedding = None
        if self.embeddings:
            embed_text = _build_embed_text(
                input.name, input.description, input.core_patterns,
                input.goals, input.core_tools, input.core_concepts,
                input.implementation_notes,
            )
            embedding = await self._embed_with_retry(embed_text)

        # Only an auto-learned lesson is merged, and only into one: a hand-written or
        # skill body is never folded into another row, nor another row's tags into it.
        dedupe = self._dedupe_enabled and input.active is not False and _in_dedupe_population(input)
        # The merged body is embedded before the create lock too; the merge uses it if the target is unchanged.
        prepared = await self._prepare_merge(input, embedding, session) if dedupe else None
        if input.idempotency_key is not None or dedupe:
            await self._lock_creates(session)
        if input.idempotency_key is not None:
            replayed = await self._replay(input, session)
            if replayed is not None:
                return replayed
        if dedupe and (target := await self._merge_target(input, embedding, session)) is not None:
            merged = await self._merge(*target, input, session, prepared)
            if merged is not None:
                return merged

        procedure = Procedure(
            agent_id=self.agent_id,
            name=input.name,
            domain=input.domain,
            description=input.description,
            goals=input.goals or None,
            core_patterns=input.core_patterns or None,
            core_tools=input.core_tools or None,
            core_concepts=input.core_concepts or None,
            implementation_notes=input.implementation_notes or None,
            tags=input.tags or None,
            embedding=embedding,
            active=input.active if input.active is not None else True,
            # F064.4: always-persist semantic — runtime_metadata is None
            # when the manifest didn't declare any of the new fields, but
            # we don't gate on a flag at write time (silent-drop fix).
            runtime_metadata=input.runtime_metadata,
            # Reasoning Maps L1 (migration 079)
            kind=input.kind,
            version=1,
        )
        # A job lesson never makes a card give its name up: the unique index refuses it instead.
        if procedure.active and not is_strategy_card(procedure) and not is_job_lesson(procedure):
            await self._take_name_from_card(procedure.name, session)
        session.add(procedure)
        await session.flush()
        self._record_version(procedure, session, "create", input=input)
        await session.flush()

        # F040: Emit procedure_stored event for graph linking
        await self._emit_event(session, "procedure_stored", {
            "procedure_id": str(procedure.id),
            "name": input.name,
            "domain": input.domain or "",
            "description": input.description or "",
            "tags": input.tags or [],
        })

        return self._to_detail(procedure)

    # ------------------------------------------------------------------
    # update_body() — in-place skill refresh (audit bug 9)
    # ------------------------------------------------------------------

    async def update_body(
        self,
        procedure_id: UUID,
        input: ProcedureInput,
        session: AsyncSession | None = None,
        *,
        expected_version: int | None = None,
        reason: str | None = None,
    ) -> ProcedureDetail:
        """Refresh a procedure's body fields in place, preserving learned stats.

        Used by learn_skill on a name collision instead of retire+store, which
        reset activation/success/failure counts and orphaned task affinity
        (audit bug 9). Counts, created_at, and supersession state are untouched.
        Raises ValueError for a strategy card's row and a how-to input: a how-to
        body never replaces a card. The write is a new version (F102); with
        ``expected_version`` it is refused (ProcedureConflictError) unless the row
        is still at that version. ``reason`` is kept on the version. An input that
        does not set runtime_metadata leaves the row's as it is.
        """
        if session is None:
            async with self.db.session() as session:
                result = await self._update_body(procedure_id, input, session, expected_version, reason)
                await session.commit()
                return result
        return await self._update_body(procedure_id, input, session, expected_version, reason)

    async def _update_body(
        self,
        procedure_id: UUID,
        input: ProcedureInput,
        session: AsyncSession,
        expected_version: int | None = None,
        reason: str | None = None,
    ) -> ProcedureDetail:
        procedure = await self._locked_procedure(procedure_id, session)
        if procedure is None:
            raise ValueError(f"Procedure {procedure_id} not found")
        _check_version(procedure, expected_version)
        if is_strategy_card(procedure) and not is_strategy_card(input):
            # The card's lesson and the link to its decision would be gone, and
            # the decision's graph edge would start at a how-to row.
            raise ValueError(f"Procedure {procedure_id} is a strategy card: a how-to body does not replace it")

        # The row's body already is the input's: no version, and no embedding call unless the row has none.
        unchanged = (
            (procedure.name, procedure.domain, procedure.description, procedure.kind)
            == (input.name, input.domain, input.description, input.kind)
            and all(list(getattr(procedure, field) or []) == getattr(input, field) for field in _LIST_FIELDS)
            and (
                "runtime_metadata" not in input.model_fields_set
                or procedure.runtime_metadata == input.runtime_metadata
            )
        )
        embedding = procedure.embedding
        if self.embeddings and not (unchanged and embedding is not None):
            embed_text = _build_embed_text(
                input.name, input.description, input.core_patterns,
                input.goals, input.core_tools, input.core_concepts,
                input.implementation_notes,
            )
            # Re-embed from the new body. On failure _embed_with_retry returns None and
            # logs ERROR; store NULL (consistent with _store) rather than keep the now-stale
            # vector for the old body — otherwise the row is served/deduped under an obsolete
            # embedding and reembed_all can't tell it needs repair.
            embedding = await self._embed_with_retry(embed_text)

        if input.active and not procedure.active and not is_strategy_card(input):
            # This refresh turns the row active (the skill re-import does, once the
            # skill's requirement is set): as for an insert, a card gives the name up.
            await self._take_name_from_card(input.name, session)
        procedure.name = input.name
        procedure.domain = input.domain
        procedure.description = input.description
        procedure.goals = input.goals or None
        procedure.core_patterns = input.core_patterns or None
        procedure.core_tools = input.core_tools or None
        procedure.core_concepts = input.core_concepts or None
        procedure.implementation_notes = input.implementation_notes or None
        procedure.tags = input.tags or None
        if "runtime_metadata" in input.model_fields_set:
            procedure.runtime_metadata = input.runtime_metadata
        # Propagate kind from the input. (A how-to input over a card's row was
        # refused above: a card is not turned into a skill here.)
        procedure.kind = input.kind
        if input.active is not None:
            procedure.active = input.active
        procedure.embedding = embedding
        if unchanged:
            await session.flush()
            return self._to_detail(procedure)
        procedure.updated_at = datetime.now(UTC)
        await self._bump_version(procedure, session, "update_body", reason=reason)
        return self._to_detail(procedure)

    # ------------------------------------------------------------------
    # append_notes() — evidence for a lesson the procedure already teaches
    # ------------------------------------------------------------------

    async def append_notes(
        self,
        procedure_id: UUID,
        pick: Callable[[list[str]], list[str]],
        session: AsyncSession | None = None,
        *,
        expected_version: int | None = None,
    ) -> list[str]:
        """Append the notes ``pick`` chooses, leaving every other field and the stats as they are.

        ``pick`` gets the procedure's current notes, read under a row lock, and returns the
        notes to append, so a choice that depends on them (an ID cited once, a cap) still holds
        when two callers append to one procedure at once. Unlike update_body, nothing is taken
        from an input model, so runtime_metadata (a hand-set skip_until_scored, for one) and
        kind survive. The embedding is not recomputed: the notes add evidence for the lesson,
        not a new lesson. Returns the notes appended. Raises ValueError for a strategy card.
        Appended notes are a new version (F102); ``expected_version`` as for update_body.
        """
        if session is None:
            async with self.db.session() as session:
                result = await self._append_notes(procedure_id, pick, session, expected_version)
                await session.commit()
                return result
        return await self._append_notes(procedure_id, pick, session, expected_version)

    async def _append_notes(
        self,
        procedure_id: UUID,
        pick: Callable[[list[str]], list[str]],
        session: AsyncSession,
        expected_version: int | None,
    ) -> list[str]:
        # Locked and read fresh, not the session's copy of the row: pick() decides on these notes.
        procedure = await self._locked_procedure(procedure_id, session)
        if procedure is None:
            raise ValueError(f"Procedure {procedure_id} not found")
        _check_version(procedure, expected_version)
        if is_strategy_card(procedure):
            raise ValueError(f"Procedure {procedure_id} is a strategy card: notes are not appended to it")
        notes = pick(list(procedure.implementation_notes or []))
        if notes:
            # A new list, not an in-place append: the ARRAY column only sees reassignment.
            procedure.implementation_notes = [*(procedure.implementation_notes or []), *notes]
            procedure.updated_at = datetime.now(UTC)
            await self._bump_version(procedure, session, "append_notes")
        return notes

    # ------------------------------------------------------------------
    # list_versions() / restore() — F102 version history
    # ------------------------------------------------------------------

    async def list_versions(
        self, procedure_id: UUID, session: AsyncSession | None = None
    ) -> list[ProcedureVersionDetail]:
        """Every recorded version of this agent's procedure, oldest first ([] for none)."""
        if session is None:
            async with self.db.session() as session:
                return await self._list_versions(procedure_id, session)
        return await self._list_versions(procedure_id, session)

    async def _list_versions(self, procedure_id: UUID, session: AsyncSession) -> list[ProcedureVersionDetail]:
        result = await session.execute(
            select(ProcedureVersion)
            .where(ProcedureVersion.procedure_id == procedure_id)
            .where(ProcedureVersion.agent_id == self.agent_id)
            .order_by(ProcedureVersion.version)
        )
        return [
            ProcedureVersionDetail(
                procedure_id=row.procedure_id,
                version=row.version,
                name=row.name,
                domain=row.domain,
                description=row.description,
                **{field: list(getattr(row, field) or []) for field in _LIST_FIELDS},
                kind=row.kind,
                change_source=row.change_source,
                reason=row.reason,
                created_at=row.created_at,
            )
            for row in result.scalars().all()
        ]

    async def restore(
        self,
        procedure_id: UUID,
        version: int,
        session: AsyncSession | None = None,
        *,
        expected_version: int | None = None,
    ) -> ProcedureDetail:
        """Bring back the body of ``version`` as a new version (change_source ``restore``).

        Restores the name, domain, description and list fields, and re-embeds. Kind,
        runtime_metadata, active and the stats stay as they are. Raises ValueError for an
        unknown procedure or version, and ProcedureConflictError for a stale
        ``expected_version`` or a name another active procedure holds now.
        """
        if session is None:
            async with self.db.session() as session:
                result = await self._restore(procedure_id, version, session, expected_version)
                await session.commit()
                return result
        return await self._restore(procedure_id, version, session, expected_version)

    async def _restore(
        self, procedure_id: UUID, version: int, session: AsyncSession, expected_version: int | None
    ) -> ProcedureDetail:
        procedure = await self._locked_procedure(procedure_id, session)
        if procedure is None:
            raise ValueError(f"Procedure {procedure_id} not found")
        _check_version(procedure, expected_version)
        snapshot = (await session.execute(
            select(ProcedureVersion)
            .where(ProcedureVersion.procedure_id == procedure_id)
            .where(ProcedureVersion.version == version)
        )).scalars().first()
        if snapshot is None:
            raise ValueError(f"Procedure {procedure_id} has no version {version}")

        if procedure.active and snapshot.name.lower() != procedure.name.lower():
            # The (agent_id, lower(name)) WHERE active index: checked the way _reactivate checks it,
            # so a strategy card gives the name up to a how-to row instead of blocking it.
            clash = (
                select(Procedure.id)
                .where(func.lower(Procedure.name) == func.lower(snapshot.name))
                .where(Procedure.agent_id == self.agent_id)
                .where(Procedure.active == True)  # noqa: E712
                .where(Procedure.id != procedure_id)
            )
            if not is_strategy_card(procedure):
                clash = clash.where(Procedure.kind.is_distinct_from(STRATEGY_CARD_KIND))
            if (await session.execute(clash.limit(1))).scalars().first() is not None:
                raise ProcedureConflictError(
                    f"Procedure {procedure_id}: an active procedure is named {snapshot.name!r}, so version "
                    f"{version} is not restored"
                )
            if not is_strategy_card(procedure):
                await self._take_name_from_card(snapshot.name, session)

        procedure.name = snapshot.name
        procedure.domain = snapshot.domain
        procedure.description = snapshot.description
        for field in _LIST_FIELDS:
            setattr(procedure, field, list(getattr(snapshot, field) or []) or None)
        await self._reembed(procedure)
        procedure.updated_at = datetime.now(UTC)
        await self._bump_version(procedure, session, "restore", reason=f"restored version {version}")
        logger.info("Procedure %s restored to the body of version %d (now version %d)",
                    procedure_id, version, procedure.version)
        return self._to_detail(procedure)

    # ------------------------------------------------------------------
    # activate()
    # ------------------------------------------------------------------

    async def activate(self, procedure_id: UUID, session: AsyncSession | None = None) -> ProcedureDetail:
        """Mark a procedure as activated."""
        if session is None:
            async with self.db.session() as session:
                result = await self._activate(procedure_id, session)
                await session.commit()
                return result
        return await self._activate(procedure_id, session)

    async def _activate(self, procedure_id: UUID, session: AsyncSession) -> ProcedureDetail:
        procedure = await self._get_procedure_orm(procedure_id, session)
        if procedure is None:
            raise ValueError(f"Procedure {procedure_id} not found")

        # P2-9: NULL-safe counter
        procedure.activation_count = (procedure.activation_count or 0) + 1
        procedure.last_activated = datetime.now(UTC)
        await session.flush()

        await self._emit_event(
            session,
            "procedure_activated",
            {"procedure_id": str(procedure_id)},
        )

        return self._to_detail(procedure)

    # ------------------------------------------------------------------
    # record_outcome()
    # ------------------------------------------------------------------

    _VALID_OUTCOMES: set[str] = {"success", "failure", "neutral"}

    async def record_outcome(
        self,
        procedure_id: UUID,
        outcome: ProcedureOutcome,
        frame_type: str | None = None,
        session: AsyncSession | None = None,
    ) -> ProcedureDetail:
        """Record procedure activation outcome.

        Args:
            procedure_id: The procedure to record outcome for.
            outcome: 'success', 'failure', or 'neutral'.
            frame_type: Optional cognitive frame type (e.g. 'task', 'conversation').
                        When provided, also upserts the procedure_task_affinity row (F037).
            session: Optional existing session.
        """
        if session is None:
            async with self.db.session() as session:
                result = await self._record_outcome(procedure_id, outcome, frame_type, session)
                await session.commit()
                return result
        return await self._record_outcome(procedure_id, outcome, frame_type, session)

    async def _record_outcome(
        self,
        procedure_id: UUID,
        outcome: ProcedureOutcome,
        frame_type: str | None,
        session: AsyncSession,
    ) -> ProcedureDetail:
        if outcome not in self._VALID_OUTCOMES:
            raise ValueError(f"Invalid outcome {outcome!r}; must be one of {sorted(self._VALID_OUTCOMES)}")

        procedure = await self._get_procedure_orm(procedure_id, session)
        if procedure is None:
            raise ValueError(f"Procedure {procedure_id} not found")

        # P2-9: NULL-safe counter increments
        if outcome == "success":
            procedure.success_count = (procedure.success_count or 0) + 1
        elif outcome == "failure":
            procedure.failure_count = (procedure.failure_count or 0) + 1
        elif outcome == "neutral":
            procedure.neutral_count = (procedure.neutral_count or 0) + 1

        await session.flush()

        # F037: Upsert task affinity row when frame_type is known
        if frame_type:
            await self._upsert_task_affinity(procedure_id, frame_type, outcome, session)

        await self._emit_event(
            session,
            "procedure_outcome",
            {"procedure_id": str(procedure_id), "outcome": outcome},
        )

        return self._to_detail(procedure)

    # ------------------------------------------------------------------
    # get()
    # ------------------------------------------------------------------

    async def get(self, procedure_id: UUID, session: AsyncSession | None = None) -> ProcedureDetail | None:
        """Fetch procedure with computed effectiveness."""
        if session is None:
            async with self.db.session() as session:
                return await self._get(procedure_id, session)
        return await self._get(procedure_id, session)

    async def _get(self, procedure_id: UUID, session: AsyncSession) -> ProcedureDetail | None:
        procedure = await self._get_procedure_orm(procedure_id, session)
        if procedure is None:
            return None
        return self._to_detail(procedure)

    # ------------------------------------------------------------------
    # search()
    # ------------------------------------------------------------------

    async def search(
        self,
        query: str,
        limit: int = 10,
        domain: str | None = None,
        frame_type: str | None = None,
        session: AsyncSession | None = None,
        variant_pairs: list[tuple[str, list[float] | None]] | None = None,
        penalty_limit: int | None = None,
    ) -> list[ProcedureSummary]:
        """Hybrid search over procedures. Optional domain filter.

        When utility boost is enabled (F037), applies an effectiveness-weighted
        score boost: final_score = hybrid_score * (1 + α*utility + β*affinity).

        Args:
            variant_pairs: F050 — when set with len > 1, routes through
                hybrid_search_multi for RRF fusion across query variants.
                F037 utility-boost still applies downstream of routing.
                Defaults to None (single-query path; backwards compatible).
        """
        if session is None:
            async with self.db.session() as session:
                return await self._search(query, limit, domain, frame_type, session, variant_pairs, penalty_limit)
        return await self._search(query, limit, domain, frame_type, session, variant_pairs, penalty_limit)

    async def _search(
        self,
        query: str,
        limit: int,
        domain: str | None,
        frame_type: str | None,
        session: AsyncSession,
        variant_pairs: list[tuple[str, list[float] | None]] | None = None,
        penalty_limit: int | None = None,
    ) -> list[ProcedureSummary]:
        embedding = None
        if self.embeddings:
            try:
                embedding = await self.embeddings.embed(query)
            except Exception:
                logger.warning("Embedding generation failed for procedure search")

        # Reasoning Maps L1: a strategy card is a lesson, not a how-to
        # procedure, so no search-driven read returns one (passive Track B, the
        # recall tool's procedure scope, the K-line learner's "auto:" sweep).
        # A job lesson is shown only on its own job's runs, so no search returns one.
        extra_where = (
            " AND t.active = true AND t.kind IS DISTINCT FROM :strategy_kind"
            " AND t.kind IS DISTINCT FROM :job_lesson_kind"
        )
        extra_params: dict = {"strategy_kind": STRATEGY_CARD_KIND, "job_lesson_kind": JOB_LESSON_KIND}
        if domain:
            extra_where += " AND t.domain = :domain"
            extra_params["domain"] = domain

        if variant_pairs and len(variant_pairs) > 1:
            results = await hybrid_search_multi(
                session=session,
                table="heart.procedures",
                queries=variant_pairs,
                agent_id=self.agent_id,
                extra_where=extra_where,
                extra_params=extra_params,
                limit=limit,
                penalty_limit=penalty_limit,
                any_word_fallback=True,
            )
        else:
            results = await hybrid_search(
                session=session,
                table="heart.procedures",
                embedding=embedding,
                query_text=query,
                agent_id=self.agent_id,
                extra_where=extra_where,
                extra_params=extra_params,
                limit=limit,
                penalty_limit=penalty_limit,
                any_word_fallback=True,
            )

        if not results:
            return []

        ids = [r[0] for r in results]
        scores = {r[0]: r[1] for r in results}

        proc_result = await session.execute(select(Procedure).where(Procedure.id.in_(ids)))
        procedures = {p.id: p for p in proc_result.scalars().all()}

        # F037: Load affinity data for current frame_type if provided
        affinity_map: dict[UUID, tuple[int, int]] = {}
        if frame_type and self._utility_boost:
            affinity_rows = await session.execute(
                select(ProcedureTaskAffinity)
                .where(ProcedureTaskAffinity.procedure_id.in_(ids))
                .where(ProcedureTaskAffinity.frame_type == frame_type)
                .where(ProcedureTaskAffinity.agent_id == self.agent_id)
                .where(ProcedureTaskAffinity.active == True)
            )
            for row in affinity_rows.scalars().all():
                affinity_map[row.procedure_id] = (row.success_count, row.failure_count)

        summaries = []
        for pid in ids:
            p = procedures.get(pid)
            if p is None:
                continue

            hybrid_score = scores.get(p.id, 0.0)
            effectiveness = self._compute_effectiveness(p)

            # F037: Apply utility boost when enabled and procedure has sufficient history
            final_score = hybrid_score
            if self._utility_boost and effectiveness is not None:
                activation_count = p.activation_count or 0
                if activation_count >= self._min_activations_for_boost:
                    utility_signal = effectiveness - 0.5
                    boost = self._utility_alpha * utility_signal

                    # Apply frame-type affinity boost if data is available
                    if pid in affinity_map:
                        aff_success, aff_failure = affinity_map[pid]
                        aff_total = aff_success + aff_failure
                        if aff_total >= self._min_activations_for_boost:
                            frame_eff = (aff_success + 1) / (aff_total + 2)
                            boost += self._affinity_beta * (frame_eff - 0.5)

                    final_score = hybrid_score * (1.0 + boost)

            summaries.append(
                ProcedureSummary(
                    id=p.id,
                    name=p.name,
                    domain=p.domain,
                    description=p.description,
                    activation_count=p.activation_count or 0,
                    effectiveness=effectiveness,
                    score=final_score,
                    kind=p.kind,
                    # F079 P1: carry body fields for the pull path (recall_deep).
                    # `p` is the full ORM row already in scope — no extra query.
                    core_patterns=list(p.core_patterns or []),
                    implementation_notes=list(p.implementation_notes or []),
                )
            )

        # Re-sort by final_score descending (boost may have changed order)
        summaries.sort(key=lambda s: s.score or 0.0, reverse=True)
        return summaries

    # ------------------------------------------------------------------
    # find_similar_for_selection() — §14 cosine leg (codex P2, PR #495)
    # ------------------------------------------------------------------

    async def find_similar_for_selection(
        self,
        query: str,
        limit: int = 10,
        session: AsyncSession | None = None,
        *,
        cards_only: bool = False,
    ) -> list[ProcedureSummary]:
        """Raw-cosine nearest-neighbor probe for §14 procedure selection.

        Same rationale as FactManager.find_similar_for_dedup (audit S1):
        ``search()`` returns normalized RRF scores that encode RANK, not
        closeness — the nearest procedure scores ~0.95 for ANY query, so a
        relevance floor compared against it never filters. The §14 cosine
        leg needs a score a floor can be calibrated against; this returns
        ``score`` = raw cosine similarity. Active procedures only.

        Probes ONE population: how-to procedures by default, strategy cards
        alone with ``cards_only`` (Reasoning Maps L1) — never both, so neither
        can use up the other's LIMIT window.

        Returns [] when embeddings are unavailable or the embed fails —
        the §14 ladder then simply leaves the remaining slots empty.
        """
        if session is None:
            async with self.db.session() as session:
                return await self._find_similar_for_selection(query, limit, session, cards_only)
        return await self._find_similar_for_selection(query, limit, session, cards_only)

    async def _find_similar_for_selection(
        self,
        query: str,
        limit: int,
        session: AsyncSession,
        cards_only: bool = False,
    ) -> list[ProcedureSummary]:
        return await self._nearest_summaries(
            query,
            "(kind IS NOT DISTINCT FROM :strategy_kind) = :cards_only AND kind IS DISTINCT FROM :job_lesson_kind",
            {"strategy_kind": STRATEGY_CARD_KIND, "cards_only": cards_only, "job_lesson_kind": JOB_LESSON_KIND},
            limit,
            session,
            probe="procedure selection probe",
        )

    async def _nearest_summaries(
        self,
        query: str,
        where: str,
        params: dict,
        limit: int,
        session: AsyncSession,
        *,
        probe: str,
        embedding: list[float] | None = None,
    ) -> list[ProcedureSummary]:
        """Raw-cosine nearest active procedures of this agent to ``query`` that also match ``where``.

        The one cosine probe find_similar_for_selection, find_similar_job_lessons and the
        create-time dedupe run: ``where`` is the caller's fixed SQL condition (code, never
        input) with its binds in ``params``; ``score`` is the raw cosine. An ``embedding``
        already computed is used instead of embedding ``query``. Returns [] when embeddings
        are unavailable or the embed fails (logged as a failure of ``probe``).
        """
        if embedding is None:
            if not self.embeddings:
                return []
            try:
                embedding = await self.embeddings.embed(query)
            except Exception:
                logger.warning("Embedding generation failed for %s", probe)
                return []

        embedding_str = "[" + ",".join(str(float(v)) for v in embedding) + "]"
        # Same filtered-ANN horizon widening as the fact probes (codex P2):
        # agent_id/active and ``where`` are post-applied to the approximate walk.
        from nous.heart.search import set_local_ef_search
        await set_local_ef_search(session, 100)
        rows = (await session.execute(text(f"""
            SELECT id, 1 - (embedding <=> CAST(:embedding AS vector)) AS similarity
            FROM heart.procedures
            WHERE agent_id = :agent_id
              AND active = true
              AND embedding IS NOT NULL
              AND {where}
            ORDER BY embedding <=> CAST(:embedding AS vector)
            LIMIT :limit
        """), {
            **params,
            "embedding": embedding_str,
            "agent_id": self.agent_id,
            "limit": limit,
        })).all()
        if not rows:
            return []

        ids = [r.id for r in rows]
        sims = {r.id: float(r.similarity) for r in rows}
        proc_result = await session.execute(select(Procedure).where(Procedure.id.in_(ids)))
        procedures = {p.id: p for p in proc_result.scalars().all()}

        return [
            ProcedureSummary(
                id=p.id,
                name=p.name,
                domain=p.domain,
                description=p.description,
                activation_count=p.activation_count or 0,
                effectiveness=self._compute_effectiveness(p),
                score=sims.get(p.id),
                kind=p.kind,
                core_patterns=list(p.core_patterns or []),
                implementation_notes=list(p.implementation_notes or []),
            )
            for pid in ids
            if (p := procedures.get(pid)) is not None
        ]

    # ------------------------------------------------------------------
    # Job lessons — read per job (runtime_metadata.job.schedule_id)
    # ------------------------------------------------------------------

    async def list_job_lessons(
        self,
        schedule_id: str | None = None,
        *,
        active: bool | None = True,
        limit: int | None = None,
        session: AsyncSession | None = None,
    ) -> list[JobLesson]:
        """This agent's job lessons, newest first.

        One job's with ``schedule_id``, every job's without. ``active`` keeps the
        active (True) or the retired (False) ones; None keeps both. ``limit`` keeps
        the newest N (applied in SQL); None keeps all.
        """
        if session is None:
            async with self.db.session() as session:
                return await self._list_job_lessons(schedule_id, active, limit, session)
        return await self._list_job_lessons(schedule_id, active, limit, session)

    async def _list_job_lessons(
        self, schedule_id: str | None, active: bool | None, limit: int | None, session: AsyncSession
    ) -> list[JobLesson]:
        query = (
            select(Procedure)
            .options(defer(Procedure.embedding))
            .where(Procedure.agent_id == self.agent_id)
            .where(Procedure.kind == JOB_LESSON_KIND)
        )
        if schedule_id is not None:
            query = query.where(Procedure.runtime_metadata["job"]["schedule_id"].astext == schedule_id)
        if active is not None:
            query = query.where(Procedure.active == active)
        query = query.order_by(Procedure.created_at.desc(), Procedure.id)
        if limit is not None:
            query = query.limit(limit)
        result = await session.execute(query)
        return [lesson for row in result.scalars().all() if (lesson := self._to_job_lesson(row)) is not None]

    async def find_similar_job_lessons(
        self,
        query: str,
        schedule_id: str,
        limit: int = 3,
        session: AsyncSession | None = None,
    ) -> list[ProcedureSummary]:
        """Raw-cosine nearest active job lessons of one job (``schedule_id``) to ``query``.

        Scored as find_similar_for_selection scores (``score`` = raw cosine), over
        that job's lessons alone, so neither another job's lessons nor how-to
        procedures use up the LIMIT window. Returns [] when embeddings are
        unavailable or the embed fails.
        """
        if session is None:
            async with self.db.session() as session:
                return await self._find_similar_job_lessons(query, schedule_id, limit, session)
        return await self._find_similar_job_lessons(query, schedule_id, limit, session)

    async def _find_similar_job_lessons(
        self, query: str, schedule_id: str, limit: int, session: AsyncSession
    ) -> list[ProcedureSummary]:
        return await self._nearest_summaries(
            query,
            "kind = :job_lesson_kind AND runtime_metadata->'job'->>'schedule_id' = :schedule_id",
            {"job_lesson_kind": JOB_LESSON_KIND, "schedule_id": schedule_id},
            limit,
            session,
            probe="job lesson probe",
        )

    async def similarities(
        self,
        query: str,
        ids: list[UUID],
        session: AsyncSession | None = None,
    ) -> dict[UUID, float]:
        """Cosine of each of this agent's procedures in ``ids`` to ``query``.

        The raw cosine find_similar_for_selection scores with, for given rows
        instead of the nearest ones. A row of another agent, without an
        embedding, with a zero-length one, or not found is absent; so is every
        row when there is no embedding provider, the embed fails, or the query's
        vector has zero length.
        """
        if not ids or not self.embeddings:
            return {}
        try:
            vector = await self.embeddings.embed(query)
        except Exception:
            logger.warning("Embedding generation failed for procedure similarities")
            return {}
        # A zero-length vector has no direction, so no cosine: pgvector's is NaN,
        # where cosine_similarity would say 0.0. Neither such a query nor such a
        # row gets one.
        if not any(vector):
            return {}
        stmt = (
            select(Procedure.id, Procedure.embedding)
            .where(Procedure.agent_id == self.agent_id)
            .where(Procedure.id.in_(ids))
            .where(Procedure.embedding.is_not(None))
        )
        if session is None:
            async with self.db.session() as session:
                rows = (await session.execute(stmt)).all()
        else:
            rows = (await session.execute(stmt)).all()
        return {row.id: cosine_similarity(vector, list(row.embedding)) for row in rows if any(row.embedding)}

    # ------------------------------------------------------------------
    # get_evolution_candidates() — F037 Part 3
    # ------------------------------------------------------------------

    async def get_evolution_candidates(
        self, session: AsyncSession | None = None
    ) -> list[EvolutionCandidate]:
        """Return procedures that should be rewritten, retired, or investigated.

        Categories:
        - 'retire': effectiveness < 0.3 AND activation_count >= 10
        - 'rewrite': effectiveness < 0.5 AND activation_count >= 15
        - 'investigate': activation_count >= 30 but effectiveness < 0.6
        - 'star': effectiveness >= 0.85 AND activation_count >= 10 (candidates for templates)

        A procedure can match multiple categories; the highest-priority one wins
        (retire > rewrite > investigate > star).
        """
        if session is None:
            async with self.db.session() as session:
                return await self._get_evolution_candidates(session)
        return await self._get_evolution_candidates(session)

    async def _get_evolution_candidates(self, session: AsyncSession) -> list[EvolutionCandidate]:
        result = await session.execute(
            select(Procedure)
            .where(Procedure.agent_id == self.agent_id)
            .where(Procedure.active == True)  # noqa: E712
            .where(Procedure.kind.is_distinct_from(JOB_LESSON_KIND))
        )
        procedures = list(result.scalars().all())

        candidates: list[EvolutionCandidate] = []
        for p in procedures:
            eff = self._compute_effectiveness(p)
            if eff is None:
                continue  # No outcome data yet
            activation_count = p.activation_count or 0

            if eff < 0.3 and activation_count >= 10:
                candidates.append(EvolutionCandidate(
                    id=p.id,
                    name=p.name,
                    category="retire",
                    effectiveness=eff,
                    activation_count=activation_count,
                    reason=f"Effectiveness {eff:.2f} below retirement threshold (0.3) with {activation_count} activations",
                ))
            elif eff < 0.5 and activation_count >= 15:
                candidates.append(EvolutionCandidate(
                    id=p.id,
                    name=p.name,
                    category="rewrite",
                    effectiveness=eff,
                    activation_count=activation_count,
                    reason=f"Effectiveness {eff:.2f} below rewrite threshold (0.5) with {activation_count} activations",
                ))
            elif activation_count >= 30 and eff < 0.6:
                candidates.append(EvolutionCandidate(
                    id=p.id,
                    name=p.name,
                    category="investigate",
                    effectiveness=eff,
                    activation_count=activation_count,
                    reason=f"High activation count ({activation_count}) but effectiveness {eff:.2f} below 0.6 — may be declining",
                ))
            elif eff >= 0.85 and activation_count >= 10:
                candidates.append(EvolutionCandidate(
                    id=p.id,
                    name=p.name,
                    category="star",
                    effectiveness=eff,
                    activation_count=activation_count,
                    reason=f"Effectiveness {eff:.2f} with {activation_count} activations — candidate for template",
                ))

        return candidates

    # ------------------------------------------------------------------
    # get_effectiveness() — F037 diagnostic helper
    # ------------------------------------------------------------------

    async def get_effectiveness(
        self, procedure_id: UUID, session: AsyncSession | None = None
    ) -> float | None:
        """Return Laplace-smoothed effectiveness for a procedure, or None if no outcome data."""
        if session is None:
            async with self.db.session() as session:
                return await self._get_effectiveness(procedure_id, session)
        return await self._get_effectiveness(procedure_id, session)

    async def _get_effectiveness(self, procedure_id: UUID, session: AsyncSession) -> float | None:
        procedure = await self._get_procedure_orm(procedure_id, session)
        if procedure is None:
            return None
        return self._compute_effectiveness(procedure)

    # ------------------------------------------------------------------
    # list_all() — F021 dashboard browse mode
    # ------------------------------------------------------------------

    async def list_all(
        self,
        limit: int = 50,
        offset: int = 0,
        domain: str | None = None,
        active_only: bool = True,
        min_activations: int | None = None,
        session: AsyncSession | None = None,
        *,
        include_strategy_cards: bool = False,
        include_job_lessons: bool = False,
    ) -> tuple[list[ProcedureSummary], int]:
        """Paginated procedure list with filters (F021).

        Strategy cards (Reasoning Maps L1) are left out of both the rows and the
        total unless ``include_strategy_cards``: the Procedure Catalog and the
        Critic's skill menu list how-to procedures, and a card must never use up
        their row windows. Job lessons are left out the same way unless
        ``include_job_lessons``. Only the dashboard browse asks for all of them.
        """
        if session is None:
            async with self.db.session() as session:
                return await self._list_all(
                    limit, offset, domain, active_only, min_activations, session,
                    include_strategy_cards, include_job_lessons,
                )
        return await self._list_all(
            limit, offset, domain, active_only, min_activations, session,
            include_strategy_cards, include_job_lessons,
        )

    async def _list_all(
        self, limit, offset, domain, active_only, min_activations, session, include_strategy_cards=False,
        include_job_lessons=False,
    ):
        from sqlalchemy import func as sa_func
        conditions = [Procedure.agent_id == self.agent_id]
        if not include_strategy_cards:
            conditions.append(Procedure.kind.is_distinct_from(STRATEGY_CARD_KIND))
        if not include_job_lessons:
            conditions.append(Procedure.kind.is_distinct_from(JOB_LESSON_KIND))
        if active_only:
            conditions.append(Procedure.active == True)  # noqa: E712
        if domain:
            conditions.append(Procedure.domain == domain)
        if min_activations is not None:
            conditions.append(Procedure.activation_count >= min_activations)

        count_q = select(sa_func.count()).select_from(Procedure).where(*conditions)
        total = (await session.execute(count_q)).scalar() or 0

        # Secondary sort by id: created_at can tie (NOW() is txn-start time, so
        # same-transaction batch inserts share it) — without a tiebreaker the row order
        # is non-deterministic, which would bust the static-tier Procedure Catalog cache
        # turn-to-turn (F079 §8.2).
        q = (select(Procedure).where(*conditions)
             .order_by(Procedure.created_at.desc(), Procedure.id).limit(limit).offset(offset))
        result = await session.execute(q)
        procs = list(result.scalars().all())

        # ProcedureSummary fields: id, name, domain, description, activation_count, effectiveness, score
        # NOTE: No success_count/failure_count fields. Compute effectiveness from counts.
        summaries = [
            ProcedureSummary(
                id=p.id, name=p.name, domain=p.domain,
                description=p.description,
                activation_count=p.activation_count or 0,
                effectiveness=self._compute_effectiveness(p),
                kind=p.kind,
            )
            for p in procs
        ]
        return summaries, total

    # ------------------------------------------------------------------
    # get_low_effectiveness() — F034: Heartbeat health check
    # ------------------------------------------------------------------

    async def get_low_effectiveness(
        self,
        threshold: float = 0.5,
        min_scored: int = 5,
        session: AsyncSession | None = None,
    ) -> list[ProcedureSummary]:
        """Return active procedures with effectiveness below threshold.

        Only includes procedures that have been scored at least *min_scored*
        times (success + failure >= min_scored).  The default of 5 prevents
        newly-registered procedures with 1–4 scored activations from
        generating false-positive low-effectiveness heartbeat findings.

        A procedure whose ``runtime_metadata.skip_until_scored`` is an integer
        is also left out while its scored count (cumulative, not counted from
        the fix) is below that number. This is the grace period after a fix.
        Merge the key in, don't replace the column, because strategy cards and
        skills keep other keys there:
        ``SET runtime_metadata = coalesce(runtime_metadata, '{}') || '{"skip_until_scored": 10}'``.
        Re-registering a skill rewrites ``runtime_metadata``, so set the key
        after the re-registration.
        """
        if session is None:
            async with self.db.session() as session:
                return await self._get_low_effectiveness(threshold, min_scored, session)
        return await self._get_low_effectiveness(threshold, min_scored, session)

    async def _get_low_effectiveness(
        self, threshold: float, min_scored: int, session: AsyncSession,
    ) -> list[ProcedureSummary]:
        result = await session.execute(
            select(Procedure)
            .where(Procedure.agent_id == self.agent_id)
            .where(Procedure.active == True)  # noqa: E712
        )
        procedures = list(result.scalars().all())

        low: list[ProcedureSummary] = []
        for p in procedures:
            scored = (p.success_count or 0) + (p.failure_count or 0)
            if scored < min_scored:
                continue  # insufficient data — skip to avoid FP alerts
            # runtime_metadata.skip_until_scored lets a recently-fixed procedure
            # suppress the low-effectiveness finding until enough new post-fix
            # activations have accumulated to give a fair signal.
            meta = p.runtime_metadata or {}
            skip_until = meta.get("skip_until_scored") if isinstance(meta, dict) else None
            if isinstance(skip_until, int):
                if scored < skip_until:
                    continue  # post-fix grace period — not enough new data yet
            elif skip_until is not None:
                # A hand-written value that is not a whole number: report this
                # procedure as usual. Raising here would abort the scan, and the
                # heartbeat would lose every other procedure's finding with it.
                logger.warning(
                    "Procedure %s: ignoring runtime_metadata.skip_until_scored=%r (not an integer)",
                    p.id, skip_until,
                )
            eff = self._compute_effectiveness(p)
            if eff is not None and eff < threshold:
                low.append(ProcedureSummary(
                    id=p.id,
                    name=p.name,
                    domain=p.domain,
                    description=p.description,
                    activation_count=p.activation_count or 0,
                    effectiveness=eff,
                ))
        return low

    # ------------------------------------------------------------------
    # retire()
    # ------------------------------------------------------------------

    async def retire(
        self, procedure_id: UUID, session: AsyncSession | None = None, *, reason: dict | None = None
    ) -> None:
        """Retire a procedure (set active=false).

        With ``reason``, it is kept as ``runtime_metadata["retired"]``, beside the
        row's other runtime metadata (a job lesson's job record, for one).
        """
        if session is None:
            async with self.db.session() as session:
                await self._retire(procedure_id, session, reason)
                await session.commit()
                return
        await self._retire(procedure_id, session, reason)

    async def _retire(self, procedure_id: UUID, session: AsyncSession, reason: dict | None = None) -> None:
        if reason is None:
            procedure = await self._get_procedure_orm(procedure_id, session)
        else:
            # Locked and read fresh, not the session's copy of the row: the reason is
            # added to the runtime_metadata the row holds now.
            result = await session.execute(
                select(Procedure)
                .where(Procedure.id == procedure_id)
                .where(Procedure.agent_id == self.agent_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            procedure = result.scalars().first()
        if procedure is None:
            raise ValueError(f"Procedure {procedure_id} not found")
        procedure.active = False
        if reason is not None:
            # A new dict, not an in-place update: the JSONB column only sees reassignment.
            procedure.runtime_metadata = {**(procedure.runtime_metadata or {}), "retired": reason}
        await session.flush()

    # ------------------------------------------------------------------
    # reactivate()
    # ------------------------------------------------------------------

    async def reactivate(self, procedure_id: UUID, session: AsyncSession | None = None) -> None:
        """Set an inactive procedure back to active."""
        if session is None:
            async with self.db.session() as session:
                await self._reactivate(procedure_id, session)
                await session.commit()
                return
        await self._reactivate(procedure_id, session)

    async def _reactivate(self, procedure_id: UUID, session: AsyncSession) -> None:
        procedure = await self._get_procedure_orm(procedure_id, session)
        if procedure is None:
            raise ValueError(f"Procedure {procedure_id} not found")
        if procedure.active:
            return
        # B6: an active row with the same lower(name) already exists -> flipping this
        # one active would violate the (agent_id, lower(name)) WHERE active unique index
        # (migration 058) and, uncaught, abort the whole reactivate_skills loop. Skip
        # with a warning instead (the live row already provides the capability).
        query = (
            select(Procedure.id)
            # Compared the way the index compares: lower() in the database, both sides.
            .where(func.lower(Procedure.name) == func.lower(procedure.name or ""))
            .where(Procedure.agent_id == self.agent_id)
            .where(Procedure.active == True)  # noqa: E712
            .where(Procedure.id != procedure_id)
        )
        if not is_strategy_card(procedure):
            # For a how-to procedure a strategy card is not that live row: it gives
            # the name up below. A card's row keeps the check as it was, so it is
            # skipped next to another card and never takes that card's name.
            query = query.where(Procedure.kind.is_distinct_from(STRATEGY_CARD_KIND))
        clash = await session.execute(query.limit(1))
        if clash.scalars().first() is not None:
            logger.warning(
                "Skipping reactivation of %s — an active procedure with the same name exists",
                procedure.name,
            )
            return
        if not is_strategy_card(procedure):
            # Only a how-to row makes a card give its name up.
            await self._take_name_from_card(procedure.name, session)
        procedure.active = True
        await session.flush()

    # ------------------------------------------------------------------
    # reembed_all() — issue #197 backfill
    # ------------------------------------------------------------------

    async def reembed_all(self, session: AsyncSession | None = None) -> int:
        """Recompute embeddings for all active procedures using expanded embed_text.

        Use after changing the embedding formula to backfill existing records.
        Returns the number of procedures re-embedded.
        """
        if session is None:
            async with self.db.session() as session:
                count = await self._reembed_all(session)
                await session.commit()
                return count
        return await self._reembed_all(session)

    async def _reembed_all(self, session: AsyncSession) -> int:
        if not self.embeddings:
            return 0

        result = await session.execute(
            select(Procedure)
            .where(Procedure.agent_id == self.agent_id)
            .where(Procedure.active == True)  # noqa: E712
        )
        procedures = list(result.scalars().all())

        count = 0
        for proc in procedures:
            embed_text = _build_embed_text(
                proc.name, proc.description, proc.core_patterns,
                proc.goals, proc.core_tools, proc.core_concepts,
                proc.implementation_notes,
            )
            try:
                proc.embedding = await self.embeddings.embed(embed_text)
                count += 1
            except Exception:
                logger.warning("Re-embed failed for procedure %s", proc.id)

        await session.flush()
        return count

    # ------------------------------------------------------------------
    # list_inactive_skills()
    # ------------------------------------------------------------------

    async def list_inactive_skills(self, session: AsyncSession | None = None) -> list[ProcedureDetail]:
        """List inactive procedures tagged as 'skill'."""
        if session is None:
            async with self.db.session() as session:
                return await self._list_inactive_skills(session)
        return await self._list_inactive_skills(session)

    async def _list_inactive_skills(self, session: AsyncSession) -> list[ProcedureDetail]:
        result = await session.execute(
            select(Procedure)
            .where(Procedure.agent_id == self.agent_id)
            .where(Procedure.active == False)  # noqa: E712
            .where(Procedure.tags.contains(["skill"]))
            # Dedup Phase 0: never resurrect a deliberately-consolidated duplicate.
            # A superseded row was archived because its capability now lives in the
            # canonical procedure; reactivate_skills must not un-archive it.
            .where(Procedure.superseded_by.is_(None))
            # F081: never resurrect a deliberately ARCHIVED skill either (e.g. a
            # source-gone stub archived by the re-import). archived_at is set on
            # any intentional archival; only env-inactive skills (archived_at NULL)
            # are eligible for requirements-based reactivation.
            .where(Procedure.archived_at.is_(None))
        )
        return [self._to_detail(p) for p in result.scalars().all()]

    async def is_name_superseded(self, name: str, session: AsyncSession | None = None) -> bool:
        """True if an archived (consolidated) procedure with this exact name exists.

        Dedup Phase 0: the bootstrap re-import path checks this before recreating a
        skill from disk. A superseded row means the capability was merged into a
        canonical procedure — recreating it would resurrect the duplicate.
        """
        if session is None:
            async with self.db.session() as session:
                return await self._is_name_superseded(name, session)
        return await self._is_name_superseded(name, session)

    async def _is_name_superseded(self, name: str, session: AsyncSession) -> bool:
        result = await session.execute(
            select(Procedure.id)
            # Compared the way the index compares: lower() in the database, both sides.
            .where(func.lower(Procedure.name) == func.lower(name))
            .where(Procedure.agent_id == self.agent_id)
            .where(Procedure.superseded_by.isnot(None))
            .where(Procedure.kind.is_distinct_from(STRATEGY_CARD_KIND))
            .where(Procedure.kind.is_distinct_from(JOB_LESSON_KIND))
            .limit(1)
        )
        return result.scalars().first() is not None

    async def get_by_name(self, name: str, session: AsyncSession | None = None) -> ProcedureDetail | None:
        """Fetch the active how-to procedure with this name (never a strategy card)."""
        if session is None:
            async with self.db.session() as session:
                return await self._get_by_name(name, session)
        return await self._get_by_name(name, session)

    async def _get_by_name(self, name: str, session: AsyncSession) -> ProcedureDetail | None:
        # Deterministic tiebreak: same `name` can have multiple active rows (name-dedup is
        # bypassable — see procedure-subsystem-audit). Without ORDER BY, LIMIT 1 returns an
        # arbitrary row. Order by created_at DESC, id — a STABLE, non-volatile key (NOT
        # activation_count, which changes per use and would make the resolved row drift, and
        # whose NULLs sort first under DESC). This is the SAME ordering the Procedure Catalog
        # uses to pick its displayed winner (list_all is created_at desc, id), so the catalog
        # entry and the loaded body always agree. Case-insensitive match (B6) — pairs with the
        # (agent_id, lower(name)) WHERE active unique index so a casing variant still resolves.
        result = await session.execute(
            select(Procedure)
            # Compared the way the index compares: lower() in the database, both sides.
            .where(func.lower(Procedure.name) == func.lower(name))
            .where(Procedure.agent_id == self.agent_id)
            .where(Procedure.active == True)  # noqa: E712
            # Every caller means a skill: a strategy card with this name is not one.
            .where(Procedure.kind.is_distinct_from(STRATEGY_CARD_KIND))
            .where(Procedure.kind.is_distinct_from(JOB_LESSON_KIND))
            .order_by(Procedure.created_at.desc(), Procedure.id)
            .limit(1)
        )
        procedure = result.scalars().first()
        if procedure is None:
            return None
        return self._to_detail(procedure)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _get_procedure_orm(self, procedure_id: UUID, session: AsyncSession) -> Procedure | None:
        """Fetch Procedure ORM scoped by agent_id."""
        result = await session.execute(
            select(Procedure).where(Procedure.id == procedure_id).where(Procedure.agent_id == self.agent_id)
        )
        return result.scalars().first()

    async def _locked_procedure(self, procedure_id: UUID, session: AsyncSession) -> Procedure | None:
        """This agent's procedure, row-locked and read fresh (not the session's copy), or None."""
        result = await session.execute(
            select(Procedure)
            .where(Procedure.id == procedure_id)
            .where(Procedure.agent_id == self.agent_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return result.scalars().first()

    def _record_version(
        self,
        procedure: Procedure,
        session: AsyncSession,
        change_source: ProcedureChangeSource,
        *,
        reason: str | None = None,
        input: ProcedureInput | None = None,
    ) -> None:
        """Add the body ``procedure`` holds now as its version ``procedure.version``.

        ``input`` is the create request that wrote it, whose idempotency key is kept with it.
        """
        key = input.idempotency_key if input is not None else None
        session.add(ProcedureVersion(
            agent_id=self.agent_id,
            procedure_id=procedure.id,
            version=procedure.version,
            name=procedure.name,
            domain=procedure.domain,
            description=procedure.description,
            **{field: getattr(procedure, field) for field in _LIST_FIELDS},
            kind=procedure.kind,
            runtime_metadata=procedure.runtime_metadata,
            change_source=change_source,
            reason=reason,
            idempotency_key=key,
            request_fingerprint=_fingerprint(input) if key is not None else None,
        ))

    async def _bump_version(
        self,
        procedure: Procedure,
        session: AsyncSession,
        change_source: ProcedureChangeSource,
        *,
        reason: str | None = None,
        input: ProcedureInput | None = None,
    ) -> None:
        """Make the body ``procedure`` holds now its next version, recorded; the row must be locked."""
        procedure.version = (procedure.version or 1) + 1
        await session.flush()
        self._record_version(procedure, session, change_source, reason=reason, input=input)
        await session.flush()

    async def _reembed(self, procedure: Procedure) -> None:
        """Re-embed the restored body ``procedure`` holds; NULL on failure, as _update_body stores it."""
        if self.embeddings:
            procedure.embedding = await self._embed_with_retry(_build_embed_text(
                procedure.name, procedure.description, procedure.core_patterns,
                procedure.goals, procedure.core_tools, procedure.core_concepts,
                procedure.implementation_notes,
            ))

    async def _lock_creates(self, session: AsyncSession) -> None:
        """Serialize this agent's deduped and keyed creates until the transaction ends.

        Two learners storing the same lesson at once would both miss each other's row
        and insert twice. Postgres-only; the lock timeout is lifted for the wait, as in
        FactManager.learn, because the holder may be waiting on an embedding call.
        """
        if not _is_postgres(session):
            return
        lifted = bool(self.db.lock_timeout_seconds)
        if lifted:
            await session.execute(text("SET LOCAL lock_timeout = 0"))
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
            {"k": f"procedure_store:{self.agent_id}"},
        )
        if lifted:
            await session.execute(text("SET LOCAL lock_timeout = DEFAULT"))

    async def _replay(self, input: ProcedureInput, session: AsyncSession) -> ProcedureDetail | None:
        """The procedure an earlier request with this idempotency key wrote, or None for a new key."""
        prior = (await session.execute(
            select(ProcedureVersion)
            .where(ProcedureVersion.agent_id == self.agent_id)
            .where(ProcedureVersion.idempotency_key == input.idempotency_key)
        )).scalars().first()
        if prior is None:
            return None
        if prior.request_fingerprint != _fingerprint(input):
            raise ProcedureConflictError(
                f"Idempotency key {input.idempotency_key!r} was used for a different request "
                f"(procedure {prior.procedure_id})"
            )
        procedure = await self._get_procedure_orm(prior.procedure_id, session)
        if procedure is None:
            raise ValueError(f"Procedure {prior.procedure_id} not found")
        if not procedure.active:
            logger.warning("Procedure store replayed idempotency key %r to %s, which is retired",
                           input.idempotency_key, procedure.id)
        logger.info("Procedure store replayed idempotency key %r: procedure %s", input.idempotency_key, procedure.id)
        return self._to_detail(procedure)

    async def _same_name_procedure(self, name: str, session: AsyncSession) -> UUID | None:
        """The active auto-learned procedure (not a skill) whose normalized name is ``name``'s, or None."""
        result = await session.execute(
            select(Procedure.id, Procedure.name, Procedure.tags)
            .where(Procedure.agent_id == self.agent_id)
            .where(Procedure.active == True)  # noqa: E712
            .where(Procedure.kind.is_distinct_from(STRATEGY_CARD_KIND))
            .where(Procedure.kind.is_distinct_from(JOB_LESSON_KIND))
            .order_by(Procedure.created_at, Procedure.id)
        )
        wanted = _normalize_name(name)
        return next(
            (
                row.id for row in result.all()
                if _is_learned(row.tags) and not _is_skill(row.tags) and _normalize_name(row.name) == wanted
            ),
            None,
        )

    async def _merge_target(
        self, input: ProcedureInput, embedding: list[float] | None, session: AsyncSession, *, log: bool = True
    ) -> tuple[UUID, str] | None:
        """The procedure a deduped input merges into, with the rule that chose it, or None.

        The active auto-learned procedure of the same normalized name, if the semantic probe
        agrees (cosine at or above the threshold; with no embedding to compare, the name
        alone); else the nearest one at or above the threshold. ``log`` off for a dry run.
        """
        postgres = _is_postgres(session)
        if (same_name := await self._same_name_procedure(input.name, session)) is not None:
            if embedding is None or not postgres:
                return same_name, "name lock: same normalized name"
            agree = await self._nearest_summaries(
                "", f"{LEARNED_WHERE} AND id = :target", {**LEARNED_PARAMS, "target": same_name}, 1, session,
                probe="procedure dedupe name check", embedding=embedding,
            )
            score = agree[0].score if agree else None
            if score is not None and score >= self._dedupe_threshold:
                return same_name, f"name lock: same normalized name, cosine {score:.3f}"
            if log:
                logger.info("Procedure dedupe: %r is named like %s but not alike (cosine %s)", input.name,
                            same_name, f"{score:.3f}" if score is not None else "n/a")
        if not postgres:
            return None
        if embedding is None:
            if log:
                logger.warning(
                    "Procedure dedupe: no embedding for %r, so no near-duplicate check; stored as new", input.name
                )
            return None
        nearest = await self._nearest_summaries(
            "", LEARNED_WHERE, LEARNED_PARAMS, 1, session, probe="procedure dedupe probe", embedding=embedding,
        )
        if nearest and nearest[0].score is not None and nearest[0].score >= self._dedupe_threshold:
            return nearest[0].id, f"semantic: cosine {nearest[0].score:.3f}"
        return None

    def _merged_body(self, procedure: Procedure, input: ProcedureInput) -> dict | None:
        """The body fields a merge of ``input`` writes to ``procedure``; None when it adds nothing.

        The list fields are unioned, except the notes: only evidence not cited yet is
        appended, up to the cap (as the learner's covered path appends it). The incoming
        description is taken only by a procedure without one.
        """
        notes = list(procedure.implementation_notes or [])
        body: dict = {
            field: _union(getattr(procedure, field), getattr(input, field))
            for field in _LIST_FIELDS if field != "implementation_notes"
        }
        body["implementation_notes"] = [*notes, *new_evidence_notes(notes, input.implementation_notes)]
        body["description"] = procedure.description or input.description
        unchanged = body["description"] == procedure.description and all(
            body[field] == list(getattr(procedure, field) or []) for field in _LIST_FIELDS
        )
        return None if unchanged else body

    async def _prepare_merge(
        self, input: ProcedureInput, embedding: list[float] | None, session: AsyncSession
    ) -> tuple[UUID, int, list[float]] | None:
        """Embed, before the create lock, the body a merge of ``input`` would write; None if there is none.

        Returns the target, the version the body was built from and its embedding: the merge
        under the lock uses the embedding only for that target at that version.
        """
        if self.embeddings is None or embedding is None:
            return None
        target = await self._merge_target(input, embedding, session, log=False)
        if target is None:
            return None
        procedure = (await session.execute(
            select(Procedure).where(Procedure.id == target[0]).execution_options(populate_existing=True)
        )).scalars().first()
        if procedure is None or (body := self._merged_body(procedure, input)) is None:
            return None
        merged_embedding = await self._embed_with_retry(
            _build_embed_text(
                procedure.name, body["description"], body["core_patterns"], body["goals"],
                body["core_tools"], body["core_concepts"], body["implementation_notes"],
            ),
            on_failure="the merge keeps the procedure's previous embedding",
        )
        return (procedure.id, procedure.version, merged_embedding) if merged_embedding is not None else None

    async def _merge(
        self,
        procedure_id: UUID,
        rule: str,
        input: ProcedureInput,
        session: AsyncSession,
        prepared: tuple[UUID, int, list[float]] | None = None,
    ) -> ProcedureDetail | None:
        """Merge ``input`` into the procedure as a new version (``dedupe_merge``); see _merged_body.

        The procedure keeps its name, domain, kind, runtime_metadata and stats; the version's
        reason keeps the rule, the incoming name, description and lesson. The embedding is
        ``prepared``'s, made before the lock from this version of the row; for a row that has
        moved on since, the old vector is kept (no embedding call while holding the lock).
        A merge that adds nothing writes nothing, unless it carries an idempotency key to keep.
        None when the procedure, locked, is no longer an active auto-learned how-to: the caller inserts.
        """
        procedure = await self._locked_procedure(procedure_id, session)
        if procedure is None or not procedure.active or not _in_dedupe_population(procedure):
            logger.info("Procedure dedupe: %s is no longer an active learned procedure, %r is stored as new",
                        procedure_id, input.name)
            return None
        body = self._merged_body(procedure, input)
        if body is None and input.idempotency_key is None:
            logger.info("Procedure dedupe: %r adds nothing to %s (%r) (%s)", input.name, procedure.id,
                        procedure.name, rule)
            return self._to_detail(procedure)
        if body is not None:
            for field, value in body.items():
                setattr(procedure, field, value or None)
            if prepared is not None and prepared[:2] == (procedure.id, procedure.version):
                procedure.embedding = prepared[2]
            elif self.embeddings:
                logger.info("Procedure dedupe: %s changed since its merged body was embedded; its vector is kept",
                            procedure.id)
        procedure.updated_at = datetime.now(UTC)
        lesson = next((note for note in input.implementation_notes if note.startswith("lesson: ")), None)
        reason = f"{rule}; incoming {input.name!r}: {(input.description or '')[:300]!r}"
        if lesson is not None:
            reason += f"; {lesson[:300]!r}"
        await self._bump_version(procedure, session, "dedupe_merge", reason=reason, input=input)
        await self._emit_event(session, "procedure_merged", {
            "procedure_id": str(procedure.id),
            "incoming_name": input.name,
            "reason": rule,
            "version": procedure.version,
        })
        logger.info(
            "Procedure dedupe: %r merged into %s (%r) as version %d (%s)",
            input.name, procedure.id, procedure.name, procedure.version, rule,
        )
        return self._to_detail(procedure)

    async def _take_name_from_card(self, name: str, session: AsyncSession) -> None:
        """Make an active strategy card called ``name`` give the name up.

        Called when a how-to procedure is about to hold ``name`` as an active row.
        Cards and how-to procedures share the (agent_id, lower(name)) WHERE active
        unique index, so a card holding the name would block the insert or the
        reactivation. Nothing looks a card up by its name (its source decision
        identifies it), so the card is renamed, with a suffix from its own id,
        and stays active.
        """
        result = await session.execute(
            select(Procedure)
            .where(Procedure.agent_id == self.agent_id)
            .where(Procedure.kind == STRATEGY_CARD_KIND)
            .where(Procedure.active == True)  # noqa: E712
            # Compared the way the index compares: lower() in the database, both sides.
            .where(func.lower(Procedure.name) == func.lower(name))
        )
        cards = result.scalars().all()
        for card in cards:
            card.name = await self._free_card_name(card, session)
        # Flushed here, before the caller's own write: a reactivation is an UPDATE
        # like the rename, and a flush sends UPDATEs in primary-key order.
        await session.flush()
        # Logged after the flush: only for a rename that went out.
        for card in cards:
            logger.info(
                "Strategy card %s renamed to %r: a how-to procedure takes the name %r", card.id, card.name, name
            )

    async def _free_card_name(self, card: Procedure, session: AsyncSession) -> str:
        """The name a card moves to when it gives its name up.

        ``<name> (<6 hex of its id>)``, or ``(<6 hex>-2)``, ``-3`` and so on while an
        active row of the agent holds it, compared the way the index compares. The
        name is cut so that the whole fits the column.
        """
        width = Procedure.name.type.length
        n = 1
        while True:
            tail = f" ({card.id.hex[:6]})" if n == 1 else f" ({card.id.hex[:6]}-{n})"
            moved = card.name[: width - len(tail)].rstrip() + tail
            held = await session.execute(
                select(Procedure.id)
                .where(Procedure.agent_id == self.agent_id)
                .where(Procedure.active == True)  # noqa: E712
                .where(func.lower(Procedure.name) == func.lower(moved))
                .limit(1)
            )
            if held.scalars().first() is None:
                return moved
            n += 1

    def _compute_effectiveness(self, procedure: Procedure) -> float | None:
        """P3-4: Laplace smoothing for effectiveness.

        effectiveness = (success + 1) / (success + failure + 2)
        """
        success = procedure.success_count or 0
        failure = procedure.failure_count or 0
        total = success + failure
        if total == 0:
            return None
        return (success + 1) / (success + failure + 2)

    async def _upsert_task_affinity(
        self,
        procedure_id: UUID,
        frame_type: str,
        outcome: str,
        session: AsyncSession,
    ) -> None:
        """Upsert a procedure_task_affinity row for the given frame_type (F037)."""
        now = datetime.now(UTC)
        success_inc = 1 if outcome == "success" else 0
        failure_inc = 1 if outcome == "failure" else 0

        # Try to find existing row
        result = await session.execute(
            select(ProcedureTaskAffinity)
            .where(ProcedureTaskAffinity.procedure_id == procedure_id)
            .where(ProcedureTaskAffinity.frame_type == frame_type)
            .where(ProcedureTaskAffinity.agent_id == self.agent_id)
        )
        row = result.scalars().first()

        if row is not None:
            row.activation_count += 1
            row.success_count += success_inc
            row.failure_count += failure_inc
            row.last_activated_at = now
        else:
            row = ProcedureTaskAffinity(
                procedure_id=procedure_id,
                frame_type=frame_type,
                activation_count=1,
                success_count=success_inc,
                failure_count=failure_inc,
                last_activated_at=now,
                agent_id=self.agent_id,
            )
            session.add(row)

        await session.flush()

    def _to_detail(self, procedure: Procedure) -> ProcedureDetail:
        """Convert ORM Procedure to ProcedureDetail DTO."""
        return ProcedureDetail(
            id=procedure.id,
            agent_id=procedure.agent_id,
            name=procedure.name,
            domain=procedure.domain,
            description=procedure.description,
            goals=procedure.goals or [],
            core_patterns=procedure.core_patterns or [],
            core_tools=procedure.core_tools or [],
            core_concepts=procedure.core_concepts or [],
            implementation_notes=procedure.implementation_notes or [],
            activation_count=procedure.activation_count or 0,
            success_count=procedure.success_count or 0,
            failure_count=procedure.failure_count or 0,
            neutral_count=procedure.neutral_count or 0,
            last_activated=procedure.last_activated,
            effectiveness=self._compute_effectiveness(procedure),
            related_procedures=procedure.related_procedures or [],
            censor_ids=procedure.censor_ids or [],
            tags=procedure.tags or [],
            active=procedure.active if procedure.active is not None else True,
            created_at=procedure.created_at,
            kind=procedure.kind,
            version=procedure.version or 1,
        )

    def _to_job_lesson(self, procedure: Procedure) -> JobLesson | None:
        """Convert a job-lesson ORM row to a JobLesson; None for a row without created_at or a malformed job record."""
        if procedure.created_at is None:
            return None
        metadata = procedure.runtime_metadata
        if not isinstance(metadata, dict):
            return None
        job = metadata.get("job")
        if not isinstance(job, dict):
            return None
        prefix = "lesson: "
        notes = procedure.implementation_notes or []
        lesson = next((n.removeprefix(prefix) for n in notes if n.startswith(prefix)), "")
        return JobLesson(
            id=procedure.id,
            schedule_id=job.get("schedule_id") or "",
            name=procedure.name,
            cue=(procedure.goals or [""])[0],
            lesson=lesson,
            learned_from_run=job.get("learned_from_run"),
            baseline=job.get("baseline"),
            retired=metadata.get("retired"),
            active=procedure.active if procedure.active is not None else True,
            created_at=procedure.created_at,
        )
