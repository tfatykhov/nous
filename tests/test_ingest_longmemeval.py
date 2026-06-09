"""F051.5 — LongMemEval Phase 2 ingest tests.

Covers:
1. Handler instantiation with bus=None (FactExtractor, EpisodeSummarizer)
2. _session_to_transcript format conformance (matches prod handler expectation)
3. _write_qrels populates gold_ids from provenance + answer_session_ids
4. _write_qrels skips non-int answer_session_id values (TypeError + ValueError)
5. _write_qrels emits qrel with empty gold_ids when no provenance + WARNs
6. _write_qrels uses source="longmemeval" (NOT "longmemeval_s" — Phase-1 latent bug fix)
7. _default_out_qrels respects NOUS_EVAL_FIXTURES_DIR env var
8. SHA256 fail-closed: download with mismatched hash raises SystemExit
9. extract_and_store accepts explicit candidate_facts (production handle path)
10. extract_and_store falls back to summary["candidate_facts"] when not passed (ingest path)

Tests #11-12 (handler refactor backward compatibility) live in test_event_bus.py
which already exercises the full bus-driven path; if those tests pass after the
refactor, the refactor is correct.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from nous_eval.ingest_longmemeval import (
    LMEIngestStats,
    _default_out_qrels,
    _session_to_transcript,
    _write_qrels,
)


# ---------------------------------------------------------------------------
# 1. Handler instantiation with bus=None
# ---------------------------------------------------------------------------


def test_fact_extractor_constructs_with_bus_none() -> None:
    """F051.5 refactor: FactExtractor.__init__ accepts bus=None and skips subscription."""
    from nous.handlers.fact_extractor import FactExtractor

    heart = MagicMock()
    settings = MagicMock()
    extractor = FactExtractor(heart=heart, settings=settings, bus=None)
    assert extractor._bus is None


def test_episode_summarizer_constructs_with_bus_none() -> None:
    """F051.5 refactor: EpisodeSummarizer.__init__ accepts bus=None and skips subscription."""
    from nous.handlers.episode_summarizer import EpisodeSummarizer

    heart = MagicMock()
    settings = MagicMock()
    summarizer = EpisodeSummarizer(heart=heart, brain=None, settings=settings, bus=None)
    assert summarizer._bus is None


def test_fact_extractor_subscribes_when_bus_provided() -> None:
    """Backward compat: when a bus is provided, FactExtractor still subscribes."""
    from nous.handlers.fact_extractor import FactExtractor

    heart = MagicMock()
    settings = MagicMock()
    bus = MagicMock()
    extractor = FactExtractor(heart=heart, settings=settings, bus=bus)
    assert bus.on.called
    assert bus.on.call_args[0][0] == "episode_summarized"
    # The second positional arg is the bound method — it should be callable.
    assert callable(bus.on.call_args[0][1])
    assert bus.on.call_args[0][1] == extractor.handle


# ---------------------------------------------------------------------------
# 2. _session_to_transcript format conformance
# ---------------------------------------------------------------------------


def test_session_to_transcript_list_format() -> None:
    """LongMemEval session as list of turns → '\\n\\n'-joined 'role: content' lines."""
    session = [
        {"role": "user", "content": "What's the weather?"},
        {"role": "assistant", "content": "Sunny, 75F."},
    ]
    out = _session_to_transcript(session)
    assert out == "user: What's the weather?\n\nassistant: Sunny, 75F."


def test_session_to_transcript_dict_format() -> None:
    """Some LongMemEval entries wrap turns in a {'turns': [...]} dict."""
    session = {"turns": [{"role": "user", "content": "Hi"}]}
    assert _session_to_transcript(session) == "user: Hi"


def test_session_to_transcript_skips_empty_content() -> None:
    """Turns with empty content are omitted (matches prod handler's >=50 char gate)."""
    session = [
        {"role": "user", "content": "Hi"},
        {"role": "assistant", "content": ""},  # skipped
        {"role": "user", "content": "There"},
    ]
    out = _session_to_transcript(session)
    assert out == "user: Hi\n\nuser: There"


def test_session_to_transcript_handles_missing_role() -> None:
    """Missing role defaults to 'user' rather than crashing."""
    session = [{"content": "no role here"}]
    assert _session_to_transcript(session) == "user: no role here"


# ---------------------------------------------------------------------------
# 3-6. _write_qrels behavior
# ---------------------------------------------------------------------------


def _make_qrels_inputs(tmp_path: Path) -> tuple[Path, list[dict]]:
    """Build a 3-question fixture for _write_qrels tests."""
    out = tmp_path / "qrels.jsonl"
    picked = [
        {
            "question_id": "q-single",
            "question": "What was the user's preference?",
            "question_type": "single-session-preference",
            "answer_session_ids": [0],
        },
        {
            "question_id": "q-multi",
            "question": "Across sessions, what changed?",
            "question_type": "multi-session",
            "answer_session_ids": [0, 1, 2],
        },
        {
            "question_id": "q-missing",
            "question": "Question with no provenance.",
            "question_type": "temporal-reasoning",
            "answer_session_ids": [0],
        },
    ]
    return out, picked


def test_write_qrels_populates_gold_ids_from_provenance(tmp_path: Path) -> None:
    """gold_ids = (episodes ∪ facts) from sessions named in answer_session_ids.

    F051.5 hotfix: empty-gold qrels are SKIPPED. q-missing here has no
    provenance → no emitted row.
    """
    out, picked = _make_qrels_inputs(tmp_path)
    ep1, fact1, fact2 = uuid4(), uuid4(), uuid4()
    ep2, fact3 = uuid4(), uuid4()
    provenance = {
        "q-single": {0: {"episode": [ep1], "fact": [fact1, fact2]}},
        "q-multi": {
            0: {"episode": [ep1], "fact": [fact1]},
            1: {"episode": [ep2], "fact": [fact3]},
            # session 2 absent → contributes nothing
        },
        "q-missing": {},  # no provenance for any session → SKIPPED post-hotfix
    }
    _write_qrels(picked, LMEIngestStats(), provenance, out)
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    # Only 2 rows emitted (q-single + q-multi); q-missing skipped per hotfix.
    assert len(rows) == 2
    qids = {r["notes"]["question_id"] for r in rows}
    assert qids == {"q-single", "q-multi"}

    single_row = next(r for r in rows if r["notes"]["question_id"] == "q-single")
    assert set(single_row["gold_ids"]) == {str(ep1), str(fact1), str(fact2)}

    multi_row = next(r for r in rows if r["notes"]["question_id"] == "q-multi")
    assert set(multi_row["gold_ids"]) == {str(ep1), str(fact1), str(ep2), str(fact3)}


def test_write_qrels_uses_correct_source_value(tmp_path: Path) -> None:
    """F051.5 P1 (devil): source='longmemeval' (NOT 'longmemeval_s' — Phase-1 latent bug)."""
    out, picked = _make_qrels_inputs(tmp_path)
    # Need to give q-single provenance so it actually emits (post-hotfix skip).
    ep = uuid4()
    provenance = {"q-single": {0: {"episode": [ep], "fact": []}}}
    _write_qrels(picked[:1], LMEIngestStats(), provenance, out)
    row = json.loads(out.read_text(encoding="utf-8").strip())
    assert row["source"] == "longmemeval"


def test_write_qrels_handles_unmatched_answer_session_ids(tmp_path: Path, caplog) -> None:
    """Unmatched answer_session_ids are skipped with WARN; matched ones contribute.

    Cleaned upstream uses string IDs; pre-cleaned upstream used int indices.
    Loader tries both — string lookup first, then int(sid) fallback. IDs that
    match neither produce a "not found in provenance map" WARN.
    """
    out = tmp_path / "qrels.jsonl"
    picked = [{
        "question_id": "q-bad",
        "question": "?",
        "question_type": "single-session-user",
        "answer_session_ids": ["nonexistent_id", None, 0],  # unmatched + None + matched-as-int
    }]
    ep = uuid4()
    provenance = {"q-bad": {0: {"episode": [ep], "fact": []}}}
    with caplog.at_level(logging.WARNING):
        _write_qrels(picked, LMEIngestStats(), provenance, out)
    row = json.loads(out.read_text(encoding="utf-8").strip())
    # Only sid=0 (matched as int index) contributes
    assert row["gold_ids"] == [str(ep)]
    # WARN fired twice (once for "nonexistent_id", once for None)
    warns = [r for r in caplog.records if "not found in provenance map" in r.getMessage()]
    assert len(warns) == 2


def test_write_qrels_handles_string_session_ids(tmp_path: Path) -> None:
    """Cleaned upstream: answer_session_ids are strings matching haystack_session_ids."""
    out = tmp_path / "qrels.jsonl"
    picked = [{
        "question_id": "q-clean",
        "question": "?",
        "question_type": "single-session-user",
        "answer_session_ids": ["answer_280352e9"],
    }]
    ep, fact = uuid4(), uuid4()
    # Provenance keyed by string session ID (cleaned-upstream shape).
    provenance = {"q-clean": {"answer_280352e9": {"episode": [ep], "fact": [fact]}}}
    _write_qrels(picked, LMEIngestStats(), provenance, out)
    row = json.loads(out.read_text(encoding="utf-8").strip())
    assert set(row["gold_ids"]) == {str(ep), str(fact)}


def test_write_qrels_skips_when_no_gold(tmp_path: Path, caplog) -> None:
    """F051.5 hotfix: question with no provenance produces NO emitted row + WARN.

    Pre-hotfix this emitted an empty-gold row that load_qrels then rejected
    (Qrel requires gold_ids min_length=1). Now we skip + count.
    """
    out, picked = _make_qrels_inputs(tmp_path)
    with caplog.at_level(logging.WARNING):
        _write_qrels([picked[2]], LMEIngestStats(), {"q-missing": {}}, out)
    # File should exist but be empty (the qrel was skipped)
    content = out.read_text(encoding="utf-8")
    assert content == "", "empty-gold qrels should not emit"
    warns = [r for r in caplog.records if "no gold_ids populated" in r.getMessage()]
    assert len(warns) == 1


def test_write_qrels_skips_empty_gold_qrels(tmp_path: Path, caplog) -> None:
    """F051.5 hotfix: qrels with empty gold_ids are SKIPPED (not emitted).

    Qrel pydantic model requires gold_ids min_length=1. Emitting empty-gold
    rows would make the resulting JSONL un-loadable by load_qrels (rejects
    on the first such row). The hotfix counts them via WARN but skips emit.
    """
    out, picked = _make_qrels_inputs(tmp_path)
    # All three qrels in fixture; only q-single has provenance.
    ep, fact = uuid4(), uuid4()
    provenance = {"q-single": {0: {"episode": [ep], "fact": [fact]}}}
    with caplog.at_level(logging.WARNING):
        _write_qrels(picked, LMEIngestStats(), provenance, out)
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1, "only q-single should emit; q-multi + q-missing have no gold"
    assert rows[0]["notes"]["question_id"] == "q-single"
    # Verify the file is round-trippable through the loader (this would have
    # caught the pre-hotfix bug where notes-as-dict crashed pydantic).
    from nous_eval.qrels_loader import load_qrels
    loaded = load_qrels(out)
    assert len(loaded) == 1
    assert loaded[0].notes["question_id"] == "q-single"  # type: ignore[index]


def test_qrels_round_trip_loads_dict_notes(tmp_path: Path) -> None:
    """F051.5 hotfix regression test: ingest emits notes as dict; loader
    must accept dict shape (was str|None pre-hotfix; widened to dict|str|None)."""
    from nous_eval.qrels_loader import load_qrels

    qrel_row = {
        "query": "test",
        "gold_ids": ["00000000-0000-0000-0000-000000000001"],
        "memory_types": ["episode", "fact"],
        "source": "longmemeval",
        "notes": {
            "question_id": "q1",
            "question_type": "single-session-user",
            "answer_session_ids": [0, 1],
            "n_replayed_sessions": 2,
        },
        "reviewed_by": None,
    }
    out = tmp_path / "qrels.jsonl"
    out.write_text(json.dumps(qrel_row) + "\n", encoding="utf-8")

    loaded = load_qrels(out)
    assert len(loaded) == 1
    # notes is preserved as dict (not coerced to str or rejected)
    assert isinstance(loaded[0].notes, dict)
    assert loaded[0].notes["question_id"] == "q1"
    assert loaded[0].notes["answer_session_ids"] == [0, 1]


def test_qrels_round_trip_loads_string_notes(tmp_path: Path) -> None:
    """Backward compat: existing qrels with string notes (probes, hand_labels) still load."""
    from nous_eval.qrels_loader import load_qrels

    qrel_row = {
        "query": "test",
        "gold_ids": ["00000000-0000-0000-0000-000000000001"],
        "source": "probes",
        "notes": "F049 fact",
    }
    out = tmp_path / "qrels.jsonl"
    out.write_text(json.dumps(qrel_row) + "\n", encoding="utf-8")
    loaded = load_qrels(out)
    assert len(loaded) == 1
    assert loaded[0].notes == "F049 fact"


def test_write_qrels_string_answer_session_id_parsed(tmp_path: Path) -> None:
    """answer_session_ids as ['0', '1'] (string ints) parse correctly via int()."""
    out = tmp_path / "qrels.jsonl"
    picked = [{
        "question_id": "q-str",
        "question": "?",
        "question_type": "single-session-user",
        "answer_session_ids": ["0", "1"],  # strings that parse as ints
    }]
    ep0, ep1 = uuid4(), uuid4()
    provenance = {"q-str": {
        0: {"episode": [ep0], "fact": []},
        1: {"episode": [ep1], "fact": []},
    }}
    _write_qrels(picked, LMEIngestStats(), provenance, out)
    row = json.loads(out.read_text(encoding="utf-8").strip())
    assert set(row["gold_ids"]) == {str(ep0), str(ep1)}


# ---------------------------------------------------------------------------
# 7. _default_out_qrels env-var resolution
# ---------------------------------------------------------------------------


def test_default_out_qrels_uses_fixtures_dir_env(monkeypatch) -> None:
    """When NOUS_EVAL_FIXTURES_DIR is set, default path lives there."""
    monkeypatch.setenv("NOUS_EVAL_FIXTURES_DIR", "/some/fixture/dir")
    out = _default_out_qrels()
    assert str(out) in ("/some/fixture/dir/qrels_longmemeval.jsonl",
                        r"\some\fixture\dir\qrels_longmemeval.jsonl",
                        "\\some\\fixture\\dir\\qrels_longmemeval.jsonl")


def test_default_out_qrels_falls_back_to_tests_fixtures(monkeypatch) -> None:
    """When env unset, default path is tests/fixtures/qrels_longmemeval.jsonl."""
    monkeypatch.delenv("NOUS_EVAL_FIXTURES_DIR", raising=False)
    out = _default_out_qrels()
    assert out.name == "qrels_longmemeval.jsonl"
    # Either tests/fixtures or tests\fixtures depending on platform
    assert out.parent.name == "fixtures"


# ---------------------------------------------------------------------------
# 8. SHA256 fail-closed verification
# ---------------------------------------------------------------------------


def test_download_aborts_on_sha256_mismatch(tmp_path: Path) -> None:
    """When LONGMEMEVAL_SHA256 is set and the cached file's hash differs, abort."""
    from nous_eval.ingest_longmemeval import _download_if_missing

    # Pre-populate cache with known-bad content
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "longmemeval_s_cleaned.json").write_text('{"some": "content"}', encoding="utf-8")
    correct_hash = hashlib.sha256(b'{"some": "content"}').hexdigest()
    wrong_hash = "0" * 64

    # Correct hash → returns path without raising
    out = _download_if_missing(cache, "https://unused.example/", correct_hash)
    assert out.exists()

    # Wrong hash → SystemExit fail-closed
    with pytest.raises(SystemExit, match="SHA-256 mismatch"):
        _download_if_missing(cache, "https://unused.example/", wrong_hash)


