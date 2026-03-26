"""
Nous LoCoMo Benchmark Adapter
==============================
Feeds LoCoMo conversations through Nous's /chat endpoint,
then asks benchmark questions and scores answers.

Usage:
    python nous_locomo_adapter.py \
        --data ./data/locomo10.json \
        --nous-url http://localhost:8000 \
        --output ./results/locomo_results.jsonl

Flow:
    Phase 1 (Ingest):  Feed each conversation session turn-by-turn via POST /chat
    Phase 2 (Test):    Send each QA question via POST /chat
    Phase 3 (Score):   Compare answers to gold labels (BLEU, ROUGE-L, F1)
"""

import json
import time
import argparse
import requests
import uuid
from pathlib import Path
from datetime import datetime

# ---------------------------------------------------------------------------
# Scoring helpers (lightweight, no heavy deps needed)
# ---------------------------------------------------------------------------

def tokenize(text: str) -> list[str]:
    """Simple whitespace + lowercase tokenizer."""
    return text.lower().split()


def compute_f1(prediction: str, reference: str) -> float:
    """Token-level F1 between prediction and reference."""
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


def compute_bleu1(prediction: str, reference: str) -> float:
    """Unigram BLEU (BLEU-1) — simple precision of pred tokens in ref."""
    pred_tokens = tokenize(prediction)
    ref_tokens = set(tokenize(reference))
    if not pred_tokens:
        return 0.0
    hits = sum(1 for t in pred_tokens if t in ref_tokens)
    return hits / len(pred_tokens)


