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

# Pads each real session past episode_min_content_length (200 chars), so end_session keeps it.
PLAN_DETAIL = "loosen the soil, add compost, water the beds the evening before and label each row by variety."
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
                "content": "Good plan. We will prepare the Larkspur beds for the March 29 planting: " + PLAN_DETAIL,
            },
        ],
        [
            {
                "role": "user",
                "id": 2,
                "content": "Next I set up the irrigation timer for the Larkspur beds, watering at 6am.",
            },
            {"role": "assistant", "id": 3, "content": "Set the irrigation timer to run at 6am: " + PLAN_DETAIL},
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
            {"role": "assistant", "id": 5, "content": "Noted, the first planting is now April 12: " + PLAN_DETAIL},
        ],
        # Trivial: one turn, under episode_min_content_length -> soft-deleted, as end_session does.
        [{"role": "user", "id": 6, "content": "ok"}, {"role": "assistant", "id": 7, "content": "Sure."}],
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
    assert [(s.batch, s.index) for s in conv.sessions] == [(1, 0), (1, 1), (2, 0), (2, 1)]
    assert conv.batch_ends == (1, 3)
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
# Arms: overrides, flags file, settings, snapshot, scratch-DB and out-dir guards
# ---------------------------------------------------------------------------

DB_URL = "postgresql+asyncpg://u:pw@dbhost:5999/nous_eval_scratch"


def test_parse_overrides_accepts_arm_settings_case_insensitively() -> None:
    assert arms.parse_overrides(["nous_sleep_reflection_hours=48", "NOUS_CONTEXT_QUERY_MODE=conditional"]) == {
        "NOUS_SLEEP_REFLECTION_HOURS": "48",
        "NOUS_CONTEXT_QUERY_MODE": "conditional",
    }


@pytest.mark.parametrize(
    "pair",
    [
        "NOUS_NOT_A_REAL_FLAG=1",
        "OPENAI_API_KEY=x",
        "ANTHROPIC_AUTH_TOKEN=x",
        "DB_NAME=prod",
        "NOUS_AGENT_ID=x",
        "NOUS_EMERSON_HOOK_URL=https://example.invalid/hook",
        "NOUS_EMAIL_USER=someone",
        'GOOGLE_SERVICE_ACCOUNT_JSON={"private_key": "x"}',
        "NOUS_STALE_SCAN_AGE_DAYS=1",  # set by the harness for every arm
        "NOUS_FACT_EXTRACTION_ENABLED=false",  # a handler switch, not an arm
        "NOUS_SLEEP_REFLECTION_HOURS",
    ],
)
def test_parse_overrides_refuses_everything_outside_the_allowlist(pair: str) -> None:
    with pytest.raises(SystemExit):
        arms.parse_overrides([pair])


def test_arm_settings_are_real_settings_and_never_credentials_or_endpoints() -> None:
    from nous.config import Settings

    assert arms.ARM_SETTINGS <= set(Settings.model_fields)
    words = {"token", "password", "secret", "url", "user", "json", "email", "login", "hook", "credential", "chat"}
    flagged = [n for n in arms.ARM_SETTINGS if n.endswith("_key") or "api_key" in n or words & set(n.split("_"))]
    assert not flagged
    assert not arms.ARM_SETTINGS & {"episode_summary_enabled", "fact_extraction_enabled", *arms.EVAL_OVERRIDES}


def test_flags_file_reads_multiline_values_and_keeps_only_arm_settings(tmp_path: Path) -> None:
    f = tmp_path / "flags.env"
    f.write_text(
        "# comment\n"
        'NOUS_IDENTITY_PROMPT="You are Nous.\n'
        "NOUS_SLEEP_REFLECTION_HOURS=1\n"
        'Second line of the prompt."\n'
        "NOUS_SLEEP_REFLECTION_HOURS=48\n"
        'export NOUS_EPISODE_CHUNKS_ENABLED="true"\n'
        "NOUS_EMERSON_HOOK_URL=https://example.invalid/hook\n"
        "NOUS_EMAIL_USER=someone\n"
        'GOOGLE_SERVICE_ACCOUNT_JSON=\'{"type": "service_account",\n "private_key": "do-not-read"}\'\n'
        "NOUS_CONTEXT_QUERY_MODE=conditional\n"
        "NOUS_NOT_A_REAL_FLAG=1\n"
        "ANTHROPIC_AUTH_TOKEN=do-not-read\n"
        "NOUS_AGENT_ID=nous-default\n"
        "DB_PASSWORD=do-not-read\n",
        encoding="utf-8",
    )
    flags, ignored = arms.load_flags_file(f)
    assert flags == {
        "NOUS_SLEEP_REFLECTION_HOURS": "48",
        "NOUS_EPISODE_CHUNKS_ENABLED": "true",
        "NOUS_CONTEXT_QUERY_MODE": "conditional",
    }
    assert ignored == 8


