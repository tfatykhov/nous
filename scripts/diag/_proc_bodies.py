"""Read-only dump of full procedure bodies for the dedup consolidation review.

Pulls description / core_patterns / implementation_notes / core_tools / core_concepts /
goals / censor_ids / related_procedures / tags for a fixed set of procedure IDs (the
duplicate clusters + the empty-description rows), so canonical selection and body-coverage
can be judged before any archive. Secrets come from PROD_SSH_* env vars.
Writes scripts/diag/_proc_bodies.json.
"""
import json
import os

import paramiko

host = os.environ["PROD_SSH_HOST"]
user = os.environ["PROD_SSH_USER"]
pw = os.environ["PROD_SSH_PW"]
port = int(os.environ.get("PROD_SSH_PORT", "22"))
agent = os.environ.get("PROD_AGENT_ID", "nous-default")

IDS = [
    # email cluster
    "54fedf18-1299-41ae-8ad8-e7a795f4f70a",  # send_email (canonical candidate)
    "97cb7b96-41f2-400f-bef1-b6d1ae38f77f",  # validated-email-sending
    "47fbb706-7aff-4811-8136-8973f4894197",  # Send Email via Gmail SMTP #1
    "2a1f583b-d495-4a0e-9131-30d27dfc30c5",  # Send Email via Gmail SMTP #2
    "576e10f4-4a45-4e3b-a7aa-8efd962c588a",  # Send Email via Gmail SMTP #3
    "828da799-9890-4a52-b99a-87bed68731be",  # email
    "a45afe76-2a31-435c-8e05-060e4a477dae",  # notify_tim
    "b6af7630-4fc6-4c21-8c68-5f63e74c317b",  # internal-comms
    "6a4cebf2-44a4-40fc-b0af-c4e75525be72",  # talk_to_emerson
    # actiongate cluster
    "a1cefc07-80cb-45cd-84ff-497564eab187",  # duplicate_action_gate_recovery
    "408d59db-3cb1-4eb3-be75-1cd7045dc1d4",  # duplicate-action-gate-acknowledgment
    # compaction cluster
    "656693fb-1319-4e92-89ee-0668246a4e1c",  # Conversation Compaction Awareness
    "f045d70f-332e-4f14-b667-74aff68f7553",  # Conversation Compaction Management
    # context cluster
    "003835b1-2759-4227-8200-75b2f380496f",  # context-degradation
    "9d0ebef0-b41d-4070-aa24-37952556e7d7",  # context-compression
    "5a1cdf77-53de-406f-b906-0af088b6f21c",  # context-fundamentals
    # empty-description rows (goal 1b)
    "f6515b17-5a8a-4696-a425-3714dddcffdc",  # deep-researcher
    "84a25199-a70f-4a26-a022-57f670e1feed",  # investigate
    "de9ff940-6e8c-4ffa-b292-3f519ca3ded2",  # cso
    "87dceb55-8d5d-406f-ae94-0103899ed2d3",  # office-hours
]

cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect(host, port=port, username=user, password=pw, timeout=20)


def run(cmd: str):
    _i, o, e = cli.exec_command(cmd)
    return o.read().decode("utf-8", "replace"), e.read().decode("utf-8", "replace")


def qj(sql: str):
    w = "SELECT coalesce(json_agg(row_to_json(t)),json_build_array()) FROM (" + sql + ") t"
    out, err = run('docker exec nous-postgres psql -U nous -d nous -tAc "' + w + '"')
    ln = next((l for l in out.splitlines() if l.strip().startswith("[")), None)
    if ln is None:
        print("ERR sql:", err[:600])
        return []
    return json.loads(ln)


id_list = ",".join(f"'{i}'" for i in IDS)
rows = qj(
    "SELECT id::text, name, domain, tags::text AS tags, description, "
    "goals::text AS goals, core_patterns::text AS core_patterns, "
    "core_tools::text AS core_tools, core_concepts::text AS core_concepts, "
    "implementation_notes::text AS implementation_notes, "
    "censor_ids::text AS censor_ids, related_procedures::text AS related_procedures, "
    "activation_count, success_count, failure_count, active, "
    "created_at::text AS created_at "
    f"FROM heart.procedures WHERE agent_id='{agent}' AND id IN ({id_list}) "
    "ORDER BY name, created_at"
)

# affinity rows for the same ids (to size the merge)
aff = qj(
    "SELECT procedure_id::text, frame_type, activation_count, success_count, failure_count, active "
    f"FROM heart.procedure_task_affinity WHERE agent_id='{agent}' AND procedure_id IN ({id_list}) "
    "ORDER BY procedure_id, frame_type"
)

# incident graph edges for the same ids
edges = qj(
    "SELECT id::text, source_id::text, source_type, target_id::text, target_type, relation "
    "FROM brain.graph_edges "
    f"WHERE agent_id='{agent}' AND ((source_id IN ({id_list}) AND source_type='procedure') "
    f"OR (target_id IN ({id_list}) AND target_type='procedure'))"
)

out = {"agent": agent, "procedures": rows, "affinity": aff, "edges": edges}
path = os.path.abspath("scripts/diag/_proc_bodies.json")
with open(path, "w", encoding="utf-8") as f:
    json.dump(out, f, indent=2, ensure_ascii=False)
print(f"WROTE {path}")
print(f"procedures: {len(rows)}  affinity rows: {len(aff)}  incident edges: {len(edges)}")
cli.close()
