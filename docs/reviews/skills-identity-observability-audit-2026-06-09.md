# Skills / Identity / Observability / Integrations — Deep Code Audit

**Date:** 2026-06-09
**Scope:** `nous/skills/`, `nous/identity/`, `nous/observability/`, `nous/integrations/` (every file read fully), with call-path tracing into `nous/main.py`, `nous/api/tools.py`, `nous/api/runner.py`, `nous/api/rest.py`, `nous/heart/heart.py`, `nous/heart/procedures.py`, `nous/cognitive/layer.py`, `nous/cognitive/context.py`, `nous/heartbeat/checks.py`, `sql/migrations/026_observability.sql`.
**Method:** Code-only. Reachability verdicts against `nous/config.py` defaults AND `.env.prod-snapshot`. F081 (PR #494) fixes verified at HEAD and NOT re-reported; residual gaps around them are.

Reachability legend:
- **LIVE** — executes on the default + prod-configured path.
- **LATENT** — reachable but requires a state/config precondition not currently true in prod.
- **INERT** — code wired but a flag/credential gate means it never runs in prod as configured.
- **DEAD** — no caller / unreachable branch.

---

## 1. How it actually works

### 1.1 Skills (`nous/skills/`)

Two ingestion paths converge on `heart.procedures`:

1. **Startup bootstrap** (`bootstrap.py::bootstrap_local_skills`, called from `main.py:466-472` every startup): scans `{workspace_dir}/skills/*/SKILL.md`, parses each with `SkillParser`, dedups by exact (case-insensitive) active name (`heart.get_procedure_by_name` → `procedures.py:925-945`), then checks the F081 Phase-0 supersession guard (`is_procedure_name_superseded`, `bootstrap.py:56`) before `store_procedure`. Per-skill failures are caught and warned (`bootstrap.py:65-66`); the whole call is wrapped in a `try/except` at `main.py:471-472` that logs only at DEBUG.
2. **`learn_skill` tool** (`api/tools.py:930-1017`, frames conversation/question/task → LIVE): fetches markdown from URL (`httpx` GET, `tools.py:949-955`), local path (`tools.py:956-964`), or inline; parses; gates `active` on `requires` env vars (`tools.py:969-972`); on name collision updates in place via `update_procedure_body` (preserves stats — audit bug 9 fix), else `store_procedure`.

`SkillParser.parse` (`parser.py:222-396`) does strict `---` frontmatter, then three lenient fallbacks (leading whitespace, fenced ```` ```yaml ````, missing closing `---`). The hand-rolled YAML subset (`parser.py:73-177`) handles scalars, inline lists, block lists, block dicts (F064.4 `hooks:`), and block scalars (`|`/`>`). F064.4 runtime fields (`concurrency_cap`, `timeout_override_seconds`, `hooks`, `requires_human_review`) are always parsed with fail-loud validation and always persisted to `procedures.runtime_metadata` (`parser.py:428-446`, `procedures.py:161,226`); the `NOUS_SKILL_RUNTIME_METADATA_ENABLED` flag (default `False`, `config.py:892`) gates only a v2 consumer that does not exist yet — `runtime_metadata` currently has **zero readers** (INERT by documented design).

**F081 verification at HEAD:** `to_procedure_input` persists the full `raw_content` body into `implementation_notes` with `first_section` only as empty-body fallback (`parser.py:409-416`) — the first-H2-only truncation is fixed on BOTH ingestion paths (bootstrap and learn_skill share `to_procedure_input`). NULL-embed is fail-loud with retry (`procedures.py:112-132`). Resurrection guards exist in bootstrap (`bootstrap.py:52-58`) and in `list_inactive_skills` (`superseded_by IS NULL AND archived_at IS NULL`, `procedures.py:884-892`). Residual gap: the supersession guard is bootstrap-only — see SK-1.

`reactivate_skills` (`bootstrap.py:74-104`, startup): re-activates inactive skill procedures whose `requires:*` env vars are now set; protected from unique-index aborts by the B6 clash guard (`procedures.py:802-819`).

### 1.2 Identity (`nous/identity/`)

`IdentityManager` (`manager.py`) versions six sections (`character`, `values`, `protocols`, `preferences`, `boundaries`, `environment`) in `agent_identity` with an `is_current` flag and a 60 s in-process TTL cache. `CognitiveLayer.pre_turn` loads sections each turn (`layer.py:601-605`), assembles them via `assemble_prompt` (`manager.py:273-283`) and passes the string as `identity_override` into `ContextEngine.build` (`layer.py:622`), where it becomes the `## Identity` section verbatim (`context.py:214-221`).

Initiation: tri-state `agents.is_initiated` (False → NULL=in-progress → True). `pre_turn` claims via atomic `UPDATE ... WHERE is_initiated = FALSE → NULL` (`manager.py:201-216`, `layer.py:402-417`); the winner gets the `initiation` frame whose tool set is hard-restricted to `store_identity` + `complete_initiation` (`runner.py:99`). `complete_initiation` requires `character` + `preferences` sections before `mark_initiated` (`tools.py:54-73`). `POST /reinitiate` resets everything (`rest.py:742-751`). An upgrade path auto-seeds identity from existing preference/person/rule facts at startup (`manager.py:218-271`, `main.py:98-106`). Prod is initiated, so the initiation path is LATENT; section CRUD + prompt injection are LIVE.

### 1.3 Observability (`nous/observability/`)

- **`ContextLogger`** (F035.4, LIVE — `context_log_enabled` default `True`, prod sets `NOUS_CONTEXT_LOG_FULL_PAYLOAD=true`): `AgentRunner._build_payload` logs every Anthropic API call (`runner.py:828-849`) — section split by marker (`context_logger.py:16-67`), len/4 token estimates, item-count heuristics, in-memory deque of 200 + per-session full-payload ring buffer (10/session, 50 total), and a fire-and-forget DB insert into `nous_system.context_log` (`main.py:540-569`). `update_response` back-fills actual usage **in memory only** (`context_logger.py:386-402`). REST exposes `/context/log*` (`rest.py:2579-2582`).
- **`BehaviorSnapshot` + `DriftDetector`** (F035.3, LIVE — `drift_detection_enabled` default `True`, registered at `main.py:649-656`): `BehaviorDriftCheck` (`heartbeat/checks.py:907-1044`) captures counts hourly, loads a 168 h baseline from `nous_system.behavior_snapshots`, runs z-score detection (`drift.py:40-63`, warn at |z|>k, alert at |z|≥3), emits heartbeat Findings, then stores the snapshot. Dashboard endpoints read the table (`rest.py:1478,2412-2470`). The wire is **not** fully severed — findings flow into the heartbeat triage pipeline — but half the monitored metrics are constant zero (OB-2).

### 1.4 Integrations (`nous/integrations/gdrive.py`)

Synchronous Google Drive v3 wrapper. OAuth (env `GDRIVE_CLIENT_ID/SECRET/REFRESH_TOKEN`, read straight from `os.environ`, not Settings) preferred over service account (`GOOGLE_SERVICE_ACCOUNT_JSON` base64 or `GOOGLE_SERVICE_ACCOUNT_PATH`). Lock-guarded token refresh before each call (`_ensure_valid_token`, `gdrive.py:168-190`); list/search/download/upload/update/delete/folder/health methods. Sole consumer is `DriveCheck` (`heartbeat/checks.py:756-899`), which is only registered when `settings.heartbeat_drive_enabled AND settings.google_service_account_json` (`main.py:645-646`). `.env.prod-snapshot` contains no `GDRIVE_*` or `GOOGLE_*` keys → the entire module is **INERT in prod**.

---

## 2. Findings register

### P1

#### SK-1 — `learn_skill` bypasses the Phase-0 supersession guard: re-learning a consolidated skill resurrects the archived duplicate
- **Severity:** P1 · **Reachability:** LIVE (tool registered for conversation/question/task frames)
- **Where:** `nous/api/tools.py:975-986` vs `nous/skills/bootstrap.py:52-58`; `nous/heart/procedures.py:896-916` (guard), `nous/heart/procedures.py:925-945` (`_get_by_name` filters `active = TRUE`)
- **Evidence:** `is_procedure_name_superseded` has exactly one caller in `nous/` — `bootstrap.py:56`. In `learn_skill`, the dedup probe `heart.get_procedure_by_name(manifest.name)` (`tools.py:975`) only matches **active** rows. A consolidated duplicate has `active=False, superseded_by=<canonical>`, so `existing` is `None` and the code falls through to `heart.store_procedure(proc_input)` (`tools.py:986`), creating a brand-new active row under the consolidated name. The migration-058 unique index is `WHERE active`, so the insert succeeds. This is the same B1 resurrection loop F081 closed for the bootstrap and reactivation paths — left open on the one path the agent itself drives (skills are routinely re-imported from URLs/marketplace).
- **Fix:** In `learn_skill`, when `existing is None`, check `await heart.is_procedure_name_superseded(manifest.name)`; if true, either refuse with a message pointing at the canonical procedure, or update the canonical procedure instead.

### P2

#### ID-1 — Initiation deadlock: abandoned/crashed initiation session leaves `is_initiated = NULL` forever; claim can never be re-won
- **Severity:** P2 · **Reachability:** LATENT (prod agent already initiated; bites every fresh deployment whose first conversation doesn't reach `complete_initiation`)
- **Where:** `nous/identity/manager.py:210-216` (claim sets NULL, predicate `is_initiated == False`), `nous/cognitive/layer.py:406-415` (fallback path), `nous/identity/manager.py:140-150` (`is_initiated()` maps NULL→False)
- **Evidence:** `claim_initiation` flips `False → NULL` ("in progress"). In SQL, `is_initiated = FALSE` does not match NULL, so once the claim is committed, **no later turn can ever re-claim**: every subsequent `pre_turn` sees `is_initiated()==False` (NULL→`bool(None)`), attempts the claim, loses, logs "initiation already claimed by another session" (`layer.py:415`) and proceeds with an empty identity, indefinitely. There is no TTL, no reclaim of stale NULL, and `complete_initiation` is only callable from inside the initiation frame that can no longer be entered. Recovery requires a manual `POST /reinitiate`, or a process restart **plus** pre-existing preference/person/rule facts (the `auto_seed_from_facts` side door, `main.py:98-106`). A user who simply walks away from the first conversation (session timeout, container restart, crash) bricks initiation.
- **Fix:** Make the claim reclaimable: `WHERE is_initiated IS NOT TRUE` plus a claimed-at timestamp with timeout (e.g., reclaim NULL older than 30 min), or revert NULL→False in `end_session` when initiation didn't complete.

#### OB-1 — `nous_system.context_log` and `nous_system.behavior_snapshots` grow unbounded; `context_log_retention_days` is a dead setting
- **Severity:** P2 · **Reachability:** LIVE (defaults on; prod on)
- **Where:** `nous/config.py:846` (setting defined), `nous/main.py:540-569` (one INSERT per API call), `nous/heartbeat/checks.py:999-1014` (snapshot INSERT/hour); zero `DELETE` statements against either table anywhere in `nous/` or `sql/`
- **Evidence:** `context_log_retention_days` is referenced exactly once in the codebase — its definition. `ContextLogger.log` runs on **every** Anthropic API call (`runner.py:828`), i.e., every tool-loop iteration of every turn, including background/heartbeat/subtask turns; each call inserts a row with a JSONB `token_breakdown` and `TEXT[]` tool names. `behavior_snapshots` adds ~24 rows/day. Neither sleep, session monitor, nor any migration prunes them. On a long-lived prod instance this is the largest unbounded write amplifier in the system after `events`.
- **Fix:** Consume the existing setting: a periodic `DELETE FROM nous_system.context_log WHERE timestamp < now() - interval ':days days'` in the session-monitor sweep or sleep cycle; same for `behavior_snapshots`.

#### OB-2 — Drift detection silently blind on 5 of its 11 monitored metrics: snapshot fields are never populated
- **Severity:** P2 · **Reachability:** LIVE (BehaviorDriftCheck registered by default and in prod)
- **Where:** `nous/observability/drift.py:26-38` (THRESHOLDS) vs `nous/heartbeat/checks.py:959-997` (`_capture_snapshot`)
- **Evidence:** `_capture_snapshot` populates only `fact_count(_delta)`, `episode_count(_delta)`, `active_censor_count(_delta)`, `procedure_count`, and the event-bus fields. The THRESHOLDS entries `admission_rate`, `facts_pruned`, `findings_created`, `episodes_compacted`, and `contradictions_resolved` therefore monitor a constant-zero series: `stdev == 0` → `continue` at `drift.py:51-52`, every cycle, forever. The whole admission/sleep/heartbeat block of `BehaviorSnapshot` (`snapshots.py:26-53`: `facts_admitted`, `facts_rejected_*`, `checks_run`, `findings_resolved`, `triage_sessions_opened`, `sleep_ran`, `turns latency`, `tool_calls`, `decision_count`, `interval_changes`) is write-only dataclass surface — never assigned by any producer. The drift feature claims coverage it does not have; an admission-rate collapse or a fact-pruning runaway (both historically real incidents) would never raise an anomaly.
- **Fix:** Either wire the producers (admission stats from Heart, sleep stats from sleep handler, heartbeat stats from the runner) or delete the dead THRESHOLDS entries and dataclass fields so the dashboard stops implying coverage.

#### GD-1 — DriveCheck registration gate ignores the OAuth credential set the module itself prefers
- **Severity:** P2 · **Reachability:** INERT in prod (no Google creds at all); bites any operator who configures the documented OAuth mode
- **Where:** `nous/main.py:645` (`if settings.heartbeat_drive_enabled and settings.google_service_account_json`), vs `nous/integrations/gdrive.py:70-91,137-143` (OAuth preferred, read from raw `os.environ`)
- **Evidence:** `GDrive.__init__` documents OAuth (`GDRIVE_CLIENT_ID/SECRET/REFRESH_TOKEN`) as the preferred mode ("preferred for uploads", auto-detected first). But the only consumer is registered solely on `settings.google_service_account_json` being non-empty (`config.py:800`). An operator who sets the three OAuth vars and flips `heartbeat_drive_enabled` (default already `True`, `config.py:811`) gets a silent no-op — no log line, no check. The OAuth vars are not even Settings fields, so there is nothing to gate on without a code change.
- **Fix:** Gate on "any credential set present": `settings.google_service_account_json or os.environ.get("GDRIVE_REFRESH_TOKEN")`, or surface the GDRIVE_* trio in Settings and check both.

#### SK-2 — Bootstrap registers skills with unmet `requires` as ACTIVE; `learn_skill` is the only path that gates on requirements
- **Severity:** P2 · **Reachability:** LIVE (bootstrap runs every startup against `/tmp/nous-workspace/skills`)
- **Where:** `nous/skills/bootstrap.py:60-61` (no requires check; `ProcedureInput.active` left `None` → `True` per `nous/heart/procedures.py:157`), vs `nous/api/tools.py:969-980` (learn_skill computes `skill_active` from env)
- **Evidence:** `bootstrap_local_skills` converts and stores the manifest without ever inspecting `manifest.requires`. `_store` defaults `active=True`. A disk skill requiring e.g. `SERPER_API_KEY` (unset) is registered active, ranked into retrieval/catalog, and offered to the agent as usable — it will fail at execution time. The companion `reactivate_skills` only moves skills inactive→active (`bootstrap.py:95-99`); nothing ever moves an active-but-unsatisfied skill the other way, so the asymmetry is permanent. `learn_skill` got this right (`proc_input.active = skill_active`, `tools.py:980`); bootstrap predates it and was never aligned.
- **Fix:** In bootstrap, mirror learn_skill: `missing = [v for v in manifest.requires if not os.environ.get(v)]; proc_input.active = not missing`.

### P3

#### SK-3 — UTF-8 BOM defeats every parse mode: `str.lstrip()` does not strip U+FEFF
- **Severity:** P3 · **Reachability:** LIVE (any Windows-Notepad-saved or BOM-emitting SKILL.md; bootstrap reads `encoding="utf-8"`, not `utf-8-sig`, `bootstrap.py:43`; URL fetch via `resp.text` can preserve BOM)
- **Where:** `nous/skills/parser.py:243-265` (all lenient fallbacks rely on `text.lstrip()`)
- **Evidence:** Verified: `'﻿'.lstrip() == '﻿'` and `'﻿'.isspace() is False` (Python 3.12). A file beginning `﻿---\n` fails the strict match (BOM at pos 0), and all three lenient retries re-lstrip the same string ineffectively → `ValueError: SKILL.md must start with YAML frontmatter` with a misleading message (the file *does* start with `---` visually).
- **Fix:** `text = markdown.lstrip("﻿")` (or `removeprefix("﻿")`) at the top of `parse()`.

#### SK-4 — YAML comment line inside a block list silently truncates the list
- **Severity:** P3 · **Reachability:** LIVE (any SKILL.md author using `# comment` inside `triggers:`/`frames:` lists)
- **Where:** `nous/skills/parser.py:124-175` (`_parse_frontmatter` line loop)
- **Evidence:** For `triggers:\n  - a\n  # note\n  - b`, the `  # note` line matches neither the list regex (`^\s+-\s+`) nor the dict regex (`^(\s+)(\w...)`), so the loop falls through to `_flush_block()` (`parser.py:152`) — `triggers=[a]` is committed and `current_key` cleared. The following `  - b` then matches the list regex but `current_key` is `None`, so the line is **silently dropped**. Same applies to blank-line-separated list items. No warning is appended.
- **Fix:** Skip lines whose stripped form starts with `#` (and blank lines) before the flush fall-through.

#### SK-5 — Bootstrap outer failure is logged at DEBUG: a fully broken skill subsystem is invisible at the default log level
- **Severity:** P3 · **Reachability:** LIVE (startup path; default `NOUS_LOG_LEVEL=info`)
- **Where:** `nous/main.py:471-472`
- **Evidence:** `except Exception: logger.debug("Skill bootstrap skipped or failed (non-fatal)")` — no `exc_info`, DEBUG level. If `heart.get_procedure_by_name` raises on the first skill (e.g., post-migration schema drift — exactly the migration-058 family that `_reactivate` had to defend against at `procedures.py:802-805`), bootstrap AND `reactivate_skills` both silently no-op every startup. The per-skill handler inside the loop (`bootstrap.py:65-66`) warns properly; only catastrophic failure is muted.
- **Fix:** `logger.warning(..., exc_info=True)`.

#### SK-6 — Inactive (non-superseded) skill + `learn_skill` re-import creates a second row of the same name
- **Severity:** P3 · **Reachability:** LIVE
- **Where:** `nous/api/tools.py:975` (`get_procedure_by_name` → active-only), `nous/heart/procedures.py:938`
- **Evidence:** A skill deactivated for unmet `requires` (`active=False`, `superseded_by` NULL, `archived_at` NULL) is invisible to the dedup probe. Re-learning the same skill (now with requires satisfied) inserts a **new** active row; the old inactive row remains. `reactivate_skills` will later try to reactivate the old row and only the B6 clash guard (`procedures.py:806-819`) prevents a unique-index abort — leaving permanent duplicate rows (one active, one inactive zombie). Embedding-based dedup is blind to this (cos 0.54-0.81 per the 2026-06-06 procedure audit).
- **Fix:** Probe should also look up inactive non-superseded rows by name and update those in place (re-activating), rather than insert.

#### ID-2 — Concurrent `update_section` race produces two `is_current=True` rows, which then bricks all future updates of that section
- **Severity:** P3 · **Reachability:** LIVE (REST PUT `/identity/{section}` + initiation tool + auto-seed can interleave; no row lock)
- **Where:** `nous/identity/manager.py:92-125` (read-then-write without `FOR UPDATE`), failure mode at `manager.py:102` (`scalar_one_or_none`)
- **Evidence:** Two writers both read the same `current` row, both set `is_current=False` on it, both insert version N+1 with `is_current=True` → two current rows. From then on, every `update_section` for that section raises `MultipleResultsFound` from `scalar_one_or_none()` — the section becomes permanently un-updatable until manual SQL repair. `get_current` also returns a nondeterministic winner (`{r.section: r.content}` overwrite, `manager.py:68`).
- **Fix:** `.with_for_update()` on the current-row select, or a partial unique index on `(agent_id, section) WHERE is_current`.

#### ID-3 — `auto_seed_from_facts` defeats `POST /reinitiate` across a restart
- **Severity:** P3 · **Reachability:** LATENT (requires reset + restart ordering)
- **Where:** `nous/main.py:98-106` (runs unconditionally every startup), `nous/identity/manager.py:229-263`
- **Evidence:** `/reinitiate` clears sections and `is_initiated=False` expecting the next conversation to run initiation. If the process restarts before that conversation (deploys do exactly this), `auto_seed_from_facts` sees empty identity + existing preference/person/rule facts (always true on a mature prod DB), seeds a `preferences` section, and calls `mark_initiated` — silently cancelling the operator's reset. The seeding path also cannot distinguish "fresh upgrade" from "deliberate reset".
- **Fix:** Persist a `reinitiate_pending` marker (e.g., the existing `status` control section) that suppresses auto-seed, or only auto-seed when the agent row has never been initiated (`is_initiated IS DISTINCT FROM FALSE` won't work post-reset; needs an explicit marker).

#### OB-3 — Actual token usage is never persisted: `context_log` columns `input_tokens_actual`/`output_tokens`/`cache_*`/`duration_ms`/`stop_reason` are always NULL
- **Severity:** P3 · **Reachability:** LIVE
- **Where:** `nous/main.py:544-566` (INSERT omits the columns), `nous/observability/context_logger.py:386-402` (`update_response` mutates only the in-memory entry), `sql/migrations/026_observability.sql:41-46` (columns exist)
- **Evidence:** The DB row is written fire-and-forget at request time, before the response exists; `update_response` (called at `runner.py:1202,1601`) never issues an UPDATE. The schema promises actuals; any SQL analysis of cache efficiency or real token spend over history silently gets NULLs. Only the in-memory deque (last 200 calls, lost on restart) has the data.
- **Fix:** Add a second db_writer hook in `update_response` (UPDATE by id), or drop the six columns.

#### OB-4 — `parse_system_sections` markers are missing three live section headers, mis-attributing their tokens to the preceding section
- **Severity:** P3 · **Reachability:** LIVE
- **Where:** `nous/observability/context_logger.py:16-35` vs `nous/cognitive/context.py:358,384,401,418`
- **Evidence:** ContextEngine emits `## Procedure Awareness` (context.py:384), `## Epistemic Routing` (context.py:401), and `## Cached Results` (context.py:418); none is in `SECTION_MARKERS`. Their lines are absorbed into whatever section preceded them — e.g., Procedure Awareness inflates `procedure_catalog`'s token estimate and the F079 catalog item counter (`_count_items("procedure_catalog", "- ")` counts any `- ` line in the absorbed text). Dashboard token-breakdown and delivery counters drift accordingly.
- **Fix:** Add the three markers.

#### OB-5 — Fire-and-forget DB write task is created without a strong reference (GC can cancel it mid-flight)
- **Severity:** P3 · **Reachability:** LIVE
- **Where:** `nous/observability/context_logger.py:376-382`
- **Evidence:** `loop.create_task(self._db_writer(entry))` discards the returned Task. Per the documented asyncio footgun, the event loop holds only a weak reference; under GC pressure the task can be collected before completion, dropping log rows nondeterministically (compounds OB-3's already-partial persistence).
- **Fix:** Keep a `set` of in-flight tasks with `task.add_done_callback(tasks.discard)`.

#### GD-2 — `GDrive._call` is dead; the retry/rebuild recovery it implements protects only `list_files`
- **Severity:** P3 · **Reachability:** INERT in prod (module unreachable), LATENT otherwise
- **Where:** `nous/integrations/gdrive.py:192-204` (defined, zero callers), `gdrive.py:259-263` (list_files has its own inline copy), all other methods (`download_file:292`, `upload_file:355-363`, `update_file:393-402`, `delete_file:412`, `create_folder:428-433`, `get_file_info:441-448`, `health_check:504`) call `.execute()` raw
- **Evidence:** The class docstring claims "Stale httplib2 connections are recovered via automatic service rebuild", but only `list_files` does. A stale-socket `BrokenPipeError` during `download_file`'s metadata `get` (`gdrive.py:292`) or any upload propagates uncaught. Note also `_TRANSIENT_ERRORS` (`gdrive.py:113`) ends with bare `OSError`, which makes the first three members redundant and would also retry non-transient `FileNotFoundError`/`PermissionError` raised inside the HTTP stack.
- **Fix:** Route all `.execute()` calls through `_call`, and narrow `_TRANSIENT_ERRORS`.

#### GD-3 — `upload_to_nousdrive` builds a Drive query with an unescaped subfolder name
- **Severity:** P3 · **Reachability:** INERT in prod, LATENT otherwise
- **Where:** `nous/integrations/gdrive.py:478` (`f"name = '{subfolder}' and ..."`), contrast `search_files` which escapes (`gdrive.py:276`)
- **Evidence:** A subfolder name containing `'` (e.g., "Tim's docs") produces a malformed query → HttpError; a crafted value can alter the query semantics (match arbitrary folders inside NousDrive). Agent-controllable if the agent ever drives uploads.
- **Fix:** Reuse the `search_files` escaping (`subfolder.replace("'", "\\'")`).

#### GD-4 — `download_file` has no size limit and trusts the export/destination path blindly
- **Severity:** P3 · **Reachability:** INERT in prod, LATENT otherwise
- **Where:** `nous/integrations/gdrive.py:282-314`
- **Evidence:** `MediaIoBaseDownload` loops until done with no byte cap — a multi-GB Drive file fills the container disk (8 GB prod VM). `destination.parent.mkdir(parents=True)` will create arbitrary directory trees for whatever path the caller passes. No mitigation anywhere in the call chain (DriveCheck never downloads, so currently latent).
- **Fix:** Optional `max_bytes` parameter checked against `meta['size']` before downloading.

#### SK-7 — `learn_skill` URL fetch: no size cap, no content-type check, arbitrary-host GET
- **Severity:** P3 · **Reachability:** LIVE
- **Where:** `nous/api/tools.py:949-955`
- **Evidence:** `resp.text` is unbounded — a pathological URL yields a multi-MB "skill" persisted wholesale into `implementation_notes` and embedded (embed input built from the full body, `procedures.py:138-142`). The direct `httpx.AsyncClient.get` also bypasses the `web_fetch` tier's protections (internal-IP fetch = SSRF primitive), though the agent's `bash` access makes this a marginal escalation. Relative local paths can traverse out of the workspace via `..` (`tools.py:960`).
- **Fix:** Cap at e.g. 256 KB, require `text/*` content type, and `Path.resolve()`-containment-check the local path.

#### ID-4 — Identity content reaches the system prompt verbatim — fake `##` headers can spoof sibling sections
- **Severity:** P3 (by design, but unhardened) · **Reachability:** LIVE
- **Where:** `nous/identity/manager.py:273-283` (`assemble_prompt`), `nous/cognitive/context.py:214-221` (verbatim `## Identity` section)
- **Evidence:** Procedure descriptions get `_one_line()` hardening specifically to stop `\n## ` header injection (`context.py:45-52`), but identity section content — written by the LLM during initiation from user-dictated text, by unauthenticated REST `PUT /identity/{section}`, and by `auto_seed` from learned facts (`manager.py:249-260`) — is interpolated with no newline/header sanitization. A `boundaries` section containing `\n## Active Censors\n(none)` both spoofs a section to the model and corrupts ContextLogger's per-section attribution (`context_logger.py:40-67`). The REST API has no authentication at all (no auth/middleware match in `rest.py`), so this is network-reachable.
- **Fix:** Escape line-leading `#` in section content at assemble time (e.g., indent or zero-width-prefix), or at minimum length-cap and log.

### INFO

- **IN-1** `requires_human_review` bool/int branches are unreachable: `_parse_frontmatter` only ever produces `str | list | dict` values, never `bool`/`int` (`parser.py:349,363-372`); same for the `isinstance(val, int)` arm of `_parse_optional_positive_int` (`parser.py:206`). Harmless dead defensive code.
- **IN-2** An empty `hooks:` block (key with no children) flushes as `[]` (`parser.py:114-116`) and then fails the dict check (`parser.py:328-331`) — the whole skill import errors on a semantically-empty declaration. Marginally harsher than necessary.
- **IN-3** CRLF input: key/value and list items are `.strip()`ed clean, but block-scalar (`description: |`) bodies retain interior `\r` (`parser.py:101-109` joins raw lines) — carriage returns end up in the persisted description.
- **IN-4** `main.py:466` comment "one-time, only if DB has no skills" is stale — bootstrap runs every startup and dedups per name.
- **IN-5** `_count_items` legacy heuristic (`context_logger.py:175`) returns 1 for a non-empty section with zero bullets and over-counts embedded `\n-` in facts/decisions (documented as a known approximation at `context_logger.py:180-187`).
- **IN-6** Prod runs `NOUS_CONTEXT_LOG_FULL_PAYLOAD=true`: complete system prompts + message histories (identity, censors, memory, user content) are retrievable via unauthenticated `GET /context/log/{id}/payload` (`rest.py:2354-2358`). In-memory only and ring-bounded, but it is the single most sensitive unauthenticated read in these subsystems.
- **IN-7** `BehaviorDriftCheck` deltas reset on process restart (`_last_snapshot=None` → delta 0, `checks.py:986-991`), slightly diluting the baseline after deploys. Cosmetic given OB-2.
- **IN-8** `GDrive` is fully synchronous; `DriveCheck` correctly wraps `list_files` in `asyncio.to_thread` (`checks.py:804`), but any future async caller using download/upload directly will block the loop. The hardcoded `NOUSDRIVE_FOLDER_ID` and "Ask Tim to re-authorize" string couple the module to one person's account.
- **IN-9** `reset_identity` invalidates the TTL cache *before* the (possibly caller-deferred) commit (`manager.py:193`) — a concurrent `get_current` can re-cache pre-reset rows for up to 60 s. Same staleness window applies to cross-process cache invalidation generally (each process has its own cache).

---

## 3. Dead-code inventory

| Item | Location | Note |
|---|---|---|
| `UPGRADE_INITIATION_PROMPT` | `nous/identity/protocol.py:59-75` | No caller in `nous/` (only tests). The upgrade path was implemented as `auto_seed_from_facts` instead; the prompt (and its `{existing_facts}` placeholder) is orphaned. |
| `GDrive._call` | `nous/integrations/gdrive.py:192-204` | Never invoked; its retry semantics exist only as an inline copy in `list_files`. |
| `import io`, `import time` | `nous/integrations/gdrive.py:28,34` | Unused imports. |
| `"status"` identity section | `nous/identity/manager.py:29` | In `VALID_SECTIONS`, excluded from prompts, never written anywhere — a control field with no controller. |
| `requires_human_review` bool/int parse arms | `nous/skills/parser.py:349,363-372` | Unreachable given `_parse_frontmatter`'s output types (also `_parse_optional_positive_int`'s int arm, `parser.py:206`). |
| `BehaviorSnapshot` unproduced fields | `nous/observability/snapshots.py:26-53` | `facts_admitted/rejected_*`, `admission_rate`, `checks_run`, `findings_created/resolved`, `triage_sessions_opened`, `interval_changes`, `sleep_ran`, `episodes_compacted`, `facts_pruned`, `contradictions_resolved`, `avg_turn_latency_ms`, `tool_calls`, `decision_count` — defined, defaulted, serialized, never assigned by any producer (see OB-2). |
| `context_log_retention_days` | `nous/config.py:846` | Setting with zero consumers (see OB-1). |
| `cache_break*` fields persistence | `nous/observability/context_logger.py:127-130` | Set by runner (`runner.py:844-847`), exposed nowhere in `to_dict()` nor DB — in-memory write-only except for direct attribute access in REST section endpoints. Documented "in-memory only", borderline. |

---

## 4. Improvement opportunities

1. **Unify the two skill ingestion paths.** Bootstrap and `learn_skill` have drifted (supersession guard, requires-gating, update-in-place). Extract a single `register_skill(manifest, *, source) -> outcome` in `nous/skills/` that both call; SK-1/SK-2/SK-6 all disappear structurally instead of patch-by-patch.
2. **Replace the hand-rolled YAML subset with `yaml.safe_load`.** PyYAML is already in the transitive dependency set of the dev stack; the parser has accumulated five lenient modes plus block-scalar/dict/list state machines and still mishandles comments, BOM, and CRLF block scalars. Keep the lenient frontmatter *extraction* regexes, delegate value parsing.
3. **Close the observability loop honestly.** Either wire producers for the dead drift metrics (admission stats are already computed by F023; sleep stats exist in the sleep handler) or prune them — a drift dashboard that shows flat zeros for `admission_rate` reads as "healthy", which is worse than absent. Same spirit as the repo's recurring severed-wire pattern (rubric loop, §14 telemetry).
4. **Add a retention sweep task** covering `context_log`, `behavior_snapshots` (and arguably `events`) keyed off the existing `*_retention_days` settings — one generic "table TTL" helper in the session monitor would serve all three.
5. **Initiation state machine**: replace the NULL-as-in-progress tri-state with an explicit `initiation_claimed_at TIMESTAMPTZ` — self-expiring claims, trivially observable, and removes the SQL-NULL-comparison trap that caused ID-1.
6. **gdrive**: if the Drive integration is to stay, surface `GDRIVE_*` in `Settings`, route everything through `_call`, and parameterize the NousDrive folder id; if it isn't (INERT in prod for months), consider moving it to `scripts/` to shrink the audited runtime surface.
