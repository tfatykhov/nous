#!/bin/bash
# ============================================================
# Claude Code Job Runner — F054 v2
# Launches Claude Code tasks in ISOLATED WORKTREES per job
# Every job gets its own git worktree — safe for concurrency
# Runs as 'claude-runner' user to allow --dangerously-skip-permissions
# Usage: runner.sh <action> [args]
#   runner.sh launch <repo> "<prompt>" [--model X] [--effort X] [--on-complete notify|cleanup|notify+cleanup]
#   runner.sh status <job_id>
#   runner.sh list [--active|--all]
#   runner.sh result <job_id>
#   runner.sh cancel <job_id>
#   runner.sh cleanup <job_id>
# ============================================================

set -euo pipefail

JOBS_DIR="/tmp/nous-workspace/claude-jobs"
REPOS_FILE="$JOBS_DIR/repos.json"
MAX_CONCURRENT=3
RUN_USER="claude-runner"

log() { echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] $*"; }

get_repo_path() {
  python3 -c "
import json, sys
with open('$REPOS_FILE') as f:
    repos = json.load(f)
r = repos.get('repos', {}).get('$1')
if r: print(r['path'])
else: sys.exit(1)
" 2>/dev/null
}

get_repo_default() {
  python3 -c "
import json
with open('$REPOS_FILE') as f:
    data = json.load(f)
repo_cfg = data.get('repos', {}).get('$1', {}).get('defaults', {})
global_cfg = data.get('global_defaults', {})
val = repo_cfg.get('$2', global_cfg.get('$2', ''))
print(val)
" 2>/dev/null
}

count_active() {
  local count=0
  for d in "$JOBS_DIR"/job-*/; do
    [ -d "$d" ] || continue
    [ -f "$d/status" ] && grep -q "running" "$d/status" && count=$((count + 1))
  done
  echo "$count"
}

generate_job_id() {
  local ts=$(date -u '+%Y%m%d-%H%M%S')
  local rand=$(head -c 4 /dev/urandom | od -An -tx1 | tr -d ' \n' | head -c 8)
  echo "job-${ts}-${rand}"
}

do_launch() {
  local repo="$1"; shift
  local prompt="$1"; shift
  local model="" effort="" on_complete=""

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --model)       model="$2"; shift 2 ;;
      --effort)      effort="$2"; shift 2 ;;
      --on-complete) on_complete="$2"; shift 2 ;;
      *) log "Unknown arg: $1"; shift ;;
    esac
  done

  if [ -n "$on_complete" ]; then
    case "$on_complete" in
      notify|cleanup|notify+cleanup) ;;
      *) log "ERROR: --on-complete must be notify, cleanup, or notify+cleanup"; exit 1 ;;
    esac
  fi

  local repo_path
  repo_path=$(get_repo_path "$repo") || { log "ERROR: Unknown repo '$repo'"; exit 1; }

  [ -z "$model" ]  && model=$(get_repo_default "$repo" "model")
  [ -z "$effort" ] && effort=$(get_repo_default "$repo" "effort")

  local active
  active=$(count_active)
  if [ "$active" -ge "$MAX_CONCURRENT" ]; then
    log "ERROR: $active jobs already running (max $MAX_CONCURRENT). Wait or cancel one."
    exit 1
  fi

  local job_id
  job_id=$(generate_job_id)
  local job_dir="$JOBS_DIR/$job_id"
  mkdir -p "$job_dir"

  # --- WORKTREE SETUP ---
  # Always create an isolated worktree for each job
  local short_id="${job_id##*-}"
  local worktree_branch="claude-job-${short_id}"
  local worktree_path="$job_dir/worktree"

  log "Creating worktree at $worktree_path (branch: $worktree_branch)"
  cd "$repo_path"
  git fetch origin main 2>/dev/null || true
  git worktree add "$worktree_path" -b "$worktree_branch" origin/main 2>&1 || {
    log "ERROR: Failed to create worktree"
    rm -rf "$job_dir"
    exit 1
  }
  # Ensure claude-runner can write to it
  chown -R "$RUN_USER":"$RUN_USER" "$worktree_path"
  log "Worktree created successfully"

  cat > "$job_dir/config.json" <<EOF
{
  "job_id": "$job_id",
  "repo": "$repo",
  "repo_path": "$repo_path",
  "worktree_path": "$worktree_path",
  "worktree_branch": "$worktree_branch",
  "model": "$model",
  "effort": "$effort",
  "on_complete": "$on_complete",
  "created_at": "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
}
EOF

  printf '%s' "$prompt" > "$job_dir/prompt.md"
  echo "running" > "$job_dir/status"
  date -u '+%Y-%m-%dT%H:%M:%SZ' > "$job_dir/started_at"

  local sys_prompt="You are working as a sub-agent of Nous. Repo: $repo. You are in an isolated git worktree on branch '$worktree_branch'. Make your changes, commit, and push the branch. Then create a PR to main. HARD RULE: Never commit to main."

  # Build the run script — runs in WORKTREE, not main repo
  cat > "$job_dir/run.sh" <<RUNEOF