def test_build_settings_applies_override_and_eval_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOUS_SLEEP_REFLECTION_HOURS", raising=False)
    monkeypatch.setenv("NOUS_STALE_SCAN_AGE_DAYS", "1")  # an environment value never beats the harness
    s = arms.build_settings(DB_URL, "beam-100k-1-t", {"NOUS_SLEEP_REFLECTION_HOURS": "48"})
    assert s.sleep_reflection_hours == 48  # parsed to int, as the deployed env is
    assert s.stale_scan_age_days == arms.EVAL_OVERRIDES["stale_scan_age_days"]
    assert (s.db_host, s.db_port, s.db_user, s.db_name, s.agent_id) == (
        "dbhost",
        5999,
        "u",
        "nous_eval_scratch",
        "beam-100k-1-t",
    )
    assert "NOUS_SLEEP_REFLECTION_HOURS" not in os.environ


def test_build_settings_rejects_malformed_db_url() -> None:
    with pytest.raises(SystemExit):
        arms.build_settings("not-a-url", "a", {})


@pytest.mark.parametrize("name", ["nous", "nous_prod", "nous_prod_clone", "postgres", "eval_nous_test"])
def test_scratch_db_guard_refuses_other_databases(name: str) -> None:
    url = f"postgresql+asyncpg://u:pw@h:5432/{name}"
    with pytest.raises(SystemExit, match="i-know-this-is-scratch"):
        arms.check_scratch_db(url)
    arms.check_scratch_db(url, allow_any=True)


@pytest.mark.parametrize("name", ["nous_eval", "nous_eval_scratch", "nous_test_mem_m", "beam_scratch"])
def test_scratch_db_guard_accepts_eval_and_test_databases(name: str) -> None:
    arms.check_scratch_db(f"postgresql+asyncpg://u:pw@h:5432/{name}")


def test_run_record_keeps_names_and_snapshot_values_only() -> None:
    overrides = {"NOUS_SLEEP_REFLECTION_HOURS": "48", "NOUS_EPISODE_SEED_SUMMARY_CHARS": "123"}
    s = arms.build_settings(DB_URL, "a", overrides)
    record = beam_run.run_settings_record(s, overrides, ignored=2)
    assert record["override_names"] == sorted(overrides)
    snap = record["settings"]
    assert set(snap) == {*arms.SNAPSHOT_FIELDS, *arms.EVAL_OVERRIDES, "episode_seed_summary_chars"}
    assert snap["sleep_reflection_hours"] == 48 and snap["episode_seed_summary_chars"] == 123
    assert snap["stale_scan_age_days"] == arms.EVAL_OVERRIDES["stale_scan_age_days"]
    for name, value in snap.items():
        assert value == "absent" or value == getattr(s, name)
    assert record["clock_notes"] == list(arms.CLOCK_NOTES)
    assert "pw" not in json.dumps(record, default=str)


def test_out_dir_must_not_be_tracked(tmp_path: Path) -> None:
    arms.check_out_dir(tmp_path / "beam-run")  # outside any repo: fine
    with pytest.raises(SystemExit):
        arms.check_out_dir(REPO_ROOT / "nous_eval" / "beam" / "out-test")


def test_trivial_session_matches_end_session_rule() -> None:
    now = datetime.now(UTC)
    short = corpus.Session(1, 0, (corpus.Message("user", "ok"), corpus.Message("assistant", "Sure.")), now)
    two_turns = corpus.Session(1, 0, (corpus.Message("user", "ok"), corpus.Message("user", "and?")), now)
    long = corpus.Session(1, 0, (corpus.Message("user", "x" * 150), corpus.Message("assistant", "y" * 60)), now)
    assert harness.is_trivial(short, 200)
    assert not harness.is_trivial(two_turns, 200)
    assert not harness.is_trivial(long, 200)


# ---------------------------------------------------------------------------
# Resume guards: an out-dir holds one configuration and one judge
# ---------------------------------------------------------------------------


class _StopBeforeDB(Exception):
    pass


