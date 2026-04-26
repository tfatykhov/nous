@echo off
REM Interactive review of F050 hand-labeled qrels.
REM Just double-click this file or run `review_hand_labels.bat` from cmd.
REM
REM What it does:
REM   1. Reads OPENAI_API_KEY from .env in this directory
REM   2. Sets the prod-DB connection vars (for run-history persistence)
REM   3. Sets the eval-DB connection vars (for hybrid_search candidate lookup)
REM   4. Sets the fixtures dir + agent_id
REM   5. Launches the interactive review CLI
REM
REM Resume-able: rows you already reviewed are skipped on re-run.
REM Auto-saves after every row, so Ctrl-C is always safe.

setlocal EnableExtensions EnableDelayedExpansion

REM Make sure we're in the right directory (the repo root, where .env lives)
cd /d "%~dp0"

if not exist .env (
    echo ERROR: .env not found in %CD%
    echo This script must be in the nous repo root.
    pause
    exit /b 1
)

REM Read OPENAI_API_KEY from .env (key=value format, simple split on first '=')
set "OPENAI_API_KEY="
for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
    if "%%A"=="OPENAI_API_KEY" set "OPENAI_API_KEY=%%B"
)
if not defined OPENAI_API_KEY (
    echo ERROR: OPENAI_API_KEY not found in .env
    pause
    exit /b 1
)

REM Strip surrounding quotes if present (some .env files quote values)
if "!OPENAI_API_KEY:~0,1!"==^"  set "OPENAI_API_KEY=!OPENAI_API_KEY:~1,-1!"

REM --- Connection settings ---

REM Main Nous DB (prod) — used for run-history persistence to nous_system.eval_runs.
REM Optional: comment these out + add NOUS_EVAL_RUN_HISTORY_ENABLED=false below
REM if you don't want each review session to write to prod's eval_runs table.
set DB_HOST=192.168.1.141
set DB_PORT=5432
set DB_USER=nous
set DB_PASSWORD=nous_dev_password
set DB_NAME=nous

REM Eval scratch DB (local Docker) — for hybrid_search candidate lookup.
set NOUS_EVAL_DB_HOST=127.0.0.1
set NOUS_EVAL_DB_PORT=5433
set NOUS_EVAL_DB_USER=nous
set NOUS_EVAL_DB_PASSWORD=nous_eval
set NOUS_EVAL_DB_NAME=nous_eval_scratch

REM Where the qrels_hand_labels.jsonl file lives.
set NOUS_EVAL_FIXTURES_DIR=E:\Projects\nous-eval-fixtures\v2026-Q2

REM agent_id of the corpus loaded in the scratch DB (must match what ingest stamped).
set NOUS_EVAL_AGENT_ID=nous-eval-corpus

echo.
echo Running hand-labels review...
echo   fixtures: %NOUS_EVAL_FIXTURES_DIR%
echo   eval DB:  %NOUS_EVAL_DB_HOST%:%NOUS_EVAL_DB_PORT%/%NOUS_EVAL_DB_NAME%
echo.

uv run python -m nous_eval.hand_labels_review

echo.
echo --- Review session ended. ---
pause
