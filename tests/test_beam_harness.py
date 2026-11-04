"""PR-M BEAM harness: corpus loader, the ported BEAM scorer, arm/env plumbing,
and an offline end-to-end smoke (Postgres, fake LLM, fake judge, mock embeddings).

The fixture conversation is written by the tests; no BEAM data is used and no
LLM API is called.
"""

from __future__ import annotations

import json
import os
import random
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from nous_eval.beam import arms, corpus, harness, scoring
from nous_eval.beam import run as beam_run

REPO_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Fixture conversation (BEAM layout, synthetic text)
# ---------------------------------------------------------------------------

BATCH_1 = {
    "batch_number": 1,
    "time_anchor": None,
    "turns": [
        [
            {
                "role": "user",
                "id": 0,
                "time_anchor": "March-15-2024",
                "content": "I am planning the Larkspur garden project and the first planting is on March 29.",
            },
            {
                "role": "assistant",
                "id": 1,
                "content": "Good plan. We will prepare the Larkspur beds for the March 29 planting.",
            },
        ],
        [
            {
                "role": "user",
                "id": 2,
                "content": "Next I set up the irrigation timer for the Larkspur beds, watering at 6am.",
            },
            {"role": "assistant", "id": 3, "content": "Here is how to configure the irrigation timer to run at 6am."},
        ],
    ],
}
BATCH_2 = {
    "batch_number": 2,
    "time_anchor": None,
    "turns": [
        [
            {
                "role": "user",
                "id": 4,
                "time_anchor": "April-05-2024",
                "content": "Update for Larkspur: the first planting moved to April 12 because of frost.",
            },
            {"role": "assistant", "id": 5, "content": "Noted: the Larkspur first planting is now April 12."},
        ]
    ],
}
QUESTIONS = {
    "information_extraction": [
        {
            "question": "When is the first Larkspur planting?",
            "answer": "April 12",
            "rubric": ["LLM response should state: April 12"],
        }
    ],
    "event_ordering": [
        {
            "question": "In what order did I bring up the planting date and the irrigation timer?",
            "rubric": ["Planting date", "Irrigation timer"],
        }
    ],
}


def write_conversation(root: Path, bucket: str = "100K", conv_id: str = "1", *, ten_m: bool = False) -> Path:
    conv = root / bucket / conv_id
    (conv / "probing_questions").mkdir(parents=True)
    chat = [{"plan-1": [BATCH_1]}, {"plan-2": [BATCH_2]}] if ten_m else [BATCH_1, BATCH_2]
    (conv / "chat.json").write_text(json.dumps(chat), encoding="utf-8")
    (conv / "probing_questions" / "probing_questions.json").write_text(json.dumps(QUESTIONS), encoding="utf-8")
    return root


# ---------------------------------------------------------------------------
# Corpus
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ten_m", [False, True])
def test_load_conversation_both_shapes(tmp_path: Path, ten_m: bool) -> None:
    write_conversation(tmp_path, ten_m=ten_m)
    conv = corpus.load_conversation(tmp_path, "100K", "1")

    assert conv.key == "100K/1"
    assert [(s.batch, s.index) for s in conv.sessions] == [(1, 0), (1, 1), (2, 0)]
    assert conv.batch_ends == (1, 2)
    assert conv.sessions[0].started_at == datetime(2024, 3, 15, tzinfo=UTC)
    assert conv.sessions[1].started_at == datetime(2024, 3, 15, tzinfo=UTC) + corpus.SESSION_SPACING
    assert conv.sessions[2].started_at == datetime(2024, 4, 5, tzinfo=UTC)
    assert [m.role for m in conv.sessions[0].messages] == ["user", "assistant"]
    assert [(q.ability, q.index, q.rubric) for q in conv.questions] == [
        ("information_extraction", 0, ("LLM response should state: April 12",)),
        ("event_ordering", 0, ("Planting date", "Irrigation timer")),
    ]


