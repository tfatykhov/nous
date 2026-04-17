-- F047: Goal / Project Registry — Phase 1 (Registry & Resolver)
-- Two new tables in the heart schema for tracking active projects and their event log.

-- 1. projects — persistent registry of active workstreams
CREATE TABLE IF NOT EXISTS heart.projects (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id VARCHAR(100) NOT NULL,
    name VARCHAR(200) NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status VARCHAR(20) NOT NULL DEFAULT 'active',
    priority REAL NOT NULL DEFAULT 0.5,
    tags TEXT[] DEFAULT '{}',
    source_decision_id UUID REFERENCES brain.decisions(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_touched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    embedding vector(1536),
    CONSTRAINT ck_projects_status CHECK (status IN ('active', 'paused', 'completed', 'abandoned')),
    CONSTRAINT ck_projects_priority CHECK (priority >= 0.0 AND priority <= 1.0)
);

-- Unique name per agent
CREATE UNIQUE INDEX IF NOT EXISTS uq_projects_agent_name ON heart.projects (agent_id, name);

-- Index for listing active projects
CREATE INDEX IF NOT EXISTS idx_projects_agent_status ON heart.projects (agent_id, status);

-- HNSW index for embedding search
CREATE INDEX IF NOT EXISTS idx_projects_embedding ON heart.projects
    USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);

-- 2. project_events — append-only event log per project
CREATE TABLE IF NOT EXISTS heart.project_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES heart.projects(id) ON DELETE CASCADE,
    agent_id VARCHAR(100) NOT NULL,
    event_type VARCHAR(30) NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    episode_id UUID REFERENCES heart.episodes(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_project_events_type CHECK (
        event_type IN ('created', 'session', 'milestone', 'blocker', 'status_change', 'note')
    )
);

-- Index for fetching events by project
CREATE INDEX IF NOT EXISTS idx_project_events_project ON heart.project_events (project_id, created_at DESC);