async def test_answer_resume_refuses_a_changed_resolved_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A setting changed through the environment (no override) still changes the run."""

    def stop_before_db(*args, **kwargs):
        raise _StopBeforeDB

    monkeypatch.setattr(harness, "open_stack", stop_before_db)
    monkeypatch.setenv("NOUS_CONTEXT_QUERY_MODE", "bag")
    chats = write_conversation(tmp_path / "chats")
    out = tmp_path / "out"
    out.mkdir()
    argv = ["answer", "--chats-dir", str(chats), "--bucket", "100K", "--conv", "1", "--db-url", DB_URL]
    args = beam_run.parse_args([*argv, "--agent-tag", "t", "--out-dir", str(out), "--mode", "preturn"])
    with pytest.raises(_StopBeforeDB):  # first run: run.json written, then the stack would open
        await beam_run.cmd_answer(args)
    assert json.loads((out / "run.json").read_text(encoding="utf-8"))["settings"]["context_query_mode"] == "bag"

    with pytest.raises(_StopBeforeDB):  # same configuration: resumes
        await beam_run.cmd_answer(args)
    monkeypatch.setenv("NOUS_CONTEXT_QUERY_MODE", "conditional")
    with pytest.raises(SystemExit, match="context_query_mode"):
        await beam_run.cmd_answer(args)


class NamedJudge:
    """A fake judge with a model name, as OpenAIJudge has."""

    def __init__(self, model: str) -> None:
        self.model = model
        self._judge = exact_match_judge()

    async def __call__(self, messages: list[dict[str, str]]) -> str:
        return await self._judge(messages)


async def test_score_refuses_scores_from_another_judge(tmp_path: Path) -> None:
    (tmp_path / "run.json").write_text(json.dumps({"mode": "preturn"}), encoding="utf-8")
    row = {
        "conversation": "100K/1",
        "ability": "information_extraction",
        "index": 0,
        "question": "When is the first Larkspur planting?",
        "rubric": ["LLM response should state: April 12"],
        "answer": "April 12",
        "error": None,
    }
    (tmp_path / "answers.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    first = await beam_run.cmd_score(
        beam_run.parse_args(["score", "--out-dir", str(tmp_path), "--judge-model", "judge-a"]),
        judge=NamedJudge("judge-a"),
    )
    assert first["judge_model"] == "judge-a"
    scored = [json.loads(x) for x in (tmp_path / "scores.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r["judge_model"] for r in scored] == ["judge-a"]

    again = await beam_run.cmd_score(  # the same judge resumes
        beam_run.parse_args(["score", "--out-dir", str(tmp_path), "--judge-model", "judge-a"]),
        judge=NamedJudge("judge-a"),
    )
    assert again["n_answers"] == 1
    with pytest.raises(SystemExit, match="judge-a"):
        await beam_run.cmd_score(
            beam_run.parse_args(["score", "--out-dir", str(tmp_path), "--judge-model", "judge-b"]),
            judge=NamedJudge("judge-b"),
        )


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

ANSWER = "Planting date\nIrrigation timer\n"  # trailing newline: event ordering splits the raw answer


class FakeLLM:
    """Routes Anthropic-shaped payloads by prompt: summaries carry one candidate
    fact per session; the outcome classifier reports a correction for the frost
    session and the correction extractor turns it into a rule; the answer prompt
    gets an ordered list; anything else raises, which each caller handles as it
    handles an API failure."""

    def __init__(self) -> None:
        self.routes: list[str] = []

    @staticmethod
    def _reply(text: str) -> SimpleNamespace:
        return SimpleNamespace(content=[{"type": "text", "text": text}], stop_reason="end_turn")

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
            return self._reply(json.dumps(body))
        if "outcome signal classifier" in system_text:
            self.routes.append("outcome")
            signals = [{"type": "corrected", "confidence": 0.9, "evidence": "frost"}] if "frost" in user else []
            return self._reply(json.dumps({"signals": signals}))
        if "correction analysis system" in system_text:
            self.routes.append("correction")
            principle = "Always re-check the Larkspur planting date after a frost warning before scheduling work."
            return self._reply(json.dumps({"principle": principle, "subject": "Larkspur planting", "confidence": 0.8}))
        if "STRICT INSTRUCTIONS" in user:
            self.routes.append("answer")
            return self._reply(ANSWER)
        self.routes.append("unrouted")
        raise RuntimeError("fake llm: no route for this call")


async def purge_agent(db, agent_id: str) -> list[str]:
    """Delete every row the test agent wrote (several passes, for foreign keys), then the
    agent; returns the tables whose delete still failed. Not the scoring schema: the
    harness never writes it, and the application role (the DB roles CI job) cannot."""
    from sqlalchemy import text

    async with db.engine.connect() as conn:
        tables = (
            (
                await conn.execute(
                    text(
                        "SELECT c.table_schema || '.' || c.table_name FROM information_schema.columns c"
                        " JOIN information_schema.tables t USING (table_schema, table_name)"
                        " WHERE c.column_name = 'agent_id' AND t.table_type = 'BASE TABLE'"
                        " AND c.table_schema IN ('brain', 'heart', 'nous_system')"
                    )
                )
            )
            .scalars()
            .all()
        )
    pending = list(tables)
    for _ in range(6):
        failed = []
        for table in pending:
            try:
                async with db.engine.begin() as conn:
                    await conn.execute(text(f"DELETE FROM {table} WHERE agent_id = :a"), {"a": agent_id})
            except Exception:
                failed.append(table)
        pending = failed
        if not pending:
            break
    async with db.engine.begin() as conn:
        await conn.execute(text("DELETE FROM nous_system.agents WHERE id = :a"), {"a": agent_id})
    return pending


@pytest.mark.postgres_only
@pytest.mark.parametrize(("flag", "wired"), [("true", True), ("false", False)])
async def test_sleep_stack_wires_the_profile_card_writer_behind_its_flag(db, flag: str, wired: bool) -> None:
    """As main.py does: with sleep on, the writer is injected exactly when its flag is on."""
    from conftest import MockEmbeddingProvider

    from nous.heart.profile_card import ProfileCardWriter

    db_url = db.engine.url.render_as_string(hide_password=False)
    settings = arms.build_settings(db_url, "beam-profile-card-check", {"NOUS_PROFILE_CARD_WRITER_ENABLED": flag})
    async with harness.open_stack(settings, llm=FakeLLM(), embeddings=MockEmbeddingProvider(), sleep=True) as stack:
        writer = stack.sleep_handler._profile_card_writer
    assert isinstance(writer, ProfileCardWriter) is wired


@pytest.mark.postgres_only
async def test_offline_smoke_ingest_answer_score(tmp_path: Path, db) -> None:
    from conftest import MockEmbeddingProvider
    from sqlalchemy import text

    db_url = db.engine.url.render_as_string(hide_password=False)
    chats = write_conversation(tmp_path / "chats")
    out = tmp_path / "out"
    out.mkdir()
    tag = uuid.uuid4().hex[:8]
    agent = f"beam-100k-1-{tag}"
    common = ["--chats-dir", str(chats), "--bucket", "100K", "--conv", "1", "--db-url", db_url]
    common += ["--agent-tag", tag, "--i-know-this-is-scratch"]  # the CI test database is named "nous"
    llm = FakeLLM()
    try:
        ingest_args = beam_run.parse_args(
            ["ingest", *common, "--out-dir", str(out), "--sleep", "--set", "NOUS_SLEEP_REFLECTION_HOURS=200000"]
        )
        [record] = await beam_run.cmd_ingest(ingest_args, llm=llm, embeddings=MockEmbeddingProvider(), prepare=False)
        assert record["agent_id"] == agent
        assert record["sessions"] == 4 and record["sleep_cycles"] == 2
        assert record["rows"]["episodes"] == 4
        assert record["rows"]["facts"] >= 3, record
        assert record["settings"]["sleep_reflection_hours"] == 200000
        assert record["override_names"] == ["NOUS_SLEEP_REFLECTION_HOURS"] and "overrides" not in record
        assert record["eval_overrides"] == arms.EVAL_OVERRIDES and record["clock_notes"]
        assert llm.routes.count("summary") == 3  # the trivial session's transcript is too short to summarize
        assert {"outcome", "correction"} <= set(llm.routes)
        saved = json.loads((out / f"ingest-100K-1-{tag}.json").read_text(encoding="utf-8"))
        assert saved["agent_id"] == agent

        async with db.session() as s:
            episodes = (
                await s.execute(
                    text(
                        "SELECT started_at, ended_at, transcript FROM heart.episodes"
                        " WHERE agent_id = :a ORDER BY started_at"
                    ),
                    {"a": agent},
                )
            ).all()
            facts = (
                await s.execute(
                    text("SELECT created_at, learned_at, source, category FROM heart.facts WHERE agent_id = :a"),
                    {"a": agent},
                )
            ).all()
            signals = (
                (
                    await s.execute(
                        text("SELECT signal_type FROM heart.outcome_signals WHERE agent_id = :a"), {"a": agent}
                    )
                )
                .scalars()
                .all()
            )
        dates = [e.started_at.date().isoformat() for e in episodes]
        assert dates == ["2024-03-15", "2024-03-15", "2024-04-05", "2024-04-05"]
        # The trivial session is soft-deleted, never ended (end_episode would set ended_at and the transcript).
        assert episodes[3].ended_at is None and episodes[3].transcript is None
        assert all(e.ended_at and e.ended_at > e.started_at and e.transcript for e in episodes[:3]), [
            (e.started_at, e.ended_at, bool(e.transcript)) for e in episodes
        ]
        assert episodes[0].transcript.startswith("User: I am planning the Larkspur garden project")
        # Every fact carries its session's (or the following sleep's) BEAM date, never the wall clock.
        assert facts and all(f.created_at.year == 2024 and f.learned_at.year == 2024 for f in facts)
        assert "corrected" in signals
        assert any(f.source == "correction_extraction" and f.category == "rule" for f in facts)

        async with harness.open_stack(
            arms.build_settings(db_url, agent, {}), llm=llm, embeddings=MockEmbeddingProvider()
        ) as stack:
            # The facts' date columns: mentioned_at joins once its migration (#839) lands.
            async with db.session() as s:
                has_mentioned = (
                    await s.execute(
                        text(
                            "SELECT count(*) FROM information_schema.columns WHERE table_schema = 'heart'"
                            " AND table_name = 'facts' AND column_name = 'mentioned_at'"
                        )
                    )
                ).scalar_one()
            assert stack.fact_time_columns == ("created_at", "learned_at", "mentioned_at")[: 3 if has_mentioned else 2]
            with pytest.raises(SystemExit, match="already has"):  # a second ingest into the agent
                await harness.ingest_conversation(stack, corpus.load_conversation(chats, "100K", "1"))

        for mode in ("preturn", "recall_deep"):
            mode_out = out / mode
            args = beam_run.parse_args(
                ["answer", *common, "--out-dir", str(mode_out), "--mode", mode, "--answer-model", "fake-model"]
            )
            mode_out.mkdir()
            assert await beam_run.cmd_answer(args, llm=llm, embeddings=MockEmbeddingProvider()) == 2
            assert await beam_run.cmd_answer(args, llm=llm, embeddings=MockEmbeddingProvider()) == 0  # resumes
            answers = [json.loads(x) for x in (mode_out / "answers.jsonl").read_text(encoding="utf-8").splitlines()]
            assert [(a["ability"], a["error"]) for a in answers] == [
                ("information_extraction", None),
                ("event_ordering", None),
            ]
            assert all(a["answer"] == ANSWER and a["stop_reason"] == "end_turn" for a in answers)
            assert all("Larkspur" in a["context"] for a in answers), [a["context"][:300] for a in answers]
            if mode == "preturn":
                assert answers[0]["retrieval"]["plan_queries"].get("fact")
                assert answers[0]["retrieval"]["recalled_ids"].get("fact")
            run_info = json.loads((mode_out / "run.json").read_text(encoding="utf-8"))
            assert run_info["mode"] == mode and run_info["answer_model"] == "fake-model" and run_info["git"]["sha"]
            assert run_info["recall_deep_limit"] == harness.RECALL_DEEP_LIMIT and run_info["clock_notes"]
            assert run_info["override_names"] == [] and "overrides" not in run_info

            summary = await beam_run.cmd_score(
                beam_run.parse_args(["score", "--out-dir", str(mode_out)]), judge=exact_match_judge()
            )
            assert summary["n_errors"] == 0
            assert summary["abilities"]["information_extraction"]["score"] == pytest.approx(1.0)
            assert summary["abilities"]["event_ordering"]["score"] == pytest.approx(1.0)  # tau_norm
            assert summary["event_ordering"]["aligned_events_per_question"] == [2]
            scored = [json.loads(x) for x in (mode_out / "scores.jsonl").read_text(encoding="utf-8").splitlines()]
            eo = next(r["score"] for r in scored if r["ability"] == "event_ordering")
            assert eo["n_system"] == 3 and eo["f1"] == pytest.approx(0.8)  # the trailing blank line counts
            result = json.loads((mode_out / "results" / "100K" / "1" / "result.json").read_text(encoding="utf-8"))
            assert set(result) == {"information_extraction", "event_ordering"}
            assert result["event_ordering"][0]["llm_response"] == ANSWER
    finally:
        leftover_tables = await purge_agent(db, agent)
    assert leftover_tables == []
    async with db.session() as s:
        left = (
            await s.execute(
                text(
                    "SELECT (SELECT count(*) FROM heart.episodes WHERE agent_id = :a)"
                    " + (SELECT count(*) FROM heart.facts WHERE agent_id = :a)"
                    " + (SELECT count(*) FROM heart.outcome_signals WHERE agent_id = :a)"
                    " + (SELECT count(*) FROM brain.graph_edges WHERE agent_id = :a)"
                    " + (SELECT count(*) FROM nous_system.agents WHERE id = :a)"
                ),
                {"a": agent},
            )
        ).scalar_one()
    assert left == 0
