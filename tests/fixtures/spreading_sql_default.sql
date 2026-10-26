
        WITH RECURSIVE activation AS (
            SELECT id, node_type, score AS activation, 0 AS depth, id AS seed_id
            FROM (VALUES (CAST(:id_0 AS UUID), CAST(:type_0 AS VARCHAR), CAST(:score_0 AS FLOAT)), (CAST(:id_1 AS UUID), CAST(:type_1 AS VARCHAR), CAST(:score_1 AS FLOAT))) AS seeds(id, node_type, score)

            UNION ALL

            SELECT
                CASE WHEN e.source_id = a.id THEN e.target_id ELSE e.source_id END,
                CASE WHEN e.source_id = a.id THEN e.target_type ELSE e.source_type END,
                -- Clamp the weight term at 1.0 (no DB CHECK on
                -- graph_edges.weight). CASE, not LEAST: the suite's default
                -- SQLite backend has no LEAST() (codex PR #558 P2).
                a.activation
                    * CASE WHEN COALESCE(e.weight, 1.0) > 1.0 THEN 1.0
                           ELSE COALESCE(e.weight, 1.0) END
                    * :decay,
                a.depth + 1,
                a.seed_id
            FROM activation a
            JOIN brain.graph_edges e
                ON (e.source_id = a.id OR e.target_id = a.id)
            WHERE a.depth < :max_depth
                -- 2b: exclude non-associative edges (lineage supersedes,
                -- negative contradicts, temporal happened_before, builder
                -- co_occurred/co_mention) so spreading never traverses them.
                -- Single source of truth: graph_constants.autobehavior_exclusion_sql.
                AND e.relation NOT IN ('co_occurred', 'contradicts', 'happened_before', 'supersedes') AND (e.extraction_method IS NULL OR e.extraction_method NOT IN ('co_mention'))
                AND e.agent_id = :agent_id
        ),
        -- A8: keep the winning path's depth alongside its activation.
        -- ROW_NUMBER over (activation DESC, depth ASC) selects exactly the row
        -- MAX(activation) used to return, so `total_activation` is unchanged;
        -- the depth tie-break only decides which of two equally-activated
        -- paths is reported, preferring the shorter. seed_id is the LAST key:
        -- it changes neither activation nor depth, only makes the reported
        -- seed of an exact tie deterministic.
        ranked AS (
            SELECT act.id, act.node_type, act.activation, act.depth, act.seed_id,
                   ROW_NUMBER() OVER (
                       PARTITION BY act.id, act.node_type
                       ORDER BY act.activation DESC, act.depth ASC, act.seed_id ASC
                   ) AS rn
            FROM activation act
            WHERE act.id != ALL(CAST(:excluded_ids AS uuid[])) AND NOT (act.node_type = 'decision' AND EXISTS (  SELECT 1 FROM brain.decisions d  WHERE d.id = act.id AND d.agent_id = :agent_id    AND ((d.outcome = 'failure' AND d.confidence = 0.0) OR COALESCE(d.outcome, 'pending') IN (:demoted_0, :demoted_1)))) AND NOT (act.node_type = 'procedure' AND EXISTS (  SELECT 1 FROM heart.procedures p  WHERE p.id = act.id AND p.kind IN (:strategy_kind, :job_lesson_kind)))
        )
        SELECT id, node_type, activation AS total_activation, depth, seed_id
        FROM ranked
        WHERE rn = 1
        ORDER BY total_activation DESC
        LIMIT :result_limit
    