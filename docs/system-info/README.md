# System Information

Technical documentation for the Nous cognitive agent framework internals.

## Documents

| Document | Description |
|----------|-------------|
| [Architecture](architecture.md) | Complete system architecture — all 12 components, database schemas, API endpoints, and how they connect |
| [Loop Flow](loop-flow.md) | Step-by-step message processing pipeline — from user input through pre-turn, LLM execution, post-turn, to background learning |

## Quick Reference

**Tech Stack:** Python 3.12 · Starlette/ASGI · PostgreSQL + pgvector · Anthropic Claude API · SQLAlchemy (async) · Uvicorn

**Entry Point:** `nous/main.py` → `main()` → `build_app()` → lifespan startup

**Core Loop:** `AgentRunner.run_turn()` → `CognitiveLayer.pre_turn()` → Anthropic API → Tool Loop → `CognitiveLayer.post_turn()`

**Database:** 3 schemas (`nous_system`, `brain`, `heart`), 22 tables, pgvector for embeddings

**API Surface:** 23 REST endpoints at `/api/*`, 5 MCP tools at `/mcp`, Telegram bot

**Tools:** 15 tools available to Claude, frame-gated (not all available in all frames)

**Background:** 8 event handlers process learning asynchronously via EventBus
