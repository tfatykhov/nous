# Nous Benchmark Adapters

Minimal adapters to run Nous against memory benchmarks. 
**One endpoint. One loop. Let Nous be Nous.**

## Philosophy

These adapters don't reach into Nous's internals. They:
1. Feed conversations through `POST /chat` (Nous stores memory automatically)
2. Ask questions through `POST /chat` (Nous recalls what it needs)
3. Compare answers to gold labels

This tests the **full cognitive pipeline** — not individual components.

---

## LoCoMo (ACL 2024)

**What it tests:** Very long-term conversational memory (10 conversations spanning weeks/months)

### Setup
```bash
# Get the data
git clone https://github.com/snap-research/locomo.git
# Data is at locomo/data/locomo10.json
```

### Run
```bash
# Full run (10 conversations, all QA)
python nous_locomo_adapter.py \
    --data ./locomo/data/locomo10.json \
    --nous-url http://localhost:8000 \
    --output ./results/locomo_results.jsonl

# Quick test (1 conversation only)
python nous_locomo_adapter.py \
    --data ./locomo/data/locomo10.json \
    --nous-url http://localhost:8000 \
    --output ./results/locomo_test.jsonl \
    --max-conversations 1
```

### Output
- `locomo_results.jsonl` — per-question results with F1, BLEU-1, ROUGE-L
- `locomo_results.aggregate.json` — overall and per-category scores

### Baseline to beat
- GPT-4 (full context): F1 = 32.1
- RAG + GPT-3.5: F1 = ~28

---

## LongMemEval (ICLR 2025)

**What it tests:** 5 core memory abilities across 500 questions

### Setup
```bash
# Get the data
mkdir -p data/
cd data/
wget https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_s_cleaned.json
cd ..

# Get the official evaluator
git clone https://github.com/xiaowu0162/LongMemEval.git

# Install eval dependencies
cd LongMemEval
pip install -r requirements-lite.txt
cd ..
```

### Run
```bash
# Full run (500 questions, ~40 sessions each)
python nous_longmemeval_adapter.py \
    --data ./data/longmemeval_s_cleaned.json \
    --nous-url http://localhost:8000 \
    --output ./results/longmemeval_results.jsonl

# Quick test (first 10 questions)
python nous_longmemeval_adapter.py \
    --data ./data/longmemeval_s_cleaned.json \
    --nous-url http://localhost:8000 \
    --output ./results/longmemeval_test.jsonl \
    --max-questions 10

# Resume from question 50 (if interrupted)
python nous_longmemeval_adapter.py \
    --data ./data/longmemeval_s_cleaned.json \
    --nous-url http://localhost:8000 \
    --output ./results/longmemeval_results.jsonl \
    --start-from 50
```

### Official Scoring
```bash
# Local scores are approximate — use this for real numbers
export OPENAI_API_KEY=your_key
cd LongMemEval/src/evaluation
python evaluate_qa.py gpt-4o ../../results/longmemeval_results.jsonl ../../data/longmemeval_oracle.json
```

### Output
- `longmemeval_results.jsonl` — LongMemEval-compatible format (question_id + hypothesis)
- `longmemeval_results.summary.json` — local F1/ROUGE-L scores by question type

### Question Types Tested
- **Information Extraction** — single-session fact recall
- **Multi-Session Reasoning** — connecting facts across sessions  
- **Knowledge Updates** — handling changed/corrected information
- **Temporal Reasoning** — "before/after" time-based questions
- **Abstention** — knowing when to say "I don't know"

---

## Cost Estimates

| Benchmark | Questions | Sessions to Ingest | Est. API Cost | Est. Time |
|-----------|----------|-------------------|---------------|-----------|
| LoCoMo | ~100 QA | 10 conversations | $5-10 | 1-2 hours |
| LongMemEval_S | 500 | ~40 per question | $30-60 | 4-8 hours |
| LongMemEval_M | 500 | ~500 per question | $200+ | 24+ hours |

---

## Tips

- **Start small:** Use `--max-conversations 1` or `--max-questions 5` first
- **Monitor memory:** Watch Nous logs to see what facts/episodes get stored
- **Rate limits:** Adjust `--delay` if hitting API limits
- **Resume:** LongMemEval adapter supports `--start-from` for interrupted runs
- **Fresh sessions:** Each conversation/question gets a unique session ID so there's no cross-contamination
