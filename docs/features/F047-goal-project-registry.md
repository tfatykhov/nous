# F047: Goal / Project Registry (G9)

**Status:** Draft
**Proposed by:** Tim
**Date:** 2026-04-16
**Addresses gap:** G9 (Goal/Project Registry) — from Max Talanov neuromodulator synthesis
**Related:** G4 (memory lifecycle — goal-weighted scoring), F027 (Supersession), F024 (Critic), CraniMem goal-conditioned gating

---

## Goal

Give Nous a persistent, first-class representation of **what it is currently working on** — the active projects, their goals, their status, and their session-to-session continuity — so that working memory does not have to rediscover context from raw episodes every turn.

## Problem Statement

Nous has no explicit "active projects" concept. Context about ongoing work is reconstructed at each turn from episodes, facts, and decisions via similarity search. This fails in two concrete ways:

1. **Cross-session amnesia.** A workstream discussed minutes ago can silently drop out of the next turn's retrieved context if the new message's embedding doesn't align with it. This was observed directly (decision `ba3878cf`: "Working memory failed to carry forward active multimodal workstream from minutes-ago conversation. Root cause: no persistent 'active projects/focus' tracking").
2. **No goal conditioning for retrieval/gating.** Without a registered goal, retrieval (G4 goal-weighted scoring) and input gating (CraniMem-style `Sim(u_t, g_t)`) have nothing to condition on. Relevance becomes purely topical, not intentional.

The gap is **felt, not theoretical** — this was the founding observation for G9.

## Why This Matters

- **Continuity** — "What are we working on?" should be answerable without recall gymnastics.
- **Goal-conditioned cognition** — Unlocks G4+ (goal-weighted retrieval), CraniMem-style gating, and neuromodulator-style attention modulation (dopamine → boost retrieval on active-goal matches).
- **Prioritization** — The scheduler, heartbeat checks, and Critic can all reference the registry to decide what's worth doing autonomously.
- **Self-model** — A registry of goals is a prerequisite for any credible claim that Nous has persistent intent.

---

## Architecture

### Data Model

Two new tables:

**`projects`**
| column | type | notes |
|---|---|---|
| `id` | uuid PK | |
| `name` | text | short slug, e.g. `F041-snn-sleep` |
| `title` | text | human title |
| `description` | text | 1–3 sentences of goal/intent |
| `status` | enum | `active`, `paused`, `completed`, `abandoned` |
| `priority` | real | 0.0–1.0 (modulates retrieval/gating weight) |
| `created_at` | ts | |
| `updated_at` | ts | auto-touched on any session/milestone |
| `last_touched_at` | ts | last time this project was mentioned/worked on |
| `source_decision_id` | uuid | optional back-link |
| `tags` | text[] | keywords for filtering |

**`project_events`** (append-only log per project)
| column | type | notes |
|---|---|---|
| `id` | uuid PK | |
| `project_id` | uuid FK | |
| `event_type` | enum | `created`, `session`, `milestone`, `blocker`, `status_change`, `note` |
| `summary` | text | 1–2 lines |
| `episode_id` | uuid | link to originating episode if any |
| `created_at` | ts | |

One embedding per `projects.description` (for goal-conditioned similarity).

### Lifecycle

```
                  ┌──────────────────┐
 user mentions ──▶│ project_resolver │──▶ existing? update last_touched_at
 new work         └──────────────────┘         │
                         │                      └──▶ new? propose→confirm→insert
                         ▼
                  ┌──────────────────┐
 end-of-session ─▶│ session_closer   │──▶ append project_event(session, summary)
 / sleep phase    └──────────────────┘
                         │
                         ▼
                  ┌──────────────────┐
 new conversation▶│ context_injector │──▶ inject active projects into working memory
                  └──────────────────┘
```

### Components

1. **`ProjectResolver`** — For each user message, detects whether it references an existing project (cosine match on description embedding, tag overlap, or explicit `F\d{3}` mention). If match above threshold → touch `last_touched_at`. If no match and message looks like new substantive work → propose project creation (auto-create if confidence > 0.85, else surface suggestion).

2. **`SessionCloser`** — Runs at end of conversation (on sleep tick or idle >N min). For each project touched this session, writes a `project_event(session)` with an LLM-generated 1–2 line summary.

