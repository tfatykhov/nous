-- 022: Rubric versions + outcome signals for F024 Phase 3b

-- Rubric versions — immutable snapshots of evaluation criteria
CREATE TABLE IF NOT EXISTS heart.rubric_versions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id VARCHAR(100) NOT NULL REFERENCES nous_system.agents(id),
    version VARCHAR(20) NOT NULL,          -- semver: "1.0.0", "1.1.0"
    parent_version VARCHAR(20),            -- previous version string
    change_reason TEXT NOT NULL,
    dimensions JSONB NOT NULL,             -- array of dimension objects
    outcome_correlations JSONB DEFAULT '{}', -- dimension->outcome correlation data
    status VARCHAR(20) NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'superseded', 'rollback')),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Only one active rubric per agent at a time
CREATE UNIQUE INDEX idx_rubric_active_agent
    ON heart.rubric_versions(agent_id) WHERE status = 'active';

-- Outcome signals — per-episode ground truth for rubric evolution
CREATE TABLE IF NOT EXISTS heart.outcome_signals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id VARCHAR(100) NOT NULL REFERENCES nous_system.agents(id),
    episode_id UUID NOT NULL REFERENCES heart.episodes(id) ON DELETE CASCADE,
    signal_type VARCHAR(30) NOT NULL
        CHECK (signal_type IN ('corrected', 'completed', 'praised', 'reworked', 'self_corrected')),
    confidence FLOAT NOT NULL DEFAULT 0.5  -- detector confidence in classification
        CHECK (confidence BETWEEN 0 AND 1),
    evidence TEXT,                           -- what triggered the classification
    self_improvement_scores JSONB,          -- snapshot of rubric scores at time of episode
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_outcome_signals_agent_episode
    ON heart.outcome_signals(agent_id, episode_id);

CREATE INDEX idx_outcome_signals_agent_type
    ON heart.outcome_signals(agent_id, signal_type);
