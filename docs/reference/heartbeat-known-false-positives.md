# Heartbeat known false positives

Findings already diagnosed as recurring false positives can be closed at ingest instead of being triaged again. Put rules in the JSON file at `NOUS_HEARTBEAT_KNOWN_FP_PATH` (default `/tmp/nous-workspace/heartbeat/known_false_positives.json`); edits are picked up on the next tick, no restart needed. Start from [`docs/examples/known_false_positives.example.json`](../examples/known_false_positives.example.json). Each rule has:

| Field | Required | Meaning |
|-------|----------|---------|
| `id` | yes | Stable rule id, recorded on every finding it closes |
| `match.check` | one of the three | Exact check name (also matches agent-raised `agent:<check>:…` items) |
| `match.contains` | one of the three | Case-insensitive substring of the finding summary |
| `match.regex` | one of the three | Case-insensitive regex searched in the summary |
| `reason` | yes | Why this is a false positive |
| `added_by` | yes | Who added the rule |
| `review_by` | yes | `YYYY-MM-DD`. After this date the rule stops matching and logs one warning |

All `match` criteria given must hold. A `review_by` more than 90 days after the file is loaded makes the rule invalid (skipped with a warning), so no rule can be made permanent.

Nothing is hidden: auto-closed findings keep state `auto_closed_known_fp` and their rule id in `GET /heartbeat/findings`, and `stats.auto_closed_by_rule` counts them per rule. High-urgency or escalated findings are never auto-closed. A missing file means no rules. A malformed file keeps the last good rules and never stops the heartbeat.

See the `NOUS_HEARTBEAT_KNOWN_FP_PATH` row in [environment-variables.md](environment-variables.md) for the setting itself.
