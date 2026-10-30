# Nous Quickstart Guide

Deploy Nous from scratch. This guide covers Docker deployment (recommended), local development, all environment variables, and verification.

---

## Prerequisites

- **Docker** and **Docker Compose** (v2+)
- **Anthropic API key** (or Max subscription OAT token)
- **OpenAI API key** (optional, enables semantic search via embeddings)

---

## 1. Docker Deployment (Recommended)

### 1.1 Clone and Configure

```bash
git clone <repo-url> nous
cd nous
```

Create a `.env` file in the project root:

```bash
# === REQUIRED ===
ANTHROPIC_API_KEY=sk-ant-...          # Or use ANTHROPIC_AUTH_TOKEN instead

# === RECOMMENDED ===
OPENAI_API_KEY=sk-...                 # Enables semantic search (embeddings)
BRAVE_SEARCH_API_KEY=BSA...           # Enables web_search tool

# === TELEGRAM BOT (optional) ===
TELEGRAM_BOT_TOKEN=123456:ABC-...     # From @BotFather
NOUS_ALLOWED_USERS=12345,67890        # Comma-separated Telegram user IDs

# === DATABASE (defaults work for Docker) ===
# DB_HOST=localhost
# DB_PORT=5432
# DB_USER=nous
# DB_PASSWORD=nous_dev_password
# DB_NAME=nous
```

### 1.2 Start Everything

```bash
docker compose up -d
```

This launches three containers:

| Container | Image | Port | Purpose |
|-----------|-------|------|---------|
| `nous-postgres` | `pgvector/pgvector:pg17` | 5432 | PostgreSQL + pgvector |
| `nous-agent` | Built from `Dockerfile` | 8000 | Nous agent (REST + MCP) |
| `nous-telegram` | Built from `Dockerfile` | — | Telegram bot (chat interface) |

> The `telegram` service is optional. If `TELEGRAM_BOT_TOKEN` is not set, the container starts but the bot exits immediately. To skip it entirely, run `docker compose up -d nous postgres`.

PostgreSQL initializes automatically with:
- `sql/init.sql` — Creates 3 schemas, 23 tables, 79 indexes, pgvector + pg_trgm extensions
- `sql/seed.sql` — Seeds default agent, 6 cognitive frames, 4 guardrails
- `sql/migrations/010_subtasks.sql` — Subtask and schedule tables

### 1.3 Verify

```bash
# Health check
curl http://localhost:8000/health
# Expected: {"status":"healthy"}

# Agent status
curl http://localhost:8000/status

# Check logs
docker logs nous-agent
```

### 1.4 Chat

```bash
# Single message
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "Hello, who are you?"}'

# Streaming (SSE)
curl -X POST http://localhost:8000/chat/stream \
  -H "Content-Type: application/json" \
  -d '{"message": "What can you do?"}'
```

---

## 2. Local Development Setup

### 2.1 Start Postgres Only

```bash
docker compose up -d postgres
```

### 2.2 Install Dependencies

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/):

```bash
uv sync
```

### 2.3 Configure Environment

Create `.env` in the project root (same format as above). For local dev, set:

```bash
DB_HOST=localhost
```

### 2.4 Run Nous

```bash
uv run python -m nous.main
```

### 2.5 Run Tests

```bash
# Requires Postgres running
uv run pytest tests/ -v
```

---

## 3. Telegram Bot (Optional)

The Telegram bot runs as a separate container in docker-compose, connecting to the Nous REST API over the internal Docker network.

**Docker (included in `docker compose up`):**

Set `TELEGRAM_BOT_TOKEN` and optionally `NOUS_ALLOWED_USERS` in your `.env` file. The bot starts automatically alongside the agent.

To run only the agent without the bot:

```bash
docker compose up -d nous postgres
```

**Local development (standalone):**

```bash
export TELEGRAM_BOT_TOKEN=123456:ABC-...    # From @BotFather
export NOUS_API_URL=http://localhost:8000    # Nous REST API URL
export NOUS_ALLOWED_USERS=12345,67890       # Optional: restrict access

uv run python -m nous.telegram_bot
```

---

## 4. Environment Variables Reference

### Legend

| Symbol | Meaning |
|--------|---------|
| **Required** | Nous will not function correctly without this |
| **Recommended** | Significantly enhances capabilities |
| **Optional** | Tuning, additional features, or advanced config |

---

### 4.1 Database Connection

These variables are **unprefixed** to share a single `.env` file with docker-compose.

