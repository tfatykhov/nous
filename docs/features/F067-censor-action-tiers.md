# F067 — Censor Action Tiers (redirect | refuse | absolute)

**Status:** 📝 Draft (reconstructed from memory 2026-05-23)
**Proposed by:** Tim
**Original draft:** 2026-04-25 (lost; file was untracked and cleaned up)
**Renumbering history:** Originally drafted as F052, renumbered F053 (2026-04-25), now F067 — both prior slots reassigned to other features.
**Depends on:** F031 (Censor Middleware with Action Payloads — ✅ shipped via PR #209)
**Related:** F018 (Agent Identity), F024 (Critic Agent), Minsky *Society of Mind* Ch. 27

---

## Problem

Nous censors currently expose three actions — `warn`, `block`, `absolute` — defined in `nous/heart/schemas.py:16` and constrained at the DB level (`nous/storage/models.py:618`). After living with this vocabulary for ~6 months and shipping F031 (which gave warn-tier censors first-class middleware behavior), three problems are now visible:

1. **`warn` is a misnomer.** Post-F031, a "warn" censor is no longer just a logged warning — it can execute a `trigger_action`, inject context, attach an `action_instruction`, and steer the next turn. Calling that a "warning" misdescribes what the system actually does. New censor authors read "warn" and assume it's advisory; in reality it's the *normal* steering channel.

2. **`block` is too blunt and opaque to the user.** When `block` fires, the LLM never runs. Tim gets a generic refusal string (`"Blocked by censor: <reason>"`) with no path forward, no explanation tuned to the actual ask, and no ability for the model to offer a workable alternative. Empirically this produces a worse UX than letting the model handle the refusal itself — see the long tail of episodes where Tim re-prompts 2–3 times trying to get around a block that could have been redirected on turn one.

3. **The two-tier soft/hard split conflates edge cases with hard limits.** Right now any rule that's "more serious than warn" becomes a `block`. That false equivalence puts `"don't paste a 5000-word article into Telegram"` in the same bucket as `"never exfiltrate API keys"`. The first deserves graceful redirection; the second deserves an unconditional cutoff. Operators currently have no way to express the difference.

### What this is not

- **Not a re-architecture of the F031 middleware.** `trigger_action`, `action_instruction`, and `unblock_pattern` remain unchanged. F067 is purely a *vocabulary and semantics* refinement on top of F031.
- **Not adding an `observe` / passive-logging tier.** Considered and rejected — the metrics layer already captures censor activations; a fourth tier would add cost without expressive gain.
- **Not changing how `absolute` works.** The hard-cutoff tier keeps its exact current semantics.

---

## Goals

1. **Vocabulary that matches behavior.** Names should describe what the system *does*, not what it nominally logs.
2. **Refusals stay in the LLM's hands.** When a censor judges an ask out of bounds but not catastrophic, let the model decline gracefully and offer an alternative — instead of replacing the model with a canned string.
3. **Loop-free.** A turn that was refused by censor C should not, in attempting to explain its refusal, retrigger C and recurse.
4. **Backward compatible at the API surface.** Existing censors keep working. Migration is a renaming exercise, not a behavior change for any individual censor (unless the operator opts in).
5. **One migration window.** No long-lived "transitional" state where both vocabularies coexist in user-facing fields.

## Non-goals

- No new pipeline hook points beyond what F031 already added.
- No changes to `unblock_pattern` semantics.
- No per-domain or per-user tier overrides (deferred — see Deferred).

## Deferred

- **F067.1 — Per-user tier overrides.** Let a single censor be `refuse` for one user and `redirect` for another. Out of scope until we have multi-user.
- **F067.2 — `suggested_alternative` library.** A reusable bank of "did you mean…" alternatives the model can pull from when issuing refuse-tier refusals. Phase 1 just lets the model generate one ad-hoc.
- **F067.3 — Auto-promotion.** Censors with high false-positive rates auto-demote `refuse → redirect`. Needs F024 critic-agent feedback to be reliable.

---

## Design

### 1. New tier vocabulary

| Old action | New action | Semantics |
|---|---|---|
| `warn` | `redirect` | LLM still runs. Pipeline injects `action_instruction` and/or `trigger_action` results into the turn context (F031 middleware). The model is *steered*, not stopped. |
| `block` | `refuse` | LLM **still runs**, but with a system-prompt directive: "*Decline this request and propose a `suggested_alternative`. Reason: <censor.reason>*". The model handles the refusal in natural language, in context, with full memory access. |
| `absolute` | `absolute` | Unchanged. Hard cutoff at the pipeline layer — LLM is never invoked. Reserved for safety-critical / destructive / exfiltration scenarios. |

The deletion of bare-string `block` is deliberate: anything that currently uses `block` either (a) actually wants `refuse` semantics (most cases) or (b) is so dangerous it should be `absolute`. The migration step forces operators to make that choice explicitly per censor.

### 2. Why no `observe` tier

Considered. Rejected because:
- The metrics layer (`activation_count`, `last_activated`, `false_positive_count`) already gives passive observation for free on every existing tier.
- Adding `observe` would create a fourth code path with no behavioral effect, making the tier ladder harder to reason about.
- If logging-only is genuinely wanted, set `action=redirect` with no `action_instruction` and no `trigger_action`. That's already the no-op steering case.

### 3. Refuse-tier mechanics

When a `refuse` censor matches user input:

1. Pipeline assembles a **refusal directive** for the system prompt:
   ```
   [REFUSAL CONTEXT]
   The user's request matched censor <id> (<reason>).
   You MUST decline this request in your reply.
   Offer a concrete suggested_alternative if one exists.
   Do not perform the requested action even if subsequent reasoning seems to permit it.
   ```
2. Pipeline stamps `refusal_censor_ids: list[UUID]` onto the turn's `TurnContext`.
3. LLM runs normally with full tools, full memory, and the refusal directive in its system prompt.
4. The LLM's reply IS the refusal — phrased in context, with whatever alternative makes sense.

### 4. Loop prevention via `refusal_censor_ids`

Without protection, this loop is possible:
- Turn N: User asks X → refuse censor C fires → model declines, explaining "I can't do X because of <reason>".
- Turn N+1 (post-turn censor check on the *reply*): the explanation re-matches C → model is asked to refuse its own refusal → ∞.

Prevention:
- `TurnContext.refusal_censor_ids` records which censors triggered refuse on this turn.
- Post-turn censor checks (`cognitive/layer.py:1065`) **skip** any censor whose ID is in `refusal_censor_ids` for that turn only.
- The skip is per-turn, not session-wide — a censor refused on turn N can still fire on turn N+2 if the user asks again.

### 5. Schema migration

```python
# nous/heart/schemas.py
CensorAction = Literal["redirect", "refuse", "absolute"]
```

DB migration (`sql/migrations/NNN_censor_action_tiers.sql`):
```sql
ALTER TABLE censors DROP CONSTRAINT censors_action_check;

UPDATE censors SET action = 'redirect' WHERE action = 'warn';
UPDATE censors SET action = 'refuse'   WHERE action = 'block';
-- 'absolute' rows unchanged

ALTER TABLE censors ADD CONSTRAINT censors_action_check
  CHECK (action IN ('redirect', 'refuse', 'absolute'));
```

Migration is **forced and one-shot**. There is no transition period where both vocabularies are accepted — the goal is to never have to maintain two parallel code paths.

### 6. Code touch points

Concrete callsites that need updating (from current `grep`):

- `nous/heart/schemas.py:16` — `CensorAction` literal
- `nous/storage/models.py:618,649-650` — CHECK constraint (rewrite via migration)
- `nous/cognitive/layer.py:756,808,1065,1070` — four branches on `match.action == "block" | "warn"` → rename to `refuse | redirect`, plus new refusal-directive injection in the `refuse` branch
- `nous/api/tools.py:1414,1433` — same rename
- `nous/heart/censors.py:182` — `escalation_threshold` check (`action == "warn"`) → `action == "redirect"`
- `nous/heart/censor_actions.py` — unchanged (F031 executor is tier-agnostic)
- Identity/system-prompt rendering — wherever censors are listed for the model to see, surface the new vocabulary

### 7. Interaction with F031 middleware

F031 fields are tier-neutral and keep their current semantics:

| Field | redirect | refuse | absolute |
|---|---|---|---|
| `trigger_action` | Executes, results injected | Executes, results injected into refusal context | Never executes |
| `action_instruction` | Injected as steering text | Appended to refusal directive | Ignored |
| `unblock_pattern` | N/A | If action_result matches, downgrade `refuse → redirect` (same as today's `block → warn`) | N/A — absolute is unconditional |

This means F031's existing "conditional unblock" mechanism continues to work — it just downgrades to `redirect` instead of `warn`.

---

## Rollout

### Phase 1 — Schema + literal rename (one PR)
- Add migration, update `Literal`, mechanical rename of all four code branches.
- Snapshot test: any existing censor's behavior on the same input is byte-identical after migration (modulo the `block → refuse` semantic upgrade).

### Phase 2 — Refuse-tier behavior change
- Wire the refusal directive into the LLM prompt path.
- Add `TurnContext.refusal_censor_ids` and the post-turn skip logic.
- Update the in-system censor documentation and identity context.

### Phase 3 — Operator audit
- One-time pass over every existing `refuse` censor (i.e. former `block`) to decide: is this really `refuse`, or should it have been `absolute` all along? Document the call per censor.

### Migration risk
- **Low.** No live data is lost; only the action label changes. The only behavioral change is `refuse` (formerly `block`) now invokes the LLM where it previously didn't — and that's the entire point of the spec.

---

## Open questions

1. Should the refusal directive be a single hard-coded template, or a per-censor `refusal_template` field? Phase 1 says template; F067.2 may make it configurable.
2. Should `refusal_censor_ids` persist into Heart episode metadata for later analysis, or stay in-memory per turn? Lean toward persisting — useful for false-positive auditing.
3. When `unblock_pattern` matches and `refuse` downgrades to `redirect`, do we still stamp `refusal_censor_ids`? Lean **no** — downgrade means the refusal didn't happen.

---

## Provenance

This spec was reconstructed on 2026-05-23 from memory after the original draft file was discovered missing. Sources:
- Brain decision `7614dff0` — original tier-rename design (2026-04-25)
- Brain decision `2eb3070d` + `f3d82fdb` — F031 middleware design (shipped via PR #209)
- Brain decision `bfbc0cbf` — F052→F053 renumbering note
- Heart fact `41884380` — UX rationale for tiered model
- Heart fact `1d40972e` — Minsky Suppressors/Censors/Correctors taxonomy
- Current code state: `nous/heart/schemas.py`, `nous/cognitive/layer.py:750–830`, `nous/api/tools.py:1411–1440`

A second-opinion review of this reconstruction is the next step before this spec is considered authoritative.
