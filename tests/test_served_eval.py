"""Served-precision eval library (nous_eval.served): statistics, settings resolution, the shared
judge cache, the scorer and compare (offline, fake judge), and one Postgres smoke that runs both
entry points end to end with mock embeddings and a fake judge on its own agent.

All data here is synthetic; no LLM or embedding API is called.
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from nous_eval.served import cli, compare, db, inputs, judge, pin, score, settings, stats

# ---------------------------------------------------------------------------
# A fake judge: an item is relevant iff it shares a word of 6+ letters with the query
# ---------------------------------------------------------------------------


def _words(text: str) -> set[str]:
    return {w.lower() for w in re.findall(r"[A-Za-z]{6,}", text)}


class FakeJudgeClient:
    def __init__(self, fail_first: int = 0) -> None:
        self.calls: list[dict] = []
        self.fail_first = fail_first

    async def call(self, payload: dict) -> SimpleNamespace:
        self.calls.append(payload)
        if self.fail_first:
            self.fail_first -= 1
            raise RuntimeError("transient")
        user = payload["messages"][0]["content"]
        query = re.search(r"<<<\n(.*?)\n>>>", user, re.S).group(1)
        labels = {}
        for m in re.finditer(r"^(i\d+) \[\w+\]: (.*)$", user, re.M):
            labels[m.group(1)] = bool(_words(query) & _words(m.group(2)))
        return SimpleNamespace(
            content=[{"type": "text", "text": json.dumps({"labels": labels})}],
            usage={"input_tokens": 1000, "output_tokens": 100},
            stop_reason="end_turn",
        )

    async def close(self) -> None:
        return None


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------


def test_paired_bootstrap_identical_arms_is_exactly_zero() -> None:
    pairs = [(float(i % 3), float(i % 3)) for i in range(50)]
    assert stats.paired_bootstrap(pairs, stats.mean) == (0.0, 0.0, 0.0)


def test_paired_bootstrap_constant_shift_is_exact_and_ci_brackets_a_noisy_shift() -> None:
    assert stats.paired_bootstrap([(float(i), i + 2.0) for i in range(40)], stats.mean) == (2.0, 2.0, 2.0)
    pairs = [(float(i % 5), (i % 5) + (1.0 if i % 2 else 0.0)) for i in range(200)]
    point, lo, hi = stats.paired_bootstrap(pairs, stats.mean)
    assert point == pytest.approx(0.5) and lo < 0.5 < hi and lo > 0.3 and hi < 0.7


def test_paired_bootstrap_ratio_and_percentiles() -> None:
    pairs = [((1, 2), (2, 2)), ((0, 2), (1, 2))]
    point, _, _ = stats.paired_bootstrap(pairs, stats.ratio)
    assert point == pytest.approx(0.5)  # 1/4 -> 3/4
    assert stats.pct([5, 1, 3, 2, 4], 0.5) == 3 and stats.pct([1, 2, 3, 4], 0.95) == 4
    assert stats.ratio([]) == 0.0
    with pytest.raises(ValueError):
        stats.paired_bootstrap([], stats.mean)


def test_kappa_hand_case() -> None:
    a = {1: True, 2: True, 3: False, 4: False}
    b = {1: True, 2: False, 3: False, 4: False, 5: True}
    k = stats.kappa(a, b)
    assert k["shared"] == 4 and k["agreement"] == 0.75 and k["kappa"] == pytest.approx(0.5)
    assert stats.kappa({}, b)["agreement"] is None


# ---------------------------------------------------------------------------
# settings resolution
# ---------------------------------------------------------------------------

DB = {"db_host": "127.0.0.1", "db_port": 5433, "db_user": "nous", "db_password": "pw", "db_name": "nous_eval_t"}


def _base_files(tmp_path: Path) -> tuple[Path, Path]:
    snap = tmp_path / "snap.json"
    snap.write_text(
        json.dumps(
            {
                "settings": {
                    "keyed_fact_leg_enabled": True,
                    "keyed_fact_leg_rounds": 2,
                    "cross_encoder_model": "model-a",
                    "context_window": 1234,
                    "rrf_k": 45,
                    "openai_api_key": "sk-never",
                    "db_name": "nous",
                    "no_such_field": 1,
                }
            }
        ),
        encoding="utf-8",
    )
    flags = tmp_path / "flags.env"
    flags.write_text(
        "NOUS_RRF_K=30\nNOUS_QUERY_EXPANSION_ENABLED=true\nNOUS_QUERY_EXPANSION_ENABLED=false\n"
        "NOUS_CROSS_ENCODER_MAX_CANDIDATES=20\nNOUS_NOT_A_SETTING=1\n",
        encoding="utf-8",
    )
    return snap, flags


def test_resolve_layers_in_order_and_records_sources(tmp_path: Path) -> None:
    snap, flags = _base_files(tmp_path)
    r = settings.resolve([snap, flags], ["NOUS_CROSS_ENCODER_MAX_CANDIDATES=10"], db=DB, agent_id="a1")
    s = r.settings
    assert s.keyed_fact_leg_enabled is True and s.keyed_fact_leg_rounds == 2
    assert s.context_window == 1234  # an aliased field: applied through its alias, not silently dropped
    assert s.rrf_k == 30 and r.sources["rrf_k"] == "base:flags.env"  # a later layer wins
    assert s.query_expansion_enabled is False  # a repeated dotenv name: the last line wins
    assert s.cross_encoder_max_candidates == 10 and r.sources["cross_encoder_max_candidates"] == "arm"
    assert r.arm_overrides == {"cross_encoder_max_candidates": 10}
    assert s.event_bus_enabled is False and r.sources["event_bus_enabled"] == "eval"
    assert "query_expansion_enabled" not in [k for k, v in r.sources.items() if v == "eval"]
    assert s.db_name == "nous_eval_t" and s.agent_id == "a1"
    assert sorted(r.ignored["snap.json"]) == ["db_name", "no_such_field", "openai_api_key"]
    assert r.ignored["flags.env"] == ["NOUS_NOT_A_SETTING"]
    assert [f["name"] for f in r.base_files] == ["snap.json", "flags.env"]


def test_record_never_holds_credentials_or_connection_fields(tmp_path: Path) -> None:
    snap, flags = _base_files(tmp_path)
    rec = settings.resolve([snap, flags], [], db=DB, agent_id="a1").record()
    assert rec["rrf_k"] == {"value": 30, "source": "base:flags.env"}
    assert rec["vector_weight"]["source"] == "default"  # a RECORD_ALWAYS knob no layer named
    assert not [k for k in rec if settings.NEVER_APPLY.search(k)]
    assert "pw" not in json.dumps(rec) and "sk-never" not in json.dumps(rec)


@pytest.mark.parametrize("pair", ["NOUS_NOPE=1", "DB_NAME=nous", "OPENAI_API_KEY=x", "NOUS_AGENT_ID=x", "rrf_k"])
def test_set_refuses_unknown_connection_credential_and_malformed(pair: str) -> None:
    with pytest.raises(SystemExit):
        settings.parse_overrides([pair])


def test_set_parses_like_the_environment() -> None:
    assert settings.parse_overrides(["nous_mmr_enabled=TRUE", "NOUS_RRF_K=12"]) == {"mmr_enabled": True, "rrf_k": 12}


def test_cross_field_validation_sees_base_and_arm_together(tmp_path: Path) -> None:
    flags = tmp_path / "flags.env"
    flags.write_text("NOUS_CONTEXT_QUERY_MODE=conditional\n", encoding="utf-8")
    r = settings.resolve([flags], ["NOUS_CONTEXT_SKIP_UNCHANGED_QUERY=true"], db=DB, agent_id="a1")
    assert r.settings.context_query_mode == "conditional"
    assert r.settings.context_skip_unchanged_query is True  # the arm is on, not reset by a lone-layer check
    assert r.arm_overrides == {"context_skip_unchanged_query": True}


def test_a_validator_that_changes_a_requested_value_stops_the_run(tmp_path: Path) -> None:
    flags = tmp_path / "flags.env"
    flags.write_text("NOUS_CONTEXT_QUERY_MODE=bag\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="context_skip_unchanged_query.*a validator changed it"):
        settings.resolve([flags], ["NOUS_CONTEXT_SKIP_UNCHANGED_QUERY=true"], db=DB, agent_id="a1")


def test_a_json_snapshot_value_is_parsed_like_the_field(tmp_path: Path) -> None:
    snap = tmp_path / "snap.json"
    snap.write_text(json.dumps({"settings": {"context_budget_overrides": {"facts": 900}}}), encoding="utf-8")
    flags = tmp_path / "flags.env"
    flags.write_text("NOUS_RRF_K=30\n", encoding="utf-8")
    s = settings.resolve([snap, flags], ['NOUS_CONTEXT_BUDGET_OVERRIDES={"facts": 700}'], db=DB, agent_id="a1").settings
    assert s.context_budget_overrides == {"facts": 700} and s.rrf_k == 30


def test_never_apply_keeps_retrieval_knobs_whose_names_look_like_secrets() -> None:
    for name in (
        "keyed_fact_leg_enabled",
        "hybrid_search_keyword_enabled",
        "entity_key_min_chars",
        "profile_card_max_tokens",
        "supersession_key_resolution_enabled",
    ):
        assert not settings.NEVER_APPLY.search(name), name
    for name in (
        "openai_api_key",
        "anthropic_auth_token",
        "github_token",
        "db_password",
        "agent_id",
        "google_service_account_json",
        "telegram_chat_id",
        "api_base_url",
    ):
        assert settings.NEVER_APPLY.search(name), name


# ---------------------------------------------------------------------------
# inputs, pinning, database guards
# ---------------------------------------------------------------------------


def _gold_file(path: Path, gtype: str, rows: list[tuple[str, str, str]]) -> Path:
    path.write_text(
        "".join(
            json.dumps(
                {
                    "id": i,
                    "question": q,
                    "gold": [g],
                    "gold_type": gtype,
                    "gold_category": "c",
                    "gold_content": f"content {g}",
                }
            )
            + "\n"
            for i, q, g in rows
        ),
        encoding="utf-8",
    )
    return path


def test_inputs_keys_sets_and_hash(tmp_path: Path) -> None:
    q = tmp_path / "q.jsonl"
    q.write_text(
        json.dumps({"key": "Q000", "query": "alpha", "logged_at": "2026-10-01T10:00:00+00:00", "session_id": "chat-s1"})
        + "\n",
        encoding="utf-8",
    )
    gf = _gold_file(tmp_path / "f.jsonl", "fact", [("A000", "where is alpha", "f-1")])
    ge = _gold_file(tmp_path / "e.jsonl", "episode", [("A000", "what happened", "e-1")])
    items = inputs.recall_items(q, [gf, ge], "2026-11-05")
    assert [(i["key"], i["set"], i["as_of"], i["source_session"]) for i in items] == [
        ("Q000", "prod", "2026-10-01", "chat-s1"),  # the logged call's own conversation
        ("gf-A000", "gold", "2026-11-05", None),  # a gold question has none
        ("ge-A000", "gold", "2026-11-05", None),
    ]
    assert items[1]["gold"]["ids"] == ["f-1"]
    h = inputs.inputs_sha256(items)
    assert h == inputs.inputs_sha256(list(reversed(items)))
    items[0]["text"] = "beta"
    assert inputs.inputs_sha256(items) != h
    with pytest.raises(SystemExit, match="duplicate"):
        inputs.recall_items(None, [gf, gf], "2026-11-05")
    assert len(inputs.sample(items, 2, 1)) == 2 and inputs.sample(items, 0, 1) == items


def test_preturn_items_from_prompts_file(tmp_path: Path) -> None:
    p = tmp_path / "prompts.json"
    p.write_text(
        json.dumps(
            {
                "prompts": [
                    {
                        "kind": "dynamic",
                        "group": "g",
                        "group_rows": 2,
                        "session_id": "dynamic-check-x",
                        "logged_query": "short",
                        "full_text": None,
                        "recovered": False,
                    },
                    {
                        "kind": "subtask",
                        "group": "h",
                        "group_rows": 1,
                        "session_id": "subtask-y",
                        "logged_query": "x" * 500,
                        "full_text": "full text",
                        "recovered": True,
                    },
                    {
                        "kind": "dag",
                        "group": "i",
                        "group_rows": 1,
                        "session_id": "dag-summary-z",
                        "logged_query": "summarise the run",
                        "full_text": None,
                        "recovered": False,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    gold = _gold_file(tmp_path / "d.jsonl", "decision", [("D000", "why did we pick it", "d-1")])
    items = inputs.preturn_items(p, [gold], "2026-11-05")
    assert [(i["key"], i["text"], i["turn_number"]) for i in items] == [
        ("B000", "short", 1),
        ("B001", "full text", 1),
        ("B002", "summarise the run", 1),
        ("gd-D000", "why did we pick it", 2),
    ]
    assert items[0]["complete"] and items[1]["complete"]
    # pre_turn's first turn: turn 1 of a session that does not run as a subtask
    assert [i["first_turn"] for i in items] == [False, False, True, False]
    assert [i["source_session"] for i in items] == ["dynamic-check-x", "subtask-y", "dag-summary-z", None]


def test_check_code_refuses_split_roots_other_roots_and_dirty() -> None:
    info = {"root": "/a", "nous_eval_root": "/a", "dirty_files": 0}
    pin.check_code(info, expected_root=None, allow_dirty=False)
    with pytest.raises(SystemExit, match="different roots"):
        pin.check_code({**info, "nous_eval_root": "/b"}, expected_root=None, allow_dirty=False)
    with pytest.raises(SystemExit, match="pinned root"):
        pin.check_code(info, expected_root="/c", allow_dirty=False)
    with pytest.raises(SystemExit, match="uncommitted"):
        pin.check_code({**info, "dirty_files": 2}, expected_root=None, allow_dirty=False)
    pin.check_code({**info, "dirty_files": 2}, expected_root=None, allow_dirty=True)


def test_reexec_strips_code_and_child_env_hides_nous_vars(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert cli._strip_code_arg(["recall", "run", "--code", "abc", "--tag", "t", "--code=x"]) == [
        "recall",
        "run",
        "--tag",
        "t",
    ]
    monkeypatch.setenv("NOUS_RRF_K", "99")
    monkeypatch.setenv("DB_NAME", "nous")
    env = pin.child_env(tmp_path)
    assert "NOUS_RRF_K" not in env and "DB_NAME" not in env and env["PYTHONPATH"] == str(tmp_path)
    assert env["PYTHONHASHSEED"] == "0"
    seen = {}

    def fake_run(cmd, cwd, env):
        seen.update(cmd=cmd, cwd=cwd)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(pin.subprocess, "run", fake_run)
    assert pin.reexec(tmp_path, ["recall", "run"], cwd=tmp_path / "start") == 0
    # -P: the working directory never shadows the pinned root on sys.path
    assert seen["cmd"][1:4] == ["-P", "-m", "nous_eval.served"] and seen["cwd"] == str(tmp_path / "start")


def test_path_arguments_resolve_against_the_starting_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    args = cli.parse_args(
        [
            "recall",
            "run",
            "--data-dir",
            "d",
            "--tag",
            "t",
            "--queries",
            "d/q.jsonl",
            "--base",
            "b.json",
            "--gold",
            "g.jsonl",
            "--db-name",
            "nous_eval_x",
        ]
    )
    cli._absolutize(args)
    assert args.data_dir == tmp_path / "d" and args.queries == tmp_path / "d" / "q.jsonl"
    assert args.base == [tmp_path / "b.json"] and args.gold == [tmp_path / "g.jsonl"]
    assert args.warm == -1  # every input is warmed by default


def test_clone_names_are_bounded_valid_and_distinct() -> None:
    tag = "a" * 40  # the longest tag --tag accepts
    names = {db.clone_name(f"{entry}_{t}") for entry in ("recall", "preturn") for t in (tag, tag[:-1] + "b")}
    assert len(names) == 4
    for n in names:
        assert db.check_ident(n) == n and len(n) <= 63 and n.startswith(db.CLONE_PREFIX)
    assert db.clone_name("recall_Base-1") == "nous_eval_served_run_recall_base_1"  # short names unchanged


async def test_database_guards() -> None:
    assert db.clone_name("recall_Base-1") == "nous_eval_served_run_recall_base_1"
    for ok in ("nous_eval_x", "nous_test_p0", "beam_1"):
        assert db.check_scratch(ok) == ok
    assert db.check_scratch("nous", any_scratch_name=True) == "nous"  # CI's test database
    for bad in ("nous", "postgres"):
        with pytest.raises(SystemExit):
            db.check_scratch(bad)
    # a prod copy, a served template or another run's clone: refused whatever the flag says
    for bad in ("nous_prod_x", "nous_eval_served_tpl_x", "nous_eval_served_run_x"):
        for flag in (False, True):
            with pytest.raises(SystemExit):
                db.check_scratch(bad, any_scratch_name=flag)
    with pytest.raises(SystemExit):
        db.check_ident('x"; DROP DATABASE y; --')
    conn = {"db_user": "u", "db_password": "p", "db_host": "h", "db_port": 1}
    for name, created in (
        ("nous_eval_served_tpl_x", None),
        ("nous_eval_served_run_a", "nous_eval_served_run_b"),
        ("nous_eval_served_tpl_x", "nous_eval_served_tpl_x"),
    ):
        with pytest.raises(SystemExit, match="not the clone this run created"):
            await db.drop_clone(conn, name, created=created)


# ---------------------------------------------------------------------------
# judge cache and judge runner
# ---------------------------------------------------------------------------


def _recall_row(key: str, text: str, items: list[tuple[str, str, str]], **kw) -> dict:
    """items: (type:id, leg, content)"""
    return {
        "key": key,
        "set": kw.pop("set", "prod"),
        "text": text,
        "wall_ms": kw.pop("ms", 100.0),
        "chars": kw.pop("chars", 400),
        "rendered": [{"k": k, "leg": leg, "rank": i + 1, "score": 0.5} for i, (k, leg, _) in enumerate(items)],
        "contents": {k: c for k, _, c in items},
        **kw,
    }


def test_label_key_follows_what_the_judge_sees() -> None:
    k = judge.label_key("recall", "q", "fact", "text")
    assert k == judge.label_key("recall", "q", "fact", "text")
    assert (
        len(
            {
                k,
                judge.label_key("recall", "q", "fact", "text2"),
                judge.label_key("recall", "q2", "fact", "text"),
                judge.label_key("recall", "q", "episode", "text"),
                judge.label_key("preturn", "q", "fact", "text"),
            }
        )
        == 5
    )
    long = "x" * 7000
    assert judge.shown_prompt("preturn", long).count("[... middle omitted ...]") == 1
    assert judge.shown_prompt("recall", long) == long
    row = _recall_row("Q1", "q", [("fact:1", "heart_primary", "y" * 900)])
    [u] = judge.units_for_rows("recall", [row])
    assert len(u["items"][0]["shown"]) == 800


async def test_judge_units_shares_labels_and_retries_failures(tmp_path: Path) -> None:
    cache = judge.LabelCache(tmp_path / "judge" / "m.jsonl")
    ledger = judge.CostLedger(tmp_path / "cost.jsonl")
    rows = [
        _recall_row(
            "Q1",
            "larkspur garden plan",
            [("fact:1", "heart_primary", "the larkspur beds"), ("fact:2", "chunk", "unrelated tomato")],
        ),
        # a second arm's row rendering the same items for the same query: no new judge call
        _recall_row(
            "Q1b",
            "larkspur garden plan",
            [("fact:2", "chunk", "unrelated tomato"), ("fact:1", "heart_primary", "the larkspur beds")],
        ),
    ]
    client = FakeJudgeClient(fail_first=1)
    counts = await judge.judge_units(
        "recall",
        judge.units_for_rows("recall", rows),
        model="m",
        cache=cache,
        client=client,
        ledger=ledger,
        run="r1",
        min_interval_s=0.0,
    )
    # one call (the first attempt failed and was retried): Q1b's items are Q1's, asked once
    assert counts["calls"] == 1 and counts["new_labels"] == 2 and len(client.calls) == 2
    labels = score.row_labels("recall", rows, cache)
    assert labels == {"Q1": [True, False], "Q1b": [False, True]}
    again = await judge.judge_units(
        "recall",
        judge.units_for_rows("recall", rows),
        model="m",
        cache=judge.LabelCache(cache.path),
        client=client,
        ledger=ledger,
        run="r1",
        min_interval_s=0.0,
    )
    assert again["jobs"] == 0  # the cache file persisted every label
    cost = ledger.summary(run="r1")
    assert cost["by_model"]["m"]["calls"] == 1 and cost["usd_total"] == 0.0  # no price for a fake model


async def test_judge_units_batches_by_the_entry_points_chunk_size(tmp_path: Path) -> None:
    cache = judge.LabelCache(tmp_path / "c.jsonl")
    items = [(f"fact:{i}", "heart_primary", f"item number {i} about larkspur") for i in range(30)]
    client = FakeJudgeClient()
    counts = await judge.judge_units(
        "recall",
        judge.units_for_rows("recall", [_recall_row("Q1", "larkspur", items)]),
        model="claude-sonnet-5-5",
        cache=cache,
        client=client,
        ledger=judge.CostLedger(tmp_path / "l.jsonl"),
        run="r",
        min_interval_s=0.0,
    )
    assert counts["jobs"] == 2 and [p["max_tokens"] for p in client.calls] == [judge.JUDGE_MAX_TOKENS] * 2  # 25 + 5
    # no sampling parameter: the current API version rejects it, and thinking models ignore it
    assert set(client.calls[0]) == {"model", "system", "messages", "max_tokens"}
    assert client.calls[0]["system"][0]["text"].startswith("You are Claude Code")
    assert judge.CostLedger(tmp_path / "l.jsonl").summary()["usd_total"] == pytest.approx(
        2 * (1000 * 2 + 100 * 10) / 1e6
    )


async def test_judge_sees_items_in_rank_blind_order(tmp_path: Path) -> None:
    """Two arms ranking the same items differently produce the same judge calls: the calls must not
    group a query's top items together (the judge grades items against their call-mates)."""
    items = [(f"fact:{i}", "heart_primary", f"item number {i} about larkspur") for i in range(30)]
    prompts = []
    for order in (items, list(reversed(items))):
        client = FakeJudgeClient()
        await judge.judge_units(
            "recall",
            judge.units_for_rows("recall", [_recall_row("Q1", "larkspur", order)]),
            model="m",
            cache=judge.LabelCache(tmp_path / f"c{len(prompts)}.jsonl"),
            client=client,
            ledger=judge.CostLedger(tmp_path / "l.jsonl"),
            run="r",
            min_interval_s=0.0,
        )
        prompts.append(sorted(p["messages"][0]["content"] for p in client.calls))
    assert prompts[0] == prompts[1]
    first_call = re.findall(r"item number (\d+)", prompts[0][0] + prompts[0][1])
    assert sorted(map(int, first_call)) == list(range(30)) and first_call[:5] != ["0", "1", "2", "3", "4"]


