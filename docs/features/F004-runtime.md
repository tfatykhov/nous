# F004: Agent Runtime (Docker)

**Status:** Shipped
**Priority:** P0 — Deployment shell
**Origin:** Requirement for containerized, cloud-ready deployment

## Summary

The Nous runtime is a Docker container that packages Brain, Heart, Cognitive Layer, and Claude Agent SDK into a runnable agent. One `docker compose up` gives you a thinking agent with persistent memory.

## Architecture

```
┌─────────────────────────────────────────────────┐
│                Nous Container                  │
│                                                  │
│  ┌────────────────────────────────────────────┐  │
│  │          Claude Agent SDK                   │  │
│  │  ┌──────────────────────────────────────┐  │  │
│  │  │       Cognitive Layer (Hooks)         │  │  │
│  │  │  Frame → Recall → Deliberate →       │  │  │
│  │  │  → Act → Monitor → Learn             │  │  │
│  │  └──────────┬──────────────┬────────────┘  │  │
│  │             │              │                │  │
│  │     ┌───────▼──────┐ ┌────▼─────────┐     │  │
│  │     │    Brain      │ │    Heart      │     │  │
│  │     │  (decisions)  │ │  (memory)     │     │  │
│  │     └───────┬──────┘ └────┬─────────┘     │  │
│  │             │              │                │  │
│  │     ┌───────▼──────────────▼────────────┐  │  │
│  │     │     Shared DB Connection Pool      │  │  │
│  │     └───────────────┬───────────────────┘  │  │
│  └─────────────────────┼──────────────────────┘  │
│                        │                          │
│  ┌─────────────────────┼──────────────────────┐  │
│  │  MCP Interface      │    REST API           │  │
│  │  (external access)  │   (chat, status)      │  │
│  └─────────────────────┼──────────────────────┘  │
└────────────────────────┼──────────────────────────┘
                         │
                    ┌────▼─────┐
                    │ Postgres  │
                    │ pgvector  │
                    └──────────┘
```

## Docker Compose

```yaml
version: "3.8"

services:
  nous:
    build: .
    ports:
      - "8000:8000"           # REST API
      - "8001:8001"           # MCP endpoint (external)
    environment:
      - NOUS_DB_URL=postgresql://nous:${DB_PASSWORD}@postgres:5432/nous
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
      - NOUS_AGENT_ID=default
      - NOUS_LOG_LEVEL=info
    depends_on:
      postgres:
        condition: service_healthy
    volumes:
      - ./config:/app/config   # Identity, frames, initial censors

  postgres:
    image: pgvector/pgvector:pg17
    environment:
      POSTGRES_DB: nous
      POSTGRES_USER: nous
      POSTGRES_PASSWORD: ${DB_PASSWORD}
    volumes:
      - pgdata:/var/lib/postgresql/data
      - ./init.sql:/docker-entrypoint-initdb.d/init.sql
    ports:
      - "5432:5432"
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U nous"]
      interval: 5s
      timeout: 5s
      retries: 5

  dashboard:
    build: ./dashboard
    ports:
      - "8080:8080"
    environment:
      - NOUS_DB_URL=postgresql://nous:${DB_PASSWORD}@postgres:5432/nous
    depends_on:
      postgres:
        condition: service_healthy

volumes:
  pgdata:
```

## Quick Start

```bash
# Clone
git clone https://github.com/tfatykhov/nous.git
cd nous

# Configure
cp .env.example .env
# Edit .env: set ANTHROPIC_API_KEY and DB_PASSWORD

# Run
docker compose up -d

# Talk to your agent
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -H "X-Nous-Owner-Secret: $NOUS_OWNER_ROUTE_SECRET" \
  -d '{"message": "Hello Nous"}'
```

## REST API