3. **`ContextInjector`** — On new conversation start, injects top-K active projects (by `last_touched_at` + `priority`) into the working-memory section, ahead of generic episode recall. Shape:
   ```
   ## Active Projects
   • F041-snn-sleep (active, priority 0.7, last touched 2h ago) —
     Sleep-phase graph densification from tinyHippo .h5. Phase 1 shipped (PR #310).
   • F046-voice-output (active, priority 0.5, last touched 4h ago) —
     TTS via Chromecast. Spec drafted, awaiting implementation.
   ```

4. **`GoalSignal`** — A helper that exposes, for any retrieval/gating call, the current active-project embeddings and priorities. Consumers (G4 retrieval, CraniMem gate if/when added) multiply their scores by `1 + α·max(CosSim(query, project))`.

### Tool Surface

New tools, all thin:
- `project_register(name, title, description, priority=0.5, tags=[])`
- `project_update(name_or_id, status=?, priority=?, description=?)`
- `project_note(name_or_id, summary, event_type='note')`
- `project_list(status='active', limit=10)`

These mirror the `learn_fact` / `record_decision` ergonomic style.

### Context Injection

Add a new bracket to the working-memory render between **Current Frame** and **Working Memory**:

```
## Active Projects
• <name> — <1-line status>
  Last event: <summary> (<relative time>)
```

Capped at ~400 tokens; projects beyond the cap are summarized as a count ("+4 more paused").

---

## Integration Points

- **G4 (memory lifecycle / forgetting)** — active projects become anchors. Episodes/facts linked to an active project get a retention boost; episodes linked to `completed` or `abandoned` projects decay faster.
- **F027 (Supersession)** — a project in `completed` state flags its own `milestone` events as candidates for supersession of earlier `blocker` / `note` events.
- **F024 (Critic)** — Critic can read `project.description` as the explicit goal for subtask success/failure judgments (today it infers goal from task text).
- **Scheduler / Heartbeat** — checks can be bound to a project; if project goes `completed` or `abandoned`, checks cascade-disable.
- **F035 Observability** — project-level dashboards become trivially queryable.

---

## Non-Goals (v1)

- No Gantt-style task decomposition. Projects are flat; sub-structure lives in specs/episodes.
- No external sync (GitHub projects, Jira). Nous-internal only.
- No multi-user / collaborator attribution.
- No automatic priority learning — priorities are user-set or heuristic (recency × touch count).

---

## Phased Plan

**Phase 1 — Schema + Tools (~300 LOC)**
- Tables, migrations, embedding column.
- Four tool handlers.
- Unit tests.

**Phase 2 — Auto-resolution + Context Injection (~350 LOC)**
- `ProjectResolver` wired into pre-turn pipeline.
- Working-memory rendering includes Active Projects block.
- Heuristic auto-create on high-confidence new-work detection.

**Phase 3 — Session Closer + Goal Signal (~250 LOC)**
- End-of-session summarization via existing sleep hook.
- `GoalSignal` helper exposed; G4 retrieval boost wired in behind a setting.

**Phase 4 — Tuning + Decay Coupling (deferred)**
- Priority auto-decay, goal-weighted forgetting, Critic integration.

Total Phase 1–3: ~900 LOC. Each phase independently shippable.

---

## Risks & Open Questions

- **Registry churn.** Every offhand mention could spawn a project. Mitigation: high auto-create threshold (0.85), dedup on embedding similarity before insert.
- **Stale projects dominate context.** Mitigation: `last_touched_at` decay in the injection ranking; cap at K=5.
- **Overlap with existing spec files.** An `F###` spec is already a kind of project record. Open question: should `projects` auto-link to `docs/features/F###-*.md` when the name matches? (Proposed: yes, via a `spec_path` column in Phase 2.)
- **Who writes session summaries?** Phase 3 uses the sleep-cycle LLM call; cost is negligible but adds one more responsibility to sleep. Alternative: inline at end-of-turn when idle detected.
- **Naming collision.** "Project" overloaded with GitHub/Jira. Considered "workstream" and "focus" — `project` is clearest for users, we'll keep it.

---

## Success Criteria

1. After a 30-minute pause, asking "what are we working on?" returns the correct active projects without a `recall_deep` call — answered from the injected Active Projects block.
2. The specific failure mode behind decision `ba3878cf` (multimodal workstream dropped between turns) does not reproduce in a replay test.
3. With G4 goal-weighted retrieval enabled, retrieval precision on project-relevant queries improves ≥10% vs. baseline on a held-out eval set.
4. Projects correctly transition through `active → completed` on explicit user confirmation or detected completion signal (e.g., PR merged matching `project.name`).