def _lcs_length(a: list, b: list) -> int:
    """Longest common subsequence length."""
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
    """ROUGE-L F1 score."""
    pred_tokens = tokenize(prediction)
    ref_tokens = tokenize(reference)
    if not pred_tokens or not ref_tokens:
        return 0.0
    lcs = _lcs_length(pred_tokens, ref_tokens)
    precision = lcs / len(pred_tokens) if pred_tokens else 0.0
    recall = lcs / len(ref_tokens) if ref_tokens else 0.0
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
        """Send a message to Nous and return the response text."""
        resp = requests.post(
            f"{self.base_url}/chat",
            json={"message": message, "session_id": session_id},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        # Handle different response shapes
        if isinstance(data, dict):
            return data.get("response", data.get("reply", data.get("content", str(data))))
        return str(data)

    def health(self) -> bool:
        """Check if Nous is reachable."""
        try:
            resp = requests.get(f"{self.base_url}/health", timeout=10)
            return resp.status_code == 200
        except Exception:
            return False


# ---------------------------------------------------------------------------
# LoCoMo Adapter
# ---------------------------------------------------------------------------

def load_locomo(data_path: str) -> list[dict]:
    """Load LoCoMo dataset (locomo10.json)."""
    with open(data_path, "r") as f:
        data = json.load(f)
    # locomo10.json is a list of 10 conversation samples
    if isinstance(data, dict):
        data = [data]
    return data


def ingest_conversation(client: NousClient, sample: dict, session_id: str,
                        delay: float = 0.5, verbose: bool = True) -> int:
    """
    Phase 1: Feed conversation history into Nous turn-by-turn.
    
    Each turn is sent as the speaker's message. We alternate speakers
    to simulate a natural conversation Nous is observing/participating in.
    
    Returns the number of turns ingested.
    """
    conversation = sample.get("conversation", {})
    speaker_a = conversation.get("speaker_a", "Speaker A")
    speaker_b = conversation.get("speaker_b", "Speaker B")

    # Collect all sessions in order
    session_keys = sorted(
        [k for k in conversation if k.startswith("session_")
         and not k.endswith("_date_time")],
        key=lambda x: int(x.split("_")[1])
    )

    turn_count = 0

    # Initial context message so Nous knows what's happening
    context_msg = (
        f"I'm going to share a conversation between {speaker_a} and {speaker_b} "
        f"that happened over multiple sessions. Please pay attention to all details, "
        f"events, preferences, and facts mentioned. I'll ask you questions about it afterward."
    )
    client.chat(context_msg, session_id)
    turn_count += 1

    for session_key in session_keys:
        session = conversation[session_key]
        date_key = f"{session_key}_date_time"
        session_date = conversation.get(date_key, "unknown date")

        # Signal new session with timestamp
        session_header = f"[Session on {session_date}]"
        client.chat(session_header, session_id)
        turn_count += 1

        # Feed each turn
        for turn in session:
            speaker = turn.get("speaker", "Unknown")
            text = turn.get("text", "")
            if not text:
                continue

            message = f"{speaker}: {text}"
            client.chat(message, session_id)
            turn_count += 1

            if verbose and turn_count % 20 == 0:
                print(f"  ... ingested {turn_count} turns")

            time.sleep(delay)  # Respect rate limits

    if verbose:
        print(f"  ✅ Ingested {turn_count} turns for session {session_id}")

    return turn_count


def run_qa(client: NousClient, sample: dict, session_id: str,
           delay: float = 1.0, verbose: bool = True) -> list[dict]:
    """
    Phase 2 & 3: Ask questions and collect + score answers.
    
    Returns list of result dicts with question, gold answer, 
    Nous response, and scores.
    """
    qa_items = sample.get("qa", [])
    results = []

    for i, qa in enumerate(qa_items):
        question = qa.get("question", "")
        gold_answer = qa.get("answer", "")
        category = qa.get("category", "unknown")
        evidence = qa.get("evidence", [])

        if not question:
            continue

        # Ask Nous
        prompt = f"Based on the conversation I shared with you, please answer this question: {question}"
        nous_answer = client.chat(prompt, session_id)

        # Score
        f1 = compute_f1(nous_answer, gold_answer)
        bleu = compute_bleu1(nous_answer, gold_answer)
        rouge_l = compute_rouge_l(nous_answer, gold_answer)

        result = {
            "question_id": f"{sample.get('sample_id', 'unknown')}_{i}",
            "category": category,
            "question": question,
            "gold_answer": gold_answer,
            "nous_answer": nous_answer,
            "evidence_ids": evidence,
            "scores": {
                "f1": round(f1, 4),
                "bleu1": round(bleu, 4),
                "rouge_l": round(rouge_l, 4),
            }
        }
        results.append(result)

        if verbose:
            print(f"  Q{i+1}/{len(qa_items)} | F1={f1:.3f} BLEU={bleu:.3f} ROUGE-L={rouge_l:.3f}")
            print(f"    Q: {question[:80]}...")
            print(f"    Gold: {gold_answer[:80]}...")
            print(f"    Nous: {nous_answer[:80]}...")

        time.sleep(delay)

    return results


def compute_aggregate_scores(all_results: list[dict]) -> dict:
    """Compute aggregate scores across all questions."""
    if not all_results:
        return {}

    metrics = {"f1": [], "bleu1": [], "rouge_l": []}
    by_category = {}

    for r in all_results:
        for metric in metrics:
            metrics[metric].append(r["scores"][metric])

        cat = r.get("category", "unknown")
        if cat not in by_category:
            by_category[cat] = {"f1": [], "bleu1": [], "rouge_l": []}
        for metric in metrics:
            by_category[cat][metric].append(r["scores"][metric])

    def avg(lst):
        return round(sum(lst) / len(lst), 4) if lst else 0.0

    aggregate = {
        "overall": {
            "n_questions": len(all_results),
            "f1": avg(metrics["f1"]),
            "bleu1": avg(metrics["bleu1"]),
            "rouge_l": avg(metrics["rouge_l"]),
        },
        "by_category": {
            cat: {
                "n_questions": len(scores["f1"]),
                "f1": avg(scores["f1"]),
                "bleu1": avg(scores["bleu1"]),
                "rouge_l": avg(scores["rouge_l"]),
            }
            for cat, scores in by_category.items()
        }
    }
    return aggregate


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Nous LoCoMo Benchmark Adapter")
    parser.add_argument("--data", required=True, help="Path to locomo10.json")
    parser.add_argument("--nous-url", default="http://localhost:8000", help="Nous API base URL")
    parser.add_argument("--output", default="./results/locomo_results.jsonl", help="Output results file")
    parser.add_argument("--delay", type=float, default=0.5, help="Delay between API calls (seconds)")
    parser.add_argument("--max-conversations", type=int, default=None, help="Limit conversations (for testing)")
    parser.add_argument("--verbose", action="store_true", default=True)
    args = parser.parse_args()

    # Init
    client = NousClient(args.nous_url)
    print(f"🧠 Nous LoCoMo Benchmark Adapter")
    print(f"   API: {args.nous_url}")
    print(f"   Data: {args.data}")
    print()

    # Health check
    if not client.health():
        print("❌ Cannot reach Nous API. Is it running?")
        return

    print("✅ Nous API is healthy\n")

    # Load data
    samples = load_locomo(args.data)
    if args.max_conversations:
        samples = samples[:args.max_conversations]
    print(f"📊 Loaded {len(samples)} conversations\n")

    # Process each conversation
    all_results = []
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    for idx, sample in enumerate(samples):
        sample_id = sample.get("sample_id", f"conv_{idx}")
        session_id = f"locomo-bench-{sample_id}-{uuid.uuid4().hex[:8]}"

        print(f"{'='*60}")
        print(f"📝 Conversation {idx+1}/{len(samples)}: {sample_id}")
        print(f"   Session ID: {session_id}")
        print(f"{'='*60}")

        # Phase 1: Ingest
        print("\n🔄 Phase 1: Ingesting conversation...")
        n_turns = ingest_conversation(client, sample, session_id,
                                       delay=args.delay, verbose=args.verbose)

        # Phase 2 & 3: QA + Scoring
        print(f"\n❓ Phase 2: Running QA ({len(sample.get('qa', []))} questions)...")
        results = run_qa(client, sample, session_id,
                        delay=args.delay, verbose=args.verbose)

        all_results.extend(results)

        # Write results incrementally
        with open(output_path, "a") as f:
            for r in results:
                f.write(json.dumps(r) + "\n")

        print(f"\n✅ Conversation {sample_id} complete: {len(results)} questions answered\n")

    # Final aggregate scores
    print(f"\n{'='*60}")
    print(f"🏆 FINAL RESULTS")
    print(f"{'='*60}")

    aggregate = compute_aggregate_scores(all_results)

    print(f"\n📊 Overall ({aggregate['overall']['n_questions']} questions):")
    print(f"   F1:      {aggregate['overall']['f1']}")
    print(f"   BLEU-1:  {aggregate['overall']['bleu1']}")
    print(f"   ROUGE-L: {aggregate['overall']['rouge_l']}")

    print(f"\n📊 By Category:")
    for cat, scores in aggregate.get("by_category", {}).items():
        print(f"   {cat} (n={scores['n_questions']}): F1={scores['f1']} BLEU={scores['bleu1']} ROUGE-L={scores['rouge_l']}")

    # Save aggregate
    agg_path = output_path.with_suffix(".aggregate.json")
    with open(agg_path, "w") as f:
        json.dump(aggregate, f, indent=2)
    print(f"\n💾 Results saved to {output_path}")
    print(f"💾 Aggregate saved to {agg_path}")


if __name__ == "__main__":
    main()
