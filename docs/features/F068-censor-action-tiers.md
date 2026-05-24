# F068 — Censor Action Tiers (steer | refuse | abort)

**Status:** 📝 Draft (reconstructed from memory 2026-05-23, revised 2026-05-24)
**Proposed by:** Tim
**Original draft:** 2026-04-25 (lost; file was untracked and cleaned up)
**Renumbering history:** Originally drafted as F052, renumbered F053 (2026-04-25), then F067 — F067 was reassigned on 2026-05-24 to Episode Chunks + Parent-Episode Recall. This spec now lives at F068.
**Depends on:** F031 (Censor Middleware with Action Payloads — ✅ shipped via PR #209)
**Related:** F018 (Agent Identity), F024 (Critic Agent), Minsky *Society of Mind* Ch. 27

---

## Problem

Nous censors currently expose three actions — `warn`, `block`, `absolute` — defined in `nous/heart/schemas.py:16` and constrained at the DB level (`nous/storage/models.py:618`). After living with this vocabulary for ~6 months and shipping F031 (which gave warn-tier censors first-class middleware behavior), three problems are now visible:

1. **`warn` is a misnomer.** Post-F031, a "warn" censor is no longer just a logged warning — it can execute a `trigger_action`, inject context, attach an `action_instruction`, and steer the next turn. Calling that a "warning" misdescribes what the system actually does. New censor authors read "warn" and assume it's advisory; in reality it's the *normal* steering channel.

2. **`block` is too blunt and opaque to the user.** When `block` fires, the LLM never runs. Tim gets a generic refusal string (`"Blocked by censor: <reason>"`) with no path forward, no explanation tuned to the actual ask, and no ability for the model to offer a workable alternative. Empirically this produces a worse UX than letting the model handle the refusal itself — see the long tail of episodes where Tim re-prompts 2–3 times trying to get around a block that could have been refused-with-context on turn one.

3. **The two-tier soft/hard split conflates edge cases with hard limits.** Right now any rule that's "more serious than warn" becomes a `block`. That false equivalence puts `"don't paste a 5000-word article into Telegram"` in the same bucket as `"never exfiltrate API keys"`. The first deserves graceful in-context refusal; the second deserves an unconditional cutoff. Operators currently have no way to express the difference.

### What this is not

- **Not a re-architecture of the F031 middleware.** `trigger_action`, `action_instruction`, and `unblock_pattern` remain unchanged. F068 is a *vocabulary and semantics* refinement on top of F031, plus a small set of new behaviors for the `refuse` tier.
- **Not adding an `observe` / passive-logging tier.** Considered and rejected — the metrics layer already captures censor activations; a fourth tier would add cost without expressive gain.
- **Not changing how `abort` (formerly `absolute`) works at the pipeline cutoff layer.** The hard-cutoff tier keeps its exact current behavior — only the name changes.

---

## Goals

1. **Vocabulary that matches behavior.** Names should describe what the system *does*, not what it nominally logs.
2. **Refusals stay in the LLM's hands** for non-catastrophic cases. The model declines gracefully and offers an alternative — instead of being replaced with a canned string.
3. **`refuse` is a real safety boundary**, not a paper tiger. The LLM cannot perform the refused action under cover of explaining why it refuses.
4. **Backward compatible at the API surface.** Existing censors keep working. The migration is a renaming exercise plus a behavior upgrade for what was previously `block`.
5. **One migration window.** No long-lived "transitional" state where both vocabularies coexist in user-facing fields.

## Non-goals

- No new pipeline hook points beyond what F031 already added.
- No changes to `unblock_pattern` semantics.
- No per-domain or per-user tier overrides (deferred — see Deferred).
- No loop-prevention machinery in Phase 1 — see Section 4 for why.

## Deferred

- **F068.1 — Per-user tier overrides.** Let a single censor be `refuse` for one user and `steer` for another. Out of scope until we have multi-user.
- **F068.2 — `suggested_alternative` library.** A reusable bank of "did you mean…" alternatives the model can pull from. Phase 1 ships the schema field so the model can return *ad-hoc* alternatives; the library is a later optimization.
- **F068.3 — Auto-promotion.** Censors with high false-positive rates auto-demote `refuse → steer`. Needs F024 critic-agent feedback to be reliable.
- **F068.4 — Loop-prevention for post-turn enforcement.** Today `post_turn` is logging-only (`cognitive/layer.py:1058`) so the censor→refusal→re-match loop is architecturally impossible. When post-turn gains enforcement power, the `refusal_censor_ids` per-turn-skip mechanism described in the original F052 draft becomes necessary. Deferred until then.

---

## Design

### 1. New tier vocabulary

| Old action | New action | What the system does |
|---|---|---|
| `warn` | `steer` | LLM runs. Pipeline injects `action_instruction` and/or `trigger_action` results into the turn context (F031 middleware). The model is *steered*, not stopped. No tools removed. |
| `block` | `refuse` | LLM **still runs**, but with two changes: (a) a refusal directive is added to the system prompt; (b) all state-modifying tools — the union of `WRITE_TOOLS ∪ EXTERNAL_TOOLS ∪ IRREVERSIBLE_TOOLS` from `nous/cognitive/execution_ledger.py` — are stripped from the tool list for that turn unless the censor sets `refuse_keep_tools=true`. |
| `absolute` | `abort` | Unchanged behavior. Hard cutoff at the pipeline layer — LLM is never invoked. Reserved for safety-critical / destructive / exfiltration scenarios. |

**Naming rationale.** `steer | refuse | abort` describes what the system *does* on each tier, in active verbs:

- `steer` is honest about the no-payload case — a censor with no `trigger_action` and no `action_instruction` is a no-op steer, which is fine. (The previous draft's `redirect` overpromised here.)
- `refuse` is what the LLM does — the directive tells it to decline.
- `abort` is what the pipeline does — it halts before invoking the LLM. (Previously `absolute`, which described intensity but not action.)

The deletion of bare-string `block` is deliberate: anything that currently uses `block` either (a) actually wants `refuse` semantics (most cases) or (b) is so dangerous it should be `abort`. The migration step forces operators to make that choice explicitly per censor.

### 2. Why no `observe` tier

Considered. Rejected because:
- The metrics layer (`activation_count`, `last_activated`, `false_positive_count`) already gives passive observation for free on every existing tier.
- Adding `observe` would create a fourth code path with no behavioral effect, making the tier ladder harder to reason about.
- If logging-only is genuinely wanted, set `action=steer` with no `action_instruction` and no `trigger_action`. That's already the no-op case.

### 3. Refuse-tier mechanics

When a `refuse` censor matches user input:

1. Pipeline assembles a **refusal directive** for the system prompt:
   ```
   [REFUSAL CONTEXT]
   The user's request matched censor <id> (<reason>).
   You MUST decline this request in your reply.
   Offer a concrete value in the suggested_alternative field if one applies.
   Do not perform the requested action even if subsequent reasoning seems to permit it.
   ```
2. Pipeline strips state-modifying tools from the tool list passed to the LLM for this turn. The canonical denylist is the union of three sets defined in `nous/cognitive/execution_ledger.py`:

   - **`WRITE_TOOLS`** (line 36) — local writes, reversible. Currently: `write_file`, `learn_fact`, `record_decision`, `create_censor`, `store_identity`, `learn_skill`, `complete_initiation`, `spawn_task`, `schedule_task`, `cancel_task`, `run_python` (flagged at `:47` as state-modifying because it can call `learn_fact`), `heartbeat_check_manage`, `heartbeat_check_create`.
   - **`EXTERNAL_TOOLS`** (line 53) — external side effects. Currently: `send_file`. Extended as new notification/email tools land.
   - **`IRREVERSIBLE_TOOLS`** (line 58) — currently empty; reserved for future irreversible tools.

   Plus `bash`, which is classified per-command by `_READ_COMMANDS` allowlist (`execution_ledger.py:61`). For refuse-tier denylist purposes, treat **`bash` as always denied** — the refuse tier cannot reason about command intent at strip time.

   Read-only tools remain available: `recall_deep`, `recall_recent`, `read_file`, `get_procedure`, `web_search`, `web_fetch`, `cache_retrieve`, `list_tasks` (the `READ_TOOLS` set).

   **Pre-requisite — ledger gap.** As of 2026-05-24, `spawn_sync` (the typed inline counterpart to `spawn_task(await_result=True)`, added by F062) is **NOT** present in `WRITE_TOOLS`. This is a pre-existing F026 audit miss — `spawn_sync` should be in `WRITE_TOOLS` regardless of F068. The Phase 1 implementation **must** add `spawn_sync` to `WRITE_TOOLS` as part of the same PR (or in a precursor PR), otherwise F068's denylist has a hole.

   **Implementation note.** Reuse the three sets directly via:
   ```python
   from nous.cognitive.execution_ledger import (
       WRITE_TOOLS, EXTERNAL_TOOLS, IRREVERSIBLE_TOOLS
   )
   REFUSE_DENYLIST = WRITE_TOOLS | EXTERNAL_TOOLS | IRREVERSIBLE_TOOLS | {"bash"}
   ```
   Do not redefine the sets locally in the refuse-tier code path. Future state-modifying tools added to the ledger should automatically participate in the refuse strip.
3. If the censor sets `refuse_keep_tools=true` (default `false`), the strip step is skipped — useful for cases where the LLM *should* be able to take read-only follow-up action while refusing (e.g., logging the refusal to a fact, looking up policy).
4. LLM runs normally with full memory and the refusal directive in its system prompt.
5. The LLM's reply IS the refusal — phrased in context, with whatever `suggested_alternative` makes sense.

**Why strip tools by default.** Without this, a `refuse` censor that says "don't send email" can be circumvented by a model that includes the email-send tool call inside its refusal turn ("I won't send this email, but here's what it would have looked like 〔calls send_file〕"). Two concrete circumvention paths the denylist closes:

- `spawn_sync` / `spawn_task` — refusal could launch a subtask that performs the blocked action with its own tool budget
- `run_python` — refusal could execute arbitrary Python that calls `learn_fact` or another write inside the closure (`tools.py::run_python` exposes memory functions in scope)

The directive is a soft constraint; the tool strip is the hard one. **Sourcing the denylist from `STATE_MODIFYING_TOOLS` instead of a local list ensures new state-modifying tools cannot become accidental circumvention vectors when added** — the F026 execution-ledger contract becomes the safety boundary for F068's refuse tier too.

### 4. Why no `refusal_censor_ids` in Phase 1

The original F052 draft included a per-turn skip-list of censors that had already triggered `refuse` on this turn, to prevent a hypothetical loop where the model's refusal text retriggers the same censor and forces it to refuse the refusal.

**This loop cannot occur in current code.** `post_turn` (`cognitive/layer.py:1058`) is logging-only — it observes activations but does not interrupt the turn or issue a new prompt. `pre_turn` checks only `user_input`, not prior model output. The censor middleware in F031 fires exactly once per turn on the inbound user message.

The skip-list is forward-looking machinery for a problem we don't have. Shipping it now would be dead code with maintenance cost. Deferred to **F068.4** — implement when post-turn enforcement actually lands.

### 5. New schema field: `suggested_alternative`

The refuse-tier UX promise (LLM offers a workable alternative) requires a structured way for the model to return one. Phase 1 ships a new optional field on the turn-output envelope:

```python
# nous/cognitive/schemas.py
class TurnResult(BaseModel):
    # ... existing fields ...
    suggested_alternative: str | None = None  # populated when refusal directive fires
```

The model can reference this in its reply text and/or have it surfaced separately in UI integrations (Telegram could show it as an inline button later). F068.2 will turn this into a vocabulary the model picks from; Phase 1 leaves it free-form.

### 6. Schema migration

```python
# nous/heart/schemas.py
CensorAction = Literal["steer", "refuse", "abort"]
```

DB migration (`sql/migrations/NNN_censor_action_tiers.sql`):
```sql
-- The constraint is named ck_censors_action in nous/storage/models.py:618.
-- Verify before applying; this name was wrong in the F067 draft.
ALTER TABLE heart.censors DROP CONSTRAINT ck_censors_action;

UPDATE heart.censors SET action = 'steer'  WHERE action = 'warn';
UPDATE heart.censors SET action = 'refuse' WHERE action = 'block';
UPDATE heart.censors SET action = 'abort'  WHERE action = 'absolute';

ALTER TABLE heart.censors ADD CONSTRAINT ck_censors_action
  CHECK (action IN ('steer', 'refuse', 'abort'));

ALTER TABLE heart.censors ADD COLUMN refuse_keep_tools BOOLEAN NOT NULL DEFAULT false;
```

Migration is **forced and one-shot**. There is no transition period where both vocabularies are accepted — the goal is to never have to maintain two parallel code paths.

### 7. Code touch points

Concrete callsites that need updating (from current `grep`):

- `nous/cognitive/execution_ledger.py:36` — **add `spawn_sync` to `WRITE_TOOLS`** (precondition fix; closes a pre-existing F026 audit gap; required so F068's denylist reference covers it)
- `nous/heart/schemas.py:16` — `CensorAction` literal
- `nous/storage/models.py:618,649-650` — CHECK constraint (rewrite via migration) + new `refuse_keep_tools` column
- `nous/cognitive/layer.py:756,808,1065,1070` — four branches on `match.action == "block" | "warn"` → rename to `refuse | steer`, plus new refusal-directive injection + tool-strip in the `refuse` branch
- `nous/cognitive/schemas.py` — add `suggested_alternative: str | None` to `TurnResult`
- `nous/api/tools.py:1414,1433` — same action-name rename
- `nous/api/runner.py` — wire tool-strip when refusal directive is active for the turn (import `WRITE_TOOLS ∪ EXTERNAL_TOOLS ∪ IRREVERSIBLE_TOOLS ∪ {"bash"}` from `execution_ledger`)
- `nous/heart/censors.py:182` — `escalation_threshold` check (`action == "warn"`) → `action == "steer"`
- `nous/heart/censor_actions.py` — unchanged (F031 executor is tier-agnostic)
- Identity/system-prompt rendering — wherever censors are listed for the model to see, surface the new vocabulary

### 8. Interaction with F031 middleware

F031 fields are tier-neutral and keep their current semantics:

| Field | steer | refuse | abort |
|---|---|---|---|
| `trigger_action` | Executes, results injected | Executes, results injected into refusal context | Never executes |
| `action_instruction` | Injected as steering text | Appended to refusal directive | Ignored |
| `unblock_pattern` | N/A | If action_result matches, downgrade `refuse → steer`. No refusal directive fires; no tool strip. | N/A — abort is unconditional |
| `refuse_keep_tools` (new) | Ignored | If true, skip the tool-strip step | Ignored |

F031's existing "conditional unblock" mechanism continues to work — it just downgrades to `steer` instead of `warn`. **Open question 3 resolution:** when `unblock_pattern` triggers a downgrade, the refusal directive is not added and tools are not stripped. The censor's `id` is not recorded anywhere special — the downgrade means the refusal did not occur.

---

## Rollout

### Phase 1 — Schema + literal rename + refuse-tier behavior (one PR)
- Add migration with `refuse_keep_tools` column.
- Update `Literal` and mechanical rename of all action-name branches.
- Wire refusal-directive injection + action-tool stripping in the refuse branch.
- Add `suggested_alternative` to `TurnResult`.
- Snapshot test: existing `steer` censors (formerly `warn`) produce byte-identical behavior. Existing `refuse` censors (formerly `block`) now invoke the LLM with the refusal directive — this is the only intentional behavior change.

### Phase 2 — Operator audit
- One-time pass over every existing `refuse` censor (i.e. former `block`) to decide: is this really `refuse`, or should it have been `abort` all along? Document the call per censor.
- Sample-audit the new `refuse` outputs for the first week: are LLMs respecting the directive? Are `suggested_alternative` values useful?

### Migration risk
- **Low for `steer` censors.** Pure rename, no behavior change. Snapshot-testable.
- **Medium for `refuse` censors (former `block`).** Behavior changes from canned-string refusal to LLM-handled refusal. This is the entire point of the spec — but worth monitoring for the first wave of activations to confirm directives are obeyed.
- **None for `abort` censors.** Pure rename of `absolute → abort`.

---

## Open questions (now resolved)

1. ~~Single hard-coded refusal template vs per-censor `refusal_template` field?~~ **Resolved:** Phase 1 uses a single template (Section 3). Per-censor templates are F068.2 if the audit phase finds value in it.
2. ~~Persist `refusal_censor_ids` into episode metadata?~~ **Moot:** `refusal_censor_ids` is deferred to F068.4. When it ships, persistence for false-positive auditing is the right default.
3. ~~Stamp `refusal_censor_ids` when `unblock_pattern` downgrades?~~ **Resolved (Section 8):** No. Downgrade means the refusal did not occur.

---

## Provenance

This spec was reconstructed on 2026-05-23 from memory after the original draft file was discovered missing, then revised on 2026-05-24 in response to second-opinion review feedback and a stronger framing. Sources:

- Brain decision `7614dff0` — original tier-rename design (2026-04-25)
- Brain decision `2eb3070d` + `f3d82fdb` — F031 middleware design (shipped via PR #209)
- Brain decision `bfbc0cbf` — F052→F053 renumbering note (then F067, then F068)
- Heart fact `41884380` — UX rationale for tiered model
- Heart fact `1d40972e` — Minsky Suppressors/Censors/Correctors taxonomy
- Current code state: `nous/heart/schemas.py`, `nous/cognitive/layer.py:750–830`, `nous/api/tools.py:1411–1440`, `nous/storage/models.py:618`

### Revision history

- **2026-05-23** — F067 v1 spec written. Used `redirect | refuse | absolute` vocabulary.
- **2026-05-24 (round 1)** — Revised in response to second-opinion review (`job-20260523-190859-27ee88e5`) and codex round-1 P1 (`gh PR #440 comments`):
  - Renumbered F067 → F068 (F067 reassigned to Episode Chunks)
  - Renamed actions: `redirect | refuse | absolute` → `steer | refuse | abort` (active verbs that describe behavior)
  - Action-tool stripping added to `refuse` tier as the actual safety boundary, with `refuse_keep_tools` opt-out per censor
  - `suggested_alternative` field promoted from F067.2 deferred → Phase 1 schema
  - Migration constraint name fixed: `censors_action_check` → `ck_censors_action`
  - `refusal_censor_ids` loop-prevention moved to deferred F068.4 (the loop is architecturally impossible in current code)
  - Three open questions resolved inline
- **2026-05-24 (round 2)** — Codex round-2 P1 ×2 (PR #440, commit `0ddd590`):
  - `spawn_sync` was missing from the refuse denylist — typed inline counterpart to `spawn_task`, would have been a circumvention vector.
  - `run_python` was misclassified as read-only. It can call `learn_fact` and other writes via its scoped memory closures; `execution_ledger.py:47` marks it state-modifying. Moved to the denylist.
  - Initial fix attempt referenced a `STATE_MODIFYING_TOOLS` symbol — see round 3.
- **2026-05-24 (round 3)** — Codex round-3 P1 (PR #440, commit `06ad999` → next):
  - The `STATE_MODIFYING_TOOLS` symbol I referenced in round 2 **does not exist** in `nous/cognitive/execution_ledger.py`. The actual sets are `READ_TOOLS`, `WRITE_TOOLS`, `EXTERNAL_TOOLS`, and `IRREVERSIBLE_TOOLS` (line 24–58). Spec now correctly references the union `WRITE_TOOLS ∪ EXTERNAL_TOOLS ∪ IRREVERSIBLE_TOOLS ∪ {"bash"}`.
  - Discovered while fixing: `spawn_sync` is **also missing from `WRITE_TOOLS`** itself — pre-existing F026 audit gap. Spec now requires adding `spawn_sync` to `WRITE_TOOLS` as a precondition fix, otherwise the canonical-set denylist still has a hole. Added to the code-touch-points list.
  - `bash` is classified per-command by `_READ_COMMANDS` in the ledger; for refuse-tier purposes treat `bash` as always denied (cannot reason about command intent at strip time). Spec made this explicit.