def test_list_conversations_skips_non_conversation_entries(tmp_path: Path) -> None:
    write_conversation(tmp_path, conv_id="10")
    write_conversation(tmp_path, conv_id="2")
    (tmp_path / "100K" / "topics.json").write_text("{}", encoding="utf-8")
    (tmp_path / "100K" / "3").mkdir()  # no chat.json
    assert corpus.list_conversations(tmp_path, "100K") == ["2", "10"]


def test_question_without_rubric_is_an_error(tmp_path: Path) -> None:
    write_conversation(tmp_path)
    pq = tmp_path / "100K" / "1" / "probing_questions" / "probing_questions.json"
    pq.write_text(json.dumps({"abstention": [{"question": "q"}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="no rubric"):
        corpus.load_conversation(tmp_path, "100K", "1")


def test_batch_without_anchor_is_dated_from_its_neighbours(tmp_path: Path) -> None:
    def batch(n: int, anchor: str | None) -> dict:
        return {"batch_number": n, "turns": [[{"role": "user", "time_anchor": anchor, "content": f"batch {n}"}]]}

    conv_dir = tmp_path / "10M" / "1"
    (conv_dir / "probing_questions").mkdir(parents=True)
    chat = [{"plan-1": [batch(1, None), batch(2, "May-10-2024"), batch(3, None)]}]
    (conv_dir / "chat.json").write_text(json.dumps(chat), encoding="utf-8")
    (conv_dir / "probing_questions" / "probing_questions.json").write_text("{}", encoding="utf-8")
    conv = corpus.load_conversation(tmp_path, "10M", "1")
    assert [s.started_at.date().isoformat() for s in conv.sessions] == ["2024-05-09", "2024-05-10", "2024-05-11"]


def test_sessions_of_a_long_batch_stay_inside_its_day(tmp_path: Path) -> None:
    turn = [{"role": "user", "time_anchor": "May-01-2024", "content": "x"}]
    batch = {"batch_number": 1, "turns": [turn] * 300}
    conv_dir = tmp_path / "1M" / "1"
    (conv_dir / "probing_questions").mkdir(parents=True)
    (conv_dir / "chat.json").write_text(json.dumps([batch]), encoding="utf-8")
    (conv_dir / "probing_questions" / "probing_questions.json").write_text("{}", encoding="utf-8")
    conv = corpus.load_conversation(tmp_path, "1M", "1")
    times = [s.started_at for s in conv.sessions]
    assert times == sorted(times) and len(set(times)) == 300
    assert times[-1] - times[0] < timedelta(days=1)


def test_render_transcript_matches_layer_format() -> None:
    session = corpus.Session(
        1, 0, (corpus.Message("user", "hello there"), corpus.Message("assistant", "abcdefgh")), datetime.now(UTC)
    )
    assert harness.render_transcript(session, max_chars=5) == "User: hello\n\nAssistant: abcde"


# ---------------------------------------------------------------------------
# Scoring (port of BEAM's evaluator)
# ---------------------------------------------------------------------------


def exact_match_judge(score: float = 1.0):
    """Fake judge: equivalence = case-insensitive exact match; rubric items get ``score``."""

    async def judge(messages: list[dict[str, str]]) -> str:
        if messages[0]["role"] == "system":
            content = messages[-1]["content"]
            first = re.search(r"First snippet: (.*?) \n", content, re.DOTALL).group(1)
            second = re.search(r"Second snippet: (.*)", content, re.DOTALL).group(1)
            return "YES" if first.strip().lower() == second.strip().lower() else "NO"
        return json.dumps({"score": score, "reason": "fake"})

    return judge


def test_fill_never_resubstitutes() -> None:
    assert scoring.fill("<a> <b>", {"a": "<b>", "b": "x"}) == "<b> x"


def test_judge_prompt_leaves_question_placeholder_unfilled_as_upstream() -> None:
    filled = scoring.fill(scoring.JUDGE_PROMPT, {"rubric_item": "R", "llm_response": "A"})
    assert "- QUESTION (what the user asked): <question>" in filled
    assert "- RUBRIC CRITERION (what to check): R" in filled


@pytest.mark.parametrize(
    "reply",
    [
        '{"score": 0.5, "reason": "x"}',
        '```json\n{"score": 0.5, "reason": "x"}\n```',
        'Here: {"score": 0.5, "reason": "x"} done',
    ],
)
def test_parse_json_response(reply: str) -> None:
    assert scoring.parse_json_response(reply)["score"] == 0.5


async def test_judge_rubric_truncates_partial_credit_except_event_ordering() -> None:
    judge = exact_match_judge(score=0.5)
    plain = await scoring.score_answer(judge, "knowledge_update", ["state: 250ms"], "250ms")
    assert plain["llm_judge_score"] == 0 and plain["rubric_scores"] == [0.5] and plain["beam_score"] == 0
    eo = await scoring.judge_rubric(judge, ["a", "b"], "a\nb", float_scores=True)
    assert eo["llm_judge_score"] == 0.5


def test_kendall_tau_b_hand_cases() -> None:
    assert scoring.kendall_tau_b([1, 2, 3], [1, 2, 3]) == pytest.approx(1.0)
    assert scoring.kendall_tau_b([1, 2, 3], [3, 2, 1]) == pytest.approx(-1.0)
    # one tie in y: nc=5, nd=0, n0=6, ties_y=1 -> 5/sqrt(6*5)
    assert scoring.kendall_tau_b([1, 2, 3, 4], [1, 2, 5, 5]) == pytest.approx(5 / 30**0.5)
    assert scoring.kendall_tau_b([1, 1], [1, 2]) is None
    assert scoring.kendall_tau_b([1], [1]) is None


def test_kendall_tau_b_matches_scipy() -> None:
    stats = pytest.importorskip("scipy.stats")
    rng = random.Random(7)
    for _ in range(50):
        n = rng.randint(2, 9)
        x = [rng.randint(1, 4) for _ in range(n)]
        y = [rng.randint(1, 4) for _ in range(n)]
        ref = stats.kendalltau(x, y, variant="b").statistic
        mine = scoring.kendall_tau_b(x, y)
        if ref != ref:  # NaN
            assert mine is None
        else:
            assert mine == pytest.approx(ref)


async def test_event_ordering_score_hand_case() -> None:
    # Answer lines: B, A, a blank line, X -> canon [B, A, "", X]; C is missed.
    result = await scoring.event_ordering_score(["A", "B", "C"], ["B", "A", "", "X"], exact_match_judge())
    assert result["aligned_events"] == 2
    assert result["precision"] == pytest.approx(0.5)
    assert result["recall"] == pytest.approx(2 / 3)
    assert result["f1"] == pytest.approx(4 / 7)
    # union [A,B,C,"",X]; ranks ref [1,2,3,6,6], sys [2,1,6,3,4]: nc=6 nd=3 ties_x=1
    tau = 3 / 90**0.5
    assert result["tau_norm"] == pytest.approx((tau + 1) / 2)
    assert result["final_score"] == pytest.approx((tau + 1) / 2 * 4 / 7)
    assert (result["n_reference"], result["n_system"]) == (3, 4)


async def test_event_ordering_beam_score_is_tau_norm() -> None:
    result = await scoring.score_answer(exact_match_judge(), "event_ordering", ["A", "B"], "B\nA")
    assert result["aligned_events"] == 2
    assert result["tau_norm"] == pytest.approx(0.0)
    assert result["beam_score"] == result["tau_norm"]
    assert result["llm_judge_score"] == pytest.approx(1.0)


def test_aggregate_averages_per_conversation_first() -> None:
    def row(conv: str, ability: str, score: float, **extra) -> dict:
        return {"conversation": conv, "ability": ability, "error": None, "score": {"beam_score": score, **extra}}

    rows = [
        row("100K/1", "information_extraction", 1.0),
        row("100K/1", "information_extraction", 0.0),
        row("100K/2", "information_extraction", 1.0),
        row("100K/1", "event_ordering", 0.75, final_score=0.5, aligned_events=3),
        {"conversation": "100K/2", "ability": "event_ordering", "error": "judge ValueError", "score": None},
    ]
    agg = scoring.aggregate(rows)
    assert agg["abilities"]["information_extraction"] == {"score": pytest.approx(0.75), "n_questions": 3}
    assert agg["abilities"]["event_ordering"]["score"] == pytest.approx(0.75)
    assert agg["overall"] == pytest.approx(0.75)
    assert agg["event_ordering"]["aligned_events_total"] == 3


# ---------------------------------------------------------------------------
# Arms: overrides, flags file, settings, snapshot, out-dir guard
# ---------------------------------------------------------------------------

DB_URL = "postgresql+asyncpg://u:pw@dbhost:5999/scratchdb"


def test_parse_overrides_accepts_known_flags_case_insensitively() -> None:
    assert arms.parse_overrides(["nous_sleep_reflection_hours=48"]) == {"NOUS_SLEEP_REFLECTION_HOURS": "48"}


@pytest.mark.parametrize(
    "pair",
    [
        "NOUS_NOT_A_REAL_FLAG=1",
        "OPENAI_API_KEY=x",
        "ANTHROPIC_AUTH_TOKEN=x",
        "DB_NAME=prod",
        "NOUS_AGENT_ID=x",
        "NOUS_SLEEP_REFLECTION_HOURS",
    ],
)
def test_parse_overrides_rejects_unknown_credential_and_harness_names(pair: str) -> None:
    with pytest.raises(SystemExit):
        arms.parse_overrides([pair])


def test_flags_file_keeps_only_known_nous_flags(tmp_path: Path) -> None:
    f = tmp_path / "flags.env"
    f.write_text(
        "# comment\n"
        "NOUS_SLEEP_REFLECTION_HOURS=48\n"
        'export NOUS_EPISODE_CHUNKS_ENABLED="true"\n'
        "NOUS_NOT_A_REAL_FLAG=1\n"
        "ANTHROPIC_AUTH_TOKEN=do-not-read\n"
        "NOUS_SOME_TOKEN=do-not-read\n"
        "NOUS_AGENT_ID=nous-default\n"
        "DB_PASSWORD=do-not-read\n",
        encoding="utf-8",
    )
    flags, skipped = arms.load_flags_file(f)
    assert flags == {"NOUS_SLEEP_REFLECTION_HOURS": "48", "NOUS_EPISODE_CHUNKS_ENABLED": "true"}
    assert skipped == ["NOUS_NOT_A_REAL_FLAG"]


def test_build_settings_applies_override_and_restores_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOUS_SLEEP_REFLECTION_HOURS", raising=False)
    s = arms.build_settings(DB_URL, "beam-100k-1-t", {"NOUS_SLEEP_REFLECTION_HOURS": "48"})
    assert s.sleep_reflection_hours == 48  # parsed to int, as the deployed env is
    assert (s.db_host, s.db_port, s.db_user, s.db_name, s.agent_id) == (
        "dbhost",
        5999,
        "u",
        "scratchdb",
        "beam-100k-1-t",
    )
    assert "NOUS_SLEEP_REFLECTION_HOURS" not in os.environ


def test_build_settings_rejects_malformed_db_url() -> None:
    with pytest.raises(SystemExit):
        arms.build_settings("not-a-url", "a", {})


def test_settings_snapshot_is_allowlisted() -> None:
    s = arms.build_settings(DB_URL, "a", {"NOUS_SLEEP_REFLECTION_HOURS": "48"})
    snap = arms.settings_snapshot(s, {"NOUS_SLEEP_REFLECTION_HOURS": "48"})
    assert snap["sleep_reflection_hours"] == 48
    assert set(snap) == set(arms.SNAPSHOT_FIELDS)
    for name, value in snap.items():
        assert not re.search("key|token|password|secret", name, re.IGNORECASE)
        assert value == "absent" or value == getattr(s, name)
    assert "pw" not in json.dumps(snap, default=str)


def test_snapshot_records_an_override_outside_the_allowlist() -> None:
    s = arms.build_settings(DB_URL, "a", {"NOUS_EPISODE_SEED_SUMMARY_CHARS": "123"})
    snap = arms.settings_snapshot(s, {"NOUS_EPISODE_SEED_SUMMARY_CHARS": "123"})
    assert snap["episode_seed_summary_chars"] == 123


def test_out_dir_must_not_be_tracked(tmp_path: Path) -> None:
    arms.check_out_dir(tmp_path / "beam-run")  # outside any repo: fine
    with pytest.raises(SystemExit):
        arms.check_out_dir(REPO_ROOT / "nous_eval" / "beam" / "out-test")


# ---------------------------------------------------------------------------
# Event bus drain
# ---------------------------------------------------------------------------


async def test_drained_bus_dispatches_caused_events_in_order() -> None:
    from nous.events import Event

    bus = harness.DrainedBus()
    seen: list[str] = []

    async def on_a(event: Event) -> None:
        seen.append("a")
        await bus.emit(Event(type="b", agent_id="x"))
        seen.append("a-done")  # the caused event waits for this handler, as in the bus loop

    async def on_b(event: Event) -> None:
        seen.append("b")

    bus.on("a", on_a)
    bus.on("b", on_b)
    await bus.emit(Event(type="a", agent_id="x"))
    assert await bus.drain() == 2
    assert seen == ["a", "a-done", "b"]


# ---------------------------------------------------------------------------
# Offline smoke: ingest (with sleep) -> both answer modes -> score
# ---------------------------------------------------------------------------


class FakeLLM:
    """Routes Anthropic-shaped payloads by prompt: summaries carry one candidate
    fact per session; the answer prompt gets an ordered list; anything else
    raises, which each caller handles as it handles an API failure."""

    def __init__(self) -> None:
        self.routes: list[str] = []

    async def call(self, payload: dict) -> SimpleNamespace:
        system = payload.get("system")
        system_text = " ".join(b.get("text", "") for b in system) if isinstance(system, list) else str(system or "")
        content = payload["messages"][-1]["content"]
        user = content if isinstance(content, str) else " ".join(b.get("text", "") for b in content)
        if "summarizing a conversation episode" in system_text:
            first = re.search(r"User: (.+)", user).group(1)[:200]
            body = {
                "title": "Larkspur garden session",
                "summary": f"The user said: {first}",
                "key_points": [],
                "outcome": "informational",
                "outcome_rationale": "status",
                "topics": ["larkspur"],
                "candidate_facts": [{"subject": "Larkspur garden", "content": first, "category": "technical"}],
            }
            self.routes.append("summary")
            return SimpleNamespace(content=[{"type": "text", "text": json.dumps(body)}], stop_reason="end_turn")
        if "STRICT INSTRUCTIONS" in user:
            self.routes.append("answer")
            return SimpleNamespace(
                content=[{"type": "text", "text": "Planting date\nIrrigation timer"}], stop_reason="end_turn"
            )
        self.routes.append("unrouted")
        raise RuntimeError("fake llm: no route for this call")


@pytest.mark.postgres_only
async def test_offline_smoke_ingest_answer_score(tmp_path: Path) -> None:
    from conftest import MockEmbeddingProvider
    from sqlalchemy import text

    from nous.config import Settings

    chats = write_conversation(tmp_path / "chats")
    out = tmp_path / "out"
    out.mkdir()
    tag = uuid.uuid4().hex[:8]
    common = [
        "--chats-dir",
        str(chats),
        "--bucket",
        "100K",
        "--conv",
        "1",
        "--db-url",
        Settings().db_url,
        "--agent-tag",
        tag,
        "--out-dir",
        str(out),
    ]
    llm = FakeLLM()

    ingest_args = beam_run.parse_args(["ingest", *common, "--sleep", "--set", "NOUS_SLEEP_REFLECTION_HOURS=200000"])
    [record] = await beam_run.cmd_ingest(ingest_args, llm=llm, embeddings=MockEmbeddingProvider(), prepare=False)
    assert record["agent_id"] == f"beam-100k-1-{tag}"
    assert record["sessions"] == 3 and record["sleep_cycles"] == 2
    assert record["rows"]["episodes"] == 3
    assert record["rows"]["facts"] >= 1, record
    assert record["settings"]["sleep_reflection_hours"] == 200000
    assert llm.routes.count("summary") == 3
    assert json.loads((out / f"ingest-100K-1-{tag}.json").read_text(encoding="utf-8"))["agent_id"] == record["agent_id"]

    async with harness.open_stack(
        arms.build_settings(Settings().db_url, record["agent_id"], {}), llm=llm, embeddings=MockEmbeddingProvider()
    ) as stack:
        async with stack.db.session() as s:
            rows = (
                await s.execute(
                    text(
                        "SELECT started_at, ended_at, transcript FROM heart.episodes"
                        " WHERE agent_id = :a ORDER BY started_at"
                    ),
                    {"a": record["agent_id"]},
                )
            ).all()
        assert [r.started_at.date().isoformat() for r in rows] == ["2024-03-15", "2024-03-15", "2024-04-05"]
        assert all(r.ended_at > r.started_at for r in rows)
        assert rows[0].transcript.startswith("User: I am planning the Larkspur garden project")
        # A second ingest into the same agent is refused.
        conv = corpus.load_conversation(chats, "100K", "1")
        with pytest.raises(SystemExit, match="already has"):
            await harness.ingest_conversation(stack, conv)

    for mode in ("preturn", "recall_deep"):
        mode_out = out / mode
        mode_out.mkdir()
        args = beam_run.parse_args(
            [
                "answer",
                *[a if a != str(out) else str(mode_out) for a in common],
                "--mode",
                mode,
                "--answer-model",
                "fake-model",
                "--arm",
                "smoke",
            ]
        )
        assert await beam_run.cmd_answer(args, llm=llm, embeddings=MockEmbeddingProvider()) == 2
        assert await beam_run.cmd_answer(args, llm=llm, embeddings=MockEmbeddingProvider()) == 0  # resumes
        answers = [json.loads(line) for line in (mode_out / "answers.jsonl").read_text(encoding="utf-8").splitlines()]
        assert [(a["ability"], a["error"]) for a in answers] == [
            ("information_extraction", None),
            ("event_ordering", None),
        ]
        assert all(a["answer"] == "Planting date\nIrrigation timer" and a["stop_reason"] == "end_turn" for a in answers)
        assert all("Larkspur" in a["context"] for a in answers), [a["context"][:300] for a in answers]
        if mode == "preturn":
            assert answers[0]["retrieval"]["plan_queries"].get("fact")
            assert answers[0]["retrieval"]["recalled_ids"].get("fact")
        run_info = json.loads((mode_out / "run.json").read_text(encoding="utf-8"))
        assert run_info["mode"] == mode and run_info["answer_model"] == "fake-model" and run_info["git"]["sha"]

        summary = await beam_run.cmd_score(
            beam_run.parse_args(["score", "--out-dir", str(mode_out)]), judge=exact_match_judge()
        )
        assert summary["n_errors"] == 0
        assert summary["abilities"]["information_extraction"]["score"] == pytest.approx(1.0)
        assert summary["abilities"]["event_ordering"]["score"] == pytest.approx(1.0)  # tau_norm
        assert summary["event_ordering"]["aligned_events_per_question"] == [2]
        result = json.loads((mode_out / "results" / "100K" / "1" / "result.json").read_text(encoding="utf-8"))
        assert set(result) == {"information_extraction", "event_ordering"}
        assert result["event_ordering"][0]["llm_response"] == "Planting date\nIrrigation timer"
