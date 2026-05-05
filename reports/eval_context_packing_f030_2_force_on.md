# End-to-end context packing eval — 5 scenarios

- judge: `claude-sonnet-4-6`
- top_k: 10
- **headline sufficiency (memory bucket): 2/3 (67%)**
- docs aside (known-limitation gold hints): 0/2 (0%)

## Per-scenario

| name | bucket | sufficient | n_results | reason |
|---|---|---|---:|---|
| telegram_email | memory | FAIL | 24 | parse error |
| heartbeat_overview | memory | OK | 27 | The assembled context contains detailed information about F034 heartbeat system including proactive monitoring, health checks, email checks, self-initiated checks, and the tick interval (30s), fully covering the gold-answer requirements. |
| skill_management | docs | FAIL | 26 | The assembled context mentions `learn_skill` tool and SKILL.md format, but lacks details about SkillParser, bootstrap process, and auto-activation via RECALL — the key components of F011 skill discovery required for a complete answer. |
| subtask_workers | docs | FAIL | 22 | The assembled context contains no mention of NOUS_SUBTASK_WORKERS or the default number of subtask workers (2). While there are many facts about subtasks, none address this specific configuration. |
| rubric_evolution | memory | OK | 31 | The assembled context explicitly describes RubricEvolver as handling weekly evolution cycles, and the facts describe the flow: outcome signals (collected via OutcomeDetector/outcome_signals table), correlation with dimensions (CorrelationEngine), and rubric weight evolution (L1 weight adjustment, ±5% shifts per cycle) — matching the gold answer's F024-3b flow of outcome signals → dimension proposals → rubric weight evolution. |