# F078 — Correct-Side Censor Enforcement

**Status:** 📝 Draft v3 — **operator backward-compatibility constraint (2026-06-05)**; see "## v3" at the end (supersedes R2/R4). Then "## v2" (supersedes §1–§8).
**Author:** Tim + Claude
**Absorbs:** F068 (Censor Action Tiers — `steer | refuse | abort`; was draft, never shipped) — its tier vocabulary is folded in here.
**Depends on:** F031 (Censor Middleware with Action Payloads — shipped, PR #209)
**Decision:** forge `4c72c61f`. **Migration input:** `docs/reviews/censor-triage-2026-06-05.md`.

---

## Problem

A prod audit of `nous-default` (48 active censors, `scripts/diag/censor_audit.json`) shows the censor system **fires on the wrong side** and **halts the agentic loop on false positives**:

1. **Anti-hallucination censors match the wrong input.** ~10 of the 28 `block` censors were written to constrain the **model's output** (e.g. `delivered|was sent`, `can't do`, `psql|SELECT.*FROM`, `my skills`), but the only enforcing check is `pre_turn` against **user input** (`cognitive/layer.py:767`). So when *the user* says "did that email get sent?", the censor matches and the turn is replaced by a canned `"Blocked by censor: …"` — the agent can't even answer. This is the dominant false-halt.
2. **`block` halts instead of refusing.** When `block` fires, the LLM never runs (`layer.py:810-822`, `runner.py:400/958`); Tim gets a generic refusal with no path forward and re-prompts 2–3× to get around it. (F068's original motivation.)
3. **Silent auto-escalation.** A `warn` censor auto-escalates to `block` at `activation_count ≥ 3` (`censors.py:182`) with no human in the loop — a loose auto-created rule promotes itself into a turn-killer.
4. **The auto-learn loop produces dead clutter.** 19 of 20 `warn` censors have **never once matched** — they're English-prose triggers fed to `re.search` (from `monitor.py` tool-error learning + F039 correction extraction). Plus 2 test rows and a duplicate `rm -rf /`.
5. **`false_positive_count = 0` on all 48** — `record_false_positive` is unwired, so there is no signal to self-correct bad censors.

### Hard constraint (operator)
**F026 ActionGate is DISABLED in prod** (`NOUS_ACTION_GATING_ENABLED=false`) — it "blocked almost everything." F078 **must not depend on ActionGate**, and must **bias to advisory (steer) over blocking**, or it repeats the over-gating that got ActionGate turned off.

---

## Goals
1. **Fire on the correct side.** Output-shaping rules (anti-hallucination, preferences) become **non-blocking pre-turn directives**; input-gating rules (prohibitive actions, destructive) gate the inbound intent.
2. **Never halt the loop on a false positive.** A false match must degrade to a harmless extra directive, not a dead turn.
3. **Refusals stay in the LLM's hands** for non-catastrophic cases (F068).
4. **Auto-learned censors can never become turn-killers.** Provenance-capped to `steer`.
5. **Self-correcting.** Repeated false positives demote a censor; promotion to a harder tier is human-gated.
6. **No ActionGate dependency.** Self-contained enforcement.

## Non-goals (deferred)
- **F078.1 — per-tool-call action-scope.** Gate a *specific* tool call by argument (e.g. "decline `send_file` when recipient ∉ verified") rather than turn-level tool-strip. v1 uses F068's turn-level strip for `refuse`. (This is where ActionGate-style precision returns — done right this time, per-censor-scoped, not a global LLM gate.)
- **F078.2 — enforcing output-side check.** Actually intercept the model's *output* if it still violates a rule (regenerate/append). v1 relies on the pre-turn steer directive; output enforcement risks loops (see F068.4) and is deferred.
- **F078.3 — Haiku intent-confirm** before `refuse`/`abort` fires (cheap "does this input really violate `<reason>`?"). Schema leaves room; default off.

---

## Design

### 1. Tier × side model
Two tiers shape output (non-blocking); one gates input. The **tier implies the side**, so no separate `side` column is needed:

| Tier | Side | Behavior | Use for | False-match cost |
|---|---|---|---|---|
| **steer** | output-shaping | LLM runs. The censor's **directive** (`action_instruction`) + any `trigger_action` results are injected into the system prompt as guidance. **Never blocks.** | anti-hallucination ("verify before asserting"), verify-before-acting ("check recipient before send"), preferences, positive directives ("always answer Maya") | **benign** — one extra directive line |
| **refuse** | input-gate | LLM runs with a refusal directive; the **state-modifying tool denylist** (F068 §3, sourced from `execution_ledger` `WRITE_TOOLS ∪ EXTERNAL_TOOLS ∪ IRREVERSIBLE_TOOLS ∪ {bash}`) is stripped for the turn unless `refuse_keep_tools`. Model declines gracefully + may offer `suggested_alternative`. | genuinely prohibitive actions (never sell underwater, never lower exit_threshold to flush, decline autopilot-noise `record_decision`) | turn loses write tools (recoverable; LLM still answers) |
| **abort** | input-gate, pre-LLM | Hard cut before the LLM — canned refusal. | destructive only (`rm -rf /`) | turn dead (reserved for cases where that's correct) |

**Why this is the fix:** the false-halt came from output-intent rules matched on input as `block`. Moving them to `steer` makes a false match cost one harmless directive line instead of a dead turn. `refuse`/`abort` (which *can* block) are reserved for the few precise, prohibitive/destructive rules — biasing the whole system to advisory, as the ActionGate lesson demands.

### 2. `steer` mechanics (the workhorse)
On a `steer` match against user input:
1. Inject the censor's **directive** into the system prompt under a `## Active Guidance` block: `action_instruction` (preferred) or fall back to `reason`.
2. If `trigger_action` is set, execute it (F031 read-only executor, unchanged) and inject results too.
3. The LLM runs normally. The directive shapes the response (e.g. "you have previously falsely claimed delivery — verify before asserting").

Because a false `steer` match is benign, **steer matching may stay loose** (keyword/semantic). This is the robustness inversion: the common tier's failure mode is harmless.

### 3. Creation hardening
- **Provenance + max-tier cap.** New `provenance` field: `auto` (monitor tool-errors + F039), `agent` (`create_censor` tool), `human` (REST/operator). Enforce a cap at create *and* escalation time: `auto → max steer`, `agent → max refuse`, `human → abort`. An auto-learned censor **can never reach a halting tier.** (`monitor.py:172/378`, `correction_extractor.py:131` set `provenance="auto"`, force `tier="steer"`.)
- **Kill silent auto-escalation.** Remove the `activation_count ≥ threshold → promote` logic (`censors.py:182-193`). Promotion to a harder tier is a deliberate human action only. Replace the upward ratchet with **FP-driven demotion** (§4).
- **Validate at create, both paths.** `create_censor` tool (`tools.py:798`) and `_add` (`censors.py:84`) must validate: `trigger_pattern` compiles (`re.compile` in try/except), `unblock_pattern` compiles, `trigger_action.tool ∈ ALLOWED_TOOLS` (today only REST `PUT` validates — `rest.py:607`). Invalid → reject at the boundary, not silently dead at check time.

### 4. Self-correction (wire `false_positive_count`)
- Wire `record_false_positive` (exists, unused). Signal sources: user override/re-prompt immediately after a `refuse`/`abort` (heuristic), or explicit operator dismissal.
- **Auto-demote** on repeated FPs: `refuse → steer → inactive` past per-tier thresholds. Demotion is safe (less blocking); promotion is human-only.

### 5. Persisted audit (parity with CEL guardrails / F026)
Emit `nous_system.events` for actual enforcement outcomes — `censor_steered`, `censor_refused`, `censor_aborted`, `censor_demoted` — not just the existing match-level `censor_triggered`. So "what actually blocked / what self-demoted" is queryable. (CEL guardrails already do this; censors are `logger`-only today — G5.)

### 6. Schema migration
```python
# nous/heart/schemas.py
CensorAction = Literal["steer", "refuse", "abort"]   # was warn|block|absolute
```
```sql
-- migration NNN_censor_correct_side.sql  (constraint is ck_censors_action, models.py:618)
ALTER TABLE heart.censors ADD COLUMN provenance TEXT NOT NULL DEFAULT 'human'
  CHECK (provenance IN ('auto','agent','human'));
ALTER TABLE heart.censors ADD COLUMN refuse_keep_tools BOOLEAN NOT NULL DEFAULT false;  -- F068
-- action enum migrated by the data-migration script (§7), NOT a blind rename,
-- then the CHECK constraint is swapped to ('steer','refuse','abort').
```
The action remap is **not** a blind `warn→steer / block→refuse`; it is the per-censor triage (§7).

### 7. Data migration = the triage, dry-run first (NOT a rename)
Per `docs/reviews/censor-triage-2026-06-05.md`:
- **RETIRE 19** (dead prose, 2 tests, dup `rm -rf /`, broken trigger/reason) → `active=false`.
- **CONSOLIDATE 11→5** (Celsius, runner.sh, capability-claims, workspace-path; deliverables stay 3 distinct).
- **RE-TIER 16:** 10 → `steer`, 3 → `refuse`, 1 → `abort`, +2 deliverable steers.
- Script `scripts/migrate_censors_f078.py`: **`--dry-run` prints the full before/after table**; `--commit` applies under a transaction; reversible (retired rows kept, not deleted). **Run against prod ONLY after Tim reviews the dry-run** (hard-to-reverse prod write — gated).
- Provenance backfill: F039/`Auto-created` reason → `provenance='auto'`; `learned_from_decision IS NOT NULL` → `'agent'`; else `'human'`.

### 8. Code touch points
- `nous/heart/schemas.py` — `CensorAction` literal; add `provenance`; (directive reuses `action_instruction`).
- `nous/storage/models.py:618` — CHECK constraint via migration + `provenance`, `refuse_keep_tools` columns.
- `nous/cognitive/layer.py:767-846,1072-1110` — rewrite the censor block: `steer` → inject `## Active Guidance` (non-block); `refuse` → refusal directive + tool-strip (import denylist from `execution_ledger`, NOT ActionGate); `abort` → pre-LLM cut. Remove the output-side side-effecting `check()` call (G8) — output path becomes read-only/log or removed.
- `nous/heart/censors.py:84-121` (validate), `:182-193` (delete auto-escalation; add demotion), `_check` side-effects audited.
- `nous/cognitive/monitor.py:172,378` + `nous/handlers/correction_extractor.py:131` — set `provenance='auto'`, force `action='steer'`.
- `nous/api/tools.py:798` — `create_censor` validates + provenance='agent' + cap at refuse; expose `action_instruction`.
- `nous/api/rest.py:586` — provenance='human'; keep validation.
- `nous/cognitive/schemas.py` — `suggested_alternative` on `TurnResult` (F068).
- System-prompt rendering — surface new vocabulary where censors are listed.

---

## Rollout
1. **Phase 1 — schema + enforcement rewrite (one PR).** Migration (columns + constraint), tier behaviors (steer/refuse/abort), provenance cap, kill auto-escalation, validation, audit events. Snapshot test: a no-directive `steer` is a no-op; `refuse` invokes the LLM with directive+strip; `abort` unchanged from `absolute`.
2. **Phase 2 — data migration (gated).** Dry-run the triage on prod → Tim reviews → `--commit`. Sample-audit the first wave of `steer`/`refuse` activations.
3. **Phase 3 — self-correction + FP wiring** (can trail Phase 1/2).

### Risk
- Medium: `block→refuse/steer` changes behavior for the 28 real block censors — but that's the point; the triage makes each call explicit and the dry-run is reviewable.
- Low: `steer` false matches are benign; auto-cap prevents new turn-killers.

## Open questions (for review)
1. Is turn-level tool-strip for `refuse` (F068) acceptable for v1, or do the trading `refuse` censors need per-tool action-scope (F078.1) now to avoid stripping unrelated writes?
2. Should `steer` directives be deduped/budgeted in the system prompt (many steers matching one turn → prompt bloat)?
3. FP-demotion heuristic: is "user re-prompt after refuse" a reliable enough false-positive signal, or operator-only to start?
4. The SOL `record_decision` noise-filter (`4e697cb1`) — keep as `refuse`, or move to decision-admission (out of censor scope)?

---

## v2 — Post-review revisions (2026-06-05, 3-agent review)

Reviews: architecture (REWORK), devil's-advocate (P1 security), implementation (APPROVE-WITH-CHANGES). These resolutions supersede conflicting parts of §1–§8.

**R1 (P1) — `steer` must inject into `sections_by_tier`, not the flat `system_prompt`.** F036 cache-split (`NOUS_CACHE_SPLIT_SYSTEM_PROMPT=true`, default) makes `runner._build_system_prompt` (runner.py:1884-1918) build from `sections_by_tier` and **ignore** `turn_context.system_prompt`. So the existing F031 `## Censor-Injected Context` AND F078 steer would be silently dropped under default config. **PRE-EXISTING PROD BUG: F031 censor injection has been a no-op since F036 shipped** — worth fixing regardless of F078. Fix: inject a dynamic section into `sections_by_tier`; add a test asserting injected guidance reaches the API payload. [layer.py:636,849-854,896]

**R2 (P1) — migration is atomic + self-valid auto-SQL; the gated script does only judgment.** `056_censor_correct_side.sql` (idempotent, fresh-DB-safe): `DROP CONSTRAINT IF EXISTS ck_censors_action` → mechanical `UPDATE` (`block→refuse`, `warn→steer`, `absolute→abort`) → `ALTER COLUMN action SET DEFAULT 'steer'` → `ADD CONSTRAINT … CHECK (action IN ('steer','refuse','abort'))` → `ADD COLUMN IF NOT EXISTS provenance, refuse_keep_tools`. Update `models.py` CHECK (it's at **models.py:674**, not :618) + server_default + `schemas.py` `CensorAction` literal **in the same PR** (the ORM CHECK is enforced in SQLite test mode). `block→refuse` (not `→steer`) preserves enforcement through the window with no halts (refuse runs the LLM); the gated `scripts/migrate_censors_f078.py` then runs UNDER the new constraint and does only the triage judgment (retire 19 / consolidate / demote anti-hallucination to steer / set the 1 abort).

**R3 (P1) — retier the subtask-spawn gate (`tools.py:1922`) — REQUIRED touch-point (was missing).** Today it rejects on `"block"`; after the swap both branches are dead → zero censor enforcement on the background/subtask path (the exfil-incident path, `cc5e6284`). Fix: spawn gate **rejects on `abort` AND `refuse`**; `steer` injects its directive into the subtask task context (or logs). The `if not is_subtask` guard (layer.py:767) is benign for steer but the spawn gate is the only censor enforcement subtasks get — it must honor the new tiers.

**R4 (P1, security) — exfil censors are `refuse`, NOT `steer`.** Demoting `send.*email`/`sending email with sensitive data` to advisory steer re-enables the documented incident. Revised tiering: `cc5e6284`, `27dc4c8a` → **refuse**, so the retiered spawn gate (R3) **rejects** them on the autonomous path (restoring protection) and interactive declines + strips bash/send. Cost: interactive legit email is over-blocked when matched; the precise fix (allow verified recipient, block others) is **F078.1 per-tool recipient-scope = fast-follow** (not indefinitely deferred). *Operator note: elevate to `abort` if email-send should be hard-blocked.* Triage doc updated accordingly.

**R5 (P1, security) — `refuse`/`abort` demotion is OPERATOR-ONLY.** The re-prompt FP heuristic applies to `steer` only. An insistent user re-prompting a trading safety rule (`95c945d8`, `c58c6cf3`) must not auto-walk it down. Add an operator digest of "censors that keep matching" as the human-gated **promotion** signal (counters demotion-only erosion). Resolves open-Q3.

**R6 (P2) — the `refuse` tool-strip is the only net-new mechanism; spec it.** `absolute` is a no-op today (only block/warn are branched in layer.py), so `abort`=rename of block-halt and `steer`=rename of warn-inject (low risk); `refuse` (LLM-runs-but-strip-writes) is new. Wire: `TurnContext.refuse_active: bool` + denylist; `run_turn`/`_tool_loop` filter the tool schema before `_call_api` using `WRITE_TOOLS ∪ EXTERNAL_TOOLS ∪ IRREVERSIBLE_TOOLS ∪ {bash}` imported from `execution_ledger` (NOT ActionGate, NOT the existing whitelist `tool_filter`). Build last.

**R7 (P2) — `steer` directive budget + dedup (required).** ~13 steers + loose matching → prompt bloat that dilutes the security steers. Cap N directives/turn, dedup by censor-id/reason-hash, prioritize security-domain. [layer.py:848 has no cap today]

**R8 (P2) — mis-tiered irreversible rules flagged.** `git push.*main` and the runtime-path rules (`write to /root/nous/`, `cd /app/`) are left `steer` only because turn-level strip is too blunt; they're genuinely under-guarded until **F078.1** per-tool scope. Documented, not silently accepted.

**R9 (P2) — ReDoS is NOT closed by `re.compile`** (catastrophic backtracking compiles fine; the 10K bound limits length not time). Accept as pre-existing residual risk + recommend a per-match timeout (or `re2`) as hardening; don't claim compile-check closes it.

**R10 (P2) — provenance cap + validation live in ONE place: `CensorManager._add` (censors.py:84).** All create paths funnel through `add_censor→_add`. Callers only SET provenance (`monitor.py`×2 + `correction_extractor.py` → `auto`; `create_censor` tool → `agent`). `_add` enforces cap (`auto→steer`, `agent→refuse`, `human→abort`) + regex-compile validation + `trigger_action.tool ∈ ALLOWED_TOOLS`. **No POST /censors endpoint exists** (`rest.py:586` is PUT/update) — `human` provenance = migration default + direct DB only; a POST endpoint is out-of-scope.

**R11 (P3 corrections):** constraint at `models.py:674` (citations of `:618` were wrong); `abort` is NEW behavior not a rename (today `absolute` is a silent input no-op) — fix snapshot-test premise; keep legacy `created_by` for audit, `provenance` for the cap (retiring auto-escalation orphans the `auto_escalation` value — stop writing it, leave it); rework `_ESCALATION_ORDER`/`escalate()` (censors.py:26,491) to new vocab + human-gated + their tests; provenance backfill — **F039/`Auto-created` reason wins → `auto` even when `learned_from_decision` is set**.

### Implementation order (adopted from impl review)
1. Schema + ORM lockstep migration `056` (R2, R11).
2. Cap + validation + demotion in `_add`; set provenance at 3 callers; delete auto-escalation (R10, R11).
3. Enforcement rewrite — `steer` (→`sections_by_tier`, R1) + `abort` renames first; output-side check read-only; retier spawn gate (R3); **then** the `refuse` tool-strip wire last (R6).
4. Audit events + `record_false_positive` wiring (§4/§5, R5).
5. Tests — `postgres_only` migration test; convert ~10 `block` cases; steer-no-op, refuse-strips-tools, abort-halts, provenance-cap, regex-reject, injection-reaches-payload (R1).
6. Gated triage script with idempotent re-run guard.

### Verdict after revisions
Design holds; resolutions clear the P1s. **Genuine operator fork:** R4 (exfil = refuse + spawn-reject + F078.1 fast-follow — or elevate to `abort`). **Separately surfaced:** R1 is a pre-existing prod bug (F031 injection dropped since F036), fixable independently.

---

## v3 — Backward-compatibility constraint (2026-06-05)

**Operator hard rule: F078 must not break any currently-working path** — in particular the **daily scheduled tasks that send email**. Email is sent by agent-authored code (run_python/bash + `smtplib` using `NOUS_EMAIL_*` creds) — there is **no email-tool chokepoint** in `nous/` (verified: no `smtplib`/`send_email` in the package). This supersedes R2 and R4.

**BC-1 — email censors are `steer`, never `refuse`/`abort` (revises R4).** `refuse` strips bash/write tools; `abort` and the spawn-reject would kill the daily email subtasks on the autonomous path. The only non-breaking tier is `steer` (advisory, non-blocking). So `cc5e6284` / `27dc4c8a` → **steer**.

**BC-2 — the mechanical migration is functionally INERT: `block→steer`, `warn→steer`, `absolute→abort` (revises R2's `block→refuse`).** Mapping `block→steer` changes **no working path's behavior except to stop the false-halts** (blocks become advisory; wrongly-halted turns now run). **The migration creates NO new hard tier.** The few genuinely-prohibitive censors (trading → `refuse`; `rm -rf` → `abort`) are PROMOTED **only by the gated triage script after Tim reviews the dry-run** — human-gated, explicit, reversible. Net automatic behavior change = "blocks stop halting." Nothing new blocks. (`absolute→abort` is safe: `absolute` is a no-op on input today and the only case is `rm -rf /`.)

**BC-3 — exfil re-hardening is ADDITIVE (F078.1), not a block on the existing path.** The real fix = a guarded `send_email` tool (or SMTP wrapper) with a **recipient allowlist** (seeded from the daily tasks' recipients + Tim + Maya), added ALONGSIDE the working ad-hoc path; the steer directive points the agent to it; daily tasks migrate over time. Until then exfil protection is advisory (steer) — an explicit, accepted v1 gap, the price of the no-break rule. (Allowlist seed: audit prod `schedules` for daily email recipients.)

**BC invariant (whole feature):** every change is either (a) non-blocking (steer / directive / the F036 injection fix), (b) a human-gated promotion via the reviewed triage, or (c) additive (new guarded tool). **No automatic step removes or blocks a currently-working capability.** This is the acceptance test for every PR in F078.

### Net effect on the migration + triage
- Mechanical migration (auto-SQL `056`): `block→steer`, `warn→steer`, `absolute→abort`. Inert except halts→advisory.
- Gated triage script (post dry-run review): retire 19 dead, consolidate 11→5, and **promote** the explicit short-list to hard tiers — `refuse`: `95c945d8` (exit_threshold), `c58c6cf3` (sell-underwater), `4e697cb1` (record_decision noise); `abort`: `0b7dd037` (`rm -rf /`). **Email stays steer.** Everything else stays steer.
- Spawn-gate retier (R3) still rejects `refuse`/`abort` — but post-triage those are only the trading rules + `rm -rf`, which SHOULD reject on the autonomous path; **email (steer) passes**, so daily email subtasks are unaffected.
