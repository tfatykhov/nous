-- CI probe: an unqualified CREATE TABLE lands in the public schema, where the application role may not create.
-- Must FAIL under the application role.
CREATE TABLE ci_probe_9999 (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid()
);
