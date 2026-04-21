#!/usr/bin/env bash
# F051 eval-db fixture loader.
#
# Runs inside the `ingest` build stage AFTER
# docker-entrypoint-initdb.d/*.sql has been applied. Boots a temporary Postgres,
# waits for readiness, bulk-COPYs the JSONL fixtures into the corresponding
# tables, stamps nous_eval_meta, then shuts the server down cleanly so the
# runtime stage can copy the cooked data directory.
#
# MUST be LF-terminated — enforced via /.gitattributes (`*.sh text eol=lf`).
set -euo pipefail

FIXTURES_DIR="/fixtures"
PGUSER="${POSTGRES_USER:-nous}"
PGDB="${POSTGRES_DB:-nous_eval}"
FIXTURE_VERSION="${NOUS_EVAL_FIXTURE_VERSION:-unknown}"

echo "[load.sh] fixture_version=${FIXTURE_VERSION}"

# Start Postgres in the background for the duration of the COPY phase.
# docker-entrypoint.sh already ran init scripts in a throwaway server; we need
# a fresh one under our control so we can COPY and shut it down cleanly.
pg_ctl -D "${PGDATA}" -o "-c listen_addresses=''" -w start

trap 'pg_ctl -D "${PGDATA}" -m fast -w stop || true' EXIT

# Wait until pg is actually accepting connections — pg_ctl -w already blocks
# but this is belt-and-suspenders on slower CI.
for _ in $(seq 1 30); do
    if pg_isready -U "${PGUSER}" -d "${PGDB}" -q; then
        break
    fi
    sleep 1
done

load_jsonl_if_present() {
    local table="$1"
    local file="${FIXTURES_DIR}/$(basename "${table}").jsonl"
    if [[ ! -f "${file}" ]]; then
        echo "[load.sh] SKIP ${table} (no fixture at ${file})"
        return 0
    fi
    echo "[load.sh] LOAD ${table} <- ${file}"
    # Each JSONL row becomes one INSERT via a staging-table trick: COPY the raw
    # lines into a TEMP (col JSONB) then INSERT ... SELECT.
    psql -U "${PGUSER}" -d "${PGDB}" -v ON_ERROR_STOP=1 <<SQL
\\set fixture_path '${file}'
CALL nous_eval_load_jsonl(:'fixture_path', '${table}');
SQL
}

# Tables loaded in dependency order (decisions first so facts/episodes can
# reference them via cross-type graph edges; procedures last).
load_jsonl_if_present brain.decisions
load_jsonl_if_present heart.facts
load_jsonl_if_present heart.episodes
load_jsonl_if_present heart.procedures
load_jsonl_if_present heart.censors

# Stamp the fixture version so the running harness can version-check on startup
# (see nous/eval/retrieval.py::_verify_fixture_version).
psql -U "${PGUSER}" -d "${PGDB}" -v ON_ERROR_STOP=1 <<SQL
CREATE TABLE IF NOT EXISTS nous_eval_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT INTO nous_eval_meta (key, value) VALUES
    ('fixture_version', '${FIXTURE_VERSION}'),
    ('baked_at',        NOW()::TEXT)
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
SQL

echo "[load.sh] done"
