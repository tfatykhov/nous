# Agent Tools

Documented tools the agent can call, and the cognitive frames each is offered in; the `dispatcher.register()` calls are the full list. Part of the [Nous development guide](../../CLAUDE.md).

| Tool | Frame Access | Description |
|------|-------------|-------------|
| `record_decision` | decision, task, debug, conversation, question | Record a decision with confidence + reasoning |
| `recall_deep` | all | Search memory (decisions, facts, episodes) |
| `recall_recent` | all | Retrieve recent memory items |
| `learn_fact` | conversation, question, creative, task | Store a new fact |
| `learn_skill` | conversation, question, task | Register a skill from URL, local path, or inline markdown |
| `ingest_document` | conversation, question, task, debug | F069: chunk & persist a full document body (arxiv, PDF/.docx text, long markdown) to heart.episode_chunks with source_kind='document'. Use after extracting text yourself via run_python / web_fetch. |
| `get_procedure` | all | Retrieve a specific procedure by ID |
| `create_censor` | all | Create a guardrail censor |
| `cache_retrieve` | all | Retrieve original content from SmartCompressed results |
| `bash` | task, debug, conversation, question | Execute shell commands |
| `read_file` | task, debug, question | Read file contents |
| `write_file` | task, creative | Write/create files |
| `spawn_task` | conversation, debug | Spawn a background subtask. F099: while `NOUS_INTENTIONS_ENABLED` is on, takes `intent` (one line: why, and what will be done with the result; refused if missing in a chat or MCP turn, generated in a background one) and `wake_policy` |
| `spawn_sync` | conversation, debug | Spawn a subtask and wait for its typed result. F099: while `NOUS_INTENTIONS_ENABLED` is on, takes `intent` (one line: why, and what will be done with the result; refused if missing in a chat or MCP turn, generated in a background one) and `wake_policy` |
| `schedule_task` | conversation, debug | Schedule a recurring/one-shot task. F099: while `NOUS_INTENTIONS_ENABLED` is on, takes `intent` (one line: why, and what will be done with the result; refused if missing in a chat or MCP turn, generated in a background one) and `wake_policy` |
| `dag_create` | conversation, debug | Create a dependency-tracked DAG of subtasks and checks. F099: while `NOUS_INTENTIONS_ENABLED` is on, takes `intent` (one line: why, and what will be done with the result; refused if missing in a chat or MCP turn, generated in a background one) and `wake_policy` |
| `list_tasks` | conversation, question, decision, debug | List subtasks and schedules |
| `cancel_task` | conversation, question, decision, debug | Cancel a subtask or schedule |
| `web_search` | all | Search via multi-tier routing (Tavily/Exa/Brave) |
| `web_fetch` | all | Fetch and extract web content |
| `run_python` | conversation, question, debug, task | Execute Python with memory functions in scope. In-script `recall_deep()` runs the **same** `run_recall_pipeline` as the tool (since 2026-08-25 — it was `heart.search_facts`, facts-only, under a name promising the full retrieval) and returns dicts keyed `id`/`type`/`description`/`score`/`source`, with `content` kept as an alias of `description` for scripts written against the old `FactSummary` shape. Traced as F091 path `script`. Costs ~5s per call (prod p50), so budget a handful per script against the 90s deadline. |
| `send_file` | task, conversation, debug | Send files to Telegram (images as photos, rest as documents) |
| `heartbeat_check_create` | conversation, debug | Create a new dynamic heartbeat check (supports on_complete callback) |
| `heartbeat_check_manage` | conversation, debug | List, enable, disable, delete, or update dynamic checks |
| `push_surface` | conversation, debug (all frames under stable tool set) | F092: push a structured interactive surface to the companion app (templates: approval_gate, action_review, heartbeat_findings; `dedup_key` updates in place) |
