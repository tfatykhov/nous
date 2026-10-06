# F099 Phase 2: lead rulings on the decomposition and contract (2026-10-06)

The contract in `2026-10-06-f099-phase2-contract.md` is binding. Every sub-PR plan copies its names and signatures from it. Where this file and the contract disagree, this file wins.

## Sub-PR order

- 2a (enforcement) and 2b (data and routing) both depend only on PR-1, so they are planned in parallel.
- 2c (runner) follows 2a and 2b. 2d (proposals and owner actions) follows 2c. 2e (cancel and the flag gate) follows 2d.
- 2f (A2UI cards) is deferred. Telegram and REST approval paths are enough for v1.

## Flag gate

`nous/brain/continuation.py:CONTINUATION_RUNNER_READY` starts `False` in 2b. While it is `False`, `main.py` forces `continuation_enabled` off with a WARNING, and a test pins this. 2e flips it to `True`. Nothing else may flip it.

## Open questions: all four recommended answers are accepted

1. The owner-answer route addresses the **question row** (its `report_id`), not an intention. A root can hold two `awaiting_owner` arrivals at once.
2. `none` and `remember` intentions close as `delivered` only when `NOUS_CONTINUATION_ENABLED` is on. With it off they close as `legacy`, which keeps the Phase 1 pins valid.
3. The spec's `owner_approved` authority is `ContextKind` `approved_action` with `declared_tools=(tool,)`. No third value is added to `AUTHORITIES`.
4. TTL and limits reach `prepare_intention` through `IntentionSpec.ttl_hours` and `IntentionSpec.limits`. The eight spawn sites fill them via `intentions.ttl_for(settings)`.

## Flag-off change accepted for 2a

Narrowing is enforced on `ctx.authority == "internal_only"` with no flag gate. Phase 1 writes no `internal_only` rows. The only flag-off behaviour change is that a turn whose lineage stamp is damaged (and which therefore fails closed to `internal_only`) loses its non-allowed tools. That is the intended fail-closed behaviour. 2a's PR description names it.

## Models (owner, 2026-10-06)

| Role | Model |
|---|---|
| Plan writers | Sonnet 5.5 |
| Implementers | Opus 5.5 |
| Reviewers (plan, task, final) | Fable 5.1 |

## Base

Plans are written against `main` at `1c10ed8d` (PR-1 merged).

## 2a contract conflicts (rulings on plan-2a's C1–C10)

- **C1 accepted.** Add `internal_only` to `REFUSAL_CODES`, so that `record_blocked` does not raise.
- **C2 accepted.** The strict internal_only refusal records `mode: "enforce"`. This is what happened (the call was refused), and the harness dashboard already words it correctly, so no dashboard change is needed. If wrong, this costs a dashboard-wording follow-up.
- **C3 accepted.** The cancelled-root check stays in 2e (contract §1.5).
- **C4–C10.** The plan's recommended resolutions are accepted unless the plan review finds a defect in one.