# ---------------------------------------------------------------------------
# 9-10. extract_and_store explicit candidate_facts param + summary fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extract_and_store_uses_explicit_candidate_facts() -> None:
    """When caller passes candidate_facts (production handle() path), use those."""
    from nous.handlers.fact_extractor import FactExtractor

    heart = MagicMock()
    heart.find_similar_facts = AsyncMock(return_value=[])  # no dupes
    fact_id = uuid4()
    learned = MagicMock()
    learned.id = fact_id
    heart.learn = AsyncMock(return_value=learned)

    settings = MagicMock()
    settings.fact_dedup_threshold = 0.92
    extractor = FactExtractor(heart=heart, settings=settings, bus=None)

    # summary lacks candidate_facts; pass them explicitly
    cand = [{"subject": "x", "content": "Tim uses VS Code", "category": "preference"}]
    ids = await extractor.extract_and_store(
        summary={"summary": "(text)"},
        episode_id=str(uuid4()),
        candidate_facts=cand,
    )
    assert ids == [fact_id]
    # heart.learn was called with the explicit candidate, not via LLM
    heart.learn.assert_called_once()
    assert heart.learn.call_args[0][0].content == "Tim uses VS Code"


@pytest.mark.asyncio
async def test_extract_and_store_falls_back_to_summary_candidates() -> None:
    """When caller doesn't pass candidate_facts, fall back to summary['candidate_facts'] (ingest path)."""
    from nous.handlers.fact_extractor import FactExtractor

    heart = MagicMock()
    heart.find_similar_facts = AsyncMock(return_value=[])
    fact_id = uuid4()
    learned = MagicMock()
    learned.id = fact_id
    heart.learn = AsyncMock(return_value=learned)

    settings = MagicMock()
    settings.fact_dedup_threshold = 0.92
    extractor = FactExtractor(heart=heart, settings=settings, bus=None)

    # Pass candidates inside summary, not as kwarg
    summary = {
        "summary": "(text)",
        "candidate_facts": [{"subject": "y", "content": "Cat is brown", "category": "fact"}],
    }
    ids = await extractor.extract_and_store(
        summary=summary,
        episode_id=str(uuid4()),
    )
    assert ids == [fact_id]


@pytest.mark.asyncio
async def test_extract_and_store_returns_canonical_uuid_on_dedup() -> None:
    """F051.5 P2-fix: dedup-skipped facts return the canonical UUID, not nothing."""
    from nous.handlers.fact_extractor import FactExtractor

    canonical_id = uuid4()
    existing_fact = MagicMock()
    existing_fact.id = canonical_id
    existing_fact.score = 0.95  # > 0.92 default → dedup skip

    heart = MagicMock()
    heart.find_similar_facts = AsyncMock(return_value=[existing_fact])
    heart.learn = AsyncMock()  # should NOT be called (dedup skip)

    settings = MagicMock()
    settings.fact_dedup_threshold = 0.92
    extractor = FactExtractor(heart=heart, settings=settings, bus=None)

    ids = await extractor.extract_and_store(
        summary={"summary": "(text)"},
        episode_id=str(uuid4()),
        candidate_facts=[{"subject": "z", "content": "duplicate fact", "category": "fact"}],
    )
    # Canonical UUID returned even though heart.learn never fired
    assert ids == [canonical_id]
    heart.learn.assert_not_called()
