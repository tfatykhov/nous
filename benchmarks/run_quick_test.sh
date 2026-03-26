#!/bin/bash
# Quick smoke test — runs 1 LoCoMo conversation + 5 LongMemEval questions
# Usage: bash run_quick_test.sh [NOUS_URL]

NOUS_URL=${1:-"http://localhost:8000"}
RESULTS_DIR="./results"

echo "🧠 Nous Benchmark Quick Test"
echo "   API: $NOUS_URL"
echo ""

# Check Nous is up
if ! curl -s "$NOUS_URL/health" > /dev/null 2>&1; then
    echo "❌ Nous is not reachable at $NOUS_URL"
    exit 1
fi
echo "✅ Nous is healthy"
echo ""

mkdir -p "$RESULTS_DIR"

# --- LoCoMo ---
if [ -f "./locomo/data/locomo10.json" ]; then
    echo "📝 Running LoCoMo (1 conversation)..."
    python nous_locomo_adapter.py \
        --data ./locomo/data/locomo10.json \
        --nous-url "$NOUS_URL" \
        --output "$RESULTS_DIR/locomo_quick.jsonl" \
        --max-conversations 1
    echo ""
else
    echo "⚠️  LoCoMo data not found. Run: git clone https://github.com/snap-research/locomo.git"
fi

# --- LongMemEval ---
if [ -f "./data/longmemeval_s_cleaned.json" ]; then
    echo "📝 Running LongMemEval (5 questions)..."
    python nous_longmemeval_adapter.py \
        --data ./data/longmemeval_s_cleaned.json \
        --nous-url "$NOUS_URL" \
        --output "$RESULTS_DIR/longmemeval_quick.jsonl" \
        --max-questions 5
    echo ""
else
    echo "⚠️  LongMemEval data not found. Run:"
    echo "   mkdir -p data && cd data"
    echo "   wget https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_s_cleaned.json"
fi

echo "🏁 Quick test complete! Check $RESULTS_DIR/"
