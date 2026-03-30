-- =============================================================================
-- Nous Database Schema — init.sql
-- 23 tables across 3 schemas: nous_system (5), brain (8), heart (10)
-- Embedding dimension: vector(1536) for text-embedding-3-small
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1. Extensions
-- ---------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ---------------------------------------------------------------------------
-- 2. Schemas
-- ---------------------------------------------------------------------------
CREATE SCHEMA IF NOT EXISTS brain;
CREATE SCHEMA IF NOT EXISTS heart;
CREATE SCHEMA IF NOT EXISTS nous_system;

-- ---------------------------------------------------------------------------
-- 3. Trigger function: auto-update updated_at on row modification
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION update_timestamp()
RETURNS trigger AS $$
BEGIN
    NEW.updated_at = clock_timestamp();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ---------------------------------------------------------------------------
-- 3b. Immutable wrapper for array_to_string (issue #197)
--     PostgreSQL's array_to_string() is STABLE, not IMMUTABLE, so it cannot
--     be used directly in GENERATED ALWAYS columns.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION immutable_array_to_string(arr text[], sep text)
RETURNS text LANGUAGE sql IMMUTABLE PARALLEL SAFE
AS $$ SELECT array_to_string(arr, sep) $$;

-- ===========================================================================
-- NOUS_SYSTEM SCHEMA (4 tables)
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- nous_system.agents — agent registry
-- ---------------------------------------------------------------------------
CREATE TABLE nous_system.agents (
    id VARCHAR(100) PRIMARY KEY,
    name VARCHAR(200) NOT NULL,
    description TEXT,
    config JSONB NOT NULL DEFAULT '{}',
    active BOOLEAN DEFAULT TRUE,
    is_initiated BOOLEAN DEFAULT FALSE,
    last_active TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- nous_system.agent_identity — versioned identity sections
-- ---------------------------------------------------------------------------
CREATE TABLE nous_system.agent_identity (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id VARCHAR(100) NOT NULL REFERENCES nous_system.agents(id),
    section VARCHAR(50) NOT NULL,
    content TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    is_current BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_by VARCHAR(50),
    previous_version_id UUID REFERENCES nous_system.agent_identity(id)
);

CREATE UNIQUE INDEX idx_identity_agent_section_current
    ON nous_system.agent_identity(agent_id, section) WHERE is_current = TRUE;  -- UNIQUE prevents duplicate current rows

-- ---------------------------------------------------------------------------
-- nous_system.frames — cognitive frame definitions
-- ---------------------------------------------------------------------------
CREATE TABLE nous_system.frames (
    id VARCHAR(100) PRIMARY KEY,
    agent_id VARCHAR(100) REFERENCES nous_system.agents(id),
    name VARCHAR(200) NOT NULL,
    description TEXT,
    activation_patterns TEXT[],
    default_category VARCHAR(50),
    default_stakes VARCHAR(20),
    questions_to_ask TEXT[],
    agencies_to_activate TEXT[],
    suppressed_frames TEXT[],
    frame_censors TEXT[],
    usage_count INT DEFAULT 0,
    last_used TIMESTAMPTZ,
    active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- nous_system.events — audit trail
-- ---------------------------------------------------------------------------
CREATE TABLE nous_system.events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id VARCHAR(100) NOT NULL,
    session_id VARCHAR(100),
    event_type VARCHAR(50) NOT NULL,
    data JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- nous_system.schema_migrations — tracks applied SQL migrations
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS nous_system.schema_migrations (
    version    VARCHAR(20) PRIMARY KEY,
    name       VARCHAR(255) NOT NULL,
    checksum   VARCHAR(64) NOT NULL,
    applied_at TIMESTAMPTZ DEFAULT NOW()
);

-- ===========================================================================
-- BRAIN SCHEMA (8 tables)
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- brain.decisions — core decision records
-- ---------------------------------------------------------------------------
CREATE TABLE brain.decisions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id VARCHAR(100) NOT NULL,
    description TEXT NOT NULL,
    context TEXT,
    pattern TEXT,
    confidence FLOAT NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    category VARCHAR(50) NOT NULL CHECK (category IN ('architecture', 'process', 'tooling', 'security', 'integration')),
    stakes VARCHAR(20) NOT NULL CHECK (stakes IN ('low', 'medium', 'high', 'critical')),
    quality_score FLOAT,
    outcome VARCHAR(20) DEFAULT 'pending' CHECK (outcome IN ('pending', 'success', 'partial', 'failure')),
    outcome_result TEXT,
    reviewed_at TIMESTAMPTZ,
    embedding vector(1536),
    search_tsv tsvector GENERATED ALWAYS AS (
        to_tsvector('english', COALESCE(description, '') || ' ' || COALESCE(context, '') || ' ' || COALESCE(pattern, ''))
    ) STORED,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- brain.decision_tags — normalized tags
-- ---------------------------------------------------------------------------
CREATE TABLE brain.decision_tags (
    decision_id UUID NOT NULL REFERENCES brain.decisions(id) ON DELETE CASCADE,
    tag VARCHAR(100) NOT NULL,
    PRIMARY KEY (decision_id, tag)
);

-- ---------------------------------------------------------------------------
-- brain.decision_reasons — typed reasoning
-- ---------------------------------------------------------------------------
CREATE TABLE brain.decision_reasons (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    decision_id UUID NOT NULL REFERENCES brain.decisions(id) ON DELETE CASCADE,
    type VARCHAR(50) NOT NULL CHECK (type IN ('analysis', 'pattern', 'empirical', 'authority', 'intuition', 'analogy', 'elimination', 'constraint')),
    text TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- brain.decision_bridge — structure + function dual descriptions
-- ---------------------------------------------------------------------------
CREATE TABLE brain.decision_bridge (
    decision_id UUID PRIMARY KEY REFERENCES brain.decisions(id) ON DELETE CASCADE,
    structure TEXT,
    function TEXT
);

-- ---------------------------------------------------------------------------
-- brain.thoughts — micro-thoughts during deliberation
-- ---------------------------------------------------------------------------
CREATE TABLE brain.thoughts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    decision_id UUID NOT NULL REFERENCES brain.decisions(id) ON DELETE CASCADE,
    agent_id VARCHAR(100) NOT NULL,
    text TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- brain.graph_edges — decision relationships
-- ---------------------------------------------------------------------------
CREATE TABLE brain.graph_edges (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id UUID NOT NULL,
    target_id UUID NOT NULL,
    source_type VARCHAR(20) NOT NULL DEFAULT 'decision',
    target_type VARCHAR(20) NOT NULL DEFAULT 'decision',
    agent_id VARCHAR(100) NOT NULL,
    relation VARCHAR(50) NOT NULL CHECK (relation IN (
        'supports', 'contradicts', 'supersedes', 'related_to', 'caused_by',
        'informed_by', 'evidence_for', 'discussed_in', 'extracted_from'
    )),
    weight FLOAT DEFAULT 1.0,
    auto_linked BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(source_id, target_id, relation),
    CHECK (source_type IN ('decision', 'fact', 'episode', 'procedure')),
    CHECK (target_type IN ('decision', 'fact', 'episode', 'procedure'))
);

-- ---------------------------------------------------------------------------
-- brain.guardrails — configurable rules
-- ---------------------------------------------------------------------------
CREATE TABLE brain.guardrails (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id VARCHAR(100) NOT NULL,
    name VARCHAR(200) NOT NULL,
    description TEXT,
    condition JSONB NOT NULL,
    severity VARCHAR(20) NOT NULL DEFAULT 'warn' CHECK (severity IN ('warn', 'block', 'absolute')),
    priority INTEGER DEFAULT 100,
    activation_count INT DEFAULT 0,
    last_activated TIMESTAMPTZ,
    active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(agent_id, name)
);

-- ---------------------------------------------------------------------------
-- brain.calibration_snapshots — periodic calibration metrics
-- ---------------------------------------------------------------------------
CREATE TABLE brain.calibration_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id VARCHAR(100) NOT NULL,
    total_decisions INT,
    reviewed_decisions INT,
    brier_score FLOAT,
    accuracy FLOAT,
    confidence_mean FLOAT,
    confidence_stddev FLOAT,
    category_stats JSONB,
    reason_stats JSONB,
    snapshot_at TIMESTAMPTZ DEFAULT NOW()
);

-- ===========================================================================
-- HEART SCHEMA (8 tables)
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- heart.episodes — narrative records
-- ---------------------------------------------------------------------------
CREATE TABLE heart.episodes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id VARCHAR(100) NOT NULL,
    title VARCHAR(500),
    summary TEXT NOT NULL,
    detail TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at TIMESTAMPTZ,
    duration_seconds INT,
    frame_used VARCHAR(100),
    trigger VARCHAR(100),
    participants TEXT[],
    outcome VARCHAR(50) CHECK (outcome IN ('success', 'partial', 'failure', 'ongoing', 'abandoned')),
    surprise_level FLOAT CHECK (surprise_level BETWEEN 0 AND 1),
    lessons_learned TEXT[],
    embedding vector(1536),
    tags TEXT[],
    search_tsv tsvector GENERATED ALWAYS AS (
        to_tsvector('english', COALESCE(title, '') || ' ' || summary)
    ) STORED,
    active BOOLEAN DEFAULT TRUE,
    encoded_censors JSONB,
    compression_tier VARCHAR(20) DEFAULT 'raw',
    structured_summary JSONB,
    user_id VARCHAR(100),
    user_display_name VARCHAR(100),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- heart.episode_decisions — links episodes to decisions
-- ---------------------------------------------------------------------------
CREATE TABLE heart.episode_decisions (
    episode_id UUID NOT NULL REFERENCES heart.episodes(id) ON DELETE CASCADE,
    decision_id UUID NOT NULL REFERENCES brain.decisions(id) ON DELETE CASCADE,
    PRIMARY KEY (episode_id, decision_id)
);

-- ---------------------------------------------------------------------------
-- heart.facts — learned information with provenance
-- ---------------------------------------------------------------------------
CREATE TABLE heart.facts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id VARCHAR(100) NOT NULL,
    content TEXT NOT NULL,
    category VARCHAR(100),
    subject VARCHAR(500),
    confidence FLOAT DEFAULT 1.0 CHECK (confidence BETWEEN 0 AND 1),
    source VARCHAR(500),
    source_episode_id UUID REFERENCES heart.episodes(id),
    source_decision_id UUID REFERENCES brain.decisions(id),
    learned_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_confirmed TIMESTAMPTZ,
    confirmation_count INT DEFAULT 0,
    superseded_by UUID REFERENCES heart.facts(id),
    contradiction_of UUID REFERENCES heart.facts(id),
    embedding vector(1536),
    tags TEXT[],
    search_tsv tsvector GENERATED ALWAYS AS (
        to_tsvector('english', content || ' ' || COALESCE(subject, ''))
    ) STORED,
    active BOOLEAN DEFAULT TRUE,
    encoded_frame VARCHAR(100),
    encoded_censors JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    -- F023: Memory Admission Control
    admission_score FLOAT DEFAULT NULL,
    recall_count INTEGER DEFAULT 0,
    last_recalled_at TIMESTAMPTZ DEFAULT NULL,
    -- F021.1: Per-dimension scores for dashboard analytics
    admission_scores JSONB DEFAULT NULL
);

CREATE INDEX idx_facts_encoded_frame ON heart.facts (encoded_frame) WHERE encoded_frame IS NOT NULL;

-- ---------------------------------------------------------------------------
-- heart.procedures — K-lines with level-bands
-- ---------------------------------------------------------------------------
CREATE TABLE heart.procedures (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id VARCHAR(100) NOT NULL,
    name VARCHAR(500) NOT NULL,
    domain VARCHAR(100),
    description TEXT,
    goals TEXT[],
    core_patterns TEXT[],
    core_tools TEXT[],
    core_concepts TEXT[],
    implementation_notes TEXT[],
    activation_count INT DEFAULT 0,
    success_count INT DEFAULT 0,
    failure_count INT DEFAULT 0,
    neutral_count INT DEFAULT 0,
    last_activated TIMESTAMPTZ,
    related_procedures UUID[],
    censor_ids UUID[],
    embedding vector(1536),
    tags TEXT[],
    search_tsv tsvector GENERATED ALWAYS AS (
        to_tsvector('english',
            name || ' '
            || COALESCE(description, '') || ' '
            || COALESCE(immutable_array_to_string(core_patterns, ' '), '') || ' '
            || COALESCE(immutable_array_to_string(implementation_notes, ' '), '') || ' '
            || COALESCE(immutable_array_to_string(goals, ' '), '') || ' '
            || COALESCE(immutable_array_to_string(core_tools, ' '), '') || ' '
            || COALESCE(immutable_array_to_string(core_concepts, ' '), '')
        )
    ) STORED,
    active BOOLEAN DEFAULT TRUE,
    encoded_frame VARCHAR(100),
    encoded_censors JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_procedures_encoded_frame ON heart.procedures (encoded_frame) WHERE encoded_frame IS NOT NULL;

-- ---------------------------------------------------------------------------
-- heart.episode_procedures — links episodes to procedures with effectiveness
-- ---------------------------------------------------------------------------
CREATE TABLE heart.episode_procedures (
    episode_id UUID NOT NULL REFERENCES heart.episodes(id) ON DELETE CASCADE,
    procedure_id UUID NOT NULL REFERENCES heart.procedures(id) ON DELETE CASCADE,
    effectiveness VARCHAR(20) CHECK (effectiveness IN ('helped', 'neutral', 'hindered')),
    PRIMARY KEY (episode_id, procedure_id)
);

-- ---------------------------------------------------------------------------
-- heart.censors — learned constraints
-- ---------------------------------------------------------------------------
CREATE TABLE heart.censors (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id VARCHAR(100) NOT NULL,
    trigger_pattern TEXT NOT NULL,
    action VARCHAR(20) NOT NULL DEFAULT 'warn' CHECK (action IN ('warn', 'block', 'absolute')),
    reason TEXT NOT NULL,
    domain VARCHAR(100),
    learned_from_decision UUID REFERENCES brain.decisions(id),
    learned_from_episode UUID REFERENCES heart.episodes(id),
    created_by VARCHAR(50) DEFAULT 'manual' CHECK (created_by IN ('manual', 'auto_failure', 'auto_escalation')),
    activation_count INT DEFAULT 0,
    last_activated TIMESTAMPTZ,
    false_positive_count INT DEFAULT 0,
    last_false_positive TIMESTAMPTZ,
    escalation_threshold INT DEFAULT 3,
    embedding vector(1536),
    trigger_action JSONB,
    action_instruction TEXT,
    unblock_pattern TEXT,
    active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- heart.working_memory — current session state
-- ---------------------------------------------------------------------------
CREATE TABLE heart.working_memory (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id VARCHAR(100) NOT NULL,
    session_id VARCHAR(100) NOT NULL,
    current_task TEXT,
    current_frame VARCHAR(100),
    items JSONB NOT NULL DEFAULT '[]',
    open_threads JSONB DEFAULT '[]',
    max_items INT DEFAULT 20,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(agent_id, session_id)
);

-- ---------------------------------------------------------------------------
-- heart.conversation_state — persisted conversation for compaction
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS heart.conversation_state (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    summary TEXT,
    messages JSONB,
    turn_count INT NOT NULL DEFAULT 0,
    compaction_count INT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(agent_id, session_id)
);

-- ---------------------------------------------------------------------------
-- heart.subtasks — background task queue
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS heart.subtasks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id VARCHAR(100) NOT NULL,
    parent_session_id VARCHAR,
    task TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 100,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    result TEXT,
    error TEXT,
    worker_id VARCHAR(100),
    timeout_seconds INTEGER NOT NULL DEFAULT 120,
    frame_type VARCHAR(30),
    model VARCHAR(100),
    notify BOOLEAN NOT NULL DEFAULT FALSE,
    delivered BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT chk_subtask_status CHECK (status IN ('pending', 'running', 'completed', 'failed', 'cancelled')),
    CONSTRAINT chk_subtask_priority CHECK (priority > 0)
);

-- ---------------------------------------------------------------------------
-- heart.schedules — recurring/timed task schedules
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS heart.schedules (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id VARCHAR(100) NOT NULL,
    task TEXT NOT NULL,
    schedule_type VARCHAR(20) NOT NULL,
    fire_at TIMESTAMPTZ,
    interval_seconds INTEGER,
    cron_expr VARCHAR(200),
    active BOOLEAN NOT NULL DEFAULT TRUE,
    last_fired_at TIMESTAMPTZ,
    next_fire_at TIMESTAMPTZ,
    fire_count INTEGER NOT NULL DEFAULT 0,
    max_fires INTEGER,
    notify BOOLEAN NOT NULL DEFAULT FALSE,
    timeout_seconds INTEGER NOT NULL DEFAULT 120,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by_session VARCHAR,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    model VARCHAR(100),
    frame_type VARCHAR(20),
    CONSTRAINT chk_schedule_type CHECK (schedule_type IN ('once', 'recurring')),
    CONSTRAINT chk_schedule_has_timing CHECK (
        (schedule_type = 'once' AND fire_at IS NOT NULL) OR
        (schedule_type = 'recurring' AND (interval_seconds IS NOT NULL OR cron_expr IS NOT NULL))
    )
);

-- Tool result cache (F020 — ReversibleCache)
CREATE TABLE IF NOT EXISTS heart.tool_cache (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    hash_key TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    tool_input JSONB,
    original_content TEXT NOT NULL,
    compressed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    item_count INT,
    UNIQUE(session_id, hash_key)
);

CREATE INDEX IF NOT EXISTS idx_tool_cache_session ON heart.tool_cache(session_id);

-- ===========================================================================
-- INDEXES
-- ===========================================================================

-- --- nous_system indexes ---
CREATE INDEX idx_frames_agent ON nous_system.frames(agent_id);
CREATE INDEX idx_events_agent ON nous_system.events(agent_id);
CREATE INDEX idx_events_type ON nous_system.events(event_type);
CREATE INDEX idx_events_created ON nous_system.events(created_at DESC);
CREATE INDEX idx_events_session ON nous_system.events(session_id);

-- --- brain.decisions indexes ---
CREATE INDEX idx_decisions_agent ON brain.decisions(agent_id);
CREATE INDEX idx_decisions_category ON brain.decisions(category);
CREATE INDEX idx_decisions_outcome ON brain.decisions(outcome);
CREATE INDEX idx_decisions_created ON brain.decisions(created_at DESC);
CREATE INDEX idx_decisions_agent_created ON brain.decisions(agent_id, created_at DESC);
CREATE INDEX idx_decisions_embedding ON brain.decisions
    USING hnsw(embedding vector_cosine_ops);
CREATE INDEX idx_decisions_fts ON brain.decisions USING GIN(search_tsv);

-- --- brain.decision_tags indexes ---
CREATE INDEX idx_tags_tag ON brain.decision_tags(tag);

-- --- brain.decision_reasons indexes ---
CREATE INDEX idx_reasons_decision ON brain.decision_reasons(decision_id);
CREATE INDEX idx_reasons_type ON brain.decision_reasons(type);

-- --- brain.thoughts indexes ---
CREATE INDEX idx_thoughts_decision ON brain.thoughts(decision_id);
CREATE INDEX idx_thoughts_created ON brain.thoughts(created_at);

-- --- brain.graph_edges indexes ---
CREATE INDEX idx_edges_source ON brain.graph_edges(source_id);
CREATE INDEX idx_edges_target ON brain.graph_edges(target_id);
CREATE INDEX idx_graph_edges_source_type ON brain.graph_edges(source_id, source_type);
CREATE INDEX idx_graph_edges_target_type ON brain.graph_edges(target_id, target_type);
CREATE INDEX idx_graph_edges_agent ON brain.graph_edges(agent_id);
CREATE INDEX idx_graph_edges_created ON brain.graph_edges(created_at);

-- --- brain.calibration_snapshots indexes ---
CREATE INDEX idx_calibration_agent ON brain.calibration_snapshots(agent_id, snapshot_at DESC);

-- --- heart.episodes indexes ---
CREATE INDEX idx_episodes_agent ON heart.episodes(agent_id);
CREATE INDEX idx_episodes_started ON heart.episodes(started_at DESC);
CREATE INDEX idx_episodes_outcome ON heart.episodes(outcome);
CREATE INDEX idx_episodes_user_id ON heart.episodes(user_id);
CREATE INDEX idx_episodes_summary_gin ON heart.episodes USING GIN(structured_summary);
CREATE INDEX idx_episodes_tags ON heart.episodes USING GIN(tags);
CREATE INDEX idx_episodes_embedding ON heart.episodes
    USING hnsw(embedding vector_cosine_ops);
CREATE INDEX idx_episodes_fts ON heart.episodes USING GIN(search_tsv);

-- --- heart.facts indexes ---
CREATE INDEX idx_facts_agent ON heart.facts(agent_id);
CREATE INDEX idx_facts_category ON heart.facts(category);
CREATE INDEX idx_facts_subject ON heart.facts(subject);
CREATE INDEX idx_facts_active ON heart.facts(active) WHERE active = TRUE;
CREATE INDEX idx_facts_tags ON heart.facts USING GIN(tags);
CREATE INDEX idx_facts_embedding ON heart.facts
    USING hnsw(embedding vector_cosine_ops);
CREATE INDEX idx_facts_fts ON heart.facts USING GIN(search_tsv);

-- --- heart.procedures indexes ---
CREATE INDEX idx_procedures_agent ON heart.procedures(agent_id);
CREATE INDEX idx_procedures_domain ON heart.procedures(domain);
CREATE INDEX idx_procedures_active ON heart.procedures(active) WHERE active = TRUE;
CREATE INDEX idx_procedures_embedding ON heart.procedures
    USING hnsw(embedding vector_cosine_ops);
CREATE INDEX idx_procedures_fts ON heart.procedures USING GIN(search_tsv);

-- --- heart.censors indexes ---
CREATE INDEX idx_censors_agent ON heart.censors(agent_id);
CREATE INDEX idx_censors_action ON heart.censors(action);
CREATE INDEX idx_censors_domain ON heart.censors(domain);
CREATE INDEX idx_censors_active ON heart.censors(active) WHERE active = TRUE;
CREATE INDEX idx_censors_embedding ON heart.censors
    USING hnsw(embedding vector_cosine_ops);

-- --- heart.conversation_state indexes ---
CREATE INDEX idx_conversation_state_agent ON heart.conversation_state(agent_id);

-- --- heart.subtasks indexes ---
CREATE INDEX idx_subtasks_pending
    ON heart.subtasks (priority, created_at) WHERE status = 'pending';
CREATE INDEX idx_subtasks_agent
    ON heart.subtasks (agent_id, created_at DESC);
CREATE INDEX idx_subtasks_undelivered
    ON heart.subtasks (parent_session_id, created_at)
    WHERE status IN ('completed', 'failed') AND delivered = FALSE;

-- --- heart.schedules indexes ---
CREATE INDEX idx_schedules_next
    ON heart.schedules (next_fire_at) WHERE active = TRUE;
CREATE INDEX idx_schedules_agent
    ON heart.schedules (agent_id) WHERE active = TRUE;

-- ===========================================================================
-- TRIGGERS — auto-update updated_at
-- ===========================================================================
CREATE TRIGGER set_updated_at BEFORE UPDATE ON nous_system.agents
    FOR EACH ROW EXECUTE FUNCTION update_timestamp();

CREATE TRIGGER set_updated_at BEFORE UPDATE ON brain.decisions
    FOR EACH ROW EXECUTE FUNCTION update_timestamp();

CREATE TRIGGER set_updated_at BEFORE UPDATE ON heart.facts
    FOR EACH ROW EXECUTE FUNCTION update_timestamp();

CREATE TRIGGER set_updated_at BEFORE UPDATE ON heart.procedures
    FOR EACH ROW EXECUTE FUNCTION update_timestamp();

CREATE TRIGGER set_updated_at BEFORE UPDATE ON heart.censors
    FOR EACH ROW EXECUTE FUNCTION update_timestamp();

CREATE TRIGGER set_updated_at BEFORE UPDATE ON heart.working_memory
    FOR EACH ROW EXECUTE FUNCTION update_timestamp();

CREATE TRIGGER set_updated_at BEFORE UPDATE ON heart.conversation_state
    FOR EACH ROW EXECUTE FUNCTION update_timestamp();
