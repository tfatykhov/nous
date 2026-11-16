-- 115: the companion devices paired with this Nous
--
-- nous_system.a2ui_devices: one row per paired device. A device proves itself with a token it is given once, when
-- it redeems a pairing code. Only the token's SHA-256 is stored (token_sha256), so the token cannot be read back
-- from here. name is the owner's name for the device, else the one the device sent, shown as text.
-- installation_id is the device's push installation id. last_seen_at is written at most once a minute.
-- revoked_at ends a device (a soft delete, the row is kept), and at most one live device of an agent holds an
-- installation id.
--
-- Writer and reader: the app only. No grant, and scripts/ops/db_roles.sql is not touched: the db_query reader is
-- not granted this table. Created by the application role through the migrator.

CREATE TABLE IF NOT EXISTS nous_system.a2ui_devices (
    agent_id        TEXT        NOT NULL,
    id              UUID        NOT NULL DEFAULT gen_random_uuid(),
    name            TEXT        NOT NULL CHECK (char_length(name) BETWEEN 1 AND 60),
    token_sha256    TEXT        NOT NULL CHECK (token_sha256 ~ '^[0-9a-f]{64}$'),
    installation_id TEXT        NOT NULL CHECK (char_length(installation_id) BETWEEN 1 AND 100),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at    TIMESTAMPTZ,
    revoked_at      TIMESTAMPTZ,
    PRIMARY KEY (agent_id, id),
    CONSTRAINT uq_a2ui_devices_token UNIQUE (token_sha256)
);

-- A device pairing again with the same installation supersedes the live device that holds it.
CREATE UNIQUE INDEX IF NOT EXISTS uq_a2ui_devices_live_installation
    ON nous_system.a2ui_devices (agent_id, installation_id) WHERE revoked_at IS NULL;
