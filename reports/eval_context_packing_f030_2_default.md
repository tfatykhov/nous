# End-to-end context packing eval — 5 scenarios

- judge: `claude-sonnet-4-6`
- top_k: 10
- **headline sufficiency (memory bucket): 0/3 (0%)**
- docs aside (known-limitation gold hints): 0/2 (0%)

## Per-scenario

| name | bucket | sufficient | n_results | reason |
|---|---|---|---:|---|
| telegram_email | memory | FAIL | 25 | The assembled context describes how the Telegram bot works architecturally (long-polling, routing, session management, markdown formatting) but contains no information about bot token + chat ID environment variables or subtask completion notifications, which are the required elements of the gold answer. |
| heartbeat_overview | memory | FAIL | 28 | The assembled context contains various heartbeat-related facts (false positives, sweep results, timeout failures) but does not describe the heartbeat system's core design: proactive monitoring of health/email/self-initiated checks running on a tick interval (F034). The gold-answer information about what the heartbeat system fundamentally is and does is absent. |
| skill_management | docs | FAIL | 26 | The assembled context mentions `learn_skill` tool and SKILL.md format with EvoSkill auto-discovery, but lacks details on SkillParser, bootstrap process, and auto-activation via RECALL — key components required by the gold answer. |
| subtask_workers | docs | FAIL | 21 | The assembled context contains no information about the default number of subtask workers or the NOUS_SUBTASK_WORKERS environment variable. It mentions subtask timeout settings and worker bugs, but not the worker count configuration. |
| rubric_evolution | memory | FAIL | 31 | The assembled context mentions rubric evolution and self-modifying evaluation rubrics but does not contain the specific pipeline described in the gold answer: outcome signals → dimension proposals → rubric weight evolution (F024-3b). The context only states rubrics 'evolve dynamically' and mentions gating conditions, but lacks the specific mechanism of how outcome signals feed into dimension proposals which then drive rubric weight evolution. |