# ---------------------------------------------------------------------------
# scorer and compare
# ---------------------------------------------------------------------------


def test_recall_row_metrics_hand_case() -> None:
    items = [(f"fact:{i}", "heart_primary" if i < 3 else "keyed", "c") for i in range(12)]
    row = _recall_row("Q1", "q", items, chars=4000, ms=250.0)
    labs = [True, False, True] + [False] * 8 + [True]
    m = score.recall_row(row, labs)
    assert m["n"] == 12 and m["rel"] == 3 and m["prec"] == pytest.approx(0.25)
    assert m["rel_top10"] == 2 and m["n_top10"] == 10 and m["p_at_10"] == pytest.approx(0.2)
    assert m["mrr"] == 1.0 and m["tokens"] == 1000 and m["ms"] == 250
    assert m["legs"] == {"heart_primary": [2, 3], "keyed": [1, 9]}
    assert m["bands"] == {"1-10": [2, 10], "11-30": [1, 2]}
    assert [score.band_of(r) for r in (1, 10, 11, 30, 31, 60, 61, 500)] == [
        "1-10",
        "1-10",
        "11-30",
        "11-30",
        "31-60",
        "31-60",
        "61+",
        "61+",
    ]


def test_gold_rows() -> None:
    row = _recall_row(
        "gf-A1",
        "q",
        [("episode:9", "heart_primary", "c"), ("fact:7", "heart_primary", "c")],
        set="gold",
        gold={"type": "fact", "ids": ["7"], "valid": True},
    )
    assert score.recall_gold_row(row) == {
        "served": 1.0,
        "top10": 1.0,
        "rr": 0.5,
        "served_summary": 1.0,
        "top10_summary": 1.0,
        "tokens": 100.0,
        "ms": 100.0,
    }
    pre = {
        "key": "gd-D1",
        "set": "gold",
        "rendered": ["fact:1", "decision:3", "decision:4"],
        "tokens_total": 50,
        "gold": {"type": "decision", "ids": ["4"]},
        "gold_text_in_prompt": False,
    }
    assert score.preturn_gold_row(pre) == {"gic": 1.0, "rr": 0.5, "rendered": 1.0, "tokens": 50.0, "n_same_type": 2.0}
    pre["rendered"] = ["fact:1"]
    pre["gold_text_in_prompt"] = True
    assert score.preturn_gold_row(pre)["gic"] == 1.0 and score.preturn_gold_row(pre)["rr"] == 0.0


