#!/usr/bin/env bash
# F051 eval-db: record the migrations initdb just applied.
#
# docker-entrypoint-initdb.d executes every migration file but writes nothing
# to nous_system.schema_migrations, so the eval harness's run_migrations would
# replay them all and fail on the first non-idempotent statement. Named zz_ so
# it runs after every NNN_*.sql. Checksums match nous/storage/migrator.py
# (sha256 of the file contents).
#
# MUST be LF-terminated — enforced via /.gitattributes (`*.sh text eol=lf`).
set -euo pipefail

for f in /docker-entrypoint-initdb.d/[0-9][0-9][0-9]_*.sql; do
    name="$(basename "${f}" .sql)"
    version="${name%%_*}"
    checksum="$(sha256sum "${f}" | cut -d' ' -f1)"
    psql -v ON_ERROR_STOP=1 --username "${POSTGRES_USER}" --dbname "${POSTGRES_DB}" -c \
        "INSERT INTO nous_system.schema_migrations (version, name, checksum)
         VALUES ('${version}', '${name}', '${checksum}') ON CONFLICT (version) DO NOTHING"
done
