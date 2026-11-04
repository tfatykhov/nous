"""Embed-text modes for procedures (evidence notes) and censors (the regex), and the re-embed script."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from conftest import MockEmbeddingProvider
from sqlalchemy import text

from nous.heart import CensorInput, Heart, ProcedureInput
from nous.heart.procedures import _build_embed_text
from scripts.ops import reembed_write_quality

_EVIDENCE = "evidence: 0b7c1f0e-5d1c-4c55-9a43-2f1f7f0a9b1e (success) — the retry fixed the flaky upload"
_NOTES = ["lesson: retry an upload once before reporting it failed", _EVIDENCE, "Run the upload with --retries 1"]


def _text(notes: list[str], evidence: str | None = None) -> str:
    fields = ("Retry uploads", "When an upload fails", ["retry once"], ["upload ok"], ["bash"], ["upload"], notes)
    return _build_embed_text(*fields) if evidence is None else _build_embed_text(*fields, evidence)


# ---------------------------------------------------------------------------
# Procedure evidence notes
# ---------------------------------------------------------------------------


def test_procedure_embed_text_modes():
    keep = _text(_NOTES, "keep")
    assert keep == _text(_NOTES)  # the default is today's text
    assert keep == (
        "Retry uploads When an upload fails retry once upload ok bash upload "
        "lesson: retry an upload once before reporting it failed " + _EVIDENCE + " Run the upload with --retries 1"
    )
    # Only evidence notes change; the lesson and a body note stay.
    assert _text(_NOTES, "drop") == _text([_NOTES[0], _NOTES[2]])
    assert _text(_NOTES, "observed") == _text([_NOTES[0], "the retry fixed the flaky upload", _NOTES[2]])
    # An evidence note with no observed part has nothing to keep.
    assert _text(["evidence: 0b7c1f0e (success)"], "observed") == _text([])
    with pytest.raises(ValueError):
        _text(_NOTES, "none")


class _Recorder(MockEmbeddingProvider):
    """Records every embedded text. The vector depends on the first word only, so every text of one
    procedure (the name leads) is alike: a second store of the same name merges."""

    def __init__(self) -> None:
        self.texts: list[str] = []

    async def embed(self, text: str, fail_fast: bool = False) -> list[float]:
        self.texts.append(text)
        return await super().embed(text.split(" ", 1)[0])


def _heart(db, settings, embedder, **overrides) -> Heart:
    scoped = settings.model_copy(update={"agent_id": f"test-w2-{uuid.uuid4().hex[:12]}", **overrides})
    return Heart(db, scoped, embedding_provider=embedder)


def _learned(name: str, evidence_id: str, extra: str = "") -> ProcedureInput:
    return ProcedureInput(
        name=name, description="When an upload fails" + extra, goals=["retry once"], core_tools=["bash"],
        implementation_notes=[_NOTES[0], f"evidence: {evidence_id} (success) — the retry worked", _NOTES[2]],
        tags=["auto:decision"],
    )


@pytest.mark.postgres_only
async def test_every_procedure_embed_drops_evidence_in_drop_mode(db, settings, session):
    """store, the dedupe merge, update_body, restore and reembed_all all embed the same mode's text."""
    embedder = _Recorder()
    heart = _heart(db, settings, embedder, procedure_embed_evidence="drop")
    name = f"w2proc-{uuid.uuid4().hex[:8]} retry uploads"

    first = await heart.store_procedure(_learned(name, "id-one"), session=session)  # store
    merged = await heart.store_procedure(_learned(name, "id-two"), session=session)  # store + merged body
    assert merged.id == first.id and merged.version == 2
    assert any(note.startswith("evidence: id-two") for note in merged.implementation_notes)
    await heart.update_procedure_body(first.id, _learned(name, "id-three", " now"), session=session)  # update_body
    await heart.restore_procedure_version(first.id, 1, session=session)  # restore
    assert await heart.reembed_procedures(session=session) == 1  # reembed_all

    assert len(embedder.texts) == 6
    assert all(_NOTES[0] in t and "evidence: " not in t for t in embedder.texts), embedder.texts


async def test_covered_probe_embeds_the_text_the_store_embeds():
    """The learner's probe compares a new lesson with stored rows, so it must drop what they drop."""
    from nous.handlers.procedure_learner import ProcedureLearner

    settings = MagicMock()
    settings.procedure_embed_evidence = "drop"
    heart = AsyncMock()
    heart.store_procedure.return_value = MagicMock(version=1)
    learner = ProcedureLearner(AsyncMock(), heart, AsyncMock(), settings, AsyncMock())
    queries: list[str] = []

    async def find_neighbours(query: str) -> list:
        queries.append(query)
        return []

    lesson = _learned("Retry uploads", "id-one")
    assert await learner._store_unless_covered(lesson, find_neighbours=find_neighbours) is True

    fields = (lesson.name, lesson.description, lesson.core_patterns, lesson.goals, lesson.core_tools,
              lesson.core_concepts, lesson.implementation_notes)
    assert "evidence: " not in queries[0]
    assert queries == [_build_embed_text(*fields, "drop")]


