"""HaluMem harness: the data loader, the adapter-output plumbing, and an offline
end-to-end run (Postgres, fake LLM, mock embeddings, fake answer model).

The fixture user and the HaluMem source files are written by the tests; no HaluMem
data is used and no LLM API is called.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_beam_harness import PLAN_DETAIL, FakeLLM, purge_agent

from nous_eval.halumem import corpus, runner
from nous_eval.halumem import run as halumem_run

ANSWER = "April 12"
PROMPTS_PY = '''
raise RuntimeError("the harness must read this file, never run it")
PROMPT_MEMZERO = """Answer from these memories.
{context}
Question: {question}
Answer:"""
'''
MEMZERO_PY = '''
import mem0  # never imported: the harness only parses this file
TEMPLATE_MEM0 = """Memories for user {user_id}:

    {memories}
"""
'''


def point(content: str, *, update: bool = False, original: list[str] | None = None, source: str = "secondary") -> dict:
    return {
        "index": 1,
        "memory_content": content,
        "memory_type": "Event Memory",
        "memory_source": source,
        "is_update": "True" if update else "False",
        "original_memories": original or [],
        "importance": 0.8,
        "timestamp": "Mar 15, 2025, 09:00:00",
    }


def session(start: str, end: str, user: str, points: list[dict], questions: list[dict] | None = None) -> dict:
    raw = {
        "start_time": start,
        "end_time": end,
        "dialogue": [
            {"role": "user", "content": user, "timestamp": start, "dialogue_turn": 0},
            {"role": "assistant", "content": "Noted: " + PLAN_DETAIL, "timestamp": start, "dialogue_turn": 0},
        ],
        "memory_points": points,
    }
    if questions is not None:
        raw["questions"] = questions
    return raw


def write_fixture(root: Path) -> tuple[Path, Path, dict]:
    """A one-user HaluMem file (three sessions over two days) and a HaluMem checkout stub."""
    user = {
        "uuid": uuid.uuid4().hex,
        "persona_info": "Name: Ada Lark; Gender: Female; Birth Date: 1990-01-01; Location: Denver",
        "sessions": [
            session(
                "Mar 15, 2025, 09:00:00",
                "Mar 15, 2025, 09:30:00",
                "I am planning the Larkspur garden project and the first planting is on March 29.",
                [point("Ada's first Larkspur planting is on March 29.")],
                questions=[],
            ),
            session(
                "Mar 15, 2025, 15:00:00",
                "Mar 15, 2025, 15:20:00",
                "Next I set up the irrigation timer for the Larkspur beds, watering at 6am.",
                [point("Ada set the Larkspur irrigation timer to 6am.")],
            ),
            session(
                "Apr 05, 2025, 10:00:00",
                "Apr 05, 2025, 10:40:00",
                "Update for Larkspur: the first planting moved to April 12 because of frost.",
                [
                    point(
                        "Ada's first Larkspur planting moved to April 12 because of frost.",
                        update=True,
                        original=["Ada's first Larkspur planting is on March 29."],
                    )
                ],
                questions=[
                    {
                        "question": "When is Ada's first Larkspur planting?",
                        "answer": "April 12",
                        "evidence": [],
                        "difficulty": "easy",
                        "question_type": "Dynamic Update",
                    }
                ],
            ),
        ],
    }
    data = root / "HaluMem-Medium.jsonl"
    data.write_text(json.dumps(user) + "\n", encoding="utf-8")
    eval_dir = root / "HaluMem" / "eval"
    eval_dir.mkdir(parents=True)
    (eval_dir / "prompts.py").write_text(PROMPTS_PY, encoding="utf-8")
    (eval_dir / "eval_memzero.py").write_text(MEMZERO_PY, encoding="utf-8")
    return data, root / "HaluMem", user


# ---------------------------------------------------------------------------
# Loader and plumbing (no database)
# ---------------------------------------------------------------------------


def test_parse_time_and_user_name_match_the_reference_adapters() -> None:
    assert corpus.parse_time("Sep 04, 2025, 18:42:18") == datetime(2025, 9, 4, 18, 42, 18, tzinfo=UTC)
    assert corpus.user_name("Name: Martin Mark; Gender: Male; Birth Date: 1996-08-02") == "Martin Mark"
    with pytest.raises(ValueError):
        corpus.user_name("Gender: Male")


@pytest.mark.parametrize(
    ("end", "expected"),
    [
        ("Sep 04, 2025, 21:12:18", datetime(2025, 9, 4, 21, 12, 18, tzinfo=UTC)),
        ("", datetime(2025, 9, 4, 18, 47, 18, tzinfo=UTC)),  # missing: start + 5 minutes
        ("Sep 04, 2025, 18:00:00", datetime(2025, 9, 4, 18, 47, 18, tzinfo=UTC)),  # before the start
    ],
)
def test_session_end(end: str, expected: datetime) -> None:
    assert corpus.session_end({"start_time": "Sep 04, 2025, 18:42:18", "end_time": end}) == expected


def test_as_session_keeps_both_roles_in_order() -> None:
    raw = session("Mar 15, 2025, 09:00:00", "Mar 15, 2025, 09:30:00", "hello there", [])
    s = corpus.as_session(raw, 3)
    assert [m.role for m in s.messages] == ["user", "assistant"] and s.index == 3
    assert s.messages[0].content == "hello there" and s.started_at.tzinfo is UTC


@pytest.mark.parametrize(
    ("is_update", "original", "searched"),
    [("True", ["old"], True), ("True", [], False), ("False", ["old"], False), ("False", [], False)],
)
def test_update_points_are_the_ones_the_reference_adapters_search(
    is_update: str, original: list[str], searched: bool
) -> None:
    assert corpus.is_update_point({"is_update": is_update, "original_memories": original}) is searched


def test_string_constants_are_read_without_running_the_file(tmp_path: Path) -> None:
    path = tmp_path / "prompts.py"
    path.write_text(PROMPTS_PY, encoding="utf-8")
    assert corpus.load_string_constant(path, "PROMPT_MEMZERO").startswith("Answer from these memories.")
    with pytest.raises(SystemExit, match="PROMPT_ZEP"):
        corpus.load_string_constant(path, "PROMPT_ZEP")


def test_reflection_window_reaches_back_from_the_simulated_sleep() -> None:
    now = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
    sleep_at = datetime(2025, 3, 15, 15, 21, tzinfo=UTC)
    hours = runner.reflection_hours(now, sleep_at, 24)
    cutoff = now - timedelta(hours=hours)  # what list_recent filters started_at on
    assert sleep_at - timedelta(hours=25) < cutoff <= sleep_at - timedelta(hours=24)
    assert runner.reflection_hours(sleep_at, sleep_at, 24) == 24


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("User is employed as a director.", "Ada Lark is employed as a director."),
        ("User's monthly income is 15700 USD.", "Ada Lark's monthly income is 15700 USD."),
        ("The user's colleague Joshua helps.", "Ada Lark's colleague Joshua helps."),
        ("Joshua is the user's friend; the user agrees.", "Joshua is Ada Lark's friend; Ada Lark agrees."),
        ("The user dislikes snakes. User likes dogs.", "Ada Lark dislikes snakes. Ada Lark likes dogs."),
        ("The user's experience matters.", "Ada Lark's experience matters."),
        # A verb that is also a plural noun or a past tense is the owner's.
        ("The user needs to avoid peanuts.", "Ada Lark needs to avoid peanuts."),
        ("User reviews contracts for a living.", "Ada Lark reviews contracts for a living."),
        (
            "The user experiences migraines; the user focused on sleep.",
            "Ada Lark experiences migraines; Ada Lark focused on sleep.",
        ),
        # "user" as a common noun stays.
        ("She ran the user interview and the user test.", "She ran the user interview and the user test."),
        ("Other users and a username stay.", "Other users and a username stay."),
        ("Ada focuses on user experience and user feedback.", "Ada focuses on user experience and user feedback."),
        ("She improves the user experience.", "She improves the user experience."),
        ("User experience is her field.", "User experience is her field."),
        ("She favours user-centered design.", "She favours user-centered design."),
        ("She is a user of Slack.", "She is a user of Slack."),
        ("The user-friendly app helps.", "The user-friendly app helps."),
    ],
)
def test_name_owner(text: str, expected: str) -> None:
    assert runner.name_owner(text, "Ada Lark") == expected


def test_select_users() -> None:
    users = [{"uuid": str(i)} for i in range(3)]
    assert halumem_run.select_users(users, "all") == users
    assert [u["uuid"] for u in halumem_run.select_users(users, "2,0")] == ["2", "0"]
    with pytest.raises(SystemExit, match="3 users"):
        halumem_run.select_users(users, "3")


def test_merge_follows_data_order_and_skips_missing_users(tmp_path: Path) -> None:
    (tmp_path / "tmp").mkdir()
    for u in ("b", "a"):
        (tmp_path / "tmp" / f"{u}.json").write_text(json.dumps({"uuid": u}), encoding="utf-8")
    assert halumem_run.merge_results(tmp_path, ["a", "c", "b"]) == 2
    lines = (tmp_path / "nous_eval_results.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(x)["uuid"] for x in lines] == ["a", "b"]


def test_write_atomic_leaves_no_part_file(tmp_path: Path) -> None:
    (tmp_path / "tmp").mkdir()
    halumem_run.write_atomic(tmp_path / "tmp" / "a.json", json.dumps({"uuid": "a"}))
    (tmp_path / "tmp" / "b.json.part").write_text("{trunc", encoding="utf-8")  # a write a crash cut short
    assert sorted(p.name for p in (tmp_path / "tmp").iterdir()) == ["a.json", "b.json.part"]
    assert halumem_run.merge_results(tmp_path, ["a", "b"]) == 1


def test_resume_refuses_another_corpus(tmp_path: Path) -> None:
    run_path = tmp_path / "run.json"
    halumem_run._check_resume(run_path, {"data": "HaluMem-Medium.jsonl", "data_sha256": "aaa"})
    halumem_run._check_resume(run_path, {"data": "HaluMem-Medium.jsonl", "data_sha256": "aaa"})
    with pytest.raises(SystemExit, match="data_sha256"):
        halumem_run._check_resume(run_path, {"data": "HaluMem-Medium.jsonl", "data_sha256": "bbb"})


def test_score_rebuilds_the_merge_from_every_user_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two parallel runs: the merge the slower one wrote last lacks the other's user; score merges again."""
    args = halumem_run.parse_args(
        ["score", "--halumem-dir", str(tmp_path / "HaluMem"), "--version", "v", "--out-dir", str(tmp_path / "o")]
    )
    out = halumem_run.result_dir(args)
    (out / "tmp").mkdir(parents=True)
    (out / "run.json").write_text(json.dumps({"data_users": ["a", "b", "c"]}), encoding="utf-8")
    for u in ("b", "a"):
        (out / "tmp" / f"{u}.json").write_text(json.dumps({"uuid": u}), encoding="utf-8")
    (out / "nous_eval_results.jsonl").write_text(json.dumps({"uuid": "b"}) + "\n", encoding="utf-8")
    ran: list[list[str]] = []
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(halumem_run.subprocess, "run", lambda cmd, **_: ran.append(cmd))
    halumem_run.cmd_score(args)
    lines = (out / "nous_eval_results.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(x)["uuid"] for x in lines] == ["a", "b"]
    assert ran[0][-1] == "2"


def test_judge_command_runs_halumem_main_isolated() -> None:
    cmd = halumem_run.judge_command(Path("/x/HaluMem/eval"), "pilot", 1)
    assert cmd[:3] == ["uv", "run", "--no-project"] and "-I" in cmd
    assert cmd[-4:] == [str(Path("/x/HaluMem/eval")), "nous", "pilot", "1"]


# ---------------------------------------------------------------------------
# Offline run (Postgres)
# ---------------------------------------------------------------------------


@pytest.mark.postgres_only
async def test_offline_run_writes_the_adapter_output(tmp_path: Path, db) -> None:
    from conftest import MockEmbeddingProvider
    from sqlalchemy import text

    db_url = db.engine.url.render_as_string(hide_password=False)
    data, halumem_dir, user = write_fixture(tmp_path)
    tag = uuid.uuid4().hex[:8]
    agent = halumem_run.agent_id(user["uuid"], tag)
    out = tmp_path / "out"
    prompts: list[str] = []

    async def fake_answer(prompt: str) -> str:
        prompts.append(prompt)
        return ANSWER

    argv = ["run", "--data", str(data), "--users", "0", "--db-url", db_url, "--agent-tag", tag]
    argv += ["--halumem-dir", str(halumem_dir), "--out-dir", str(out), "--version", "t", "--sleep"]
    argv += ["--i-know-this-is-scratch"]  # the CI test database is named "nous"
    args = halumem_run.parse_args(argv)
    llm = FakeLLM()
    try:
        written = await halumem_run.cmd_run(
            args, llm=llm, embeddings=MockEmbeddingProvider(), answer=fake_answer, prepare=False
        )
        assert written == [user["uuid"]]
        result_dir = out / "results" / "nous-t"
        [line] = (result_dir / "nous_eval_results.jsonl").read_text(encoding="utf-8").splitlines()
        result = json.loads(line)
        assert result["uuid"] == user["uuid"] and result["user_name"] == "Ada Lark"
        sessions = result["sessions"]
        assert len(sessions) == 3
        run_stats = result["nous_run"]
        assert (run_stats["agent_id"], run_stats["sessions"]) == (agent, 3)
        assert run_stats["sleep_cycles"] == 1  # only between the two days
        assert run_stats["rows"]["episodes"] == 3
        # Every answered model call the stack made went through the usage counter (the fake reports no
        # tokens; an unrouted call raises, as a failed API call does, and has no usage to count).
        answered = [r for r in llm.routes if r != "unrouted"]
        assert sum(m["calls"] for m in run_stats["anthropic_usage"].values()) == len(answered) > 0
        assert [s["nous"]["slept_before"] for s in sessions] == [False, False, True]
        # Each session's own facts (the fake summarizer proposes its first user line), never the summary.
        assert any("March 29" in m for m in sessions[0]["extracted_memories"]), sessions[0]
        assert any("irrigation" in m for m in sessions[1]["extracted_memories"]), sessions[1]
        assert all(s["nous"]["summary"] not in s["extracted_memories"] for s in sessions)
        assert all(isinstance(s["add_dialogue_duration_ms"], float) for s in sessions)
        # The update point got the evaluator's probe; the plain points did not.
        [update] = sessions[2]["memory_points"]
        assert update["memories_from_system"] and all(": " in m for m in update["memories_from_system"])
        assert any("April 12" in m for m in update["memories_from_system"])
        assert "memories_from_system" not in sessions[0]["memory_points"][0]
        # Questions: the empty list is kept, the missing key stays missing, the asked one is answered.
        assert sessions[0]["questions"] == [] and "questions" not in sessions[1]
        [qa] = sessions[2]["questions"]
        assert qa["system_response"] == ANSWER and qa["answer"] == "April 12"
        assert qa["context"].startswith("Memories for user Ada Lark:") and "Larkspur" in qa["context"]
        assert prompts == [f"Answer from these memories.\n{qa['context']}\nQuestion: {qa['question']}\nAnswer:"]
        run_info = json.loads((result_dir / "run.json").read_text(encoding="utf-8"))
        assert run_info["answer_prompt"] == "PROMPT_MEMZERO" and run_info["sleep"] is True
        assert run_info["runtime_config"]["effective"]

        async with db.session() as s:
            facts = (
                await s.execute(
                    text("SELECT created_at, learned_at FROM heart.facts WHERE agent_id = :a"), {"a": agent}
                )
            ).all()
        # Every fact carries its session's (or the following sleep's) HaluMem date, never the wall clock.
        assert facts and all(f.created_at.year == 2025 and f.learned_at.year == 2025 for f in facts)

        # A second run resumes: the user's file exists, so nothing is ingested again.
        assert await halumem_run.cmd_run(args, llm=llm, embeddings=MockEmbeddingProvider(), answer=fake_answer) == []
        # Another arm, or a session limit, into the same out-dir is refused.
        for extra in (["--set", "NOUS_FACT_DEDUP_THRESHOLD=0.9"], ["--limit-sessions", "1"]):
            other = halumem_run.parse_args([*argv, *extra])
            with pytest.raises(SystemExit, match="other settings"):
                await halumem_run.cmd_run(other, llm=llm, embeddings=MockEmbeddingProvider(), answer=fake_answer)

        # A user that stopped part-way (its agent has episodes, its file is missing) runs again under
        # a fresh agent id, and the judge cache for that user is dropped.
        (result_dir / "tmp" / f"{user['uuid']}.json").unlink()
        (result_dir / "tmp2").mkdir()
        (result_dir / "tmp2" / f"{user['uuid']}.json").write_text("{}", encoding="utf-8")
        llm = FakeLLM()
        written = await halumem_run.cmd_run(args, llm=llm, embeddings=MockEmbeddingProvider(), answer=fake_answer)
        assert written == [user["uuid"]]
        rerun = json.loads((result_dir / "tmp" / f"{user['uuid']}.json").read_text(encoding="utf-8"))
        assert rerun["nous_run"]["agent_id"] == f"{agent}-a2"
        assert not (result_dir / "tmp2" / f"{user['uuid']}.json").exists()
    finally:
        leftover = await purge_agent(db, agent) + await purge_agent(db, f"{agent}-a2")
    assert leftover == []


def test_next_agent_id_skips_ids_with_episodes() -> None:
    assert halumem_run.next_agent_id("halumem-x-t", set()) == "halumem-x-t"
    assert halumem_run.next_agent_id("halumem-x-t", {"halumem-x-t"}) == "halumem-x-t-a2"
    assert halumem_run.next_agent_id("halumem-x-t", {"halumem-x-t", "halumem-x-t-a2"}) == "halumem-x-t-a3"


@pytest.mark.postgres_only
async def test_stored_since_counts_new_and_confirmed_facts(db) -> None:
    from types import SimpleNamespace

    from sqlalchemy import text

    agent = f"halumem-stored-{uuid.uuid4().hex[:8]}"
    stack = SimpleNamespace(db=db, settings=SimpleNamespace(agent_id=agent))
    ids = [uuid.uuid4() for _ in range(4)]

    async def insert(fid: uuid.UUID, content: str, active: bool, n: int) -> None:
        async with db.session() as s:
            await s.execute(
                text(
                    "INSERT INTO heart.facts (id, agent_id, content, active, confirmation_count, source)"
                    " VALUES (:i, :a, :c, :act, :n, 'test:x')"
                ),
                {"i": fid, "a": agent, "c": content, "act": active, "n": n},
            )
            await s.commit()

    try:
        await insert(ids[0], "old, untouched", True, 0)
        await insert(ids[1], "old, confirmed again", True, 1)
        before = await runner.fact_counts(stack)
        async with db.session() as s:
            await s.execute(text("UPDATE heart.facts SET confirmation_count = 2 WHERE id = :i"), {"i": ids[1]})
            await s.commit()
        await insert(ids[2], "new", True, 0)
        await insert(ids[3], "new, then superseded", False, 0)
        stored = await runner.stored_since(stack, before)
    finally:
        async with db.session() as s:
            await s.execute(text("DELETE FROM heart.facts WHERE agent_id = :a"), {"a": agent})
            await s.commit()
    assert sorted(stored["memories"]) == ["new", "old, confirmed again"]
    assert (stored["new_facts"], stored["confirmed_facts"], stored["new_then_inactive"]) == (1, 1, 1)
    assert stored["new_sources"] == {"test": 1}