| Variable | Default | Required | Description |
|----------|---------|----------|-------------|
| `DB_HOST` | `localhost` | **Required** | PostgreSQL hostname. Set to `postgres` inside Docker (handled by docker-compose). |
| `DB_PORT` | `5432` | Optional | PostgreSQL port. |
| `DB_USER` | `nous` | Optional | Database username. Must match the user created in PostgreSQL. |
| `DB_PASSWORD` | `nous_dev_password` | **Required** | Database password. Change this in production. |
| `DB_NAME` | `nous` | Optional | Database name. |

---

### 4.2 API Keys & Authentication

| Variable | Default | Required | Description |
|----------|---------|----------|-------------|
| `ANTHROPIC_API_KEY` | `""` | **Required** | Anthropic API key for LLM inference. Used as `x-api-key` header. |
| `OPENAI_API_KEY` | `""` | **Recommended** | OpenAI API key for embeddings (`text-embedding-3-small`). Without this, semantic search, deduplication, and vector similarity are disabled. Nous falls back to keyword-only search. |
| `BRAVE_SEARCH_API_KEY` | `""` | **Recommended** | Brave Search API key. Enables the `web_search` agent tool. Without this, the tool is unavailable. |
| `GITHUB_TOKEN` | `""` | Optional | GitHub personal access token. Used by knowledge extraction features. |

---

### 4.3 Agent Identity

| Variable | Default | Required | Description |
|----------|---------|----------|-------------|
| `NOUS_AGENT_ID` | `nous-default` | Optional | Unique identifier for this agent instance. All data (decisions, memories, episodes) is scoped to this ID. Change for multi-agent deployments. |
| `NOUS_AGENT_NAME` | `Nous` | Optional | Display name shown in responses and logs. |
| `NOUS_AGENT_DESCRIPTION` | `A thinking agent that learns from experience` | Optional | Agent profile description stored in the database. |
| `NOUS_IDENTITY_PROMPT` | *(built-in, see below)* | Optional | System prompt prefix injected at the start of every conversation. Defines the agent's personality, available tools, and behavioral guidelines. Override to fully customize agent behavior. |

**Default identity prompt:**
> You are Nous, a cognitive AI agent that learns from experience. You record decisions with reasoning (record_decision), extract and store facts (learn_fact), search all memory types (recall_deep), and create guardrails (create_censor). Be concise, honest, and thoughtful. When you make a choice, record it. When you learn something new, store it as a fact.

---

### 4.4 LLM Configuration

| Variable | Default | Required | Description |
|----------|---------|----------|-------------|
| `NOUS_MODEL` | `claude-sonnet-4-5-20250514` | Optional | Claude model for interactive chat. Any Anthropic model ID works. |
| `NOUS_BACKGROUND_MODEL` | `claude-sonnet-4-5-20250514` | Optional | Model for background tasks (episode summarization, fact extraction, sleep reflection). Can use a cheaper model to reduce costs. |
| `NOUS_MAX_TOKENS` | `4096` | Optional | Maximum output tokens per LLM response. |
| `NOUS_MAX_TURNS` | `10` | Optional | Maximum tool-use loop iterations per conversation turn. Prevents runaway tool loops. |
| `NOUS_API_BASE_URL` | `https://api.anthropic.com` | Optional | Anthropic API base URL. Change for proxies or custom endpoints. |
| `NOUS_API_TIMEOUT_CONNECT` | `10` | Optional | HTTP connection timeout in seconds for Anthropic API calls. |
| `NOUS_API_TIMEOUT_READ` | `120` | Optional | HTTP read timeout in seconds for Anthropic API calls. Long to accommodate streaming. |

---

### 4.5 Extended Thinking

| Variable | Default | Required | Description |
|----------|---------|----------|-------------|
| `NOUS_THINKING_MODE` | `off` | Optional | Extended thinking control. `off` = disabled, `adaptive` = model decides, `manual` = fixed budget. |
| `NOUS_THINKING_BUDGET` | `10000` | Optional | Token budget for extended thinking in `manual` mode. Must be >= 1024 and < `NOUS_MAX_TOKENS`. |
| `NOUS_EFFORT` | `high` | Optional | Effort level for extended thinking. One of: `low`, `medium`, `high`, `max`. |

---

### 4.6 Server & Networking

