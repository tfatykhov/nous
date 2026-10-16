-- CI probe: ordinary DDL, every name qualified. Must apply under the application role.
CREATE TABLE IF NOT EXISTS nous_system.ci_probe_9998 (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    note TEXT
);

CREATE INDEX IF NOT EXISTS idx_ci_probe_9998_note ON nous_system.ci_probe_9998 (note);

CREATE TRIGGER ci_probe_9998_updated BEFORE UPDATE ON nous_system.ci_probe_9998
    FOR EACH ROW EXECUTE FUNCTION update_timestamp();
