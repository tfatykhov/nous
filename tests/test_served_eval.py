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
        json.dumps({"key": "Q000", "query": "alpha", "logged_at": "2026-10-01T10:00:00+00:00"}) + "\n", encoding="utf-8"
    )
    gf = _gold_file(tmp_path / "f.jsonl", "fact", [("A000", "where is alpha", "f-1")])
    ge = _gold_file(tmp_path / "e.jsonl", "episode", [("A000", "what happened", "e-1")])
    items = inputs.recall_items(q, [gf, ge], "2026-11-05")
    assert [(i["key"], i["set"], i["as_of"]) for i in items] == [
        ("Q000", "prod", "2026-10-01"),
        ("gf-A000", "gold", "2026-11-05"),
        ("ge-A000", "gold", "2026-11-05"),
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
        ("gd-D000", "why did we pick it", 2),
    ]
    assert items[0]["complete"] and items[1]["complete"]


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


def test_database_guards() -> None:
    assert db.clone_name("recall_Base-1") == "nous_eval_served_recall_base_1"
    for ok in ("nous_eval_x", "nous_test_p0", "beam_1"):
        assert db.check_scratch(ok) == ok
    for bad in ("nous", "nous_prod_20261105", "postgres"):
        with pytest.raises(SystemExit):
            db.check_scratch(bad)
    with pytest.raises(SystemExit):
        db.check_ident('x"; DROP DATABASE y; --')


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
    assert counts["jobs"] == 2 and [p["max_tokens"] for p in client.calls] == [4000, 4000]  # 25 + 5
    assert client.calls[0]["temperature"] == 0.0 and client.calls[0]["system"][0]["text"].startswith(
        "You are Claude Code"
    )
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
    assert score.recall_gold_row(row) == {"served": 1.0, "top10": 1.0, "rr": 0.5, "tokens": 100.0, "ms": 100.0}
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
                {"key": "Q000", "query": "Larkspur garden watering schedule", "logged_at": "2026-10-01T09:00:00+00:00"}
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
