# Censor Triage — prod `nous-default`, 48 active (2026-06-05)

Migration input for **F078** (correct-side censor enforcement). Source: `scripts/diag/censor_audit.json` (pulled from prod `heart.censors`).
Decision author: Claude (per Tim's "decide yourself first"). Tiers are F068's `steer | refuse | abort`; **SIDE** = where the rule should fire.

> **v2 (post-review, 2026-06-05):** the exfil censors **`cc5e6284` (`send.*email…`) and `27dc4c8a` (`sending email with sensitive data`) move steer → REFUSE** (F078 R4) — advisory steer was a security downgrade re-enabling the stranger-email incident; refuse makes the retiered subtask-spawn gate reject them on the autonomous path. Precise "allow verified recipient" is F078.1 fast-follow. (Operator may elevate to `abort`.)

## Disposition summary
| Disposition | Count | Meaning |
|---|---|---|
| **RETIRE** | 19 | dead prose-regex (never matched), test rows, exact dups, broken trigger/reason |
| **CONSOLIDATE** | 11 → 5 | overlapping rules merged into one clean censor each |
| **KEEP → steer** | 10 | output-shaping directive injected pre-turn (non-blocking) |
| **KEEP → refuse** | 3 | genuinely prohibitive action → LLM runs, declines, action stripped |
| **KEEP → abort** | 1 | destructive → hard cut before LLM |

Net: **48 → ~16 clean censors.** The dominant false-halt source — anti-hallucination block censors matched on *user input* — all become **steer** directives (fire on the output side, never block the turn).

---

## KEEP → abort (1) — destructive, command-scoped, pre-LLM hard cut
| id | trigger | rationale |
|---|---|---|
| 0b7dd037 | `rm -rf /` | genuine destructive floor. (Dup `6542fead` retired.) Scope: bash command. |

## KEEP → refuse (3) — prohibitive action; LLM runs + declines + relevant action stripped
| id | trigger | rationale |
|---|---|---|
| 95c945d8 | `exit_threshold.*(lower\|reduce…)` | Tim's hard rule: never lower exit_threshold to flush an underwater position. Decline the proposal. |
| c58c6cf3 | `sell.*underwater\|liquidate.*underwater…` | hard policy: never propose selling underwater. Decline; HOLD + notify. |
| 4e697cb1 | `SOL.*HOLD\|autopilot.*tick.*triage…` | record_decision noise-filter (369×): autopilot HOLD logs are not decisions. Decline the `record_decision` call. *(Better long-term home: decision admission — noted in F078 §deferred.)* |

## KEEP → steer (10) — output-intent directive injected pre-turn; **non-blocking**
| id | trigger | becomes directive |
|---|---|---|
| cc5e6284 | `send.*email\|smtp\|send_message` | "Before sending email, verify recipient against stored facts = Tim's verified address (Tfatykhov@gmail.com); never infer/generate a recipient." *(was block — would wrongly block legit sends)* |
| e81eab2d | `delivered\|was sent\|email (went\|was)…` | "Before claiming any deliverable was sent, verify subtask result non-empty + episode recorded + send tool_call event. Never equate status='completed' with delivered." |
| 7e49ca32 | `my skills\|registered skills\|my procedures…` | "Call a retrieval tool before enumerating system facts (skills/procedures/memory/tools/tasks)." |
| 0fb5c685 | `citing.*source\|according to.*study…` | "When generating citations, fetch + verify; do not assert sourced claims unverified." |
| 28fb60e8 | `can't do\|cannot do\|not able to…` | "Before claiming inability, check whether bash/a tool accomplishes it." (consolidation anchor — see C3) |
| 2c4da9f1 | `placeholder…\|successfully (delivered\|sent)…` | "Validate actual subtask output content, not completion status." |
| b0f01858 | `psql\|SELECT.*FROM\|INSERT INTO…` | "Before writing SQL, verify schema (`\dt`,`\d`); never assume table/column names." |
| 5532eeaa | `Goal.{0,5}Project Registry\|workstream.registry` | "Nous is not a dev tool — no project/workstream tracking constructs." |
| 1e166c30 | `maechkina@gmail\|email from Maya` | **positive directive** "Always respond to Maya's (maechkina@gmail.com) emails." *(was block — a positive rule must never block)* |
| f062c16e | `one-pager\|infographic\|slide…` | "Before finalizing a visual deliverable, verify no overlap/clipping, adequate padding." (consolidation anchor for deliverable-format — see C5) |

## CONSOLIDATE (11 → 5)
- **C1 Celsius:** `68c44c0f` (`\d+°F|Fahrenheit`, keep as anchor) ⊕ `4f1d5b03` (`°F`) → one steer "Output temperatures in °C; °F only as requested parenthetical." → **retire `4f1d5b03`**.
- **C2 runner.sh launch:** `d4850ecb` (`runner\.sh launch|launch.*claude.*job`, anchor → refuse/steer) ⊕ `d7b3bfe6` (`runner\.sh launch`) → one rule "Launch Claude Code jobs only via a DAG first node; verify no stale job; cancel old first." Tier **steer** (advisory; over-blocking launches is high-friction). → **retire `d7b3bfe6`**.
- **C3 capability-claims:** `28fb60e8` (anchor, steer) ⊕ `e738cd97` (`not implemented`) ⊕ `4e6c08df` (`feature is implemented`) ⊕ `c55db37f` (`claiming inability…`) ⊕ `c0993982` (`nous doctor`) → one steer "Before asserting a capability/feature is missing/unimplemented, verify against current code/tools." → **retire the 4 non-anchors.**
- **C4 workspace-path:** `7a90f414` (`/tmp/(?!nous-workspace)`, anchor → steer) ⊕ `525edc71` (`clone…`) ⊕ `e0b3535c` (`write to /root/nous/`) ⊕ `ebf97209` (`cd /app/…`) → one steer "Write/clone only under /tmp/nous-workspace/; never runtime paths (/app, /root/nous)." → **retire the 3 non-anchors.**
- **C5 deliverable-format:** `f062c16e` (anchor, steer) ⊕ `b18be236` (long-form→.docx) ⊕ `ebbb16d8` (speaking-notes ask-first) → keep `f062c16e` + **keep `b18be236` and `ebbb16d8` as distinct steers** (different intents: format-quality / channel / pre-questions). *No retire — these three stay as 3 steers; listed here only to note adjacency.*

## KEEP → steer (deliverables, from C5) (2 more)
| id | trigger | directive |
|---|---|---|
| b18be236 | `deliver(ing)?.*article\|long-form…` | "Long-form (1000+ words) → .md→.docx→send_file, then offer email; never paste into Telegram." |
| ebbb16d8 | `speaking notes\|one-pager…` | "Before speaking-notes/one-pager, ask target time + audience/venue." |

## KEEP → steer (operational, low-activation but legitimate) (3 more)
| id | trigger | directive / tier note |
|---|---|---|
| 657e6e8e | `git commit.*main\|git push.*main` | steer "Never commit/push to main; branch + PR." *(refuse candidate, but steer avoids stripping all git for a turn; revisit with per-tool scope in v2)* |
| 6b729aa8 | `F047.*Goal.*Registry\|F047.*Phase 2` | steer "F047 = Actionability Classification (shipped); 'Goal Registry/Phase 2/3' is stale — verify from docs." |
| 27dc4c8a | `sending email with sensitive data` | steer "Never include keys/passwords/tokens/private data in emails; clear subject; ≤5 emails/hr." *(prose trigger weak — re-author as a send-scoped directive)* |

## RETIRE (19) — dead/junk/dup/broken
| id | trigger | why |
|---|---|---|
| 6542fead | `rm -rf /` | exact dup of `0b7dd037` |
| 26eb60d3 | `Delivered. ✅` | trigger/reason mismatch (reason is about acknowledging arguments) — broken |
| 1e397355 / 119fa890 | `test pattern` ("Test") | test rows |
| 22f199a1 | `8b — SNN densification` | niche; covered by general "verify before listing pipeline phases" if ever needed |
| 1e56d7fc | `phase 8b` | dup of above; niche |
| 753a7746 | `notifying Tim unnecessarily` | never matched; notification discipline belongs in heartbeat config, not a text censor |
| 8b56e01a | `depending on which side of the Atlantic` | dead prose |
| 8d75c405 | `hallucinated venue attribution…` | dead prose; intent covered by C3/citations steer |
| 593711fd | `fabricating execution results…` | dead prose; covered by delivery/placeholder steers |
| a3ec99c0 | `emoji or symbols in academic…` | dead prose; re-author as a writing-skill steer later if wanted |
| b860c5d7 | `single flat table` | dead prose; vague |
| 2d336fde | `implies user defended…` | dead prose; voice-fidelity, vague |
| ec6335d7 | `everything I know about you` | dead prose; "don't fabricate user facts" — re-author as steer later if wanted |
| efa6f810 | `Nous doesn't have / Nous lacks…` | dead prose; meta |
| 6f1978db | `not host` | dead prose; hyper-niche |
| 91aebe98 | `heartbeat_check.*instead of dag` | never matched; orchestration preference — low value as a censor |
| e0b3535c, ebf97209, 525edc71 | (workspace dups) | retired via C4 |
| 4f1d5b03, d7b3bfe6, e738cd97, 4e6c08df, c55db37f, c0993982 | (consolidation non-anchors) | retired via C1/C2/C3 |

*(Retire count includes the consolidation non-anchors; net active after migration ≈ 16.)*

---

## Cross-cutting notes for F078
1. **The 10 steer conversions are the fix.** Every one was a `block` matched on user input; as a pre-turn directive it shapes output without halting. This single reclassification removes the dominant false-halt.
2. **`false_positive_count = 0` on all 48** — the FP signal is unwired; triage here is judgment-based. F078 must wire `record_false_positive`.
3. **Regex hygiene:** several triggers carry unicode (`→`), heavy alternations, lookaheads. Validate-on-write + ReDoS bound (10K input cap already exists, `censors.py:29`) carry forward.
4. **No new auto-junk:** F078's provenance cap (auto-created censors max `steer`) + retiring the dead F039 prose prevents the warn-table from refilling.
