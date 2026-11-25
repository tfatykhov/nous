-- 125: nous_system.entity_profile.owner_verdict, the owner's own verdict on an entity key (F119 PR 2)
--
-- NULL, or {verdict, by, at} when the owner said from the dashboard that the key is not an entity: verdict is
-- not_entity, by the proof the owner's call carried (owner or proxy) and at when (ISO 8601). While it is set the sleep
-- phase neither asks about the key nor writes its row, whatever its facts do, and the 30-day and doubled-facts re-ask
-- of a not_entity verdict do not apply. The writer's own status and lines are left as they were, so taking the
-- verdict back returns the row to them. Nothing else changes: the owner's other corrections are recorded in the
-- columns migration 123 made (lines, previous_lines, rejected, active).
--
-- Writer and reader: the app only. No grant, and scripts/ops/db_roles.sql is not touched. Rollback is
-- ALTER TABLE nous_system.entity_profile DROP COLUMN IF EXISTS owner_verdict and deleting version 125 from
-- schema_migrations.

ALTER TABLE nous_system.entity_profile
    ADD COLUMN IF NOT EXISTS owner_verdict JSONB;
