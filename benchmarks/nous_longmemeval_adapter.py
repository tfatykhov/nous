"""
Nous LongMemEval Benchmark Adapter
====================================
Feeds LongMemEval conversation histories through Nous's /chat endpoint,
then asks benchmark questions and collects answers for scoring.

Usage:
    python nous_longmemeval_adapter.py \
        --data ./data/longmemeval_s_cleaned.json \
        --nous-url http://localhost:8000 \
        --output ./results/longmemeval_results.jsonl

    # Then score with LongMemEval's official evaluator:
    # cd LongMemEval/src/evaluation
    # python evaluate_qa.py gpt-4o ../../results/longmemeval_results.jsonl ../../data/longmemeval_oracle.json

Flow:
    Phase 1 (Ingest):  Feed haystack sessions turn-by-turn via POST /chat
    Phase 2 (Test):    Send the question via POST /chat
    Phase 3 (Output):  Save in LongMemEval-compatible JSONL format

Notes:
    - LongMemEval's official evaluator uses GPT-4o for semantic matching
    - This adapter also computes lightweight local scores (F1, ROUGE-L) for quick feedback
    - For official results, always use the LongMemEval evaluator
"""

import json
import time
import argparse
import requests
import uuid
from pathlib import Path
from datetime import datetime

# ---------------------------------------------------------------------------
# Lightweight scoring (for quick local feedback — official eval uses GPT-4o)
# ---------------------------------------------------------------------------

def tokenize(text: str) -> list[str]:
    return text.lower().split()


def compute_f1(prediction: str, reference: str) -> float:
    pred_tokens = set(tokenize(prediction))
    ref_tokens = set(tokenize(reference))
    if not pred_tokens or not ref_tokens:
        return 0.0
    common = pred_tokens & ref_tokens
    if not common:
        return 0.0
    precision = len(common) / len(pred_tokens)
    recall = len(common) / len(ref_tokens)
    return 2 * precision * recall / (precision + recall)


def _lcs_length(a: list, b: list) -> int:
    m, n = len(a), len(b)
    dp = [[0] * (n + 1) for _ in range(m + 1)]
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            if a[i - 1] == b[j - 1]:
                dp[i][j] = dp[i - 1][j - 1] + 1
            else:
                dp[i][j] = max(dp[i - 1][j], dp[i][j - 1])
    return dp[m][n]


def compute_rouge_l(prediction: str, reference: str) -> float:
    pred_tokens = tokenize(prediction)
    ref_tokens = tokenize(reference)
    if not pred_tokens or not ref_tokens:
        return 0.0
    lcs = _lcs_length(pred_tokens, ref_tokens)
    precision = lcs / len(pred_tokens)
    recall = lcs / len(ref_tokens)
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


# ---------------------------------------------------------------------------
# Nous API Client
# ---------------------------------------------------------------------------

class NousClient:
    """Thin wrapper around Nous REST API — only uses /chat."""

    def __init__(self, base_url: str, timeout: int = 120):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def chat(self, message: str, session_id: str) -> str:
        resp = requests.post(
            f"{self.base_url}/chat",
            json={"message": message, "session_id": session_id},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict):
            return data.get("response", data.get("reply", data.get("content", str(data))))
        return str(data)

    def health(self) -> bool:
        try:
            resp = requests.get(f"{self.base_url}/health", timeout=10)
            return resp.status_code == 200
        except Exception:
            return False


# ---------------------------------------------------------------------------
# LongMemEval Adapter
# ---------------------------------------------------------------------------

def load_longmemeval(data_path: str) -> list[dict]:
    """Load LongMemEval dataset (json — list of 500 question instances)."""
    with open(data_path, "r") as f:
        data = json.load(f)
    if isinstance(data, dict):
        data = list(data.values())
    return data