| Variable | Default | Required | Description |
|----------|---------|----------|-------------|
| `NOUS_HOST` | `0.0.0.0` | Optional | Server bind address. `0.0.0.0` listens on all interfaces. |
| `NOUS_PORT` | `8000` | Optional | HTTP port for REST API and MCP server. |
| `NOUS_LOG_LEVEL` | `info` | Optional | Python logging level. One of: `debug`, `info`, `warning`, `error`. |
| `NOUS_WORKSPACE_DIR` | `/tmp/nous-workspace` | Optional | Working directory for the `bash` and `write_file` tools. Created automatically if it doesn't exist. |

---

### 4.7 Embeddings

| Variable | Default | Required | Description |
|----------|---------|----------|-------------|
| `NOUS_EMBEDDING_MODEL` | `text-embedding-3-small` | Optional | OpenAI embedding model name. Must match `NOUS_EMBEDDING_DIMENSIONS`. |
| `NOUS_EMBEDDING_DIMENSIONS` | `1536` | Optional | Embedding vector dimensions. Must match the database `vector(N)` column size and the chosen model's output dimensions. |

---

### 4.8 Database Pool Tuning

| Variable | Default | Required | Description |
|----------|---------|----------|-------------|
| `NOUS_DB_POOL_SIZE` | `10` | Optional | SQLAlchemy async connection pool size. Increase for higher concurrency. |
| `NOUS_DB_MAX_OVERFLOW` | `5` | Optional | Maximum overflow connections beyond pool size. Total capacity = pool_size + max_overflow. |

---

### 4.9 Event Bus & Handlers

The event bus powers background processing. Disabling the bus disables all handlers.

| Variable | Default | Required | Description |
|----------|---------|----------|-------------|
| `NOUS_EVENT_BUS_ENABLED` | `true` | Optional | Master switch for the async event bus. When disabled, no background handlers run. Events still persist to the database via `Brain.emit_event`. |
| `NOUS_EPISODE_SUMMARY_ENABLED` | `true` | Optional | Auto-generate structured summaries for completed episodes using the background LLM model. |
| `NOUS_FACT_EXTRACTION_ENABLED` | `true` | Optional | Auto-extract facts from conversation turns using the background LLM model. |
| `NOUS_SLEEP_ENABLED` | `true` | Optional | Enable sleep/reflection mode. When idle, Nous reviews decisions, prunes stale censors, compresses old episodes, reflects on patterns, and generalizes facts. |
| `NOUS_DECISION_REVIEW_ENABLED` | `true` | Optional | Periodic sweep of unreviewed decisions. Uses the background LLM model to assess outcomes. |
| `NOUS_DECISION_SWEEP_INTERVAL` | `3600` | Optional | Seconds between decision review sweeps. Default: 1 hour. |
| `NOUS_TEMPORAL_CONTEXT_ENABLED` | `true` | Optional | Include recent conversation context in frame selection for temporal awareness. |

---

### 4.10 Session & Sleep Management

| Variable | Default | Required | Description |
|----------|---------|----------|-------------|
| `NOUS_SESSION_TIMEOUT` | `1800` | Optional | Seconds of inactivity before a session is auto-cleaned. Default: 30 minutes. |
| `NOUS_SLEEP_TIMEOUT` | `7200` | Optional | Seconds of inactivity before entering sleep mode (reflection). Default: 2 hours. |
| `NOUS_SLEEP_CHECK_INTERVAL` | `60` | Optional | Seconds between sleep eligibility checks. |

---

### 4.11 MCP Server

| Variable | Default | Required | Description |
|----------|---------|----------|-------------|
| `NOUS_MCP_ENABLED` | `true` | Optional | Enable the MCP (Model Context Protocol) server at `/mcp`. Exposes tools: `nous_chat`, `nous_recall`, `nous_status`, `nous_teach`, `nous_decide`. |

---

### 4.12 Web Tools

| Variable | Default | Required | Description |
|----------|---------|----------|-------------|
| `NOUS_WEB_SEARCH_DAILY_LIMIT` | `100` | Optional | Maximum web searches per day. Resets at 00:00 UTC. |
| `NOUS_WEB_FETCH_MAX_CHARS` | `10000` | Optional | Default maximum characters returned by the `web_fetch` tool. |

---

### 4.13 Tool Execution

| Variable | Default | Required | Description |
|----------|---------|----------|-------------|
| `NOUS_TOOL_TIMEOUT` | `120` | Optional | Maximum seconds for any single tool execution (bash, read_file, etc.). |
| `NOUS_KEEPALIVE_INTERVAL` | `10` | Optional | Seconds between keepalive events during long-running tool execution. Must be less than `NOUS_TOOL_TIMEOUT`. |