```
POST /chat              — Send message, get response
GET  /status            — Agent status, memory stats, calibration
GET  /memory/search     — Search across all memory types
GET  /decisions         — List decisions (Brain)
GET  /episodes          — List episodes (Heart)
GET  /calibration       — Calibration report
GET  /censors           — Active censors
GET  /frames            — Available frames
WS   /ws               — WebSocket for real-time chat
```

## MCP Interface (External)

For other agents or tools to interact with Nous:

```
Tools exposed:
- nous.chat          — Send a message to the agent
- nous.recall        — Search Nous's memory
- nous.status        — Get agent status
- nous.teach         — Add a fact or procedure
```

This is how Nous could participate in multi-agent systems — other agents talk to it via MCP.

## Configuration

```yaml
# config/agent.yaml
agent:
  id: "nous-1"
  name: "Nous"
  
identity:
  description: "A thinking agent that learns from experience"
  traits: ["analytical", "cautious", "curious"]

frames:
  - task
  - question
  - decision
  - creative
  - conversation
  - debug

brain:
  guardrails:
    - name: "no-high-stakes-low-confidence"
      condition: { stakes: "high", confidence_lt: 0.5 }
      severity: "block"
    - name: "require-reasons"
      condition: { reason_count_lt: 1 }
      severity: "block"

heart:
  working_memory_capacity: 20    # Max items in working memory
  auto_extract_facts: true       # Extract facts from episodes
  auto_create_censors: true      # Create censors from failures
  censor_escalation: true        # Auto-escalate repeated triggers

embedding:
  model: "text-embedding-3-small"
  dimensions: 1536
```

## Dockerfile

```dockerfile
FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY pyproject.toml .
RUN pip install -e .

# Copy source
COPY nous/ nous/
COPY config/ config/

# Health check
HEALTHCHECK --interval=30s --timeout=10s \
  CMD curl -f http://localhost:8000/status || exit 1

EXPOSE 8000 8001

CMD ["python", "-m", "nous.main"]
```

## Project Structure

```
nous/
├── docker-compose.yml
├── Dockerfile
├── .env.example
├── init.sql                    # Postgres schema init
├── pyproject.toml
├── config/
│   └── agent.yaml              # Agent configuration
├── nous/
│   ├── __init__.py
│   ├── main.py                 # Entry point
│   ├── brain/
│   │   ├── __init__.py
│   │   ├── brain.py            # Brain class
│   │   ├── guardrails.py       # Guardrail engine
│   │   ├── calibration.py      # Calibration math
│   │   ├── graph.py            # Decision graph
│   │   └── search.py           # Hybrid search
│   ├── heart/
│   │   ├── __init__.py
│   │   ├── heart.py            # Heart class
│   │   ├── episodic.py         # Episode management
│   │   ├── semantic.py         # Fact management
│   │   ├── procedural.py       # K-line / procedure management
│   │   ├── working.py          # Working memory
│   │   ├── censors.py          # Censor registry
│   │   └── extraction.py       # Auto-extraction pipeline
│   ├── cognitive/
│   │   ├── __init__.py
│   │   ├── layer.py            # Main cognitive layer
│   │   ├── frames.py           # Frame engine
│   │   ├── recall.py           # Recall engine
│   │   ├── deliberation.py     # Deliberation engine
│   │   ├── monitor.py          # Self-monitor
│   │   └── growth.py           # Growth engine
│   ├── api/
│   │   ├── __init__.py
│   │   ├── rest.py             # REST endpoints
│   │   ├── mcp.py              # MCP interface
│   │   └── ws.py               # WebSocket
│   └── storage/
│       ├── __init__.py
│       ├── base.py             # Abstract store
│       ├── postgres.py         # Postgres backend
│       ├── sqlite.py           # SQLite backend (dev)
│       ├── factory.py          # Backend factory
│       └── embeddings.py       # Embedding provider
├── dashboard/
│   ├── Dockerfile
│   └── ...                     # Growth dashboard UI
├── tests/
│   └── ...
└── docs/
    ├── research/               # Research notes
    └── features/               # Feature specs
```

---

*One container. One database. One mind.*
