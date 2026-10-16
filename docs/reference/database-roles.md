# Database roles

Part of the [Nous development guide](../../CLAUDE.md). How the Postgres roles of a deployment are set up, checked, backed up and rolled back. Local development and the main CI test job use one superuser login and need none of this; a production deployment, and the `DB roles` CI job, run `scripts/ops/db_roles.sql`.

## Roles

| Role | Can log in | Owns | May |
|---|---|---|---|
| `nous_admin` | on the database host only (`docker exec`), no password | the database, schema `scoring` and its two tables | everything (superuser) |
| `nous_app` | with a password | schemas `brain`, `heart`, `nous_system` and their objects | create objects in the database, read `scoring.task_scores` and `scoring.scorer_heartbeat` |
| `nous_scorer` | with a password | nothing | read the tables in `SOURCE_TABLES` (`nous/observability/task_score_job.py`); read, insert and update `scoring.task_scores` and `scoring.scorer_heartbeat` |
| bootstrap role (`nous`, the `POSTGRES_USER` of `docker-compose.yml`) | no | the extensions | PostgreSQL does not let this role lose `SUPERUSER`, so its login and password are removed |

The scorer's `search_path` is `pg_catalog, scoring` and its `row_security` is `off`. Nothing but the administrator can change a role, a grant on the database, or `scoring`.

## Run order

On a deployment, always: the application first (it applies migrations 087 and 088, which create `scoring.task_scores` and `scoring.scorer_heartbeat`), then the role script, then the check, then the scorer.

1. Deploy the release. The application still runs as the bootstrap role.
2. Set in the host `.env`: `NOUS_APP_DB_USER=nous_app`, `NOUS_APP_DB_PASSWORD`, `NOUS_SCORER_DB_USER=nous_scorer`, `NOUS_SCORER_DB_PASSWORD` (`openssl rand -hex 24` each: the script refuses a password that is not letters and digits only). Both scorer variables are required: without them the scorer falls back to the application's login, cannot write the scores, and the only sign is a stale `task_scores.scorer.last_run_at`.
3. Stop the application: `docker compose stop nous`. The first run changes the owner of every application table, which takes an exclusive lock on each.
4. Run the script, the first time as the bootstrap role: `docker exec -i -e NOUS_APP_DB_PASSWORD -e NOUS_SCORER_DB_PASSWORD nous-postgres psql -U nous -d nous -X -v ON_ERROR_STOP=1 -f - < scripts/ops/db_roles.sql` (the variables must be exported in the shell that runs it). Later runs use `-U nous_admin`.
5. Check: `docker exec -i nous-postgres psql -U nous_admin -d nous -X -v ON_ERROR_STOP=1 -f - < scripts/ops/verify_db_roles.sql` must exit 0 and print `verify_db_roles: N checks passed` (N is the number of checks in the script, 71 at present).
6. `docker compose up -d nous nous-scorer`.
7. `GET /dashboard/autonomy`: `task_scores.scorer.last_run_at` and `last_scored_at` move within 15 minutes. A scorer that cannot write (for instance one that fell back to the application's login) has its heartbeat write refused as well, so both go stale.

Restoring a dump into the same cluster: restore as `nous_admin` (a restore needs a superuser). The dump carries the recorded migrations, including 087 and 088, so the application starts as `nous_app`; run steps 4 and 5 again (as `nous_admin`) to re-apply the grants.

Building a database from nothing: a fresh cluster's bootstrap role can still log in, so start the stack with `NOUS_APP_DB_USER`, `NOUS_APP_DB_PASSWORD`, `NOUS_SCORER_DB_USER` and `NOUS_SCORER_DB_PASSWORD` unset (the application connects as the bootstrap role and applies every migration, 087 and 088 included), then follow steps 2 to 7. The script refuses to run without `scoring.task_scores` and `scoring.scorer_heartbeat`, so running it first cannot lock the application out of its own migration. The database's `POSTGRES_USER` must not be `nous_admin`: the script disables the bootstrap role's login, so it refuses to run when that role is named `nous_admin`.

## Commands that changed

The bootstrap role no longer logs in, so `docker exec nous-postgres psql -U "$POSTGRES_USER"` fails. Use `-U nous_admin`. This includes the backup taken before a deploy: `docker exec nous-postgres pg_dump -U nous_admin -d nous -Fc > backup.dump`. A dump taken as `nous_app` also completes (it reads every table); a restore needs `nous_admin`. Remote tools that connected with the old login use `nous_app`.

## Remote tools

The diagnostics scripts that read a deployment's database over the network (`scripts/diag/copy_prod_corpus.py` and nine others) use the `nous_app` login (`PROD_DB_USER`, `PROD_PW` or `PROD_DSN`). `scripts/diag/_prod_psql.py` runs `psql` and `pg_dump` inside the database container on the database host, as `PROD_PG_USER` (default `nous_admin`). Which network logins the server accepts is set in `pg_hba.conf`; see the next section.

## What the checks rely on

- **`pg_hba.conf`.** `nous_admin` has no password, so it is safe from network logins only while `pg_hba.conf` lets nothing but the local socket and loopback in without a password. `verify_db_roles.sql` reads `pg_hba_file_rules` and fails if a line beyond loopback uses `trust`, `md5` or `password`, or if a network line that covers all users or `nous_admin` uses anything but `scram-sha-256` or `reject`. A deployment that publishes the port adds lines such as `host all all <lan-cidr> scram-sha-256`, `host all all 172.16.0.0/12 scram-sha-256` and ends with `host all all all reject`. After editing the file, `select pg_reload_conf()` and run `verify`.
- **Schemas.** The application role keeps `CREATE` on the database (migrations may need to add a schema), so it can create a schema named after a role. `verify` fails if the database has any schema other than `brain`, `heart`, `nous_system`, `scoring` and `public` (plus the system ones), and the administrator's and scorer's `search_path` are pinned so that such a schema is never consulted first. The administrator drops a stray schema; the script does not.
- **What is not covered.** The application role owns, and can write, the tables the scorer reads, so the scores are only as trustworthy as those inputs. These roles separate what each database login may do; they do not restrict administrative access to the database host itself.

## Migrations

On a deployment that ran `scripts/ops/db_roles.sql`, the application applies migrations as `nous_app`. A migration may create tables, indexes, columns and triggers in `brain`, `heart` and `nous_system`. It may not install an extension, grant, change an owner, create any object (table, index, sequence, trigger, function, view, type) without naming `brain`, `heart` or `nous_system` (an unqualified name lands in `public`, where the application role may not create, and the application then fails to start), or touch `scoring.*`. `tests/test_migrations_privilege_contract.py` enforces this statically and the `DB roles` CI job enforces it by applying every migration after 088 as `nous_app`. A change to a `scoring` table is therefore not a migration file: the administrator applies it with `psql -U nous_admin`.

## Rollback

`docker compose stop nous`; `docker exec -i -e NOUS_LEGACY_DB_PASSWORD nous-postgres psql -U nous_admin -d nous -X -v ON_ERROR_STOP=1 -f - < scripts/ops/db_roles_rollback.sql` (the password the application used before, from the host `.env`'s `DB_PASSWORD`; it must be letters and digits only, and the script refuses others: if the old password is not, generate a new one with `openssl rand -hex 24` and use that); set `NOUS_APP_DB_USER=nous` and `NOUS_APP_DB_PASSWORD=<that password>` in `.env`; `docker compose up -d nous`. Running `db_roles.sql` again restores the lock-out.
