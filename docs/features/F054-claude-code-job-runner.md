# F054: Claude Code Job Runner

**Status:** Draft
**Proposed by:** Tim + Nous (design session)
**Date:** 2026-04-06
**Depends on:** None (standalone infrastructure)
**Blocks:** Autonomous coding delegation, multi-repo task execution

---

## Problem Statement

Nous can analyze code, read files, and make small edits via `bash` and `write_file`, but **cannot perform complex multi-file coding tasks** — feature implementations, refactoring, test suites, bug fixes spanning multiple files. These tasks require a dedicated coding agent with deep repo context.

Claude Code (Anthropic's official CLI) is purpose-built for this: it understands entire codebases, creates branches, writes tests, and opens PRs. But it runs **synchronously and can take 5-30+ minutes** per task — far beyond Nous's tool timeout limits (300s max for bash).

We need an **asynchronous job runner** that lets Nous:
1. Launch Claude Code tasks in the background
2. Stay responsive while they execute
3. Capture structured results when done
4. Manage multiple concurrent jobs across multiple repos

---

## Solution: Async Job Runner with Worktree Isolation

### Architecture Overview

```
Nous (orchestrator)
  │
  ├── Delegation Decision Engine
  │     Decides: handle directly vs. delegate to Claude Code
  │
  ├── Job Lifecycle Manager
  │     Launch → Monitor → Retrieve → Cleanup
  │
  └── Heartbeat Monitor
        Polls active jobs, notifies on completion/failure
```

### Directory Structure

```
/workspace/claude-jobs/
  config/
    repos.json              # Repo registry with defaults
  runner.sh                 # Wrapper script for nohup execution
  jobs/
    {job-uuid}/
      ├── prompt.md         # Task input (natural language)
      ├── config.json       # Repo, model, budget, turn limits
      ├── status            # queued → running → done → failed
      ├── pid               # Process ID for cancellation
      ├── output.json       # Claude Code structured output (--output-format json)
      ├── stderr.txt        # Error stream
      └── meta.json         # Start/end time, exit code, duration, cost
```

### Repo Registry (`repos.json`)

Repos live permanently on disk. Nous manages git state (pull before launch). Claude Code never clones — it works on existing repos.

```json
{
  "nous-forge": {
    "path": "/tmp/nous-workspace/nous",
    "remote": "github.com/tfatykhov/nous",
    "default_model": "claude-sonnet-4-6",
    "default_budget_usd": 5.00,
    "max_turns": 50,
    "claude_md": true
  },
  "cognition-engines-site": {
    "path": "/tmp/nous-workspace/cognition-agent-decisions",
    "remote": "github.com/tfatykhov/cognition-agent-decisions",
    "default_model": "claude-sonnet-4-6",
    "default_budget_usd": 3.00,
    "max_turns": 30,
    "claude_md": false
  }
}
```

### Runner Script (`runner.sh`)

Wrapper executed via `nohup` that:
1. Reads config.json for job parameters
2. Writes `running` + PID to status file
3. Does `git pull` on target repo
4. Executes Claude Code with isolation flags
5. Captures structured output
6. Writes meta.json with timing/exit info

**Key Claude Code CLI flags:**

| Flag | Purpose |
|------|---------|
| `-p` | Print mode — non-interactive, exits when done |
| `--output-format json` | Structured parseable output |
| `--model <model>` | Explicit model selection per job |
| `--max-turns <n>` | Prevent runaway loops (default: 50) |
| `--max-budget-usd <n>` | Cost safety cap per job |
| `--dangerously-skip-permissions` | Unattended execution (no approval prompts) |
| `--append-system-prompt` | Inject Nous context (branch rules, PR expectations) |
| `--worktree` | Isolated git worktree per job — critical for concurrency |
| `--no-session-persistence` | Don't save sessions to disk |

### Concurrency Model

- **Max concurrent jobs: 3** (configurable)
- **Per-repo isolation via `--worktree`**: Creates `<repo>/.claude/worktrees/<job-uuid>` — a fully isolated git worktree. Three jobs on the same repo run in separate worktrees with no conflicts.
- **Job queue**: If at capacity, new jobs are queued with `status: queued` and launched when a slot opens
- **Timeout**: 30-minute hard kill per job (configurable per repo)

### Monitoring & Notification

**Heartbeat check** (polls every 5 minutes):
- Reads status files of all active jobs
- On `done`: reads output.json, extracts key results, notifies Tim via Telegram
- On `failed`: sends error context to Tim
- On timeout: kills process, marks as failed, notifies
- Cleans up completed job dirs after 24h

**Manual check**: Tim says "check my jobs" → Nous reads all job dirs, reports status

---

## Autonomous Delegation Policy

Nous decides when to delegate based on task characteristics:

### Delegate to Claude Code when:
- Multi-file code implementation (3+ files)
- Feature branch work requiring deep codebase understanding
- Refactoring across module boundaries
- Writing comprehensive test suites
- Bug fixes requiring cross-file investigation
- Any task that would take Nous 10+ sequential bash calls

### Handle directly when:
- Single-file edits or config changes
- Code analysis and review (read-only)
- Memory operations, communication, research
- Web searches, document generation
- Quick fixes where Nous already has full context

### Delegation scoring heuristic (future):
```
delegate_score = (
  file_count_estimate * 0.3 +
  complexity_rating * 0.3 +
  codebase_context_needed * 0.2 +
  estimated_turns * 0.2
)
if delegate_score > 0.6: delegate to Claude Code
```

---

## Authentication

Claude Code in our Docker container uses **Max subscription auth** (not API key):

1. Run `claude login` once in the container
2. Authorize via browser URL (device flow)
3. Token stored in `~/.claude.json`
4. Mount as persistent volume to survive container restarts
5. Token auto-refreshes

**Fallback**: `ANTHROPIC_API_KEY` env var for pay-per-token billing if subscription auth fails.

---

## Phases

### Phase 1: Infrastructure (MVP)
- [ ] Install Claude Code in Docker container
- [ ] Authenticate (subscription or API key)
- [ ] Create `runner.sh` wrapper script
- [ ] Create `repos.json` with nous-forge repo
- [ ] Create job directory structure
- [ ] Basic launch/check/cancel via Nous skill
- [ ] Manual delegation only ("run Claude Code on X")

### Phase 2: Monitoring & Notifications
- [ ] Heartbeat check for active job polling
- [ ] Telegram notification on completion/failure
- [ ] Job timeout enforcement
- [ ] Concurrent job queue management
- [ ] Auto-cleanup of old job dirs

### Phase 3: Autonomous Delegation
- [ ] Delegation decision engine in Nous turn pipeline
- [ ] Task complexity scoring heuristic
- [ ] Auto-delegation with Tim's approval for first N jobs
- [ ] Full autonomy after calibration period
- [ ] Decision recording for delegation choices

### Phase 4: Advanced Features
- [ ] `--append-system-prompt` with Nous-injected context per repo
- [ ] CLAUDE.md management per repo (Nous updates it with project conventions)
- [ ] Job chaining (output of job A feeds into job B)
- [ ] Cost tracking and budget alerts
- [ ] Job replay (re-run failed jobs with tweaked prompts)
- [ ] Integration with Nous Brain (record CC decisions, learn from outcomes)

---

## Safety & Guardrails

- **Cost cap**: `--max-budget-usd` per job (default $5)
- **Turn cap**: `--max-turns` per job (default 50)
- **Time cap**: 30-minute hard kill
- **Concurrency cap**: Max 3 simultaneous jobs
- **Branch rules**: `--append-system-prompt` enforces "never commit to main"
- **`--allowedTools`**: Can restrict which tools CC uses per job
- **Repo CLAUDE.md**: Project-specific rules CC must follow
- **No secrets in prompts**: Git credentials stay with Nous, never in job prompts

---

## Estimated Effort

| Phase | LOC | Time |
|-------|-----|------|
| Phase 1 | ~200 (bash + skill) | 2-3 hours |
| Phase 2 | ~150 (heartbeat + queue) | 2 hours |
| Phase 3 | ~300 (delegation engine) | 4-6 hours |
| Phase 4 | ~400 (advanced) | 8+ hours |

**MVP (Phases 1+2): ~350 LOC, 4-5 hours**

---

## Success Metrics

- Jobs complete successfully >90% of the time
- Average delegation decision accuracy >80% (right tasks delegated)
- Zero cost overruns (budget caps enforced)
- Tim satisfaction: reduces manual coding time by 50%+
- Nous-to-CC handoff latency < 30 seconds
