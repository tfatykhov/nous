"""Phase 0 AGENTIC lens (load-bearing) — does the full cognitive agent solve the
concept-bridge cases via its recall_deep tool loop, where the bare pipeline failed?

This answers the gbrain/host-decides question (docs/research/017): if the agent
naturally re-queries ("what's Zterra written in?" -> "Korlang" -> "who does Korlang?")
and bridges it, the gap is "agent doesn't reliably decompose," NOT "graph too sparse"
-> Phase 2 multi-hop graph work drops in priority.

Drives the faculty instance (port 8078, agent nous-faculty-eval, corpus already loaded
by measure_bare.py) with a NEUTRAL retrieve-then-answer prompt: memory-only, abstain if
absent. Deliberately does NOT coach decomposition (whether the agent decomposes is the
signal). Grades on the checkable invented token or the abstain token.

  uv run python scripts/diag/faculty/run_faculty_instance.py   # in another shell first
  uv run python scripts/diag/faculty/measure_agentic.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2]))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from scripts.diag.faculty.corpus import (  # noqa: E402
    CONCEPT_BRIDGE, CONTROL_NEGATIVE, CONTROL_POSITIVE,
)

BASE = "http://127.0.0.1:8078"
RUN_DIR = HERE / "runs" / (time.strftime("%Y%m%d-%H%M%S") + "-agentic")
RUN_DIR.mkdir(parents=True, exist_ok=True)
LOG = open(RUN_DIR / "result.txt", "w", encoding="utf-8")

# Light prompt: private invented entities already force memory use (no parametric
# answer exists), so we DON'T over-constrain (the rigid "reply exactly NOT IN MEMORY"
# made opus-4.8 over-think and return empty text). Just ask + allow a natural abstain.
PROMPT = "{q}\n\n(Answer from what you know about my work and contacts. If you don't have that information, just say so.)"

_ABSTAIN_CUES = ("don't have", "do not have", "don't know", "do not know", "no record",
                 "not aware", "no information", "couldn't find", "can't find", "cannot find",
                 "no one named", "not sure", "don't recall", "no details",
                 "don't have that information", "nothing about", "no mention", "no entry",
                 "i have no", "isn't anyone", "no such")


def out(*a):
    s = " ".join(str(x) for x in a)
    print(s)
    LOG.write(s + "\n")
    LOG.flush()


def ask(query: str) -> tuple[str, int]:
    """Return (answer_text, tool_calls)."""
    sid = f"faculty-{int(time.time()*1000)}"
    data: dict = {}
    try:
        r = httpx.post(f"{BASE}/chat", json={"message": PROMPT.format(q=query), "session_id": sid}, timeout=280)
        if r.headers.get("content-type", "").startswith("application/json"):
            data = r.json()
    except Exception as ex:
        return f"(error: {ex})", -1
    finally:
        try:
            httpx.delete(f"{BASE}/chat/{sid}", timeout=20)
        except Exception:
            pass
    text = ""
    for key in ("response", "content", "message", "text", "answer"):
        v = data.get(key) if isinstance(data, dict) else None
        if isinstance(v, str) and v.strip():
            text = v.strip()
            break
    tc = int((data.get("usage") or {}).get("tool_calls", -1)) if isinstance(data, dict) else -1
    return (text or "(empty response)"), tc


def grade(ans: str, token: str | None) -> str:
    a = ans.lower()
    if token is None:  # negative control: must abstain
        return "PASS(abstain)" if "not in memory" in a else f"FAIL(fabricated: {ans[:60]!r})"
    if token.lower() in a:
        return "PASS"
    if "not in memory" in a:
        return "FAIL(abstained)"
    return f"FAIL(wrong: {ans[:60]!r})"


def main() -> None:
    out(f"run dir: {RUN_DIR}")
    try:
        h = httpx.get(f"{BASE}/health", timeout=5).text
    except Exception as ex:
        out(f"!! faculty instance not reachable on {BASE} ({ex}). Launch run_faculty_instance.py first.")
        return
    out(f"health: {h}")
    out("prompt: memory-only, abstain-if-absent, decomposition NOT coached.\n")

    out("=" * 70)
    # Controls
    pos, pos_tc = ask(CONTROL_POSITIVE["query"])
    out(f"[control_positive] Q={CONTROL_POSITIVE['query']!r}  tool_calls={pos_tc}")
    out(f"   ans: {pos[:220]}")
    out(f"   -> {grade(pos, CONTROL_POSITIVE['answer_token'])}  (predict PASS)\n")

    neg, neg_tc = ask(CONTROL_NEGATIVE["query"])
    out(f"[control_negative] Q={CONTROL_NEGATIVE['query']!r}  tool_calls={neg_tc}")
    out(f"   ans: {neg[:220]}")
    out(f"   -> {grade(neg, None)}  (predict ABSTAIN)\n")

    # Concept-bridge (the gbrain test). tool_calls>0 => the agent re-queried (decomposed).
    out("--- concept_bridge (bare lens FAILED 0/3; does the agent re-query?) ---")
    results, tcs = [], []
    for c in CONCEPT_BRIDGE:
        ans, tc = ask(c["query"])
        verdict = grade(ans, c["answer_token"])
        results.append(verdict.startswith("PASS"))
        tcs.append(tc)
        out(f"\n[{c['id']}] Q={c['query']!r}  (bridge='{c['concept']}', answer='{c['answer_token']}')  tool_calls={tc}")
        out(f"   ans: {ans[:320]}")
        out(f"   -> {verdict}")

    out("\n--- SUMMARY (agentic lens) ---")
    npass = sum(results)
    ctrl_ok = (CONTROL_POSITIVE["answer_token"].lower() in pos.lower())
    out(f"control_positive: {'PASS' if ctrl_ok else 'FAIL -> INSTRUMENT BROKEN, ignore the rest'}")
    out(f"concept_bridge solved by agent: {npass}/{len(CONCEPT_BRIDGE)}  | tool_calls per item: {tcs}")
    out(f"decomposition signal: {'agent re-queried (tool_calls>0)' if any(t and t > 0 for t in tcs) else 'agent did NOT re-query (answered from pre-loaded context only)'}")
    out("")
    if npass == len(CONCEPT_BRIDGE):
        out("AGENT SOLVES concept-bridges where the bare pipe failed -> the gap is query-time "
            "DECOMPOSITION (agent re-queries), NOT graph sparsity. This DEMOTES Phase 2 multi-hop "
            "graph work; the lever becomes making the agent reliably decompose-and-rehop.")
    elif npass == 0:
        out("AGENT also fails -> the agent does NOT bridge concepts on its own; the gap is real at "
            "BOTH levels. Phase 1 (plasticity/experiential) + Phase 2 (multi-hop) stand as planned.")
    else:
        out(f"MIXED ({npass}/{len(CONCEPT_BRIDGE)}) -> agent decomposes sometimes; reliability is the "
            "issue. Inspect which it solved and how (did it re-query?).")
    LOG.close()


if __name__ == "__main__":
    main()