---

### 4.14 Context Compaction (Layer 1: Tool Pruning)

Controls how old tool results are compressed in conversation history to save tokens.

| Variable | Default | Required | Description |
|----------|---------|----------|-------------|
| `NOUS_TOOL_PRUNING_ENABLED` | `true` | Optional | Enable automatic trimming of old tool results in message history. |
| `NOUS_TOOL_SOFT_TRIM_CHARS` | `4000` | Optional | Maximum characters to keep in a soft-trimmed tool result. |
| `NOUS_TOOL_SOFT_TRIM_HEAD` | `1500` | Optional | Characters preserved from the start of trimmed tool output. |
| `NOUS_TOOL_SOFT_TRIM_TAIL` | `1500` | Optional | Characters preserved from the end of trimmed tool output. Head + Tail must be < `NOUS_TOOL_SOFT_TRIM_CHARS`. |
| `NOUS_TOOL_HARD_CLEAR_AFTER` | `6` | Optional | Number of turns after which tool results are completely removed from history. |
| `NOUS_KEEP_LAST_TOOL_RESULTS` | `2` | Optional | Minimum number of recent tool results always preserved (never pruned). |

---

### 4.15 Context Compaction (Layer 2: History Compaction)

Experimental. Compresses full conversation history when token count exceeds threshold.

| Variable | Default | Required | Description |
|----------|---------|----------|-------------|
| `NOUS_COMPACTION_ENABLED` | `false` | Optional | Enable knowledge extraction and history compaction. Disabled by default (experimental). |
| `NOUS_COMPACTION_THRESHOLD` | `100000` | Optional | Token count threshold that triggers history compaction. |
| `NOUS_KEEP_RECENT_TOKENS` | `20000` | Optional | Number of recent tokens preserved during compaction. Older messages are summarized. |

---

### 4.16 Subtasks & Scheduling

| Variable | Default | Required | Description |
|----------|---------|----------|-------------|
| `NOUS_SUBTASK_ENABLED` | `true` | Optional | Enable the background subtask worker pool. Allows Nous to spawn independent tasks. |
| `NOUS_SUBTASK_WORKERS` | `2` | Optional | Number of async worker tasks polling the subtask queue. |
| `NOUS_SUBTASK_POLL_INTERVAL` | `2.0` | Optional | Seconds between subtask queue polls. |
| `NOUS_SUBTASK_DEFAULT_TIMEOUT` | `120` | Optional | Default timeout in seconds for subtasks that don't specify one. |
| `NOUS_SUBTASK_MAX_TIMEOUT` | `600` | Optional | Maximum allowed subtask timeout. Subtasks requesting more are capped here. |
| `NOUS_SUBTASK_MAX_CONCURRENT` | `3` | Optional | Maximum number of subtasks running simultaneously. |
| `NOUS_SCHEDULE_ENABLED` | `true` | Optional | Enable the task scheduler for recurring and one-shot scheduled tasks. |
| `NOUS_SCHEDULE_CHECK_INTERVAL` | `60` | Optional | Seconds between schedule checks for due tasks. |

---

### 4.17 Telegram Notifications (Subtask Results)

| Variable | Default | Required | Description |
|----------|---------|----------|-------------|
| `NOUS_TELEGRAM_BOT_TOKEN` | `None` | Optional | Telegram bot token for sending subtask completion notifications. Not the same as the standalone Telegram bot. |
| `NOUS_TELEGRAM_CHAT_ID` | `None` | Optional | Telegram chat ID to receive subtask notifications. Required if `NOUS_TELEGRAM_BOT_TOKEN` is set. |

---

### 4.18 Telegram Bot

Used by the `telegram` container in docker-compose and by `python -m nous.telegram_bot` when running standalone. These are read directly from `os.environ`, not from the Settings class.

| Variable | Default | Required | Description |
|----------|---------|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | — | **Required** | Telegram bot token from @BotFather. The bot uses long-polling to receive messages. Without this, the bot exits on startup. |
| `NOUS_API_URL` | `http://localhost:8000` | Optional | Base URL of the Nous REST API the bot connects to. In Docker, this is set to `http://nous:8000` automatically by docker-compose. |
| `NOUS_ALLOWED_USERS` | `""` (allow all) | Optional | Comma-separated Telegram user IDs allowed to interact. Empty = unrestricted access. Set this in production to prevent unauthorized use. |

---

### 4.19 Brain Tuning

