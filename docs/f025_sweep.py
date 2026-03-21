#!/usr/bin/env python3
"""F025 Retrieval Self-Optimization — Full Weight Sweep
Runs 50 queries x 7 weight ratios through the Nous API.
Stores raw results with all scores to F025-sweep-raw-results.json
"""

import json
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

API_BASE = "http://localhost:8000"
WEIGHTS = [0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]
LIMIT = 10

QUERIES = {
    "facts": [
        "Tim's email address",
        "Tim's timezone",
        "Tim's location Silver Spring",
        "Emerson contact A2A",
        "Nous memory dashboard F021",
        "hybrid search weights configuration",
        "Telegram formatting rules markdown",
        "temperature preference Celsius",
        "admin API endpoints list",
        "article LLM agent memory research papers",
    ],
    "decisions": [
        "search weight optimization decision",
        "never commit directly to main branch",
        "Exa fallback search provider Brave",
        "score normalization RRF approach",
        "dynamic weight override architecture",
        "skills auto-activation topic",
        "F025 test plan creation",
        "notification policy for Tim",
        "memory dashboard implementation decision",
        "PR review process workflow",
    ],
    "episodes": [
        "discussed Karpathy autoresearch",
        "self-evaluation tool selection weakness",
        "F025 retrieval optimization planning",
        "article writing memory research",
        "Emerson first A2A contact",
        "admin endpoint testing session",
        "skill registration conversation",
        "calibration review discussion",
        "creative writing diversity LLM",
        "context pruning research",
    ],
    "procedures": [
        "web search skill",
        "email sending procedure",
        "git workflow branch PR",
        "file reading writing skill",
        "task scheduling recurring",
        "memory recall search",
        "decision recording process",
        "fact learning storage",
        "censor guardrail creation",
        "subtask spawning delegation",
    ],
    "mixed": [
        "how does Nous handle memory",
        "what is the Society of Mind architecture",
        "recent conversations with Tim",
        "security and credential handling",
        "Nous personality and identity",
        "search relevance optimization",
        "A2A protocol integration",
        "scheduled tasks and automation",
        "Tim preferences and rules",
        "cognitive frames and deliberation",
    ],
}