def ingest_sessions(client: NousClient, instance: dict, session_id: str,
                    delay: float = 0.3, verbose: bool = True) -> int:
    """
    Phase 1: Feed haystack sessions into Nous via /chat.
    
    Each instance has:
      - haystack_sessions: list of sessions
      - haystack_dates: list of timestamps for each session
      
    Each session is a list of turns: {"role": "user"/"assistant", "content": "..."}
    
    We send them as a conversation Nous is observing.
    Returns total turns ingested.
    """
    sessions = instance.get("haystack_sessions", [])
    dates = instance.get("haystack_dates", [])
    
    turn_count = 0

    # Context message
    context_msg = (
        "I'm going to share a series of past conversation sessions with you. "
        "Please pay close attention to all details, facts, preferences, events, "
        "and any changes over time. I'll ask you a question about them afterward."
    )
    client.chat(context_msg, session_id)
    turn_count += 1

    for idx, session in enumerate(sessions):
        # Session timestamp header
        date_str = dates[idx] if idx < len(dates) else f"Session {idx + 1}"
        header = f"[Conversation session from {date_str}]"
        client.chat(header, session_id)
        turn_count += 1

        # Feed each turn
        for turn in session:
            role = turn.get("role", "user")
            content = turn.get("content", "")
            if not content:
                continue

            # Format as reported speech so Nous processes it as memory
            if role == "user":
                message = f"User said: {content}"
            else:
                message = f"Assistant said: {content}"

            client.chat(message, session_id)
            turn_count += 1

            if verbose and turn_count % 50 == 0:
                print(f"    ... {turn_count} turns ingested")

            time.sleep(delay)

    if verbose:
        print(f"    ✅ {turn_count} turns ingested across {len(sessions)} sessions")

    return turn_count