# ---------------------------------------------------------------------------
# Censor embed text
# ---------------------------------------------------------------------------


def test_censor_embed_text_modes():
    from nous.heart.censors import _censor_embed_text

    pattern, reason = r"\brm -rf\b", "never wipe a disk"
    assert _censor_embed_text(pattern, reason, "ask first", "legacy") == r"\brm -rf\b never wipe a disk"
    assert _censor_embed_text(pattern, reason, "ask first", "reason") == "never wipe a disk ask first"
    assert _censor_embed_text(pattern, reason, None, "reason") == "never wipe a disk"
    with pytest.raises(ValueError):
        _censor_embed_text("x", "y", None, "regex")


@pytest.mark.postgres_only
@pytest.mark.parametrize(("mode", "found_by"), [("reason", "prose"), ("legacy", "pattern")])
async def test_censor_is_found_by_the_text_its_mode_embeds(db, settings, session, mode, found_by):
    heart = _heart(db, settings, MockEmbeddingProvider(), censor_embed_text_mode=mode)
    reason = f"never paste a deploy key into chat {uuid.uuid4().hex}"
    # Anchored, so the keyword leg matches neither probe and only the cosine decides.
    pattern = r"^zqx-w2$"
    censor = await heart.add_censor(
        CensorInput(trigger_pattern=pattern, reason=reason, action="steer", action_instruction="redact it"),
        session=session,
    )
    probes = {"prose": f"{reason} redact it", "pattern": f"{pattern} {reason}"}

    for probe, query in probes.items():
        matches = await heart.check_censors(query, session=session)
        assert (censor.id in {m.id for m in matches}) is (probe == found_by), probe


@pytest.mark.postgres_only
@pytest.mark.parametrize(("mode", "reembeds"), [("reason", True), ("legacy", False)])
async def test_censor_action_instruction_edit_reembeds_in_reason_mode(db, settings, session, mode, reembeds):
    embedder = _Recorder()
    heart = _heart(db, settings, embedder, censor_embed_text_mode=mode)
    censor = await heart.add_censor(
        CensorInput(trigger_pattern="zqx-w2", reason="never paste a deploy key", action="steer",
                    action_instruction="redact it"),
        session=session,
    )
    embedded = len(embedder.texts)

    await heart.censors.update(censor.id, action_instruction="redact it and say so", session=session)

    assert len(embedder.texts) == embedded + (1 if reembeds else 0)
    if reembeds:
        assert embedder.texts[-1] == "never paste a deploy key redact it and say so"


# ---------------------------------------------------------------------------
# scripts/ops/reembed_write_quality.py
# ---------------------------------------------------------------------------


async def _vectors(session, table: str, agent_id: str) -> dict:
    rows = await session.execute(
        text(f"SELECT id, embedding::text AS e FROM heart.{table} WHERE agent_id = :a"), {"a": agent_id},
    )
    return {r.id: r.e for r in rows}


@pytest.mark.postgres_only
async def test_reembed_script_dry_run_counts_what_apply_rewrites(db, settings, session):
    legacy = _heart(db, settings, MockEmbeddingProvider())
    for i, active in enumerate((True, False)):
        censor = await legacy.add_censor(
            CensorInput(trigger_pattern=f"zqx-{i}", reason=f"reason {i}", action="steer", action_instruction="stop"),
            session=session,
        )
        if not active:
            await legacy.censors.update(censor.id, active=False, session=session)
        await legacy.store_procedure(
            _learned(f"w2 script {i}", f"id-{i}").model_copy(update={"active": active}), session=session,
        )
    before = {t: await _vectors(session, t, legacy.agent_id) for t in ("censors", "procedures")}
    new_mode = legacy.settings.model_copy(
        update={"censor_embed_text_mode": "reason", "procedure_embed_evidence": "drop"},
    )
    heart = Heart(db, new_mode, embedding_provider=MockEmbeddingProvider())

    dry = await reembed_write_quality.reembed(heart, session, censors=True, procedures=True, apply=False)
    assert dry == {"censors": 2, "procedures": 1}
    assert {t: await _vectors(session, t, legacy.agent_id) for t in before} == before

    applied = await reembed_write_quality.reembed(heart, session, censors=True, procedures=True, apply=True)
    assert applied == dry
    after = {t: await _vectors(session, t, legacy.agent_id) for t in before}
    assert all(after["censors"][cid] != vec for cid, vec in before["censors"].items())
    changed = [pid for pid, vec in before["procedures"].items() if after["procedures"][pid] != vec]
    assert len(changed) == 1  # the active one; the retired one keeps its vector
