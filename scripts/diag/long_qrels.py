"""Build prod-length variants of a qrel file, deterministically.

    python scripts/diag/long_qrels.py --qrels tests/fixtures/qrels_prod_snapshot_jun8.jsonl \
        --topics $FIX/topics_working_memory.json --lengths $FIX/prod_query_word_counts.json \
        --fillers $FIX/prod_query_texts.json --out $FIX/qrels_prod_snapshot_jun8.long.jsonl

Why this exists: the context path searches "{current_topic}: {input_text}"
(nous/cognitive/context.py:886-889) and prod retrieval_log queries are far
longer than any fixture query (7 days to 2026-10-16: mean 53 words, median 68,
n=2010; an earlier sample of 100 recent DISTINCT queries averaged ~28). The
all-words keyword leg's zero-hit rate depends on that length, so an A/B on
13-word fixtures cannot see the effect.

Construction, per qrel i of N in file order (nothing random, nothing keyed on
the gold):
  1. prefix  = topics[i % len(topics)] + ": "   (skipped if the topic is empty or
     equals the query, mirroring context.py:888). Topics are real
     heart.working_memory current_task values -- the literal source of
     current_topic (context.py:871).
  2. target  = the (i + 0.5) / N quantile of the real retrieval_log word-count
     distribution, so the output's median is the real median. Computed in
     integers: float math floors some exact ranks one low (n=775, L=100, i=449).
  3. filler  = real retrieval_log query text, taken from fillers[i % F] onward and
     cycled, appended until the query reaches `target` words. Never truncates the
     original query, so a qrel already longer than its target is unchanged.
Topic and filler are paired with the gold at random by construction: topic drift
is the stated cause of the zero-hit rate, and pairing them with the gold's
subject would be the favourable version. Gold ids and every other field are
copied untouched. Both A/B arms see the identical text.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path


def words(text: str) -> int:
    return len(text.split())


def build_long_rows(rows: list[dict], topics: list[str | None], lengths: list[int],
                    fillers: list[str]) -> list[dict]:
    """Pure function: same inputs, same output."""
    if not (rows and topics and lengths and fillers):
        raise ValueError("rows, topics, lengths and fillers must all be non-empty")
    if not any(f.split() for f in fillers):
        raise ValueError("fillers contain no words; the fill loop would never end")
    ordered = sorted(lengths)
    n = len(rows)
    out = []
    for i, row in enumerate(rows):
        query = row["query"]
        topic = topics[i % len(topics)]
        prefixed = topic and topic.strip().lower() != query.strip().lower()
        base = f"{topic}: {query}" if prefixed else query
        target = ordered[(2 * i + 1) * len(ordered) // (2 * n)]
        need = target - words(base)
        text = base
        if need > 0:
            pool: list[str] = []
            j = i
            while len(pool) < need:
                pool.extend(fillers[j % len(fillers)].split())
                j += 1
            text = base + " " + " ".join(pool[:need])
        new = dict(row)
        new["query"] = text
        new["long_target_words"] = target
        out.append(new)
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--qrels", required=True)
    p.add_argument("--topics", required=True, help="JSON array of strings")
    p.add_argument("--lengths", required=True, help="JSON array of ints")
    p.add_argument("--fillers", required=True, help="JSON array of strings")
    p.add_argument("--out", required=True)
    a = p.parse_args()
    rows = [json.loads(x) for x in Path(a.qrels).read_text(encoding="utf-8").splitlines() if x.strip()]
    load = lambda f: json.loads(Path(f).read_text(encoding="utf-8"))  # noqa: E731
    out = build_long_rows(rows, load(a.topics), load(a.lengths), load(a.fillers))
    Path(a.out).write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in out) + "\n",
                           encoding="utf-8")
    w = [words(r["query"]) for r in out]
    print(f"{len(out)} rows -> {a.out}: words mean {statistics.mean(w):.1f} "
          f"median {statistics.median(w):.0f} (before: median "
          f"{statistics.median([words(r['query']) for r in rows]):.0f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
