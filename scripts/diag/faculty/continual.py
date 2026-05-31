"""Phase-1 eval (NEW track): continual learning / test-time learning.

Question Phase 0 left open: does WRITING to Nous memory let the agent perform a task it
could NOT do cold — and is any gain from STORAGE+RETRIEVAL (RAG works) vs CONSOLIDATION
(plasticity / compounding)? Phase 0 said edge weights are frozen => (b) consolidation is
pre-registered NULL by architecture. This eval measures the storage+retrieval CEILING and
the ABSENCE of compounding.

Synthetic profession "Glorptax parcel fees" — invented materials/classes/credits so the
cold baseline CANNOT guess (kills parametric leak), and success on the composition cases
requires RETRIEVING and COMPOSING several private rules (not echoing one).

Advisor's four requirements, baked in:
  1. no-write control = the COLD baseline. Test turns are separate stateless /chat sessions,
     so the only way the write-arm can know an invented credit value at test time is by
     RETRIEVING it from Nous memory. Cold-vs-write on the SAME items is the within-subject control.
  2. per-turn retrieval log = usage.tool_calls per item (did it recall vs guess).
  3. (b) consolidation pre-registered NULL (frozen weights) — framed as a ceiling/absence test.
  4. tasks require APPLYING + COMPOSING rules (R5 waives R3 for Borix+Drennel).

  uv run python scripts/diag/faculty/continual.py run        # reset -> baseline -> reset -> ingest -> test
  uv run python scripts/diag/faculty/continual.py baseline   # cold only
  uv run python scripts/diag/faculty/continual.py ingest      # write rules (real learn path) + sleep + verify
  uv run python scripts/diag/faculty/continual.py test        # after-write only
"""
from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _load(path: Path) -> None:
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        os.environ[k] = v


_load(REPO / ".env.prod-snapshot")
os.environ.update({
    "DB_HOST": "127.0.0.1", "DB_PORT": "5433", "DB_NAME": "nous_eval_live",
    "DB_USER": "nous", "DB_PASSWORD": "nous_eval", "NOUS_AGENT_ID": "nous-continual-eval",
})

AGENT = "nous-continual-eval"
BASE = "http://127.0.0.1:8079"

# ---- the synthetic profession: private rules + deterministic ground truth ----
RULES = [
    "Glorptax rule R1 (class by material): a parcel's class is set by its material. "
    "vexil and mellis are class Aurex. quorn is class Borix.",
    "Glorptax rule R2 (base fee by class): an Aurex parcel has a base fee of 40 credits. "
    "A Borix parcel has a base fee of 25 credits.",
    "Glorptax rule R3 (weight surcharge): any parcel heavier than 5 kg adds a 15-credit surcharge.",
    "Glorptax rule R4 (route adjustment): the Drennel route subtracts 10 credits, the Pellan "
    "route adds 5 credits, and the standard route adds nothing.",
    "Glorptax rule R5 (special exemption): a Borix parcel shipped via the Drennel route is exempt "
    "from the R3 weight surcharge — the 15-credit surcharge does not apply to it.",
]


def ground_truth(material: str, weight: float, route: str) -> int:
    cls = "Borix" if material == "quorn" else "Aurex"
    fee = 25 if cls == "Borix" else 40
    surcharge = weight > 5
    if cls == "Borix" and route == "Drennel":  # R5 overrides R3
        surcharge = False
    if surcharge:
        fee += 15
    if route == "Drennel":
        fee -= 10
    elif route == "Pellan":
        fee += 5
    return fee


# (material, weight, route, needs_composition)  — composition = R5 is decision-relevant
TASKS = [
    ("vexil", 3, "standard", False),   # 40
    ("quorn", 2, "standard", False),   # 25
    ("mellis", 8, "standard", False),  # 55  (R3)
    ("vexil", 4, "Pellan", False),     # 45  (R4)
    ("quorn", 3, "Pellan", False),     # 30  (R4)
    ("quorn", 7, "Drennel", True),     # 15  (R5 waives R3, then R4) <-- composition
    ("quorn", 9, "Drennel", True),     # 15  (R5 waives R3) <-- composition
    ("vexil", 7, "Drennel", True),     # 45  (R5 does NOT fire for Aurex: 40+15-10) <-- discrimination
    ("mellis", 6, "Drennel", True),    # 45  (Aurex, R5 n/a: 40+15-10) <-- discrimination
    ("quorn", 4, "Drennel", True),     # 15  (under 5kg, -10; R5 moot) <-- composition-adjacent
]

PROMPT = (
    "A {w} kg {m} parcel is shipped via the {r} route. Using the Glorptax fee rules, what is "
    "the total fee in credits? Show brief reasoning, then end with a line exactly: FEE: <number>"
)

