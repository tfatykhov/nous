"""Read-only helper: run a SQL query (or pg_dump) on PROD via SSH + docker exec.

Secrets come from PROD_SSH_* env vars — never the command line / this file.
Usage:
  PROD_SSH_HOST=.. PROD_SSH_USER=.. PROD_SSH_PW=.. \
    uv run python scripts/diag/_prod_psql.py sql "SELECT count(*) FROM heart.facts;"
  ... uv run python scripts/diag/_prod_psql.py dump  > prod_dump.sql
"""
import os
import sys

import paramiko

host = os.environ["PROD_SSH_HOST"]
user = os.environ["PROD_SSH_USER"]
pw = os.environ["PROD_SSH_PW"]
port = int(os.environ.get("PROD_SSH_PORT", "22"))
container = os.environ.get("PROD_PG_CONTAINER", "nous-postgres")
db = os.environ.get("PROD_PG_DB", "nous")

cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect(host, port=port, username=user, password=pw, timeout=30)


def run(cmd: str, want_bytes: bool = False):
    _i, o, e = cli.exec_command(cmd, timeout=600)
    out = o.read()
    err = e.read().decode("utf-8", "replace")
    if err.strip():
        sys.stderr.write("[prod stderr] " + err + "\n")
    return out if want_bytes else out.decode("utf-8", "replace")


mode = sys.argv[1]

if mode == "sql":
    sql = sys.argv[2].replace('"', '\\"')
    print(run(f'docker exec {container} psql -U nous -d {db} -tAc "{sql}"'))

elif mode == "schema":
    cmd = (
        f"docker exec {container} pg_dump -U nous -d {db} --schema-only --no-owner "
        f"--no-privileges"
    )
    data = run(cmd, want_bytes=True)
    sys.stdout.buffer.write(data)
    sys.stderr.write(f"[schema] {len(data)} bytes\n")

elif mode == "dump":
    # Data-only dump of the memory + graph tables (prod is single-agent nous-default).
    tables = " ".join(
        f"-t {t}" for t in [
            "brain.decisions", "heart.facts", "heart.episodes",
            "heart.procedures", "heart.censors", "brain.graph_edges",
        ]
    )
    cmd = (
        f"docker exec {container} pg_dump -U nous -d {db} --data-only --no-owner "
        f"--no-privileges {tables}"
    )
    data = run(cmd, want_bytes=True)
    sys.stdout.buffer.write(data)
    sys.stderr.write(f"[dump] {len(data)} bytes\n")

cli.close()