| Variable | Default | Required | Description |
|----------|---------|----------|-------------|
| `NOUS_AUTO_LINK_THRESHOLD` | `0.85` | Optional | Cosine similarity threshold for auto-linking related decisions in the knowledge graph. |
| `NOUS_AUTO_LINK_MAX` | `3` | Optional | Maximum number of auto-links created per decision. |
| `NOUS_QUALITY_BLOCK_THRESHOLD` | `0.5` | Optional | Minimum quality score for decisions. Deliberation is blocked below this threshold. |

---

## 5. Architecture Overview

![Nous Architecture](nous-architecture.png)

### Schemas

| Schema | Tables | Purpose |
|--------|--------|---------|
| `nous_system` | 5 | Agent registry, cognitive frames, events, identity, migrations |
| `brain` | 8 | Decisions, reasoning, knowledge graph, guardrails, calibration |
| `heart` | 10 | Episodes, facts, procedures, censors, working memory, subtasks, schedules |

---

## 6. REST API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/chat` | Send a message, get a response |
| `POST` | `/chat/stream` | SSE streaming chat |
| `DELETE` | `/chat/{session_id}` | End a conversation session |
| `GET` | `/status` | Agent status, memory stats, calibration |
| `GET` | `/health` | Health check (DB connectivity) |
| `GET` | `/decisions` | List recent decisions |
| `GET` | `/decisions/{id}` | Decision detail |
| `GET` | `/decisions/unreviewed` | Decisions needing review |
| `POST` | `/decisions/{id}/review` | Submit external review |
| `GET` | `/episodes` | List recent episodes |
| `GET` | `/facts?q=query` | Search facts |
| `GET` | `/censors` | Active censors/guardrails |
| `GET` | `/frames` | Available cognitive frames |
| `GET` | `/calibration` | Calibration report |
| `GET` | `/identity` | Agent identity sections |
| `PUT` | `/identity/{section}` | Update identity section |
| `POST` | `/reinitiate` | Reset and re-initiate identity |
| `GET` | `/subtasks` | List subtasks |
| `GET` | `/subtasks/{id}` | Subtask detail |
| `DELETE` | `/subtasks/{id}` | Cancel a subtask |
| `GET` | `/schedules` | List schedules |
| `POST` | `/schedules` | Create a schedule |
| `DELETE` | `/schedules/{id}` | Deactivate a schedule |

---

## 7. Agent Tools

Tools available to the agent during conversations:

| Tool | Description |
|------|-------------|
| `record_decision` | Record a decision with confidence level and reasoning |
| `recall_deep` | Search across all memory types (decisions, facts, episodes, procedures); with `hours`, list recent conversations |
| `learn_fact` | Store a new fact in semantic memory |
| `create_censor` | Create a guardrail censor (warn/block triggers) |
| `bash` | Execute shell commands in the workspace |
| `read_file` | Read file contents from the workspace |
| `write_file` | Write or create files in the workspace |
| `web_search` | Search the web via Brave API (requires `BRAVE_SEARCH_API_KEY`) |
| `web_fetch` | Fetch and extract content from a URL |
| `spawn_task` | Spawn a background subtask (requires `NOUS_SUBTASK_ENABLED`) |
| `schedule_task` | Create a one-shot or recurring scheduled task |
| `list_tasks` | List active subtasks and schedules |
| `cancel_task` | Cancel a running subtask or deactivate a schedule |

---

## 8. Minimal Production Configuration

A production `.env` file with security-conscious defaults:

```bash
# Authentication
ANTHROPIC_API_KEY=sk-ant-...

# Database (change password!)
DB_PASSWORD=<strong-random-password>

# Embeddings (strongly recommended)
OPENAI_API_KEY=sk-...

# Web search (optional)
BRAVE_SEARCH_API_KEY=BSA...

# Telegram bot (optional)
TELEGRAM_BOT_TOKEN=123456:ABC-...
NOUS_ALLOWED_USERS=12345

# Agent identity
NOUS_AGENT_ID=prod-nous
NOUS_AGENT_NAME=Nous

# Logging
NOUS_LOG_LEVEL=warning

# Security: restrict workspace
NOUS_WORKSPACE_DIR=/app/workspace
```

---

## 9. Stopping & Cleanup

```bash
# Stop all containers
docker compose down

# Stop and remove data (fresh start)
docker compose down -v
```

The `-v` flag removes the `pgdata` volume, wiping all stored memories, decisions, and episodes. Omit it to preserve data across restarts.
