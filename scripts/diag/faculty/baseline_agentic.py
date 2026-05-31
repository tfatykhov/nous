"""Capability Baseline Instrument — AGENTIC pass (docs/research/018).

Stage 2: the retrieve-and-reason loop over the same corpus (facts already in
nous-baseline-eval). The bare pass showed cosine surfaces 15/16 answers BUT with false
bridges (wrong/stale value equally reachable) on c12/c13/c16/c6/c7/c10. This pass asks the
load-bearing question: does the LLM PICK THE RIGHT one among the cosine-reachable
competitors — and does it confabulate a false bridge?

Per advisor: logs seed-retrieval + recalled-fact count per item (mechanism attribution,
not tool_calls alone); tracks false bridges (precision); c4 = abstention. Flag arms for
c12/c13 are a separate relaunch with the flags ON.

  uv run python scripts/diag/faculty/baseline_agentic.py            # prod-default
  (relaunch instance with NOUS_RECENCY_RESOLVER_ENABLED=true etc. for the flag arm)
"""
from __future__ import annotations

import os
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

from scripts.diag.faculty.baseline_corpus import CELLS, GLORPTAX_PROBE  # noqa: E402

BASE = "http://127.0.0.1:8079"
PROMPT = "{q}\n\n(Answer from what you know about me, my contacts and my work. If you don't have the information, say so.)"
_ABSTAIN = ("don't have", "do not have", "don't know", "no record", "not aware",
            "nothing about", "no information", "couldn't find", "no entry", "i have no",
            "no lawyer", "don't appear to", "no mention")
RUN = HERE / "runs" / (time.strftime("%Y%m%d-%H%M%S") + "-baseline-agentic")
RUN.mkdir(parents=True, exist_ok=True)
LOG = open(RUN / "result.txt", "w", encoding="utf-8")


def out(*a):
    s = " ".join(str(x) for x in a); print(s); LOG.write(s + "\n"); LOG.flush()


def ask(query: str) -> tuple[str, int, int]:
    """Returns (answer_text, tool_calls, recalled_fact_count)."""
    sid = f"bq-{int(time.time()*1000)}"
    data: dict = {}
    try:
        r = httpx.post(f"{BASE}/chat", json={"message": PROMPT.format(q=query),
                       "session_id": sid, "debug": True}, timeout=280)
        if r.headers.get("content-type", "").startswith("application/json"):
            data = r.json()
    except Exception as ex:
        return f"(error {ex})", -1, -1
    finally:
        try:
            httpx.delete(f"{BASE}/chat/{sid}", timeout=20)
        except Exception:
            pass
    txt = ""
    for key in ("response", "content", "message"):
        if isinstance(data.get(key), str) and data[key].strip():
            txt = data[key].strip(); break
    tc = int((data.get("usage") or {}).get("tool_calls", -1))
    rf = len(((data.get("debug") or {}).get("recalled_fact_ids")) or [])
    return (txt or "(empty)"), tc, rf


def main() -> None:
    try:
        h = httpx.get(f"{BASE}/health", timeout=5)
    except Exception as ex:
        out(f"!! instance not on {BASE} ({ex}) — launch run_baseline_instance.py first"); return
    flags = httpx.get(f"{BASE}/status", timeout=10).json() if h.status_code == 200 else {}
    out(f"=== BASELINE agentic pass (recency/temporal flags per instance launch) ===")

    cells = [c for c in CELLS if c.get("lens") in ("agentic", "both")] + [GLORPTAX_PROBE]
    rows = []
    for c in cells:
        q = c.get("query")
        if not q:
            continue
        ans, tc, rf = ask(q)
        low = ans.lower()
        abstained = any(x in low for x in _ABSTAIN)
        if c["id"] == "c4_abstain":
            solved = abstained
        else:
            tok = (c.get("answer") or "").lower()
            solved = bool(tok) and tok in low
            if c.get("answer2"):
                solved = solved and c["answer2"].lower() in low
        false_named = [t for t in c.get("false", []) if t.lower() in low]
        confab = bool(false_named) and not solved
        rows.append((c["id"], solved, confab, false_named, tc, rf))
        verdict = "PASS" if solved else ("CONFABULATED" if confab else ("ABSTAIN" if abstained else "MISS"))
        fb = f" false_named={false_named}" if false_named else ""
        out(f"  [{c['id']:18}] {verdict:13} tc={tc} recalled_facts={rf}{fb}")
        out(f"      ans: {ans[:160]}")

    out("\n" + "=" * 70 + "\n--- AGENTIC SUMMARY ---")
    passed = [r[0] for r in rows if r[1]]
    confs = [r[0] for r in rows if r[2]]
    out(f"  PASS (picked right answer): {len(passed)}/{len(rows)}  {passed}")
    out(f"  CONFABULATED (named a false bridge, not the right one): {confs}")
    out(f"  also named a false token while still correct: {[r[0] for r in rows if r[1] and r[3]]}")
    out(f"\n  run dir: {RUN}")
    LOG.close()


if __name__ == "__main__":
    main()