RUN_DIR = HERE / "runs" / (time.strftime("%Y%m%d-%H%M%S") + "-continual")
RUN_DIR.mkdir(parents=True, exist_ok=True)
LOG = open(RUN_DIR / "result.txt", "w", encoding="utf-8")


def out(*a):
    s = " ".join(str(x) for x in a)
    print(s)
    LOG.write(s + "\n")
    LOG.flush()


def psql(q: str) -> str:
    return subprocess.run(
        ["docker", "exec", "nous-eval-scratch", "psql", "-U", "nous", "-d", "nous_eval_live", "-tAc", q],
        capture_output=True, text=True).stdout.strip()


def reset_namespace() -> None:
    """Wipe the agent's memory so each phase starts clean."""
    for tbl in ("heart.facts", "heart.episodes", "heart.episode_chunks", "heart.working_memory"):
        psql(f"DELETE FROM {tbl} WHERE agent_id='{AGENT}'")
    # edges reference brain.* / heart.* ids; clear any that became orphaned for this agent
    psql(f"DELETE FROM brain.graph_edges WHERE agent_id='{AGENT}'")
    remaining = psql(f"SELECT count(*) FROM heart.facts WHERE agent_id='{AGENT}'")
    out(f"  [reset] namespace {AGENT} wiped (facts={remaining})")


def ask(query: str) -> tuple[str, int]:
    sid = f"cq-{int(time.time()*1000)}"
    data: dict = {}
    try:
        r = httpx.post(f"{BASE}/chat", json={"message": query, "session_id": sid}, timeout=280)
        if r.headers.get("content-type", "").startswith("application/json"):
            data = r.json()
    except Exception as ex:
        return f"(error {ex})", -1
    finally:
        try:
            httpx.delete(f"{BASE}/chat/{sid}", timeout=20)
        except Exception:
            pass
    txt = ""
    for key in ("response", "content", "message"):
        if isinstance(data.get(key), str) and data[key].strip():
            txt = data[key].strip(); break
    return (txt or "(empty)"), int((data.get("usage") or {}).get("tool_calls", -1))


def parse_fee(answer: str) -> int | None:
    m = re.findall(r"FEE:\s*(-?\d+)", answer)
    if m:
        return int(m[-1])
    # fallback: a trailing "= N credits"
    m = re.findall(r"(-?\d+)\s*credits?\b", answer.lower())
    return int(m[-1]) if m else None


def run_tasks(label: str) -> dict:
    out(f"\n=== {label} — {len(TASKS)} tasks ===")
    rows = []
    for m, w, r, comp in TASKS:
        gt = ground_truth(m, w, r)
        ans, tc = ask(PROMPT.format(w=w, m=m, r=r))
        got = parse_fee(ans)
        ok = (got == gt)
        rows.append({"m": m, "w": w, "r": r, "comp": comp, "gt": gt, "got": got, "ok": ok, "tc": tc})
        tag = "COMPOSE" if comp else "simple "
        out(f"  [{tag}] {m:6} {w}kg {r:9} -> gt={gt:3} got={got} {'OK ' if ok else 'XX '} tc={tc}")
    simple = [x for x in rows if not x["comp"]]
    comp = [x for x in rows if x["comp"]]
    acc = sum(x["ok"] for x in rows) / len(rows)
    s_acc = sum(x["ok"] for x in simple) / len(simple) if simple else 0
    c_acc = sum(x["ok"] for x in comp) / len(comp) if comp else 0
    retrieved = sum(1 for x in rows if x["tc"] and x["tc"] > 0)
    correct_with_retrieval = sum(1 for x in rows if x["ok"] and x["tc"] and x["tc"] > 0)
    out(f"  -> acc={acc:.2f} (simple={s_acc:.2f} compose={c_acc:.2f}) "
        f"retrieved={retrieved}/{len(rows)} correct&retrieved={correct_with_retrieval}")
    return {"label": label, "acc": acc, "s_acc": s_acc, "c_acc": c_acc, "rows": rows,
            "retrieved": retrieved, "correct_with_retrieval": correct_with_retrieval}


def ingest() -> None:
    """Intervention: write the 5 rules through the agent's REAL learn path, sleep, verify coverage."""
    out("=== INGEST: write Glorptax rules via real /chat learn path + sleep ===")
    for i, rule in enumerate(RULES, 1):
        ans, tc = ask(f"Please remember this rule for my Glorptax work, exactly: {rule}\n"
                      f"Just confirm you've stored it.")
        out(f"  R{i} sent (tc={tc}): {ans[:80]}")
    out("  triggering sleep (full-cycle consolidation)...")
    try:
        httpx.post(f"{BASE}/sleep/trigger", timeout=400)
    except Exception as ex:
        out(f"  (sleep err: {ex})")
    time.sleep(8)
    # verify each rule landed; backstop direct-insert any miss (logged) so the test measures
    # retrieve+apply, not extraction coverage.
    missing = _verify_and_backstop()
    out(f"  rules in memory after ingest: {5 - len(missing)}/5  (backstopped: {missing})")