def ask_question(client: NousClient, instance: dict, session_id: str) -> str:
    """Phase 2: Ask the benchmark question via /chat."""
    question = instance.get("question", "")
    question_date = instance.get("question_date", "")

    # Include temporal context if available
    if question_date:
        prompt = (
            f"Today's date is {question_date}. "
            f"Based on all the conversations I've shared with you, "
            f"please answer this question: {question}"
        )
    else:
        prompt = (
            f"Based on all the conversations I've shared with you, "
            f"please answer this question: {question}"
        )

    return client.chat(prompt, session_id)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Nous LongMemEval Benchmark Adapter")
    parser.add_argument("--data", required=True,
                        help="Path to longmemeval_s_cleaned.json (or _m or _oracle)")
    parser.add_argument("--nous-url", default="http://localhost:8000",
                        help="Nous API base URL")
    parser.add_argument("--output", default="./results/longmemeval_results.jsonl",
                        help="Output results file (JSONL)")
    parser.add_argument("--delay", type=float, default=0.3,
                        help="Delay between API calls (seconds)")
    parser.add_argument("--max-questions", type=int, default=None,
                        help="Limit number of questions (for testing)")
    parser.add_argument("--start-from", type=int, default=0,
                        help="Skip first N questions (for resuming)")
    parser.add_argument("--verbose", action="store_true", default=True)
    args = parser.parse_args()

    # Init
    client = NousClient(args.nous_url)
    print(f"🧠 Nous LongMemEval Benchmark Adapter")
    print(f"   API: {args.nous_url}")
    print(f"   Data: {args.data}")
    print(f"   Output: {args.output}")
    print()

    # Health check
    if not client.health():
        print("❌ Cannot reach Nous API. Is it running?")
        return
    print("✅ Nous API is healthy\n")

    # Load data
    instances = load_longmemeval(args.data)
    total = len(instances)
    print(f"📊 Loaded {total} question instances\n")

    # Apply slicing
    instances = instances[args.start_from:]
    if args.max_questions:
        instances = instances[:args.max_questions]
    print(f"   Processing {len(instances)} questions (start_from={args.start_from})\n")

    # Process
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    all_results = []
    scores_by_type = {}

    for i, instance in enumerate(instances):
        qid = instance.get("question_id", f"q_{i}")
        qtype = instance.get("question_type", "unknown")
        question = instance.get("question", "")
        gold_answer = instance.get("answer", "")
        n_sessions = len(instance.get("haystack_sessions", []))

        # Each question gets its own session (fresh memory per question)
        session_id = f"longmemeval-{qid}-{uuid.uuid4().hex[:6]}"

        print(f"{'─'*50}")
        print(f"  [{i+1}/{len(instances)}] {qid} ({qtype})")
        print(f"  Sessions: {n_sessions} | Q: {question[:70]}...")

        # Phase 1: Ingest conversation history
        t0 = time.time()
        n_turns = ingest_sessions(client, instance, session_id,
                                   delay=args.delay, verbose=args.verbose)
        ingest_time = time.time() - t0

        # Phase 2: Ask the question
        t1 = time.time()
        nous_answer = ask_question(client, instance, session_id)
        answer_time = time.time() - t1

        # Local scoring (quick feedback — not official)
        f1 = compute_f1(nous_answer, gold_answer)
        rouge_l = compute_rouge_l(nous_answer, gold_answer)

        # Track by type
        if qtype not in scores_by_type:
            scores_by_type[qtype] = {"f1": [], "rouge_l": [], "count": 0}
        scores_by_type[qtype]["f1"].append(f1)
        scores_by_type[qtype]["rouge_l"].append(rouge_l)
        scores_by_type[qtype]["count"] += 1

        print(f"  ⏱️  Ingest: {ingest_time:.1f}s | Answer: {answer_time:.1f}s")
        print(f"  📊 F1={f1:.3f} ROUGE-L={rouge_l:.3f}")
        print(f"  Gold: {gold_answer[:80]}")
        print(f"  Nous: {nous_answer[:80]}")

        # Save in LongMemEval-compatible format
        # (question_id + hypothesis is what their evaluator expects)
        result = {
            "question_id": qid,
            "hypothesis": nous_answer,
            # Extra fields for our analysis (ignored by official evaluator)
            "_question_type": qtype,
            "_question": question,
            "_gold_answer": gold_answer,
            "_local_f1": round(f1, 4),
            "_local_rouge_l": round(rouge_l, 4),
            "_n_sessions": n_sessions,
            "_n_turns_ingested": n_turns,
            "_ingest_time_s": round(ingest_time, 1),
            "_answer_time_s": round(answer_time, 1),
        }
        all_results.append(result)

        # Write incrementally
        with open(output_path, "a") as f:
            f.write(json.dumps(result) + "\n")

    # ---------------------------------------------------------------------------
    # Summary
    # ---------------------------------------------------------------------------
    print(f"\n{'='*60}")
    print(f"🏆 RESULTS SUMMARY (local scoring — use official evaluator for final)")
    print(f"{'='*60}")

    def avg(lst):
        return sum(lst) / len(lst) if lst else 0.0

    all_f1 = [r["_local_f1"] for r in all_results]
    all_rouge = [r["_local_rouge_l"] for r in all_results]

    print(f"\n📊 Overall ({len(all_results)} questions):")
    print(f"   F1:      {avg(all_f1):.4f}")
    print(f"   ROUGE-L: {avg(all_rouge):.4f}")

    print(f"\n📊 By Question Type:")
    for qtype, scores in sorted(scores_by_type.items()):
        n = scores["count"]
        f1_avg = avg(scores["f1"])
        rl_avg = avg(scores["rouge_l"])
        print(f"   {qtype:30s} (n={n:3d}): F1={f1_avg:.4f}  ROUGE-L={rl_avg:.4f}")

    # Save summary
    summary = {
        "timestamp": datetime.utcnow().isoformat(),
        "data_file": args.data,
        "n_questions": len(all_results),
        "overall_f1": round(avg(all_f1), 4),
        "overall_rouge_l": round(avg(all_rouge), 4),
        "by_type": {
            qtype: {
                "count": scores["count"],
                "f1": round(avg(scores["f1"]), 4),
                "rouge_l": round(avg(scores["rouge_l"]), 4),
            }
            for qtype, scores in scores_by_type.items()
        }
    }
    summary_path = output_path.with_suffix(".summary.json")
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)

    print(f"\n💾 Results: {output_path}")
    print(f"💾 Summary: {summary_path}")
    print(f"\n📌 For official scores, run LongMemEval's evaluator:")
    print(f"   cd LongMemEval/src/evaluation")
    print(f"   python evaluate_qa.py gpt-4o {output_path} ./data/longmemeval_oracle.json")


if __name__ == "__main__":
    main()