def api_get(path):
    """GET request to API, return parsed JSON."""
    req = urllib.request.Request(f"{API_BASE}{path}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())

def api_post(path, data):
    """POST request to API, return parsed JSON."""
    body = json.dumps(data).encode()
    req = urllib.request.Request(f"{API_BASE}{path}", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())

def set_weight(vector_weight):
    """Set vector weight via admin API."""
    result = api_post("/admin/search-weights", {"vector_weight": vector_weight})
    print(f"  Set weight to {vector_weight} -> {result}")
    return result

def search_facts(query, limit=LIMIT):
    """Search facts via API."""
    encoded_q = urllib.parse.quote(query)
    return api_get(f"/facts?q={encoded_q}&limit={limit}")

import urllib.parse

def run_sweep():
    """Run the full sweep and return results dict."""
    # Record original weight
    original = api_get("/admin/search-weights")
    print(f"Original weights: {original}")

    results = {
        "metadata": {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "description": "F025 Retrieval Self-Optimization Weight Sweep — REAL DATA",
            "method": "50 queries x 7 weight ratios via /facts?q= and /admin/search-weights API",
            "api_base": API_BASE,
            "weight_ratios_tested": WEIGHTS,
            "categories": list(QUERIES.keys()),
            "queries_per_category": 10,
            "total_queries": 50,
            "total_api_calls": 50 * len(WEIGHTS),
            "original_weight": original,
        },
        "queries": QUERIES,
        "sweeps": {},
        "analysis": {},
    }

    for vw in WEIGHTS:
        key = f"v{vw}"
        print(f"\n=== Weight ratio: vector={vw}, keyword={round(1-vw, 2)} ===")
        set_weight(vw)
        time.sleep(0.5)  # Let it settle

        sweep_data = {
            "vector_weight": vw,
            "keyword_weight": round(1 - vw, 4),
            "query_results": {},
        }

        for category, queries in QUERIES.items():
            for query in queries:
                try:
                    resp = search_facts(query)
                    facts = resp.get("facts", [])
                    sweep_data["query_results"][query] = {
                        "query_category": category,
                        "result_count": len(facts),
                        "results": [
                            {
                                "id": f["id"],
                                "content": f["content"][:200],
                                "score": f.get("score"),
                                "category": f.get("category"),
                                "subject": f.get("subject"),
                                "confidence": f.get("confidence"),
                            }
                            for f in facts
                        ],
                    }
                    print(f"  [{category}] '{query}' -> {len(facts)} results, top score: {facts[0]['score'] if facts else 'N/A'}")
                except Exception as e:
                    sweep_data["query_results"][query] = {
                        "query_category": category,
                        "error": str(e),
                        "result_count": 0,
                        "results": [],
                    }
                    print(f"  [{category}] '{query}' -> ERROR: {e}")
                time.sleep(0.1)  # Rate limit

        results["sweeps"][key] = sweep_data

    # Restore original weight
    print(f"\nRestoring original weight: {original['vector_weight']}")
    set_weight(original["vector_weight"])

    # Analysis: compare result sets across weights
    print("\n=== Computing analysis ===")
    all_queries = [q for qs in QUERIES.values() for q in qs]

    # For each query, compare top-1 and top-5 result IDs across weights
    query_analysis = {}
    for query in all_queries:
        top1_by_weight = {}
        top5_by_weight = {}
        scores_by_weight = {}
        for vw in WEIGHTS:
            key = f"v{vw}"
            qr = results["sweeps"][key]["query_results"].get(query, {})
            rr = qr.get("results", [])
            top1_by_weight[key] = rr[0]["id"] if rr else None
            top5_by_weight[key] = [r["id"] for r in rr[:5]]
            scores_by_weight[key] = [r["score"] for r in rr[:5]]

        # Check if top-1 is same across all weights
        top1_values = list(top1_by_weight.values())
        top1_stable = len(set(v for v in top1_values if v)) <= 1

        # Check top-5 overlap (Jaccard similarity between extreme weights)
        set_low = set(top5_by_weight.get("v0.3", []))
        set_high = set(top5_by_weight.get("v0.9", []))
        if set_low or set_high:
            jaccard = len(set_low & set_high) / len(set_low | set_high) if (set_low | set_high) else 0
        else:
            jaccard = 1.0

        query_analysis[query] = {
            "top1_stable": top1_stable,
            "top1_by_weight": top1_by_weight,
            "top5_jaccard_0.3_vs_0.9": round(jaccard, 4),
            "scores_by_weight": scores_by_weight,
        }

    # Summary stats
    stable_count = sum(1 for qa in query_analysis.values() if qa["top1_stable"])
    avg_jaccard = sum(qa["top5_jaccard_0.3_vs_0.9"] for qa in query_analysis.values()) / len(query_analysis) if query_analysis else 0
    unstable_queries = [q for q, qa in query_analysis.items() if not qa["top1_stable"]]

    results["analysis"] = {
        "summary": {
            "total_queries": len(all_queries),
            "top1_stable_count": stable_count,
            "top1_unstable_count": len(all_queries) - stable_count,
            "top1_stability_pct": round(stable_count / len(all_queries) * 100, 1),
            "avg_top5_jaccard_0.3_vs_0.9": round(avg_jaccard, 4),
            "unstable_queries": unstable_queries,
        },
        "per_query": query_analysis,
    }

    print(f"\n=== SUMMARY ===")
    print(f"Top-1 stable: {stable_count}/{len(all_queries)} ({results['analysis']['summary']['top1_stability_pct']}%)")
    print(f"Avg Top-5 Jaccard (0.3 vs 0.9): {avg_jaccard:.4f}")
    print(f"Unstable queries: {unstable_queries}")

    return results


if __name__ == "__main__":
    data = run_sweep()
    outpath = "docs/F025-sweep-raw-results.json"
    with open(outpath, "w") as f:
        json.dump(data, f, indent=2, default=str)
    print(f"\nResults written to {outpath}")
    print(f"File size: {len(json.dumps(data, indent=2)):,} bytes")
