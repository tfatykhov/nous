# End-to-end context packing eval — 8 scenarios

- judge: `claude-sonnet-4-6`
- top_k: 10
- sufficiency: **0/8 (0%)**

## Per-scenario

| name | sufficient | n_results | reason |
|---|---|---:|---|
| telegram_email | FAIL | 26 | The assembled context covers how the Telegram bot works (long-polling, session management, REST API communication) but contains no information about bot token + chat ID environment variables or subtask completion notifications, which are required parts of the gold answer. |
| heartbeat_overview | FAIL | 27 | The assembled context contains no information about F034 proactive monitoring, health/email/self-initiated checks, or a tick-interval heartbeat system. The heartbeat-related facts present only cover tag matching case-sensitivity and environment variable limitations, not the heartbeat system's core architecture or monitoring purpose. |
| skill_management | FAIL | 26 | The assembled context mentions `learn_skill` tool and SKILL.md format with EvoSkill auto-discovery, but lacks details on SkillParser, bootstrap process, and auto-activation via RECALL — all required components of the gold answer F011 skill discovery flow. |
| subtask_workers | FAIL | 21 | The assembled context contains no information about the default number of subtask workers or the NOUS_SUBTASK_WORKERS environment variable. The context mentions subtask-related facts (session leaks, session naming format, model used) but nothing about worker count configuration. |
| rubric_evolution | FAIL | 31 | The assembled context mentions that Phase 3b (Self-Modifying Evaluation Rubric) is the next phase and describes rubric evolution mechanics (weights, constraints, gating), but it does not contain the specific pipeline described in the gold answer: outcome signals → dimension proposals → rubric weight evolution. The causal/procedural flow of how the rubric evolver works (starting from outcome signals feeding into dimension proposals) is absent. |
| procedure_learning | FAIL | 26 | The assembled context contains no information about F012 K-line procedure learning or auto-creation of procedures from decision clusters during sleep. None of the retrieved memory items address this topic. |
| graph_densification | FAIL | 24 | The assembled context contains no information about graph densification, orphan backfill, reverse linking, or per-relation thresholds during sleep cycle. The retrieved memory items are about graph overlay, graph recall defaults, FactGraphLinker, and SYNAPSE — none of which address the specific concept of graph densification as defined by the gold answer. |
| cognitive_loop | FAIL | 27 | The assembled context does not contain the 7-step cognitive loop (Sense, Frame, Recall, Deliberate, Act, Monitor, Learn). The closest reference is the SCL paper's 5-phase R-CCAM model, which is a different framework entirely. |