"""F083 follow-up association probe — drives a LOCAL full Nous instance.

Targets http://localhost:8000 (NOUS_PROBE_BASE to override). NOT prod.

Per probe, exercises the REAL cross-session timing:
  1. SEED  — POST /chat on session A (creates an episode).
  2. END   — DELETE /chat/{A}  (fires session_ended -> async summarize).
  3. WAIT  — poll GET /episodes until session A's episode has a structured
             summary (A2 needs a summarized episode to inject), bounded timeout.
  4. FOLLOW-UP — POST /chat on a NEW session B (this is the turn under test).
Score recall-precedes-clarification: PASS if the follow-up resolves the referent
from prior-session context OR calls a recall tool before clarifying; FAIL if it
asks the user to clarify without recalling. Negatives SHOULD ask for clarification.

Usage: py scripts/diag/followup_probe.py --label baseline
Env:   NOUS_PROBE_BASE (default http://localhost:8000), NOUS_PROBE_SUMMARY_WAIT (default 45)
"""
import argparse
import json
import os
import time
import urllib.request

BASE = os.environ.get("NOUS_PROBE_BASE", "http://localhost:8000")
SUMMARY_WAIT = int(os.environ.get("NOUS_PROBE_SUMMARY_WAIT", "45"))

PROBES = [
    {"id": "deictic_option", "seed": "Give me two options for caching: Redis or in-memory LRU.",
     "followup": "what about the second option you mentioned?", "negative": False},
    {"id": "continuation", "seed": "Let's start refactoring the auth module; first extract the token parser.",
     "followup": "can you continue what we were doing?", "negative": False},
    {"id": "outcome_check", "seed": "Apply the fix to the retry budget in the worker pool.",
     "followup": "did that work?", "negative": False},
    # Hard negatives (review R2): must NOT pull cross-session episodes.
    {"id": "neg_ambiguous", "seed": "Tell me about Postgres indexes.",
     "followup": "what about the other thing?", "negative": True},
    {"id": "neg_fresh_task", "seed": "Summarize HNSW indexing.",
     "followup": "write a python function to reverse a string", "negative": True},
    {"id": "neg_same_session_phrasing", "seed": "Explain async generators.",
     "followup": "use the first argument", "negative": True},
]


def _req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{BASE}{path}", data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=300).read())


def chat(message, session_id):
    return _req("POST", "/chat", {"message": message, "session_id": session_id,
                                  "user_id": "claude-code", "user_display_name": "F083 probe"})


def end_session(session_id):
    try:
        _req("DELETE", f"/chat/{session_id}")
    except Exception as e:
        print(f"  end_session({session_id}) note: {e}")


def _summarized_ids():
    """IDs of episodes that currently have a summary (structured or legacy)."""
    try:
        eps = _req("GET", "/episodes")
        rows = eps if isinstance(eps, list) else eps.get("episodes", [])
        return {str(e.get("id")) for e in rows if (e.get("structured_summary") or e.get("summary"))}
    except Exception:
        return set()


def wait_for_new_summary(before, deadline):
    """Wait until a NEW summarized episode (not in `before`) appears.

    Scoped to the just-ended seed: capture `before` after the seed chat but
    before ending it, so a pre-existing summarized episode does not satisfy the
    wait and let the follow-up run before the seed is actually summarized.
    """
    while time.time() < deadline:
        if _summarized_ids() - before:
            return True
        time.sleep(3)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    args = ap.parse_args()
    results = []
    for p in PROBES:
        seed_sid = f"f083-seed-{p['id']}-{args.label}"
        chat(p["seed"], seed_sid)
        before = _summarized_ids()                         # snapshot BEFORE ending the seed
        end_session(seed_sid)                              # trigger async summarize
        summarized = wait_for_new_summary(before, time.time() + SUMMARY_WAIT)
        r = chat(p["followup"], f"f083-followup-{p['id']}-{args.label}")  # NEW session
        resp = (r.get("response") or "")
        clarify = any(k in resp.lower() for k in
                      ["could you clarify", "which ", "what do you mean", "not sure what you", "can you specify"])
        results.append({"id": p["id"], "negative": p["negative"], "frame": r.get("frame"),
                        "summarized": summarized, "asked_clarification": clarify, "response": resp[:1500]})
        print(f"{p['id']}: frame={r.get('frame')} summarized={summarized} clarify={clarify}")
    out = f"reports/f083_probe_{args.label}.json"
    with open(out, "w", encoding="utf-8") as f:
        f.write(json.dumps(results, indent=2))
    print("saved", out)


if __name__ == "__main__":
    main()
