-- =============================================================================
-- Nous Seed Data — seed.sql
-- Default agent, 6 cognitive frames, 4 guardrails
-- =============================================================================

-- ---------------------------------------------------------------------------
-- Default agent
-- ---------------------------------------------------------------------------
INSERT INTO nous_system.agents (id, name, description, config) VALUES (
    'nous-default',
    'Nous',
    'A thinking agent that learns from experience',
    '{
        "identity": {"traits": ["analytical", "cautious", "curious"]},
        "embedding_model": "text-embedding-3-small",
        "embedding_dimensions": 1536,
        "working_memory_capacity": 20,
        "auto_extract_facts": true,
        "auto_create_censors": true,
        "censor_escalation": true
    }'::jsonb
);

-- ---------------------------------------------------------------------------
-- Default cognitive frames
-- ---------------------------------------------------------------------------
INSERT INTO nous_system.frames (id, agent_id, name, description, activation_patterns, default_category, default_stakes) VALUES
    ('task', 'nous-default', 'Task Execution', 'Focused on completing a specific task', ARRAY['build', 'fix', 'create', 'implement', 'deploy', 'run', 'execute', 'check', 'show', 'list', 'install', 'update', 'delete', 'move', 'copy'], 'tooling', 'medium'),
    ('question', 'nous-default', 'Question Answering', 'Answering questions, looking things up', ARRAY['what', 'how', 'why', 'explain', 'tell me'], 'process', 'low'),
    ('decision', 'nous-default', 'Decision Making', 'Evaluating options, choosing a path', ARRAY['should', 'choose', 'decide', 'compare', 'trade-off'], 'architecture', 'high'),
    ('creative', 'nous-default', 'Creative', 'Brainstorming, ideation, exploration', ARRAY['imagine', 'brainstorm', 'what if', 'design', 'explore'], 'architecture', 'low'),
    ('conversation', 'nous-default', 'Conversation', 'Casual or social interaction', ARRAY['hello', 'hi', 'thanks', 'how are you'], 'process', 'low'),
    ('debug', 'nous-default', 'Debug', 'Investigating problems, tracing errors', ARRAY['error', 'bug', 'broken', 'failing', 'crash', 'wrong'], 'tooling', 'medium');

-- ---------------------------------------------------------------------------
-- Default guardrails (CEL expressions)
-- ---------------------------------------------------------------------------
INSERT INTO brain.guardrails (agent_id, name, description, condition, severity, priority) VALUES
    ('nous-default', 'no-high-stakes-low-confidence', 'Block high-stakes decisions with low confidence', '{"cel": "decision.stakes == ''high'' && decision.confidence < 0.5"}', 'block', 100),
    ('nous-default', 'no-critical-without-review', 'Block critical-stakes without explicit review', '{"cel": "decision.stakes == ''critical''"}', 'block', 90),
    ('nous-default', 'require-reasons', 'Block decisions without at least one reason', '{"cel": "decision.reason_count < 1"}', 'block', 110),
    ('nous-default', 'low-quality-recording', 'Block low-quality decisions (missing tags/pattern)', '{"cel": "decision.quality_score < 0.55"}', 'block', 120);
