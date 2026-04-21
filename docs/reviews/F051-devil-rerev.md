# F051 Devil's Advocate Re-Review (v2) — nous-eval-devil-rerev

**Verdict:** APPROVE WITH MINOR REVISIONS — all 11 original P1s addressed at concept level; 4 small clarifications recommended.
**Decision ID:** `0a22d40d`
**Reviewer agent_id:** `nous-eval-devil-rerev`
**Date:** 2026-04-20
**Targets:** `docs/superpowers/plans/2026-04-20-f051-retrieval-eval-harness.md` (v2) vs prior review `docs/reviews/F051-devil-review.md` (`f5b5006e`)

## P1 scorecard

| ID | Issue | Score | Notes |
|---|---|---|---|
| P1-1 | Phantom `session_id` kwarg | ✅ | Pipeline refactor drops kwarg entirely; uses `types=None, session=None`. |
| P1-2 | Decisions not searched | ✅ | `run_recall_pipeline` mirrors `tools.py::recall_deep` including `brain.query()`. |
| P1-3 | RuntimeConfig bleed | ✅ | `RuntimeConfig.reset()` at line 328. Verified via grep: `load_from_db` only called from `main.py:58`, never from `Heart.__init__`, so eval Heart cannot repopulate `_overrides`. CE model singleton is weight cache only, harmless. |
| P1-4 | pgvector image | ✅ | Dockerfile.eval-db both stages use `pgvector/pgvector:pg17` (lines 509, 518). Note: spec §9 still shows `FROM postgres:17` — plan supersedes but spec drift remains (P3 doc bug). |
| P1-5 | Named-volume staleness | ⚠️ | Mechanism described in prose but no concrete file location for the `nous_eval_meta.fixture_version` startup check. Implementer should add it to `retrieval.py` startup with explicit error path. |
| P1-6 | CRLF on .sh | ✅ | `.gitattributes` at repo root with `*.sh text eol=lf`. |
| P1-7 | eval_runs INSERT block | ✅ | `asyncio.wait_for(..., timeout=5.0)`; covered by silent-failure test. |
| P1-8 | Random embeddings | ⚠️ | Plan says "captured once via actual embedding call" but no capture script specified. Add a `scripts/regen_smoke_embeddings.py` reference or provenance script in §D.9 so the tests agent doesn't improvise. |
| P1-9 | Security (port + password) | ✅ | `127.0.0.1:5433:5432` in both spec §10 and plan §C.3 + `warn_if_default_password()`. |
| P1-10 | `NOUS_PROD_DB_*` undeclared | ✅ | Fail-fast before any query; covered by `test_missing_prod_db_env_fails_fast`. |
| P1-11 | Ingest event-bus cascade | ✅ | All 9 background flags disabled in `_settings_for_eval_db` (lines 360–369). List matches CLAUDE.md. |

## New v2 findings

- **A (refactor snapshot test):** `test_recall_deep_text_format_unchanged` doesn't say HOW the snapshot is captured. Recommend: capture pre-refactor output to `tests/fixtures/recall_deep_text_snapshot.txt` as a **separate commit before** the refactor commit, so the diff is reviewable. ⚠️
- **B (`load_from_db` skip):** Verified non-issue. `_build_heart_for_eval` does not import `nous.main`, so `RuntimeConfig.load_from_db` never runs against the eval DB. ✅
- **C (smoke corpus type coverage):** §D.9 says "10 synthetic items" but doesn't enumerate per type. Pipeline has 5 branches (fact/decision/episode/procedure/censor); smoke must include ≥1 of each to exercise all branches. Specify e.g. "5 facts, 2 decisions, 2 episodes, 1 procedure". ⚠️
- **D (`rebuild` volume scope):** `tasks.py::_rebuild` runs `docker compose --profile eval down -v` THEN `docker volume rm -f nous_eval_db_data`. The `-v` flag is redundant and on older Compose v2 (<2.20) may sweep top-level volumes including `pgdata` (the main nous DB). Recommendation: drop `-v` from the `down` call; rely on the targeted `docker volume rm`. ⚠️

## Verdict

**APPROVE WITH MINOR REVISIONS.** All 11 original P1s adequately addressed. 4 new clarifications are non-blocking but should land before implementation dispatch (P1-5 location, P1-8 capture script, C type coverage, D `-v` removal). Plan v2 is ready for impl with these tweaks.