def _verify_and_backstop() -> list[int]:
    return asyncio.run(_verify_and_backstop_async())


async def _verify_and_backstop_async() -> list[int]:
    from sqlalchemy import text

    from nous.brain.embeddings import EmbeddingProvider
    from nous.config import Settings
    from nous.storage.database import Database

    markers = ["R1", "R2", "R3", "R4", "R5"]
    missing = []
    for i, mk in enumerate(markers, 1):
        n = psql(f"SELECT count(*) FROM heart.facts WHERE agent_id='{AGENT}' AND content ILIKE '%rule {mk}%'")
        if n == "0" or n == "":
            missing.append(i)
    if not missing:
        return []
    s = Settings()
    db = Database(s); await db.connect()
    emb = EmbeddingProvider(api_key=s.openai_api_key, model="text-embedding-3-large", dimensions=1536)
    async with db.session() as sess:
        for i in missing:
            content = RULES[i - 1]
            vec = await emb.embed(content)
            vlit = "[" + ",".join(f"{x:.6f}" for x in vec) + "]"
            await sess.execute(text(
                "INSERT INTO heart.facts (agent_id, content, source, confidence, active, embedding) "
                "VALUES (:a, :c, 'continual_backstop', 0.9, TRUE, CAST(:v AS vector))"
            ), {"a": AGENT, "c": content, "v": vlit})
        await sess.commit()
    await db.disconnect()
    try:
        httpx.post(f"{BASE}/sleep/trigger", timeout=400)
        time.sleep(6)
    except Exception:
        pass
    return missing


def report(baseline: dict, test: dict) -> None:
    out("\n" + "=" * 70)
    out("--- SUMMARY (Phase-1 continual-learning: storage+retrieval ceiling) ---")
    out("PRE-REGISTERED:")
    out("  (a) write-arm >> cold baseline IF rules are retrieved + applied (RAG works).")
    out("  (b) consolidation NULL by architecture (frozen edge weights) — not tested for compounding here;")
    out("      composition acc is the ceiling of retrieve+compose, not a plasticity signal.")
    out("  leak guard: invented credit values => cold baseline should be ~chance on exact FEE.")
    out(f"\n  COLD baseline (no rules in memory): acc={baseline['acc']:.2f} "
        f"(simple={baseline['s_acc']:.2f} compose={baseline['c_acc']:.2f})")
    out(f"  WRITE arm (rules in memory):        acc={test['acc']:.2f} "
        f"(simple={test['s_acc']:.2f} compose={test['c_acc']:.2f})")
    out(f"  DELTA (memory value):               +{test['acc'] - baseline['acc']:.2f}")
    out(f"  retrieval log (write arm): {test['retrieved']}/{len(TASKS)} items hit memory; "
        f"{test['correct_with_retrieval']} correct WITH retrieval")
    out("\nINTERPRETATION:")
    if test["acc"] - baseline["acc"] >= 0.3 and test["correct_with_retrieval"] >= test["retrieved"] * 0.5:
        out("  -> storage+retrieval WORKS: writing private rules lets the agent do a task it could not do cold,")
        out("     and correctness tracks retrieval (recalled+applied, not guessed).")
        if test["c_acc"] < test["s_acc"] - 0.2:
            out("  -> COMPOSITION CEILING: simple-rule application recovers but multi-rule composition (R5 override)")
            out("     lags — the retrieve-and-reason loop applies single rules better than it composes them.")
        else:
            out("  -> composition holds: the agent retrieves AND composes the private rules.")
    elif test["acc"] - baseline["acc"] < 0.15:
        out("  -> NO memory value: either rules aren't retrieved or aren't applied — investigate retrieval log.")
    out(f"\n  (run dir: {RUN_DIR})")
    LOG.close()


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    try:
        httpx.get(f"{BASE}/health", timeout=5)
    except Exception as ex:
        out(f"!! instance not on {BASE} ({ex})"); return
    if cmd == "baseline":
        run_tasks("COLD baseline")
    elif cmd == "ingest":
        ingest()
    elif cmd == "test":
        run_tasks("WRITE arm")
    else:  # run all
        out("##### PHASE-1 CONTINUAL-LEARNING EVAL — full run #####")
        reset_namespace()
        baseline = run_tasks("COLD baseline (no-write control)")
        reset_namespace()  # clear any facts the baseline turns extracted
        ingest()
        test = run_tasks("WRITE arm (rules in memory)")
        report(baseline, test)


if __name__ == "__main__":
    main()
