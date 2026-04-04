# F035: Observability — Knowing What the Mind Is Doing

**Status:** PROPOSED
**Author:** Nous + Tim
**Created:** 2026-04-04
**Dependencies:** F006 (Event Bus), F034 (Heartbeat), F026 (Execution Ledger)

---

## Problem

Nous now has 4+ autonomous subsystems that modify state without human initiation:

- **Heartbeat** (F034) — monitors external services, creates findings, triggers triage
- **Sleep consolidation** — rewrites memory (compaction, reflection, contradiction resolution)
- **Self-tuning** (F034.3) — adjusts heartbeat check intervals based on yield
- **Fact/episode lifecycle** — admission, dedup, pruning all happen automatically

Each system is individually well-designed. But when something unexpected happens — a fact disappears, a check stops running, behavior shifts — there's no way to answer: **"what chain of autonomous decisions led here?"**

The event bus exists (F006) and persists events to DB, but there are no processing stats, no causal chains linking autonomous actions, and no trend detection for behavioral drift. Debugging requires reading raw logs and manually reconstructing causality.

This is the difference between a system that *works* and a system you can *trust*. As Nous becomes more autonomous, observability isn't optional — it's the mechanism for accountable self-modification.

---

## Design Philosophy

The right mental model isn't "monitoring a server." It's closer to **journaling for a mind** — the system should be able to answer "why did I change my mind about X?" the same way a person with good self-awareness can.

Minsky's Chapter 6 ("Self-Knowledge is Dangerous") warns that unrestricted self-modification leads to instability. The observability layer is the read-only self-knowledge that makes self-modification safe: you can see what happened and why, but the audit trail itself can't be modified by the systems it monitors.

---

## Architecture — Three Layers

```
┌─────────────────────────────────────────────────────────┐
│                    Dashboard / API                        │
│         (query any layer, visualize trends)               │
└────────────┬──────────────────┬──────────────────┬───────┘
             │                  │                  │
    ┌────────▼────────┐ ┌──────▼───────┐ ┌────────▼────────┐
    │   F035.1        │ │   F035.2     │ │   F035.3        │
    │   Event Bus     │ │   Causal     │ │   Behavioral    │
    │   Stats         │ │   Chains     │ │   Drift         │
    │                 │ │              │ │   Detection     │
    │ "Is the system  │ │ "Why did     │ │ "Is the system  │
    │  healthy now?"  │ │  this happen?"│ │  changing?"     │
    └─────────────────┘ └──────────────┘ └─────────────────┘
             ▲                  ▲                  ▲
             │                  │                  │
    ┌────────┴──────────────────┴──────────────────┴───────┐
    │                    Event Bus (F006)                    │
    │              All events flow through here              │
    └──────────────────────────────────────────────────────┘
```

**Layer 1 — Event Bus Stats (F035.1):** Real-time operational health. Event throughput, handler success/fail rates, queue depth. "Is the system healthy right now?"

**Layer 2 — Causal Chain Tracing (F035.2):** Every autonomous action gets a `caused_by` link back to its trigger. Queryable audit trail. "Why did Nous do X?"

**Layer 3 — Behavioral Drift Detection (F035.3):** Periodic snapshots of key metrics with trend analysis. Catches slow drift that individual events don't reveal. "Is the system changing in ways nobody intended?"

---

## Sub-Specs

| Spec | Title | Priority | Depends On |
|------|-------|----------|------------|
| F035.1 | Event Bus Observability | P1 | F006 |
| F035.2 | Autonomous Action Audit Trail | P1 | F035.1 |
| F035.3 | Behavioral Drift Detection | P2 | F035.2 |

**Sequencing rationale:** F035.1 gives us the infrastructure (stats collection, endpoints). F035.2 adds causal metadata to events. F035.3 builds on both to detect trends. Each is independently useful.

---

## What This Supersedes

- **006.1 (Event Bus Observability)** — F035.1 absorbs and modernizes this planned spec. The original 006.1 was scoped before heartbeat, sleep consolidation, and self-tuning existed. F035.1 covers the same ground but accounts for the current architecture.

---

## Success Criteria

1. After any autonomous action, you can trace the full causal chain back to the originating trigger
2. Handler health (success/fail rates) is visible in real-time via API and Telegram
3. Behavioral trends (fact growth rate, censor changes, check frequency drift) are tracked and anomalies flagged
4. The dashboard has an "Autonomous Activity" panel showing recent self-modifications
5. None of this adds measurable latency to the hot path (event processing)