#!/bin/bash
cd "$worktree_path"
unset CLAUDE_CODE_OAUTH_TOKEN
export ANTHROPIC_API_KEY="\$ANTHROPIC_AUTH_TOKEN"
claude --print --output-format json --model $model --dangerously-skip-permissions --no-session-persistence --append-system-prompt "$sys_prompt" "$prompt" > "$job_dir/output.json" 2> "$job_dir/stderr.txt"
exit_code=\$?
if [ \$exit_code -eq 0 ]; then
  echo "completed" > "$job_dir/status"
else
  echo "failed" > "$job_dir/status"
  echo "\$exit_code" > "$job_dir/exit_code"
fi
date -u '+%Y-%m-%dT%H:%M:%SZ' > "$job_dir/finished_at"

# --- ON-COMPLETE CALLBACK ---
on_complete=\$(python3 -c "import json; print(json.load(open('$job_dir/config.json')).get('on_complete',''))" 2>/dev/null || echo "")
if [ -n "\$on_complete" ]; then
  # Extract PR URL from output.json if present
  pr_url=""
  if [ -f "$job_dir/output.json" ]; then
    pr_url=\$(python3 -c "
import json, re, sys
try:
    data = json.load(open('$job_dir/output.json'))
    text = json.dumps(data)
    m = re.search(r'https://github\.com/[^\s\"]+/pull/[0-9]+', text)
    if m: print(m.group(0))
except: pass
" 2>/dev/null || echo "")
  fi

  case "\$on_complete" in
    notify|notify+cleanup)
      final_status=\$(cat "$job_dir/status" 2>/dev/null || echo "unknown")
      python3 -c "
import json
data = {
    'job_id': '$job_id',
    'status': open('$job_dir/status').read().strip(),
    'finished_at': open('$job_dir/finished_at').read().strip(),
    'pr_url': '\$pr_url'
}
json.dump(data, open('$job_dir/completed.signal', 'w'))
" 2>/dev/null || true
      ;;
  esac

  case "\$on_complete" in
    cleanup|notify+cleanup)
      repo_path=\$(python3 -c "import json; print(json.load(open('$job_dir/config.json')).get('repo_path',''))" 2>/dev/null || echo "")
      worktree_path="$job_dir/worktree"
      branch=\$(python3 -c "import json; print(json.load(open('$job_dir/config.json')).get('worktree_branch',''))" 2>/dev/null || echo "")
      if [ -d "\$worktree_path" ] && [ -n "\$repo_path" ]; then
        cd "\$repo_path"
        git worktree remove "\$worktree_path" --force 2>/dev/null || rm -rf "\$worktree_path"
      fi
      if [ -n "\$branch" ] && [ -n "\$repo_path" ]; then
        cd "\$repo_path"
        git branch -D "\$branch" 2>/dev/null || true
      fi
      ;;
  esac
fi
RUNEOF
  chmod +x "$job_dir/run.sh"
  chown -R "$RUN_USER":"$RUN_USER" "$job_dir"

  log "Launching job $job_id in worktree $worktree_path"
  log "Model: $model | Effort: $effort | Branch: $worktree_branch"

  # Launch in background - su PID is the one we track
  su - "$RUN_USER" -c "ANTHROPIC_AUTH_TOKEN='$ANTHROPIC_AUTH_TOKEN' bash $job_dir/run.sh" \
    > "$job_dir/nohup.out" 2>&1 &
  local su_pid=$!
  echo "$su_pid" > "$job_dir/pid"
  # Also write a helper to find the actual claude child PID later
  sleep 1
  local claude_pid=$(pgrep -P $(pgrep -P $su_pid 2>/dev/null) 2>/dev/null | head -1)
  [ -n "$claude_pid" ] && echo "$claude_pid" > "$job_dir/claude_pid"

  log "Job $job_id started (PID: $(cat "$job_dir/pid"))"
  echo "$job_id"
}

do_status() {
  local job_id="$1"
  local job_dir="$JOBS_DIR/$job_id"
  [ -d "$job_dir" ] || { log "ERROR: Job $job_id not found"; exit 1; }

  local status=$(cat "$job_dir/status" 2>/dev/null || echo "unknown")
  local started=$(cat "$job_dir/started_at" 2>/dev/null || echo "?")
  local finished=$(cat "$job_dir/finished_at" 2>/dev/null || echo "still running")
  local pid=$(cat "$job_dir/pid" 2>/dev/null || echo "?")
  local alive="no"

  if [ "$status" = "running" ] && [ "$pid" != "?" ]; then
    if kill -0 "$pid" 2>/dev/null; then
      alive="yes"
    else
      echo "failed" > "$job_dir/status"
      status="failed (process died)"
      date -u '+%Y-%m-%dT%H:%M:%SZ' > "$job_dir/finished_at"
      finished=$(cat "$job_dir/finished_at")
    fi
  fi

  echo "Job: $job_id"
  echo "Status: $status"
  echo "PID: $pid (alive: $alive)"
  echo "Started: $started"
  echo "Finished: $finished"
  [ -f "$job_dir/config.json" ] && echo "Config:" && cat "$job_dir/config.json"
  [[ "$status" == failed* ]] && [ -f "$job_dir/stderr.txt" ] && echo "--- STDERR ---" && tail -20 "$job_dir/stderr.txt"
}