def _write_run(
    root: Path, tag: str, rows: list[dict], *, entry: str = "recall", settings_rec=None, head: str = "abc"
) -> Path:
    d = root / "runs" / entry / tag
    d.mkdir(parents=True)
    meta = {
        "entry": entry,
        "tag": tag,
        "arm": tag,
        "data_dir": str(root),
        "judge_model": "m",
        "code": {"head": head, "dirty_files": 0, "served_sha256": "s"},
        "settings": settings_rec or {},
        "inputs_sha256": "i",
    }
    (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    (d / "rows.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return d


def _label_all(root: Path, entry: str, rows: list[dict], rel_words: set[str]) -> None:
    cache = judge.LabelCache(judge.cache_path(root, "m"))
    labels = [
        (it["key"], any(w in it["shown"] for w in rel_words))
        for u in judge.units_for_rows(entry, rows)
        for it in u["items"]
    ]
    cache.put_many(labels, "m", "t")


def _arm_rows(cut: int) -> list[dict]:
    """20 prod queries rendering 12 items (relevant: 'good'), the first `cut` kept; 10 fact gold."""
    rows = []
    for q in range(20):
        items = [
            (f"fact:{q}-{i}", "heart_primary" if i % 2 else "chunk", "good" if i in (0, 3, 9) else "bad")
            for i in range(12)
        ][:cut]
        rows.append(_recall_row(f"Q{q:02d}", f"query {q}", items, chars=100 * len(items), ms=100.0 + q))
    for g in range(10):
        items = [(f"fact:g{g}", "heart_primary", "gold")] if (g < 9 or cut == 12) else []
        rows.append(
            _recall_row(
                f"gf-{g}",
                f"gold {g}",
                items + [("fact:x", "chunk", "bad")],
                set="gold",
                gold={"type": "fact", "ids": [f"g{g}"], "valid": True},
            )
        )
    return rows


def test_compare_identical_runs_and_a_precision_cut(tmp_path: Path) -> None:
    rows_a, rows_b = _arm_rows(12), _arm_rows(10)
    for rows in (rows_a, rows_b):
        _label_all(tmp_path, "recall", rows, {"good", "gold"})
    rec = {"cross_encoder_max_candidates": {"value": 20, "source": "base"}}
    a = _write_run(tmp_path, "a", rows_a, settings_rec=rec)
    a2 = _write_run(tmp_path, "a2", rows_a, settings_rec=rec)
    b = _write_run(tmp_path, "b", rows_b, settings_rec={"cross_encoder_max_candidates": {"value": 10, "source": "arm"}})

    same = compare.compare(a, a2, gold_loss_tolerance=0.0)
    assert same["differences"]["n_variables"] == 0 and same["prod"]["identical_sets"] == 20
    assert same["prod"]["prec"]["delta"] == 0.0 and same["prod"]["prec"]["ci"] == [0.0, 0.0]
    assert same["win"]["checks"]["gold_non_inferior"]["pass"] is True
    assert same["win"]["checks"]["precision_or_tokens_improve"]["pass"] is False  # nothing improved

    rep = compare.compare(a, b, gold_loss_tolerance=0.0)
    assert rep["differences"]["settings"] == {"cross_encoder_max_candidates": {"a": 20, "b": 10}}
    assert rep["win"]["one_variable"] is True
    m = rep["prod"]
    assert m["prec"]["a"] == pytest.approx(3 / 12) and m["prec"]["b"] == pytest.approx(3 / 10)
    assert m["rel_top10"]["delta"] == 0.0 and m["tokens"]["delta"] == pytest.approx(-50.0)
    assert m["relevant_lost"] == 0
    assert rep["gold"]["fact"]["served"]["delta"] == pytest.approx(-0.1)  # gold g9 fell out of the cut
    checks = rep["win"]["checks"]
    assert checks["precision_or_tokens_improve"]["pass"] is True
    assert checks["gold_non_inferior"]["pass"] is False  # -0.1 is beyond a 0 tolerance
    lo = checks["gold_non_inferior"]["ci"][0]
    assert lo < -0.1  # 10 gold rows: the bound, not the point, decides
    assert compare.compare(a, b, gold_loss_tolerance=-lo)["win"]["checks"]["gold_non_inferior"]["pass"] is True
    assert checks["p95_latency"]["pass"] is True and rep["win"]["win"] is False
    text = cli.render_compare(rep)
    assert "setting cross_encoder_max_candidates: 20 -> 10" in text and "FAIL" in text


def test_summarize_recall_per_leg_band_gold_and_agreement(tmp_path: Path) -> None:
    rows = _arm_rows(12) + [
        {"key": "Q99", "set": "prod", "text": "x", "error": "boom"},
        _recall_row("gf-bad", "q", [], set="gold", gold={"type": "fact", "ids": ["z"], "valid": False}),
    ]
    _label_all(tmp_path, "recall", rows, {"good", "gold"})
    run = _write_run(tmp_path, "s", rows)
    alt = judge.LabelCache(judge.cache_path(tmp_path, "alt"))
    alt.put_many(list(judge.LabelCache(judge.cache_path(tmp_path, "m")).labels.items()), "alt", "t")
    out = score.summarize(run, alt_model="alt")
    p = out["prod"]
    assert out["errors"] == 1 and p["n_rows"] == 20 and p["n_judged"] == 20
    assert p["precision_macro"] == pytest.approx(0.25) and p["rel_top10"] == 3.0
    assert p["per_leg"]["chunk"]["precision"] == pytest.approx(1 / 6) and p["per_leg"]["chunk"]["in_top10"] == 100
    assert p["per_leg"]["heart_primary"]["precision"] == pytest.approx(2 / 6)
    assert p["by_band"]["1-10"]["precision"] == pytest.approx(0.3) and p["by_band"]["11-30"]["items"] == 40
    assert p["latency_ms"]["p50"] == 110.0
    assert out["gold"]["fact"]["served"] == 1.0 and out["gold"]["n_invalid_gold"] == 1
    assert out["agreement"]["agreement"] == 1.0
    keyed = score.summarize(run, keys={"Q00", "Q01"})
    assert keyed["prod"]["n_rows"] == 2 and "fact" not in keyed["gold"]


def test_summarize_preturn(tmp_path: Path) -> None:
    rows = [
        {
            "key": f"B{i}",
            "set": "prod",
            "text": f"task {i} larkspur",
            "rendered": ["fact:1", "episode:2", "decision:3"],
            "contents": {"fact:1": "larkspur fact", "episode:2": "other", "decision:3": "larkspur call"},
            "tokens_total": 1000,
            "tokens_memory": 400,
            "build_ms": 50.0 + i,
        }
        for i in range(4)
    ]
    rows.append(
        {
            "key": "ge-1",
            "set": "gold",
            "text": "q",
            "rendered": ["episode:5"],
            "contents": {},
            "tokens_total": 900,
            "tokens_memory": 300,
            "build_ms": 10.0,
            "gold": {"type": "episode", "ids": ["5"], "valid": True},
            "gold_text_in_prompt": False,
        }
    )
    _label_all(tmp_path, "preturn", rows, {"larkspur"})
    out = score.summarize(_write_run(tmp_path, "p", rows, entry="preturn"))
    p = out["prod"]
    assert p["precision_micro"] == pytest.approx(2 / 3) and p["tokens_total_mean"] == 1000
    assert p["precision_by_type"]["episode"]["precision"] == 0.0 and p["precision_by_type"]["fact"]["per_build"] == 1
    assert out["gold"]["episode"] == {"n": 1, "gic": 1.0, "rr": 1.0, "rendered": 1.0, "tokens_mean": 900.0}


# ---------------------------------------------------------------------------
# Postgres smoke: both entry points end to end, mock embeddings, fake judge
# ---------------------------------------------------------------------------


async def _purge_agent(database, agent_id: str) -> list[str]:
    from test_beam_harness import purge_agent

    return await purge_agent(database, agent_id)


async def _seed_corpus(agent: str) -> dict[str, str]:
    from conftest import MockEmbeddingProvider

    from nous.brain.brain import Brain
    from nous.brain.schemas import ReasonInput, RecordInput
    from nous.config import Settings
    from nous.heart.heart import Heart
    from nous.heart.schemas import EpisodeInput, FactInput
    from nous.storage.database import Database

    s = Settings().model_copy(
        update={"agent_id": agent, "admission_control_enabled": False, "event_bus_enabled": False}
    )
    database = Database(s)
    await database.connect()
    emb = MockEmbeddingProvider()
    ids: dict[str, str] = {}
    try:
        heart = Heart(database, s, emb, owns_embeddings=False)
        async with heart:
            f = await heart.learn(
                FactInput(
                    content="The Larkspur garden beds are watered at six in the morning.",
                    category="technical",
                    confidence=0.9,
                    source="test",
                )
            )
            ids["fact"] = str(f.id)
            await heart.learn(
                FactInput(
                    content="Quarterly invoices go to the finance mailbox on Fridays.",
                    category="technical",
                    confidence=0.9,
                    source="test",
                )
            )
            ep = await heart.start_episode(
                EpisodeInput(
                    title="Larkspur irrigation",
                    summary=(
                        "Set up the Larkspur irrigation timer and planned the first planting for April; "
                        "the frost delayed it by two weeks."
                    ),
                    trigger="unit_test",
                    participants=["owner"],
                    tags=["garden"],
                )
            )
            await heart.end_episode(ep.id, "success")
            ids["episode"] = str(ep.id)
            own = await heart.start_episode(  # the logged prompts' own conversation, still ongoing
                EpisodeInput(
                    title="Larkspur garden watering check",
                    summary="Checking the Larkspur garden watering schedule and irrigation right now.",
                    trigger="unit_test",
                    participants=["owner"],
                    tags=["garden"],
                    session_id="subtask-smoke",
                ),
                dedup=False,
            )
            ids["own_episode"] = str(own.id)
        brain = Brain(database, s, emb)
        d = await brain.record(
            RecordInput(
                description="Use a drip line for the Larkspur beds instead of sprinklers",
                confidence=0.8,
                category="architecture",
                stakes="low",
                context="Sprinklers wasted water on the Larkspur beds",
                reasons=[ReasonInput(type="analysis", text="drip saves water")],
            )
        )
        ids["decision"] = str(d.id)
    finally:
        await database.disconnect()
    return ids


@pytest.mark.postgres_only
async def test_offline_smoke_both_entry_points(tmp_path: Path, db, monkeypatch: pytest.MonkeyPatch) -> None:
    from conftest import MockEmbeddingProvider

    agent = f"served-smoke-{uuid.uuid4().hex[:8]}"
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.chdir(tmp_path)  # the CLI chdirs into the data dir; restored at teardown
    url = db.engine.url
    common = [
        "--data-dir",
        str(data),
        "--db-name",
        url.database,
        "--i-know-this-is-scratch",
        "--no-migrate",
        "--db-host",
        url.host,
        "--db-port",
        str(url.port or 5432),
        "--db-user",
        url.username,
        "--db-password",
        url.password or "",
        "--agent",
        agent,
        "--allow-dirty",
        "--warm",
        "1",
        "--set",
        "NOUS_CROSS_ENCODER_ENABLED=false",
        "--set",
        "NOUS_DATE_LEG_ENABLED=false",
        "--set",
        "NOUS_QUERY_EXPANSION_ENABLED=false",
    ]
    try:
        ids = await _seed_corpus(agent)
        (data / "queries.jsonl").write_text(
            json.dumps(
                {
                    "key": "Q000",
                    "query": "Larkspur garden watering schedule",
                    "logged_at": "2026-10-01T09:00:00+00:00",
                    "session_id": "subtask-smoke",
                }
            )
            + "\n"
            + json.dumps({"key": "Q001", "query": "finance invoices mailbox"})
            + "\n",
            encoding="utf-8",
        )
        gold = [
            _gold_file(data / f"gold_{t}.jsonl", t, [(f"{t[0].upper()}000", f"Larkspur {t} question", ids[t])])
            for t in ("fact", "episode", "decision")
        ]
        gold_args = [x for g in gold for x in ("--gold", str(g))]
        prompts = data / "prompts.json"
        prompts.write_text(
            json.dumps(
                {
                    "prompts": [
                        {
                            "kind": "subtask",
                            "group": "g",
                            "group_rows": 1,
                            "session_id": "subtask-smoke",
                            "logged_query": "Check the Larkspur garden irrigation and report problems",
                            "full_text": None,
                            "recovered": False,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        emb, client = MockEmbeddingProvider(), FakeJudgeClient()

        for entry, extra in (
            ("recall", ["--queries", str(data / "queries.jsonl")]),
            ("preturn", ["--prompts", str(prompts)]),
        ):
            for tag in ("a", "b"):
                rc = await cli.cmd_run(
                    cli.parse_args([entry, "run", "--tag", tag, *extra, *gold_args, *common]),
                    embeddings=emb,
                    llm=client,
                )
                assert rc == 0, entry
            run_a, run_b = (data / "runs" / entry / t for t in ("a", "b"))
            meta = json.loads((run_a / "meta.json").read_text(encoding="utf-8"))
            assert meta["agent"] == agent and meta["code"]["head"] and meta["code"]["served_sha256"]
            assert meta["settings"]["cross_encoder_enabled"] == {"value": False, "source": "arm"}
            assert set(meta["runtime_config"]["effective"]) == {"vector_weight", "rrf_k", "cross_encoder_enabled"}
            assert meta["gold_validity"] == {"valid": 3, "invalid": 0}
            assert not url.password or url.password not in json.dumps(meta)
            rows = judge.read_jsonl(run_a / "rows.jsonl")
            assert not [r for r in rows if r.get("error")], [r.get("error") for r in rows]
            assert await cli.cmd_judge(cli.parse_args([entry, "judge", str(run_a), "--model", "m"]), client=client) == 0
            assert await cli.cmd_judge(cli.parse_args([entry, "judge", str(run_b), "--model", "m"]), client=client) == 0
            for d in (run_a, run_b):  # score reads the judge model from the run record
                m = json.loads((d / "meta.json").read_text(encoding="utf-8"))
                (d / "meta.json").write_text(json.dumps({**m, "judge_model": "m"}), encoding="utf-8")
            assert cli.cmd_score(cli.parse_args([entry, "score", str(run_a), "--alt-judge", ""])) == 0
            scores = json.loads((run_a / "scores.json").read_text(encoding="utf-8"))
            assert scores["prod"]["n_judged"] == scores["prod"]["n_rows"] >= 1
            assert scores["prod"]["unlabelled_items"] == 0
            assert set(scores["gold"]) >= {"fact", "n_invalid_gold"}
            rep = compare.compare(run_a, run_b, gold_loss_tolerance=0.0)
            assert rep["differences"]["n_variables"] == 0  # an A/A pair: same settings, code, inputs
            assert rep["prod"]["precision_micro"]["delta"] == 0.0
            assert (
                cli.cmd_compare(cli.parse_args(["compare", str(run_a), str(run_b), "--gold-loss-tolerance", "0"])) == 0
            )
        recall_rows = judge.read_jsonl(data / "runs" / "recall" / "a" / "rows.jsonl")
        larkspur = next(r for r in recall_rows if r["key"] == "Q000")
        assert any(k == f"fact:{ids['fact']}" for k in judge.rendered_keys(larkspur))
        assert all(it["leg"] for it in larkspur["rendered"])
        # the read order came from the formatter and agrees with the ids the output printed
        assert all(r["read_order_matches_text"] and r["unplaced"] == 0 for r in recall_rows)
        assert [it["rank"] for it in larkspur["rendered"]] == list(range(1, len(larkspur["rendered"]) + 1))
        # the logged call's own ongoing episode is left out, as prod leaves it out (#854)
        assert larkspur["own_session_excluded"] >= 1
        assert f"episode:{ids['own_episode']}" not in judge.rendered_keys(larkspur)
        # a gold question has no source conversation, so the same episode stays recallable there
        assert any(f"episode:{ids['own_episode']}" in judge.rendered_keys(r) for r in recall_rows if r["set"] == "gold")
        preturn_rows = judge.read_jsonl(data / "runs" / "preturn" / "a" / "rows.jsonl")
        assert {r["key"]: r["first_turn"] for r in preturn_rows if r["set"] == "prod"} == {"B000": False}
        b000 = next(r for r in preturn_rows if r["key"] == "B000")
        assert b000["own_session_excluded"] >= 1  # the build's own-session filter saw the source (#857)
        assert f"episode:{ids['own_episode']}" not in b000["rendered"]
        for entry in ("recall", "preturn"):
            run = json.loads((data / "runs" / entry / "a" / "meta.json").read_text(encoding="utf-8"))["run"]
            assert run["embeddings_added_while_measuring"] is None  # the mock embedder has no cache
    finally:
        leftover = await _purge_agent(db, agent)
    assert leftover == []


def test_label_cache_appends_whole_lines_and_keeps_the_first_label(tmp_path: Path) -> None:
    cache = judge.LabelCache(tmp_path / "judge" / "m.jsonl")
    cache.put_many([("k1", True), ("k2", False)], "m", "r1")
    cache.put_many([("k2", True), ("k3", True)], "m", "r2")  # k2 is held: its first label stays
    lines = (tmp_path / "judge" / "m.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(x)["k"] for x in lines] == ["k1", "k2", "k3"]
    assert judge.LabelCache(cache.path).labels == {"k1": True, "k2": False, "k3": True}


def test_episode_gold_is_served_by_a_chunk_of_the_episode() -> None:
    row = _recall_row(
        "ge-A1",
        "q",
        [("fact:1", "heart_primary", "c"), ("chunk:c9", "chunk", "c"), ("chunk:c7", "chunk", "c")],
        set="gold",
        gold={"type": "episode", "ids": ["e5"], "valid": True},
    )
    row["rendered"][1]["episode"] = "e4"
    row["rendered"][2]["episode"] = "e5"
    m = score.recall_gold_row(row)
    assert (m["served"], m["rr"], m["served_summary"]) == (1.0, pytest.approx(1 / 3), 0.0)
    row["rendered"].append({"k": "episode:e5", "leg": "heart_primary", "rank": 4})
    m = score.recall_gold_row(row)
    assert (m["served"], m["rr"], m["served_summary"], m["top10_summary"]) == (1.0, pytest.approx(1 / 3), 1.0, 1.0)
    # a fact gold is never served by a chunk
    row["gold"] = {"type": "fact", "ids": ["c7"], "valid": True}
    assert score.recall_gold_row(row)["served"] == 0.0


def _result(rid: str, rtype: str, **meta) -> SimpleNamespace:
    return SimpleNamespace(id=rid, type=rtype, metadata=meta)


def test_read_order_follows_the_formatters_sections_not_final_rank() -> None:
    """A decision ranked 2nd by the pipeline is read after the Heart Memory section; a parent
    episode comes after the decisions; an exemplar row comes last."""
    from nous_eval.served import recall
    from nous_eval.served.stack import render_order

    results = [
        _result("d1", "decision"),
        _result("f1", "fact"),
        _result("f2", "fact"),
        _result("x1", "fact", retrieval_leg="exemplar"),
    ]
    emitted = [("f2", "fact"), ("f1", "fact"), ("d1", "decision"), ("d1", "decision"), ("x1", "fact")]
    order = render_order(results, emitted, [("p1", "parent summary")])
    assert order == [("fact", "f2"), ("fact", "f1"), ("decision", "d1"), ("episode", "p1"), ("fact", "x1")]
    cands = [
        {"type": "decision", "id": "d1", "final_rank": 1, "entry_leg": "brain"},
        {"type": "fact", "id": "f1", "final_rank": 2, "entry_leg": "heart_primary"},
        {"type": "fact", "id": "f2", "final_rank": 3, "entry_leg": "heart_primary"},
        {"type": "episode", "id": "p1", "final_rank": None, "entry_leg": "parent_episode"},
        {"type": "fact", "id": "x1", "final_rank": 4, "entry_leg": "heart_primary"},
        {"type": "fact", "id": "zz", "final_rank": 5, "entry_leg": "keyed"},
    ]
    rendered, unplaced = recall.order_rendered(cands, order)
    assert [(it["k"], it["rank"], it["final_rank"]) for it in rendered] == [
        ("fact:f2", 1, 3),
        ("fact:f1", 2, 2),
        ("decision:d1", 3, 1),
        ("episode:p1", 4, None),
        ("fact:x1", 5, 4),
        ("fact:zz", 6, 5),
    ]
    assert unplaced == 1
    uid = ["00000000-0000-0000-0000-00000000000" + str(i) for i in range(3)]
    text = f"1. [fact] b (id: {uid[1]}, score: 1)\n2. [fact] a (id: {uid[0]}, score: 1)\n1. dec (id: {uid[2]})"
    assert recall.text_order_agrees(text, [("fact", uid[1]), ("fact", uid[0]), ("decision", uid[2])])
    assert not recall.text_order_agrees(text, [("fact", uid[0]), ("fact", uid[1]), ("decision", uid[2])])


def test_an_id_quoted_inside_an_items_text_is_not_a_row() -> None:
    """An episode whose own text quotes a later row's id: only each row's own trailer, at the end
    of its line, places an id; the quote must not make the read order look wrong."""
    from nous_eval.served import recall

    ep, dec = (str(uuid.uuid5(uuid.NAMESPACE_URL, n)) for n in ("episode", "decision"))
    text = (
        f"1. [episode] We revisited the decision (id: {dec}) and kept it. (id: {ep}, score: 0.512 [rrf])\n"
        f"2. [decision] Keep the drip line | arch | low | confidence: 0.80 (id: {dec}, score: 0.433 [rrf])"
    )
    assert recall.text_order_agrees(text, [("episode", ep), ("decision", dec)]) is True
    assert recall.text_order_agrees(text, [("decision", dec), ("episode", ep)]) is False


def _pr(rid: str, rtype: str, desc: str, source: str = "heart", score: float = 0.5, rel: str | None = None, **meta):
    """A PipelineResult-shaped row for the real formatter."""
    return SimpleNamespace(
        id=rid, type=rtype, description=desc, source=source, score=score, edge_relation=rel, metadata=meta
    )


def test_recall_judges_each_items_emitted_text_not_the_trace_snippet(monkeypatch: pytest.MonkeyPatch) -> None:
    """The judged text of a recall item is what the formatter wrote for it: the decision's
    category, stakes, confidence and pattern, a fact's date tag and merge-history lines, the
    exemplar utterance cut to 500 characters with its label, a parent episode's summary. Rank,
    score and provenance furniture (list number, similarity, type tag, [via ...], the id/score
    trailer) is not part of it, so the same item shown at another rank keeps its label."""
    import nous.api.tools as tools_mod
    from nous_eval.served import recall
    from nous_eval.served.stack import _capture_render_order

    ids = {k: str(uuid.uuid5(uuid.NAMESPACE_URL, k)) for k in ("f1", "c1", "d1", "d2", "g1", "x1", "e1")}
    history = {
        "sources": [{"learned_at": "2026-01-02", "content": "new"}, {"learned_at": "2025-12-01", "content": "old"}],
        "dropped": 1,
    }
    results = [
        _pr(ids["f1"], "fact", "the fact", retrieval_leg="keyed", event_date="2026-03-10", merge_history=history),
        _pr(ids["c1"], "chunk", "chunk line one\nchunk line two"),
        _pr(ids["d1"], "decision", "the decision", source="brain", raw_score=0.7, category="arch", stakes="low",
            confidence=0.8, pattern="the pattern", outcome="success"),
        _pr(ids["g1"], "decision", "graph decision", source="graph_expanded", rel="supports",
            stage_origin="brain_graph"),
        _pr(ids["x1"], "fact", "u" * 600 + "\nlabel: yes", retrieval_leg="exemplar", similarity=0.83),
    ]  # fmt: skip
    stats = SimpleNamespace(contradiction_edges=[], n_stage_errors={}, stage_error_msgs={})
    monkeypatch.setattr(tools_mod, "_format_pipeline_text", tools_mod._format_pipeline_text)  # restored after
    orders: list = []
    captures: list = []
    _capture_render_order(orders, captures)
    caller: list = []  # recall_deep's own emitted_out: it reads it after the call
    text = tools_mod._format_pipeline_text(
        results, stats, ["all"], parent_episodes=[(ids["e1"], "the parent summary")], emitted_out=caller,
        temporal_render=True,
    )  # fmt: skip
    assert caller == [(r.id, r.type) for r in results]
    texts, checks = recall.item_texts(captures[-1], text)
    assert checks == {"text_unmatched": 0, "output_matches_capture": True}
    assert texts[f"fact:{ids['f1']}"] == (
        "the fact [2026-03-10]\n   history (merged from 3 facts, newest first):\n"
        "   - [learned 2026-01-02] new\n   - [learned 2025-12-01] old\n   - +1 older"
    )
    assert texts[f"chunk:{ids['c1']}"] == "chunk line one\nchunk line two"
    assert (
        texts[f"decision:{ids['d1']}"]
        == "[success] the decision | arch | low | confidence: 0.80 | pattern: the pattern"
    )
    assert texts[f"decision:{ids['g1']}"] == "graph decision"
    assert texts[f"fact:{ids['x1']}"] == "u" * 500 + "\nlabel: yes"
    assert texts[f"episode:{ids['e1']}"] == "the parent summary"
    assert not [t for t in texts.values() if "(id: " in t or "score" in t]


def test_an_empty_recall_output_matches_its_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    """A recall that returns nothing writes no element, so no emission reads the formatter's frame;
    its "No results found." text is still the tool output, and nothing is unmatched."""
    import nous.api.tools as tools_mod
    from nous_eval.served import recall
    from nous_eval.served.stack import _capture_render_order

    monkeypatch.setattr(tools_mod, "_format_pipeline_text", tools_mod._format_pipeline_text)  # restored after
    orders: list = []
    captures: list = []
    _capture_render_order(orders, captures)
    stats = SimpleNamespace(contradiction_edges=[], n_stage_errors={}, stage_error_msgs={})
    text = tools_mod._format_pipeline_text([], stats, ["all"], parent_episodes=None, emitted_out=[])
    assert "No results found." in text and orders == [[]]
    texts, checks = recall.item_texts(captures[-1], text)
    assert texts == {} and checks == {"text_unmatched": 0, "output_matches_capture": True}
    _, checks = recall.item_texts(captures[-1], text + " (changed)")
    assert checks["output_matches_capture"] is False


def test_an_empty_capture_whose_output_prints_items_is_a_mismatch() -> None:
    """Nothing emitted, but the formatter's text prints an item: the model read an item the
    capture (and the trace, which reads the same emissions) never saw. Not an empty result."""
    from nous_eval.served import recall

    fid = str(uuid.uuid5(uuid.NAMESPACE_URL, "printed"))
    formatted = f"=== Heart Memory ===\n1. [fact] a fact (id: {fid}, score: 0.500 [rrf])"
    cap = {"lines": None, "at": [], "emissions": [], "exemplar": set(), "parents": [], "formatted": formatted}
    texts, checks = recall.item_texts(cap, formatted)
    assert texts == {} and checks["output_matches_capture"] is False


async def test_a_query_a_cut_emptied_is_counted_scored_and_judged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A precision-cut arm that leaves one query with no items: the run's checks pass, the row is
    counted in empty_results, judge does not refuse the run, and the row scores 0 served items."""
    import nous.api.tools as tools_mod
    from nous_eval.served import recall
    from nous_eval.served.stack import _capture_render_order

    monkeypatch.setattr(tools_mod, "_format_pipeline_text", tools_mod._format_pipeline_text)  # restored after
    stack = SimpleNamespace(
        db=None,
        recorder=SimpleNamespace(_entries=[]),
        ce_calls=[],
        render_orders=[],
        render_captures=[],
        date_parser=None,
        embeddings=None,
        source_session=None,
    )
    _capture_render_order(stack.render_orders, stack.render_captures)
    fid = str(uuid.uuid5(uuid.NAMESPACE_URL, "kept"))
    stats = SimpleNamespace(contradiction_edges=[], n_stage_errors={}, stage_error_msgs={})

    async def recall_deep(query, limit, memory_types):
        kept = [] if query == "emptied by the cut" else [_pr(fid, "fact", "a kept fact")]
        text = tools_mod._format_pipeline_text(kept, stats, ["all"], emitted_out=[])
        cands = [{"type": "fact", "id": fid, "disposition": "rendered", "entry_leg": "heart_primary"}] if kept else []
        stack.recorder._entries.append({"candidates": cands, "legs": []})
        return {"content": [{"type": "text", "text": text}]}

    stack.tools = {"recall_deep": recall_deep}
    items = [
        {"key": "Q1", "set": "prod", "text": "kept query"},
        {"key": "Q2", "set": "prod", "text": "emptied by the cut"},
    ]
    run_dir = tmp_path / "runs" / "recall" / "cut"
    run_dir.mkdir(parents=True)
    stats_run = await recall.run(stack, items, run_dir / "rows.jsonl", warm=0)
    assert stats_run["empty_results"] == 1
    assert stats_run["output_mismatches"] == 0 and stats_run["text_unmatched"] == 0
    meta = {
        "entry": "recall",
        "tag": "cut",
        "arm": "cut",
        "data_dir": str(tmp_path),
        "judge_model": "m",
        "code": {"head": "abc", "dirty_files": 0, "served_sha256": "s"},
        "settings": {},
        "inputs_sha256": "i",
        "run": stats_run,
    }
    (run_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    assert (
        await cli.cmd_judge(cli.parse_args(["recall", "judge", str(run_dir), "--model", "m"]), client=FakeJudgeClient())
        == 0
    )
    out = score.summarize(run_dir, alt_model=None)
    assert out["prod"]["n_judged"] == 2 and out["prod"]["empty_results"] == 1
    assert out["prod"]["items_mean"] == 0.5  # the emptied query serves 0 items


def test_item_texts_count_what_they_cannot_place() -> None:
    from nous_eval.served import recall

    cap = {
        "lines": ["=== Heart Memory ===", "1. [fact] a (id: f1, score: 0.500 [rrf])", "2. [fact] b without trailer"],
        "at": [1, 2],
        "emissions": [("f1", "fact"), ("f2", "fact")],
        "exemplar": set(),
        "parents": [("e1", "summary not shown")],
    }
    texts, checks = recall.item_texts(cap, "\n".join(cap["lines"]))
    assert texts == {"fact:f1": "a"} and checks == {"text_unmatched": 2, "output_matches_capture": True}
    _, checks = recall.item_texts(cap, "something else")
    assert checks["output_matches_capture"] is False
    _, checks = recall.item_texts(dict(cap, lines=None), "x")  # a formatter frame we could not read
    assert checks == {"text_unmatched": 3, "output_matches_capture": None}


def test_recall_output_check_allows_the_tools_own_changed_tools_note() -> None:
    """recall_deep appends a changed-tools note to the formatter's text when it names a changed
    tool; the capture still matches that output, and only that note is allowed."""
    from nous.tool_names import CHANGED_TOOLS, changed_tools_note
    from nous_eval.served import recall

    name = next(iter(CHANGED_TOOLS))
    cap = {
        "lines": ["=== Heart Memory ===", f"1. [fact] used {name} (id: f1, score: 0.500 [rrf])"],
        "at": [1],
        "emissions": [("f1", "fact")],
        "exemplar": set(),
        "parents": [],
    }
    formatted = "\n".join(cap["lines"])
    _, checks = recall.item_texts(cap, f"{formatted}\n\n{changed_tools_note(formatted)}")
    assert checks["output_matches_capture"] is True
    _, checks = recall.item_texts(cap, f"{formatted}\n\nNote: something else.")
    assert checks["output_matches_capture"] is False


def test_capture_render_order_wraps_the_formatter_and_restores_nothing_else(monkeypatch: pytest.MonkeyPatch) -> None:
    import nous.api.tools as tools_mod
    from nous_eval.served.stack import _capture_render_order

    def fake_format(results, stats, search_types, parent_episodes=None, emitted_out=None, **kw):
        for r in reversed(results):  # a formatter whose order is not the input order
            emitted_out.append((r.id, r.type))
        return "text"

    monkeypatch.setattr(tools_mod, "_format_pipeline_text", fake_format)
    sink: list = []
    orig = _capture_render_order(sink)
    assert orig is fake_format
    out = tools_mod._format_pipeline_text(
        [_result("a", "fact"), _result("b", "chunk")],
        None,
        ["all"],
        parent_episodes=[("e1", "s")],
        session_group_heart=True,
    )
    assert out == "text" and sink == [[("chunk", "b"), ("fact", "a"), ("episode", "e1")]]


async def test_date_store_holds_the_unpadded_window_and_each_arm_pads_its_own(tmp_path: Path) -> None:
    import datetime

    from nous.heart.date_window import DateWindow
    from nous_eval.served.stack import CachedDateParser

    class Inner:
        calls = 0

        async def _parse_llm(self, query, day):
            Inner.calls += 1
            return True, DateWindow(start=datetime.date(2026, 10, 5), end=datetime.date(2026, 10, 11))

    q = "what did we change last week"
    path = tmp_path / "w.jsonl"
    two = CachedDateParser(Inner(), path, "m", pad_days=2)
    two.day = datetime.date(2026, 10, 14)
    w2 = await two.parse(q, datetime.date(2030, 1, 1))
    assert (w2.start, w2.end) == (datetime.date(2026, 10, 3), datetime.date(2026, 10, 13))
    zero = CachedDateParser(Inner(), path, "m", pad_days=0)  # a later arm, same store file
    zero.day = datetime.date(2026, 10, 14)
    w0 = await zero.parse(q, datetime.date(2030, 1, 1))
    assert (w0.start, w0.end) == (datetime.date(2026, 10, 5), datetime.date(2026, 10, 11)) and Inner.calls == 1
    assert json.loads(path.read_text(encoding="utf-8"))["window"] == ["2026-10-05", "2026-10-11"]


async def test_one_query_text_on_two_dates_runs_each_on_its_own_date(tmp_path: Path) -> None:
    """Two inputs with the same text and different as-of dates: each is prefilled, warmed and
    measured against its own date, never against one date picked by the text."""
    import datetime

    from nous.heart.date_window import DateWindow
    from nous_eval.served import recall
    from nous_eval.served.stack import CachedDateParser

    parsed: list[datetime.date] = []

    class Inner:
        async def _parse_llm(self, query, day):
            parsed.append(day)
            return True, DateWindow(start=day - datetime.timedelta(days=7), end=day)

    parser = CachedDateParser(Inner(), tmp_path / "w.jsonl", "m", pad_days=0)
    ends: list[datetime.date] = []

    async def recall_deep(query, limit, memory_types):
        ends.append((await parser.parse(query, datetime.date(2030, 1, 1))).end)
        return {"content": []}

    stack = SimpleNamespace(
        recorder=SimpleNamespace(_entries=[]),
        ce_calls=[],
        render_orders=[],
        render_captures=[],
        date_parser=parser,
        tools={"recall_deep": recall_deep},
        embeddings=None,
    )
    q = "what did we change last week"
    d1, d2 = datetime.date(2026, 10, 1), datetime.date(2026, 10, 20)
    items = [
        {"key": "Q1", "set": "prod", "text": q, "as_of": d1.isoformat()},
        {"key": "Q2", "set": "prod", "text": q, "as_of": d2.isoformat()},
    ]
    stats = await recall.run(stack, items, tmp_path / "rows.jsonl", warm=-1)
    assert stats["date_prefill"] == {"gated": 2, "parsed_now": 2, "transient": 0}
    assert sorted(parsed) == [d1, d2]  # both parsed by the prefill, nothing parsed while measuring
    assert ends == [d1, d2, d1, d2]  # the warm pass, then the measured pass, each on its own date


async def test_recall_hands_each_inputs_source_session_to_the_own_session_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """recall_deep is called with no session (so F055 residual state stays cold, as before), and
    the pipeline's own own_session_exclusions receives the input's source session, in the warm
    pass and the measured pass; the trace's own_session drops are counted per row and per run."""
    import nous.api.retrieval_pipeline as rp
    from nous_eval.served import recall
    from nous_eval.served.stack import _route_source_session

    received: list = []

    async def fake_exclusions(heart, session_id):
        received.append(session_id)
        return None

    import nous.cognitive.context as ctx_mod

    monkeypatch.setattr(rp, "own_session_exclusions", fake_exclusions)
    monkeypatch.setattr(ctx_mod, "_is_own_session_episode", ctx_mod._is_own_session_episode)  # restored after
    stack = SimpleNamespace(
        db=None,
        recorder=SimpleNamespace(_entries=[]),
        ce_calls=[],
        render_orders=[],
        render_captures=[],
        date_parser=None,
        embeddings=None,
        source_session=None,
    )
    _route_source_session(stack)
    tool_sessions: list = []

    async def recall_deep(query, limit, memory_types, _session_id=None):
        tool_sessions.append(_session_id)
        await rp.own_session_exclusions(None, _session_id)  # as the closure looks it up at call time
        drops = [{"type": "episode", "id": "e1", "disposition": "filter_dropped", "disposition_stage": "own_session"}]
        stack.recorder._entries.append({"candidates": drops if query == "q1" else [], "legs": []})
        return {"content": [{"type": "text", "text": "x"}]}

    stack.tools = {"recall_deep": recall_deep}
    items = [
        {"key": "Q1", "set": "prod", "text": "q1", "source_session": "chat-a"},
        {"key": "Q2", "set": "prod", "text": "q2", "source_session": "chat-b"},
        {"key": "G1", "set": "gold", "text": "g1"},
    ]
    stats = await recall.run(stack, items, tmp_path / "rows.jsonl", warm=-1)
    assert received == ["chat-a", "chat-b", None, "chat-a", "chat-b", None]
    assert tool_sessions == [None] * 6
    rows = [json.loads(line) for line in (tmp_path / "rows.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r["own_session_excluded"] for r in rows] == [1, 0, 0]
    assert stats["own_session_queries"] == 1 and stats["own_session_rows"] == 1


def test_preturn_boundary_sees_the_source_session_not_the_build_id(monkeypatch: pytest.MonkeyPatch) -> None:
    import nous.cognitive.context as ctx_mod
    from nous_eval.served.stack import _route_source_session

    monkeypatch.setattr(ctx_mod, "_is_own_session_episode", ctx_mod._is_own_session_episode)  # restored after
    stack = SimpleNamespace(source_session=None)
    _route_source_session(stack)
    ongoing = SimpleNamespace(session_id="chat-a", active=True)
    ended = SimpleNamespace(session_id="chat-a", active=False)
    stack.source_session = "chat-a"
    assert ctx_mod._is_own_session_episode(ongoing, "chat-a-served") is True
    assert ctx_mod._is_own_session_episode(ended, "chat-a-served") is False
    stack.source_session = None  # a gold question: no source conversation
    assert ctx_mod._is_own_session_episode(ongoing, "chat-a-served") is False


async def test_two_prompts_of_one_conversation_do_not_share_the_unchanged_query_memo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With skip_unchanged on, the build skips memory when its session searched the same query
    within the TTL. Two prompts logged in one conversation are replayed independently, so each
    gets its own replay session (warm and measured); the conversation still reaches the
    own-session boundary as source_session."""
    from collections import OrderedDict

    from nous.cognitive.context import ContextEngine
    from nous_eval.served import preturn

    engine = ContextEngine.__new__(ContextEngine)  # only its unchanged-query memo is used
    engine._last_query_by_session = OrderedDict()
    seen: list = []

    async def fake_build(stack, text, session_id, *, turn_number, first_turn=False, source_session=None):
        skipped = engine._query_unchanged(session_id, (text,), 300.0)
        engine._record_searched_query(session_id, (text,))
        seen.append((session_id, source_session, skipped))
        return {"legs": [], "tokens_by_label": {"User Profile": 1}, "system_prompt": "p", "skipped": skipped}

    monkeypatch.setattr(preturn, "build_one", fake_build)
    items = [
        {"key": f"B00{i}", "set": "prod", "text": "check the garden", "session": "heartbeat-x",
         "source_session": "heartbeat-x", "turn_number": 1, "first_turn": False}
        for i in (0, 1)
    ]  # fmt: skip
    await preturn.run(SimpleNamespace(embeddings=None), items, tmp_path / "rows.jsonl", warm=-1)
    assert len({sid for sid, _, _ in seen}) == 4  # two inputs x (warm, measured)
    assert all(src == "heartbeat-x" for _, src, _ in seen)
    assert not [x for x in seen if x[2]]  # no build reused another input's search


async def test_preturn_runner_passes_each_inputs_source_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nous_eval.served import preturn

    calls: list = []

    async def fake_build(stack, text, session_id, *, turn_number, first_turn=False, source_session=None):
        calls.append((session_id, source_session))
        return {"legs": [], "tokens_by_label": {"User Profile": 1}, "system_prompt": "p", "own_session_excluded": 1}

    monkeypatch.setattr(preturn, "build_one", fake_build)
    items = [
        {"key": "B000", "set": "prod", "text": "t", "session": "chat-a", "source_session": "chat-a",
         "turn_number": 1, "first_turn": True},
        {"key": "gd-1", "set": "gold", "text": "g", "session": "u-1", "source_session": None,
         "turn_number": 2, "first_turn": False},
    ]  # fmt: skip
    stats = await preturn.run(SimpleNamespace(embeddings=None), items, tmp_path / "rows.jsonl", warm=-1)
    assert calls == [
        ("chat-a-B000-warm", "chat-a"),
        ("u-1-gd-1-warm", None),
        ("chat-a-B000-served", "chat-a"),  # build ids stay fresh and per input: no state from the copy
        ("u-1-gd-1-served", None),
    ]
    assert stats["own_session_queries"] == 2 and stats["own_session_rows"] == 2


class _CachingEmbedder:
    """Caches like EmbeddingProvider: one entry per distinct text, kept in ``_cache``."""

    def __init__(self, cache: dict | None = None) -> None:
        self._cache = cache if cache is not None else {}

    async def embed(self, text: str) -> list[float]:
        self._cache.setdefault(text, [1.0])
        return self._cache[text]


@pytest.mark.parametrize("prefilled", [False, True])
async def test_measured_calls_hit_a_warm_cache_whichever_run_came_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prefilled: bool
) -> None:
    """A/A by construction: after the default warm pass, the measured pass makes no embedding
    request, whether this run found the shared cache empty (first arm) or full (a later arm)."""
    from nous_eval.served import recall

    items = [{"key": f"Q{i}", "set": "prod", "text": f"query {i}"} for i in range(30)]
    emb = _CachingEmbedder({it["text"]: [1.0] for it in items} if prefilled else None)
    stack = SimpleNamespace(embeddings=emb, date_parser=None)

    async def fake_call(stack, query, day=None, session=None):
        await stack.embeddings.embed(query)
        return {"wall_ms": 1.0, "chars": 4, "rendered": [], "contents": {}}

    monkeypatch.setattr(recall, "one_call", fake_call)
    stats = await recall.run(stack, items, tmp_path / "rows.jsonl", warm=-1)
    assert stats["embeddings_added_while_measuring"] == 0
    cold = await recall.run(
        SimpleNamespace(embeddings=_CachingEmbedder(), date_parser=None), items, tmp_path / "rows2.jsonl", warm=10
    )
    assert cold["embeddings_added_while_measuring"] == 20  # what a partial warm-up left to the measured pass


def test_compare_reads_each_runs_labels_from_its_own_data_dir(tmp_path: Path) -> None:
    rows = _arm_rows(12)
    da, dbb = tmp_path / "a", tmp_path / "b"
    for d in (da, dbb):
        _label_all(d, "recall", rows, {"good", "gold"})
    rows_b = [dict(r, contents={k: v + " (b)" for k, v in r["contents"].items()}) for r in rows]
    _label_all(dbb, "recall", rows_b, {"good", "gold"})
    rep = compare.compare(_write_run(da, "a", rows), _write_run(dbb, "b", rows_b), gold_loss_tolerance=0.0)
    assert rep["n_judged_both"] == 20 and rep["prod"]["prec"]["delta"] == 0.0


def test_one_runtime_setting_change_is_one_variable(tmp_path: Path) -> None:
    rows = _arm_rows(12)
    _label_all(tmp_path, "recall", rows, {"good", "gold"})
    a = _write_run(tmp_path, "a", rows, settings_rec={"rrf_k": {"value": 30, "source": "base"}})
    b = _write_run(tmp_path, "b", rows, settings_rec={"rrf_k": {"value": 20, "source": "arm"}})
    for d, k, wins in ((a, 30, []), (b, 20, ["rrf_k"])):
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        meta["runtime_config"] = {
            "db_overrides": {"rrf_k": 30},
            "arm_wins": wins,
            "effective": {"rrf_k": k, "vector_weight": 0.5},
        }
        (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    diff = compare.differences(*(json.loads((d / "meta.json").read_text(encoding="utf-8")) for d in (a, b)))
    assert diff["n_variables"] == 1 and list(diff["settings"]) == ["rrf_k"]


def test_win_is_false_when_gold_or_judged_rows_are_missing(tmp_path: Path) -> None:
    rows = [r for r in _arm_rows(12) if r["set"] == "prod"]  # no gold rows
    cheaper = [dict(r, chars=r["chars"] // 2) for r in rows]  # tokens improve, nothing else changes
    _label_all(tmp_path, "recall", rows, {"good"})
    rep = compare.compare(_write_run(tmp_path, "a", rows), _write_run(tmp_path, "b", cheaper), gold_loss_tolerance=0.0)
    assert rep["win"]["checks"]["precision_or_tokens_improve"]["pass"] is True
    assert rep["win"]["win"] is False and "not measured: gold_non_inferior" in rep["win"]["why_not"]
    unjudged = tmp_path / "u"
    rep = compare.compare(
        _write_run(unjudged, "a", _arm_rows(12)), _write_run(unjudged, "b", _arm_rows(10)), gold_loss_tolerance=1.0
    )
    assert rep["n_judged_both"] == 0 and rep["win"]["win"] is False
    assert "not measured: no query fully judged in both runs" in rep["win"]["why_not"]


# ---------------------------------------------------------------------------
# round 3: pre-turn read order and rendered text, fingerprints, output guards, run checks
# ---------------------------------------------------------------------------


def _episode(eid: str, title: str, *, summary: str = "s", outcome: str = "success") -> SimpleNamespace:
    return SimpleNamespace(id=eid, title=title, summary=summary, outcome=outcome)


def test_recent_conversations_blocks_pair_with_the_episodes_the_build_kept() -> None:
    from nous_eval.served import preturn

    listed = [_episode("e1", "garden plan"), _episode("e2", "dropped", outcome="abandoned"), _episode("e3", "invoices")]
    text = "- [Nov 04 10:00] garden plan\n  Planned the beds.\n- [Nov 04 09:00] invoices"
    ids, blocks, ok = preturn.recent_episodes(listed, text)
    assert ok and ids == ["e1", "e3"]
    assert blocks == {"e1": "- [Nov 04 10:00] garden plan\n  Planned the beds.", "e3": "- [Nov 04 09:00] invoices"}
    ids, blocks, ok = preturn.recent_episodes(listed, "- [Nov 04 10:00] garden plan")  # one block, two episodes
    assert not ok and blocks == {} and ids == ["e1", "e3"]
    assert preturn.recent_episodes([], "") == ([], {}, True)


def test_fingerprint_covers_as_of_first_turn_and_turn_number(tmp_path: Path) -> None:
    gf = _gold_file(tmp_path / "f.jsonl", "fact", [("A000", "where is alpha", "f-1")])
    base = inputs.preturn_items(None, [gf], "2026-11-05")
    h = inputs.inputs_sha256(base)
    for change in ({"as_of": "2026-11-06"}, {"first_turn": True}, {"turn_number": 1}, {"source_session": "s"}):
        assert inputs.inputs_sha256([{**base[0], **change}]) != h, change


def test_compare_lists_as_of_and_warm(tmp_path: Path) -> None:
    rows = _arm_rows(12)
    _label_all(tmp_path, "recall", rows, {"good", "gold"})
    ma = json.loads((_write_run(tmp_path, "a", rows) / "meta.json").read_text(encoding="utf-8"))
    mb = {**ma, "as_of": "2026-11-06", "warm": 10}
    ma.update(as_of="2026-11-05", warm=-1)
    diff = compare.differences(ma, mb)
    assert diff["as_of"] == {"a": "2026-11-05", "b": "2026-11-06"} and diff["warm"] == {"a": -1, "b": 10}
    assert diff["n_variables"] == 2


def test_compare_sees_a_different_direct_database_but_not_a_clone_name(tmp_path: Path) -> None:
    rows = _arm_rows(12)
    ma = json.loads((_write_run(tmp_path, "a", rows) / "meta.json").read_text(encoding="utf-8"))
    direct = {**ma, "db_template": None}
    diff = compare.differences({**direct, "db_name": "nous_eval_x"}, {**direct, "db_name": "nous_eval_y"})
    assert diff["database"] == {"a": "nous_eval_x", "b": "nous_eval_y"} and diff["n_variables"] == 1
    cloned = {**ma, "db_template": "nous_eval_served_tpl_x"}
    diff = compare.differences(
        {**cloned, "db_name": "nous_eval_served_run_recall_a"}, {**cloned, "db_name": "nous_eval_served_run_recall_b"}
    )
    assert "database" not in diff and diff["n_variables"] == 0  # generated clone names are not a variable
    diff = compare.differences(
        {**cloned, "db_name": "nous_eval_served_run_recall_a"}, {**direct, "db_name": "nous_eval_x"}
    )
    assert diff["database"] == {"a": "template nous_eval_served_tpl_x", "b": "nous_eval_x"}


async def test_explicit_outputs_must_be_untracked(tmp_path: Path) -> None:
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        cli._check_out_file(repo / "queries.jsonl")
    assert cli._check_out_file(repo / "ignored" / "queries.jsonl")
    assert cli._check_out_file(tmp_path / "outside" / "gold.jsonl")
    # The commands, not cli.main: main configures the process's logging for a CLI run, which would
    # leak into every later test that asserts on log records.
    for cmd, argv in (
        (
            cli.cmd_queries,
            [
                "recall",
                "queries",
                "--data-dir",
                str(tmp_path / "d"),
                "--out",
                str(repo / "q.jsonl"),
                "--db-name",
                "nous_eval_x",
            ],
        ),
        (
            cli.cmd_gold,
            [
                "preturn",
                "gold",
                "--data-dir",
                str(tmp_path / "d"),
                "--type",
                "fact",
                "--out",
                str(repo / "g.jsonl"),
                "--db-name",
                "nous_eval_x",
            ],
        ),
    ):
        with pytest.raises(SystemExit):  # refused before any database or LLM is touched
            await cmd(cli.parse_args(argv))


@pytest.mark.parametrize("name", ["postgres", "template0", "template1"])
def test_server_databases_are_never_a_run_database(name: str) -> None:
    for flag in (False, True):
        with pytest.raises(SystemExit):
            db.check_scratch(name, any_scratch_name=flag)


def test_read_order_check_is_none_when_nothing_printed_an_id() -> None:
    from nous_eval.served import recall

    assert recall.text_order_agrees("recall_deep failed: timeout", [("fact", "x")]) is None


@pytest.mark.parametrize("check", ["read_order_mismatches", "text_unmatched", "output_mismatches"])
async def test_judge_refuses_a_run_whose_read_order_checks_failed(tmp_path: Path, check: str) -> None:
    rows = _arm_rows(12)
    run = _write_run(tmp_path, "r", rows)
    meta = json.loads((run / "meta.json").read_text(encoding="utf-8"))
    meta["run"] = {check: 2, "unplaced": 0}
    (run / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    with pytest.raises(SystemExit, match="run checks failed"):
        await cli.cmd_judge(cli.parse_args(["recall", "judge", str(run), "--model", "m"]), client=FakeJudgeClient())
    assert (
        await cli.cmd_judge(
            cli.parse_args(["recall", "judge", str(run), "--model", "m", "--allow-unchecked"]), client=FakeJudgeClient()
        )
        == 0
    )


# ---------------------------------------------------------------------------
# round 4: pre-turn items, text and positions come from what each section rendered
# ---------------------------------------------------------------------------


def _engine(**overrides):
    """The real ContextEngine section formatters on a minimal object (they read only settings)."""
    from nous.cognitive.context import ContextEngine
    from nous.config import Settings

    class Ctx:
        CHARS_PER_TOKEN = ContextEngine.CHARS_PER_TOKEN
        _format_facts = ContextEngine._format_facts
        _format_decisions = ContextEngine._format_decisions
        _format_episodes = ContextEngine._format_episodes
        _format_procedures = ContextEngine._format_procedures
        _truncate_to_budget = ContextEngine._truncate_to_budget

        def _format_procedure_bodies(self, details, per_item_cap):
            return [f"### {p.name}\n\n{p.body}" for p in details]

    ctx = Ctx()
    ctx._settings = Settings(_env_file=None).model_copy(update=overrides)
    return ctx


def _fact(fid: str, content: str, day: int | None) -> SimpleNamespace:
    import datetime

    d = datetime.date(2026, 10, day) if day else None
    return SimpleNamespace(
        id=fid,
        content=content,
        confidence=0.9,
        subject=None,
        valid_from=d,
        valid_to=None,
        event_date=None,
        source="test",
    )


def _read(stack, sections, traced, *, recent=([], {}, True)):
    from nous_eval.served import preturn

    prompt = "\n\n".join(f"## {label}\n\n{content}" for label, content in sections)
    return preturn.read_items(sections, preturn.formatter_lines(stack.formatter_calls), *recent, traced, prompt)


CHECKS_ZERO = {"unplaced": 0, "untraced": 0, "unmatched_segments": 0, "sections_not_in_prompt": 0}


def test_facts_are_read_in_date_order_with_the_text_the_formatter_kept() -> None:
    """temporal render re-sorts dated facts by date after the full-text exemption was decided by
    rank; fact_format_max_chars cuts the others at a word boundary."""
    from nous_eval.served.stack import _capture_formatters

    stack = SimpleNamespace(ctx=_engine(fact_format_max_chars=40), formatter_calls=[])
    _capture_formatters(stack)
    long = "the garden beds are watered at six in the morning before the heat"
    facts = [_fact("f1", long + " one", 20), _fact("f2", long + " two", 5), _fact("f3", "short fact three", 12)]
    text = stack.ctx._format_facts(facts, full_top_n=1, dated=True)
    read = _read(stack, [("Relevant Facts", text)], {("fact", "f1"), ("fact", "f2"), ("fact", "f3")})
    # the ranked input was f1, f2, f3; the model reads them by date: f2 (Oct 5), f3 (Oct 12), f1 (Oct 20)
    assert read["order"] == [("fact", "f2"), ("fact", "f3"), ("fact", "f1")]
    assert [read["texts"][k] for k in read["order"]] == text.split("\n")
    assert long + " one" in read["texts"][("fact", "f1")]  # rank 1: exempt, full text
    assert "watered at six" in read["texts"][("fact", "f2")] and "two" not in read["texts"][("fact", "f2")]
    assert "..." in read["texts"][("fact", "f2")]  # cut to 40 chars at a word boundary
    assert {k: read[k] for k in CHECKS_ZERO} == CHECKS_ZERO


def test_a_budget_cut_item_is_judged_on_what_survived_and_a_dropped_one_is_unplaced() -> None:
    from nous_eval.served.stack import _capture_formatters

    stack = SimpleNamespace(ctx=_engine(), formatter_calls=[])
    _capture_formatters(stack)
    decs = [
        SimpleNamespace(id=f"d{i}", description=f"decision number {i} " + "x" * 40, outcome="success", confidence=0.8)
        for i in range(3)
    ]
    full = stack.ctx._format_decisions(decs)
    cut = stack.ctx._truncate_to_budget(full, (len(full) - 50) // 4)  # mid-way through the last line
    traced = {("decision", f"d{i}") for i in range(3)}
    read = _read(stack, [("Related Decisions", cut)], traced)
    assert read["order"] == [("decision", "d0"), ("decision", "d1"), ("decision", "d2")]
    tail = read["texts"][("decision", "d2")]
    assert tail.endswith("...") and full.split("\n")[2].startswith(tail[:-3]) and len(tail) < len(full.split("\n")[2])
    shorter = stack.ctx._truncate_to_budget(full, (len(full.split("\n")[0]) + 3) // 4 + 2)  # d2 never shown
    read = _read(stack, [("Related Decisions", shorter)], traced)
    # the trace still says rendered (its budget probe is approximate); the text says no
    assert ("decision", "d2") not in read["texts"] and read["unplaced"] == 0 and read["budget_cut_but_traced"] == 1


def test_sections_are_read_in_prompt_order_and_checked_against_the_prompt_and_the_trace() -> None:
    from nous_eval.served import preturn
    from nous_eval.served.stack import _capture_formatters

    stack = SimpleNamespace(ctx=_engine(), formatter_calls=[])
    _capture_formatters(stack)
    past = stack.ctx._format_episodes(
        [SimpleNamespace(id="e9", summary="past session", outcome="success", started_at=None)]
    )
    blocks = stack.ctx._format_procedure_bodies([SimpleNamespace(id="p1", name="deploy", body="run the steps")], 1000)
    recent = "- [Nov 04 10:00] garden plan"
    # br.sections order: Past Episodes first; the prompt holds Recent Conversations first
    sections = [
        ("Past Episodes", past),
        ("Recommended Procedures", "\n\n".join(blocks)),
        ("Recent Conversations", recent),
    ]
    prompt = "\n\n".join(f"## {label}\n\n{c}" for label, c in (sections[2], sections[1], sections[0]))
    pools = preturn.formatter_lines(stack.formatter_calls)
    traced = {("episode", "e9"), ("procedure", "p1"), ("episode", "e1"), ("fact", "gone")}
    read = preturn.read_items(sections, pools, ["e1"], {"e1": recent}, True, traced, prompt)
    assert read["order"] == [("episode", "e1"), ("procedure", "p1"), ("episode", "e9")]
    assert read["texts"][("procedure", "p1")] == "### deploy\n\nrun the steps"
    assert read["texts"][("episode", "e1")] == recent and read["texts"][("episode", "e9")] == past
    assert read["unplaced"] == 1 and read["untraced"] == 0  # the traced fact "gone" is in no section
    read = preturn.read_items(
        sections,
        pools,
        ["e1"],
        {"e1": recent},
        True,
        {("episode", "e9")},
        prompt.replace("## Past Episodes", "## Past"),
    )
    assert read["sections_not_in_prompt"] == 1 and read["untraced"] == 2


def test_section_text_no_formatter_produced_is_counted() -> None:
    from nous_eval.served import preturn

    known = "- [success] known (confidence: 0.80)"
    items, unmatched = preturn.parse_section("- a line nobody rendered\n" + known, [(("decision", "d1"), known)])
    assert items == [(("decision", "d1"), known)] and unmatched == ["- a line nobody rendered"]


async def test_query_expansions_are_shared_across_runs(tmp_path: Path) -> None:
    from nous_eval.served.stack import ExpansionStore, share_expansions

    class Expander:
        def __init__(self, answer: list[str]) -> None:
            self.answer, self.db = answer, {}

        async def _cache_get(self, h):
            return self.db.get(h)

        async def _cache_put(self, h, query, variants):
            self.db[h] = variants

        async def expand(self, query):  # QueryExpander.expand's shape: the cache, else the LLM, then store
            h = query.encode()
            hit = await self._cache_get(h)
            if hit is not None:
                return hit
            await self._cache_put(h, query, [query, *self.answer])
            return [query, *self.answer]

    cfg = SimpleNamespace(query_expansion_model="haiku", query_expansion_max_variants=3)
    first, second = Expander(["garden schedule"]), Expander(["a different variant"])  # two arms, fresh clones
    for e in (first, second):
        share_expansions(e, ExpansionStore(tmp_path / "qe.jsonl", cfg))
    assert await first.expand("when do we water") == ["when do we water", "garden schedule"]
    assert await second.expand("when do we water") == ["when do we water", "garden schedule"]
    other = Expander(["x"])  # another expansion config does not share
    share_expansions(
        other,
        ExpansionStore(
            tmp_path / "qe.jsonl", SimpleNamespace(query_expansion_model="haiku", query_expansion_max_variants=5)
        ),
    )
    assert await other.expand("when do we water") == ["when do we water", "x"]


def test_the_changed_tools_note_is_section_furniture_not_an_item() -> None:
    from nous.tool_names import changed_tools_note
    from nous_eval.served import preturn
    from nous_eval.served.stack import _capture_formatters

    stack = SimpleNamespace(ctx=_engine(), formatter_calls=[])
    _capture_formatters(stack)
    text = stack.ctx._format_decisions(
        [SimpleNamespace(id="d1", description="we stopped using recall_hubs", outcome="success", confidence=0.8)]
    )
    note = changed_tools_note(text)
    assert note  # the fixture names a changed tool
    read = _read(stack, [("Related Decisions", f"{text}\n{note}")], {("decision", "d1")})
    assert read["order"] == [("decision", "d1")] and read["unmatched_segments"] == 0
    assert preturn.strip_tools_note("- a line\nNote: not the tools note.") == "- a line\nNote: not the tools note."


def test_a_fragment_the_trace_dropped_is_judged_as_shown_and_not_counted_untraced() -> None:
    from nous_eval.served.stack import _capture_formatters

    stack = SimpleNamespace(ctx=_engine(), formatter_calls=[])
    _capture_formatters(stack)
    eps = [
        SimpleNamespace(id=f"e{i}", summary=f"session number {i} " + "y" * 60, outcome="success", started_at=None)
        for i in range(2)
    ]
    full = stack.ctx._format_episodes(eps)
    first = full.split("\n")[0]
    cut = stack.ctx._truncate_to_budget(full, (len(first) + 1 + 8 + 3) // 4)  # e1 keeps a few characters
    # the trace dropped e1 (its content probe is not in the text), the text still shows its start
    read = _read(stack, [("Past Episodes", cut)], {("episode", "e0")})
    assert read["order"] == [("episode", "e0"), ("episode", "e1")] and read["texts"][("episode", "e1")].endswith("...")
    assert read["untraced"] == 0 and read["fragment_untraced"] == 1
