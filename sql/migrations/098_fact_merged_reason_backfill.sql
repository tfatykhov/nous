-- 098: stamp deactivation_reason = 'merged' on sources merged before 094.
-- A sleep merge (F027 cluster merge, F031 MERGE) deactivates its sources and
-- points their superseded_by at the merged fact. Since 094 it also stamps
-- 'merged', and recall_deep reads only that column to list a merged fact's
-- sources. Rows merged before 094 carry NULL, so they are stamped here from
-- the events that recorded the merge. The column then needs no event lookup
-- at read time, and it outlives the events.
-- F027: the f027_cluster_merge event that wrote the merged fact names it in
-- merged_fact_id and lists the source in source_fact_ids. A refused or
-- aborted cluster has no merged_fact_id, so its members stay NULL.
-- F031: the event carries no merged id. A source counts when a MERGE event
-- names it and the other fact of that pair is superseded by the same fact.
-- A pair whose MERGE aborted and whose two facts later lost to one winner
-- also looks like that, so three more conditions rule it out.
-- The row went inactive with the MERGE: its updated_at, which records the
-- deactivation, lies between a minute before and ten minutes after the
-- event. A later supersede of any kind, a key conflict included, writes no
-- F031 event but moves updated_at past that window. Anything that touched
-- the row after it went inactive also moves it, which leaves it unstamped.
-- No F031 SUPERSEDE_A or SUPERSEDE_B event names the row and its superseder
-- (a supersede within the window). The superseder exists no later than ten
-- minutes after the event (a write-time supersede creates a newer winner).
-- A MERGE writes its product within a second of the event, or points the
-- sources at an older fact its content duplicated.
-- A loser of a plain SUPERSEDE matches neither rule and stays NULL.
-- Every UPDATE touches only rows still NULL, so a second run changes nothing.
-- The stamp is not an edit, so it must not move updated_at (the
-- backfill_supersession rollback selects rows by updated_at). The
-- set_updated_at trigger is off for the two UPDATEs only, as in 097.
-- The partial index serves the recall_deep lookup by superseded_by.
-- Plain statements, no DO block and no semicolon in a comment.
ALTER TABLE heart.facts DISABLE TRIGGER set_updated_at;

UPDATE heart.facts AS s
    SET deactivation_reason = 'merged'
    FROM nous_system.events AS e
    WHERE s.active = false
      AND s.deactivation_reason IS NULL
      AND s.superseded_by IS NOT NULL
      AND e.agent_id = s.agent_id
      AND e.event_type = 'f027_cluster_merge'
      AND e.data->>'merged_fact_id' = s.superseded_by::text
      AND e.data->'source_fact_ids' @> jsonb_build_array(s.id::text);

UPDATE heart.facts AS s
    SET deactivation_reason = 'merged'
    FROM (
        SELECT agent_id, created_at,
            CASE WHEN self_text ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                THEN CAST(self_text AS uuid) END AS self_id,
            CASE WHEN partner_text ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                THEN CAST(partner_text AS uuid) END AS partner_id
        FROM (
            SELECT agent_id, created_at, data->>'fact1_id' AS self_text, data->>'fact2_id' AS partner_text
            FROM nous_system.events
            WHERE event_type = 'f031_contradiction_resolution'
              AND data->>'applied_action' = 'MERGE'
            UNION ALL
            SELECT agent_id, created_at, data->>'fact2_id', data->>'fact1_id'
            FROM nous_system.events
            WHERE event_type = 'f031_contradiction_resolution'
              AND data->>'applied_action' = 'MERGE'
        ) AS named
    ) AS pair
    JOIN heart.facts AS p
        ON p.id = pair.partner_id
        AND p.agent_id = pair.agent_id
    JOIN heart.facts AS m
        ON m.id = p.superseded_by
    WHERE s.id = pair.self_id
      AND s.agent_id = pair.agent_id
      AND s.active = false
      AND s.deactivation_reason IS NULL
      AND s.superseded_by IS NOT NULL
      AND p.id <> s.id
      AND p.superseded_by = s.superseded_by
      AND s.updated_at BETWEEN pair.created_at - interval '1 minute' AND pair.created_at + interval '10 minutes'
      AND m.created_at <= pair.created_at + interval '10 minutes'
      AND NOT EXISTS (
          SELECT 1 FROM nous_system.events AS x
          WHERE x.agent_id = s.agent_id
            AND x.event_type = 'f031_contradiction_resolution'
            AND x.data->>'applied_action' IN ('SUPERSEDE_A', 'SUPERSEDE_B')
            AND (
                (x.data->>'fact1_id' = s.id::text AND x.data->>'fact2_id' = s.superseded_by::text)
                OR (x.data->>'fact2_id' = s.id::text AND x.data->>'fact1_id' = s.superseded_by::text)
            )
      );

ALTER TABLE heart.facts ENABLE TRIGGER set_updated_at;

CREATE INDEX IF NOT EXISTS idx_facts_merged_sources
    ON heart.facts (agent_id, superseded_by)
    WHERE deactivation_reason = 'merged';
