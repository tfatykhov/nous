-- 077: FCM push for the A2UI companion (F097 §6)
--
-- The companion is a push surface with no way to reach a phone that is not
-- looking at it. The web client only learns about a card while its SSE stream
-- is open; the Android app is asleep most of the time. FCM is a second
-- notification leg beside the existing Telegram ping, carrying a pointer to a
-- surface that already exists and is already durable — a lost push loses the
-- pointer, never the card.
--
-- installation_id is a random UUID the app mints once per install. It is an
-- identifier, NOT a credential: the cap and the tripwire below are bounds on
-- accidental growth, not access control. Access control is the tailnet.
--
-- push_notified_at on a2ui_surfaces is an INTENT flag, not an acceptance flag.
-- It is written inside the INSERT transaction of the surface, so a dismiss can
-- never race ahead of it; stamping it after FCM accepts would leave a
-- permanent notification for any card resolved during the send, and the DAG
-- approval path closes cards within milliseconds. A dismiss sent to a phone
-- that never got the notification is a harmless cancel.

CREATE TABLE IF NOT EXISTS nous_system.a2ui_push_installations (
    agent_id              TEXT         NOT NULL,
    installation_id       TEXT         NOT NULL,
    name                  TEXT         NOT NULL DEFAULT '',
    platform              TEXT         NOT NULL DEFAULT 'android',
    fcm_token             TEXT,
    app_version           TEXT         NOT NULL DEFAULT '',
    notifications_enabled BOOLEAN      NOT NULL DEFAULT TRUE,
    last_error            TEXT,
    created_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT pk_a2ui_push_installations PRIMARY KEY (agent_id, installation_id)
);

-- The send path selects exactly the rows that can receive: a token, and
-- notifications not turned off. FCM deprioritises apps whose notifications
-- the user denied, so sending to them is waste, not reach.
CREATE INDEX IF NOT EXISTS idx_a2ui_push_installations_sendable
    ON nous_system.a2ui_push_installations (agent_id)
    WHERE fcm_token IS NOT NULL AND notifications_enabled;

ALTER TABLE nous_system.a2ui_surfaces
    ADD COLUMN IF NOT EXISTS push_notified_at TIMESTAMPTZ;