do_list() {
  local filter="${1:---active}"
  local found=0

  for d in "$JOBS_DIR"/job-*/; do
    [ -d "$d" ] || continue
    local jid=$(basename "$d")
    local st=$(cat "$d/status" 2>/dev/null || echo "unknown")
    local started=$(cat "$d/started_at" 2>/dev/null || echo "?")

    if [ "$st" = "running" ]; then
      local pid=$(cat "$d/pid" 2>/dev/null || echo "")
      if [ -n "$pid" ] && ! kill -0 "$pid" 2>/dev/null; then
        echo "failed" > "$d/status"; st="failed"
        date -u '+%Y-%m-%dT%H:%M:%SZ' > "$d/finished_at"
      fi
    fi

    [ "$filter" = "--active" ] && [ "$st" != "running" ] && continue

    local repo=$(python3 -c "import json; print(json.load(open('$d/config.json')).get('repo','?'))" 2>/dev/null || echo "?")
    local branch=$(python3 -c "import json; print(json.load(open('$d/config.json')).get('worktree_branch','?'))" 2>/dev/null || echo "?")
    echo "$jid | $st | $repo | $branch | $started"
    found=$((found + 1))
  done
  [ "$found" -eq 0 ] && echo "No jobs found."
}

do_result() {
  local job_id="$1"
  local job_dir="$JOBS_DIR/$job_id"
  [ -d "$job_dir" ] || { log "ERROR: Job $job_id not found"; exit 1; }

  local status=$(cat "$job_dir/status" 2>/dev/null || echo "unknown")
  echo "Status: $status"

  if [ -f "$job_dir/output.json" ] && [ -s "$job_dir/output.json" ]; then
    echo "--- OUTPUT ---"
    cat "$job_dir/output.json"
  else
    echo "No output yet."
    [ -f "$job_dir/stderr.txt" ] && [ -s "$job_dir/stderr.txt" ] && echo "--- STDERR ---" && tail -30 "$job_dir/stderr.txt"
  fi
}

do_cancel() {
  local job_id="$1"
  local job_dir="$JOBS_DIR/$job_id"
  [ -d "$job_dir" ] || { log "ERROR: Job $job_id not found"; exit 1; }

  local pid=$(cat "$job_dir/pid" 2>/dev/null || echo "")
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null; pkill -P "$pid" 2>/dev/null || true
    log "Killed PID $pid"
  fi
  echo "cancelled" > "$job_dir/status"
  date -u '+%Y-%m-%dT%H:%M:%SZ' > "$job_dir/finished_at"
  log "Job $job_id cancelled"
}

do_cleanup() {
  local job_id="$1"
  local job_dir="$JOBS_DIR/$job_id"
  [ -d "$job_dir" ] || { log "ERROR: Job $job_id not found"; exit 1; }

  local status=$(cat "$job_dir/status" 2>/dev/null || echo "unknown")
  if [ "$status" = "running" ]; then
    log "ERROR: Cannot cleanup a running job. Cancel it first."
    exit 1
  fi

  # Remove the git worktree
  local repo_path=$(python3 -c "import json; print(json.load(open('$job_dir/config.json')).get('repo_path',''))" 2>/dev/null)
  local worktree_path="$job_dir/worktree"

  if [ -d "$worktree_path" ] && [ -n "$repo_path" ]; then
    cd "$repo_path"
    git worktree remove "$worktree_path" --force 2>/dev/null || rm -rf "$worktree_path"
    log "Worktree removed for $job_id"
  fi

  # Optionally delete the temp branch
  local branch=$(python3 -c "import json; print(json.load(open('$job_dir/config.json')).get('worktree_branch',''))" 2>/dev/null)
  if [ -n "$branch" ] && [ -n "$repo_path" ]; then
    cd "$repo_path"
    git branch -D "$branch" 2>/dev/null && log "Branch $branch deleted" || true
  fi

  log "Job $job_id cleaned up (job dir preserved for records)"
}

action="${1:-help}"; shift || true
case "$action" in
  launch)  do_launch "$@" ;;
  status)  do_status "$@" ;;
  list)    do_list "$@" ;;
  result)  do_result "$@" ;;
  cancel)  do_cancel "$@" ;;
  cleanup) do_cleanup "$@" ;;
  *) echo "Usage: runner.sh {launch|status|list|result|cancel|cleanup} [args]" ;;
esac
