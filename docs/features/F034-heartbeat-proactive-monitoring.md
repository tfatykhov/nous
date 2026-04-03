# F034: Heartbeat — Proactive Monitoring & Autonomous Execution

**Status:** Draft v2
**Author:** Emerson (spec v1), Tim (architecture review), Nous (KAIROS analysis + v2 enhancements)
**Created:** 2026-03-31
**Updated:** 2026-04-03
**Dependencies:** F026 (Execution Ledger), F031 (Censor Middleware), F033 (Multi-Tier Search)

---

## Problem

Nous is purely reactive. If no one sends a message, nothing happens. The sleep cycle handles memory maintenance, but Nous has no way to:

- Check email and notify Tim about important messages
- Monitor Google Drive for new or modified files
- Track calendar events and send reminders
- Watch for external events that need attention
- Perform periodic self-health checks (censor stats, decision review, memory drift)
- Initiate its own cognitive work (research tasks, follow-ups, proactive analysis)

OpenClaw solves this with a heartbeat loop + HEARTBEAT.md config. KAIROS (Claude Code's assistant mode) solves this with a sophisticated cron scheduling system. Nous needs its own version — one that fits the cognitive architecture, learns from KAIROS's production-grade patterns, and goes beyond both.

---

## KAIROS Cron Analysis — Lessons Learned

KAIROS implements autonomous work through a multi-layered cron scheduling system. Key patterns worth adopting:

### What KAIROS Does Well

1. **Two-tier durability** — Session-only tasks (die with process) vs. durable tasks (persisted to `.claude/scheduled_tasks.json`). This is elegant: quick "check this in 5 minutes" tasks don't need disk I/O, while "every morning at 9am" tasks survive restarts. *Nous should adopt this pattern.*

2. **Anti-thundering-herd jitter** — KAIROS uses deterministic per-task jitter (taskId hash → fractional delay) to spread load. Recurring tasks get forward jitter (10% of interval, capped at 15min). One-shot tasks get backward jitter (fire up to 90s early on :00/:30 marks). *Not directly relevant for single-instance Nous, but the concept of staggering checks is useful if multiple checks have the same interval.*

3. **Missed-task detection** — On startup, KAIROS finds one-shot tasks whose fire time passed while the process was down and asks the user before executing them. *Nous should adopt this — if heartbeat was down, surface what was missed.*

4. **Circuit breaker via scheduler lock** — Only one session owns the scheduler at a time (PID-based liveness probe). Prevents double-firing. *Nous is single-instance, but the pattern is useful for multi-agent F024 scenarios.*

5. **File-watching + polling hybrid** — KAIROS uses chokidar to watch `scheduled_tasks.json` for external changes (another session added a task) plus a 1-second check timer for firing. *Nous should use DB-watching (already has EventBus) plus a check loop.*

6. **Killswitch pattern** — `isKilled()` is polled every tick so ops can stop the scheduler mid-session via a feature flag. *Nous should have a runtime-toggleable heartbeat enable/disable.*

7. **Permanent tasks** — KAIROS has `permanent: true` tasks that never auto-expire (catch-up, morning-checkin, dream). These are installed by the system, not user-created. *Nous should have built-in permanent checks (health, self-reflection) that can't be accidentally removed.*

8. **Workload attribution** — Cron-fired prompts are tagged with `workload: WORKLOAD_CRON` so the API can serve them at lower QoS. *Nous should tag heartbeat-initiated sessions as `source: heartbeat` for priority management and token budget separation.*

### Where Nous Should Go Beyond KAIROS

1. **Cognitive triage** — KAIROS just enqueues a prompt. Nous should classify findings by urgency and decide whether to notify, act, or just log. The thin-check → cognitive-triage pipeline in v1 is the right architecture.

2. **Learning from checks** — KAIROS checks are static (prompt text). Nous checks should be procedure-backed, meaning F012 K-line learning can optimize intervals, urgency classification, and even check logic over time.

3. **Proactive cognitive sessions** — KAIROS only fires user-defined prompts. Nous should be able to initiate its own work: "I noticed 3 decisions are overdue for review" → open a review session autonomously.

4. **Budget-aware execution** — KAIROS has no token budget concept for cron tasks. Nous should have a daily heartbeat token budget to prevent runaway costs.

5. **Quiet hours with override** — KAIROS doesn't have quiet hours. Nous should respect them but allow urgent findings to break through.

---

## Design Principles

1. **Hybrid approach** — Thin checks (no LLM) for polling, cognitive sessions only when something is found
2. **Two-tier durability** — Session-only checks (quick probes) vs. persistent checks (survive restarts)
3. **Procedure-driven** — Checks are defined as procedures, so Nous can learn to improve them via F012
4. **Ledger-integrated** — All heartbeat activity recorded in the execution ledger (F026)
5. **Censor-aware** — Cognitive sessions triggered by heartbeat go through the full pipeline
6. **Budget-conscious** — Daily token budget for heartbeat-initiated work; thin checks are free
7. **Missed-task aware** — On startup, detect and surface what was missed while down
8. **Gracefully degrading** — Individual check failures don't crash the loop; circuit breakers disable broken checks

---

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│                       main.py                             │
│                                                           │
│  ┌──────────┐  ┌──────────┐  ┌─────────────────────────┐ │
│  │ Telegram  │  │  Sleep   │  │    HeartbeatRunner       │ │
│  │   Bot     │  │  Cycle   │  │                          │ │
│  └──────────┘  └──────────┘  │  ┌─────────────────────┐ │ │
│                              │  │   Check Scheduler    │ │ │
│                              │  │  (per-check timers)  │ │ │
│                              │  └──────────┬──────────┘ │ │
│                              │             │             │ │
│                              │     ┌───────▼───────┐    │ │
│                              │     │ Check Registry │    │ │
│                              │     │ ┌───┐┌───┐┌──┐│    │ │
│                              │     │ │ E ││ D ││ H││    │ │
│                              │     │ └───┘└───┘└──┘│    │ │
│                              │     └───────┬───────┘    │ │
│                              │             │ findings   │ │
│                              │     ┌───────▼───────┐    │ │
│                              │     │    Triage     │    │ │
│                              │     │ (urgency +    │    │ │
│                              │     │  batching)    │    │ │
│                              │     └───┬───┬───┬───┘    │ │
│                              │         │   │   │        │ │
│                              └─────────┼───┼───┼────────┘ │
│                                        │   │   │          │
│                          ┌─────────────┘   │   └────────┐ │
│                          ▼                 ▼            ▼  │
│                     Notify(TG)      Cognitive       Log   │
│                                     Session       (Ledger)│
│                                   (w/ budget)             │
└──────────────────────────────────────────────────────────┘
```

---

## Components

### 1. HeartbeatRunner

The main loop. Runs as an `asyncio.Task` alongside the Telegram bot and sleep cycle.

```python
# nous/heartbeat/runner.py

class HeartbeatRunner:
    """Periodic external-world monitoring + autonomous action loop.
    
    Inspired by KAIROS cron scheduler patterns:
    - Per-check independent schedules (not one global interval)
    - Circuit breaker on consecutive failures
    - Missed-check detection on startup
    - Runtime killswitch via settings
    """

    def __init__(
        self,
        config: NousConfig,
        registry: CheckRegistry,
        runner: AgentRunner,
        telegram_bot: TelegramBot,
        heart: Heart,
        brain: Brain,
        bus: EventBus | None = None,
    ):
        self.config = config
        self.registry = registry
        self.runner = runner
        self.telegram_bot = telegram_bot
        self.heart = heart
        self.brain = brain
        self.bus = bus
        
        # Token budget tracking (daily reset)
        self.daily_token_budget = config.heartbeat_daily_token_budget  # default 50_000
        self.tokens_used_today = 0
        self.budget_reset_date = date.today()
        
        # Stats
        self.last_run: datetime | None = None
        self.run_count = 0
        self.findings_total = 0
        self.cognitive_sessions_opened = 0
        
        # Tick interval — how often we check which checks are due
        self.tick_interval = config.heartbeat_tick_interval  # default 30s

    async def start(self):
        """Start heartbeat loop. Detect missed checks first."""
        # KAIROS pattern: detect what was missed while we were down
        await self._detect_missed_checks()
        
        logger.info(
            "Heartbeat started (tick=%ds, budget=%d tokens/day, quiet=%s-%s)",
            self.tick_interval,
            self.daily_token_budget,
            *self.config.heartbeat_quiet_hours,
        )
        
        while True:
            try:
                # Runtime killswitch (KAIROS pattern)
                if not self.config.heartbeat_enabled:
                    await asyncio.sleep(self.tick_interval)
                    continue
                    
                if not self._in_quiet_hours() or self._has_urgent_override():
                    await self._tick()
                    self.run_count += 1
                    self.last_run = datetime.now(UTC)
                    
                    # Reset daily budget at midnight
                    self._maybe_reset_budget()
                    
            except Exception as e:
                logger.error(f"Heartbeat tick failed: {e}", exc_info=True)
                # Never crash the loop — log and continue
                
            await asyncio.sleep(self.tick_interval)

    async def _tick(self):
        """Run all due checks, triage findings."""
        findings: list[Finding] = []
        
        for check in self.registry.get_due_checks():
            try:
                result = await asyncio.wait_for(
                    check.run(),
                    timeout=check.timeout,
                )
                if result.has_updates:
                    findings.extend(result.findings)
                check.mark_success()
                
                # Record to ledger
                if self.bus:
                    await self.bus.emit(Event(
                        type="heartbeat_check_completed",
                        data={
                            "check": check.name,
                            "findings_count": len(result.findings) if result.has_updates else 0,
                        },
                    ))
                    
            except asyncio.TimeoutError:
                logger.warning(f"Check {check.name} timed out ({check.timeout}s)")
                check.mark_failure("timeout")
            except Exception as e:
                logger.error(f"Check {check.name} failed: {e}")
                check.mark_failure(str(e))

        if findings:
            self.findings_total += len(findings)
            await self._triage(findings)

    async def _triage(self, findings: list[Finding]):
        """Decide how to handle findings — notify, act, or both.
        
        Three-tier urgency model:
        - high: immediate notification + cognitive session (breaks quiet hours)
        - normal: batched notification + cognitive session if actionable
        - low: log to ledger only (no notification, no LLM)
        """
        urgent = [f for f in findings if f.urgency == "high"]
        normal = [f for f in findings if f.urgency == "normal"]
        low = [f for f in findings if f.urgency == "low"]

        # Urgent: notify immediately + open cognitive session
        if urgent:
            summary = format_findings(urgent)
            await self.telegram_bot.send_push(f"⚡ {summary}")
            if self._has_budget():
                await self._cognitive_triage(urgent)
            else:
                await self.telegram_bot.send_push(
                    "⚠️ Heartbeat token budget exhausted — logged but not triaged"
                )

        # Normal: batch notify + optional cognitive session
        if normal:
            summary = format_findings(normal)
            await self.telegram_bot.send_push(f"📬 {summary}")
            actionable = [f for f in normal if f.needs_action]
            if actionable and self._has_budget():
                await self._cognitive_triage(actionable)

        # Low: just log to ledger, no notification
        for f in low:
            logger.info(f"Heartbeat low-priority: {f.summary}")

    async def _cognitive_triage(self, findings: list[Finding]):
        """Open a session and let the cognitive pipeline process findings.
        
        Sessions are tagged as heartbeat-initiated for:
        - Token budget tracking (KAIROS workload attribution pattern)
        - Separate analytics in execution ledger
        - Lower priority than user-initiated sessions
        """
        context = "\n".join(f"- [{f.source}] {f.summary}" for f in findings)
        message = (
            f"Heartbeat found {len(findings)} items requiring attention:\n"
            f"{context}\n\n"
            f"Review and take appropriate action."
        )
        
        tokens_before = self.tokens_used_today
        await self.runner.process_heartbeat_message(
            message,
            metadata={
                "source": "heartbeat",
                "automated": True,
                "findings_count": len(findings),
            },
        )
        
        # Track tokens (runner should report usage)
        self.cognitive_sessions_opened += 1

    async def _detect_missed_checks(self):
        """On startup, find checks that should have fired while we were down.
        
        KAIROS pattern: surface missed one-shot tasks.
        For Nous: report stale check data that may need attention.
        """
        last_known_run = await self._load_last_run_time()
        if last_known_run is None:
            return
            
        now = datetime.now(UTC)
        gap = now - last_known_run
        
        if gap.total_seconds() > 600:  # more than 10 min gap
            missed_checks = []
            for check in self.registry.checks:
                if check.interval < gap.total_seconds():
                    missed_checks.append(check.name)
            
            if missed_checks:
                summary = ", ".join(missed_checks)
                logger.info(
                    "Heartbeat was down for %s — missed checks: %s",
                    gap, summary,
                )
                await self.telegram_bot.send_push(
                    f"🔄 Nous heartbeat restarted after {_humanize_duration(gap)} downtime.\n"
                    f"Missed checks: {summary}\n"
                    f"Running all checks now."
                )
                # Force all checks to run immediately
                for check in self.registry.checks:
                    check.last_run = None

    def _in_quiet_hours(self) -> bool:
        """Respect quiet hours (user's timezone)."""
        now = datetime.now(self.config.user_timezone)
        start, end = self.config.heartbeat_quiet_hours
        if start > end:  # crosses midnight (e.g., 23-8)
            return now.hour >= start or now.hour < end
        return start <= now.hour < end

    def _has_urgent_override(self) -> bool:
        """Allow urgent-only checks to run during quiet hours."""
        return any(
            c.urgent_override and c.is_due(datetime.now(UTC))
            for c in self.registry.checks
        )

    def _has_budget(self) -> bool:
        return self.tokens_used_today < self.daily_token_budget

    def _maybe_reset_budget(self):
        today = date.today()
        if today > self.budget_reset_date:
            self.tokens_used_today = 0
            self.budget_reset_date = today
```

### 2. CheckRegistry

Manages registered checks with individual schedules. Supports both persistent (survive restart) and session-only checks.

```python
class CheckRegistry:
    """Registry of thin checks with independent schedules.
    
    Two-tier durability (KAIROS pattern):
    - Persistent checks: stored in DB, survive restarts
    - Session-only checks: in-memory, die with process
    
    Permanent checks: system-installed, can't be user-removed.
    """

    def __init__(self):
        self.checks: list[BaseCheck] = []
        self._permanent_names: set[str] = set()

    def register(self, check: BaseCheck, permanent: bool = False):
        self.checks.append(check)
        if permanent:
            self._permanent_names.add(check.name)

    def unregister(self, name: str) -> bool:
        """Remove a check. Permanent checks cannot be removed."""
        if name in self._permanent_names:
            return False
        self.checks = [c for c in self.checks if c.name != name]
        return True

    def get_due_checks(self) -> list[BaseCheck]:
        """Return checks that are due to run based on their interval."""
        now = datetime.now(UTC)
        return [c for c in self.checks if c.active and c.is_due(now)]

    def get_status(self) -> list[dict]:
        """Return status of all checks for REST API / dashboard."""
        return [
            {
                "name": c.name,
                "active": c.active,
                "interval": c.interval,
                "last_run": c.last_run.isoformat() if c.last_run else None,
                "consecutive_failures": c.consecutive_failures,
                "permanent": c.name in self._permanent_names,
                "next_due": (
                    (c.last_run + timedelta(seconds=c.interval)).isoformat()
                    if c.last_run else "now"
                ),
            }
            for c in self.checks
        ]
```

### 3. BaseCheck Protocol

```python
@dataclass
class Finding:
    source: str          # "email", "drive", "calendar", "health", "self"
    summary: str         # human-readable one-liner
    urgency: str         # "high", "normal", "low"
    needs_action: bool   # should we open a cognitive session?
    raw_data: dict       # source-specific payload
    check_name: str = "" # which check produced this finding

class BaseCheck(ABC):
    name: str
    interval: int        # seconds between runs (independent of heartbeat tick)
    timeout: int = 30    # per-check timeout
    active: bool = True
    urgent_override: bool = False  # can this check break quiet hours?
    last_run: datetime | None = None
    consecutive_failures: int = 0
    max_failures: int = 5  # disable after N consecutive failures

    def is_due(self, now: datetime) -> bool:
        if self.consecutive_failures >= self.max_failures:
            return False  # circuit breaker
        if self.last_run is None:
            return True
        return (now - self.last_run).total_seconds() >= self.interval

    def mark_success(self):
        self.last_run = datetime.now(UTC)
        self.consecutive_failures = 0

    def mark_failure(self, reason: str):
        self.last_run = datetime.now(UTC)
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.max_failures:
            logger.warning(
                f"Check {self.name} disabled after {self.max_failures} "
                f"consecutive failures (last: {reason})"
            )

    @abstractmethod
    async def run(self) -> CheckResult: ...
```

### 4. Built-in Checks

#### EmailCheck (persistent, urgent-override)

```python
class EmailCheck(BaseCheck):
    """Check for unread emails via IMAP. No LLM needed."""

    name = "email"
    interval = 180  # every 3 minutes
    urgent_override = True  # email from Tim breaks quiet hours

    def __init__(self, config: NousConfig):
        self.host = config.email_imap_host
        self.user = config.email_user
        self.password = config.email_password
        self.seen_ids: set[str] = set()
        self._prune_threshold = timedelta(hours=24)
        self._seen_timestamps: dict[str, datetime] = {}

    async def run(self) -> CheckResult:
        findings = []
        messages = await asyncio.get_event_loop().run_in_executor(
            None, self._fetch_unread
        )
        for msg in messages:
            if msg["id"] not in self.seen_ids:
                self.seen_ids.add(msg["id"])
                self._seen_timestamps[msg["id"]] = datetime.now(UTC)
                urgency = self._classify_urgency(msg)
                findings.append(Finding(
                    source="email",
                    summary=f"From: {msg['from']} — {msg['subject']}",
                    urgency=urgency,
                    needs_action=(urgency != "low"),
                    raw_data=msg,
                    check_name=self.name,
                ))
        
        # Prune seen_ids older than 24h (prevent memory leak)
        self._prune_seen_ids()
        
        return CheckResult(has_updates=bool(findings), findings=findings)

    def _classify_urgency(self, msg: dict) -> str:
        """Simple rule-based urgency. No LLM.
        
        Phase 3: Replace with procedure-backed classification
        that F012 K-line learning can improve.
        """
        sender = msg["from"].lower()
        subject = msg.get("subject", "").lower()
        
        # Tim is always high priority
        if "tfatykhov" in sender:
            return "high"
        # Known contacts = normal
        if any(k in sender for k in ["maechkina"]):
            return "normal"
        # Urgent keywords in subject
        if any(k in subject for k in ["urgent", "asap", "critical", "down"]):
            return "normal"
        return "low"

    def _prune_seen_ids(self):
        """Remove seen_ids older than 24h to prevent memory leak."""
        cutoff = datetime.now(UTC) - self._prune_threshold
        expired = [
            mid for mid, ts in self._seen_timestamps.items()
            if ts < cutoff
        ]
        for mid in expired:
            self.seen_ids.discard(mid)
            del self._seen_timestamps[mid]

    def _fetch_unread(self) -> list[dict]:
        """Synchronous IMAP fetch."""
        import imaplib, email
        imap = imaplib.IMAP4_SSL(self.host)
        imap.login(self.user, self.password)
        imap.select("INBOX")
        _, msgs = imap.search(None, "UNSEEN")
        results = []
        for mid in msgs[0].split()[-10:]:  # last 10 max
            _, data = imap.fetch(mid, "(RFC822)")
            msg = email.message_from_bytes(data[0][1])
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        body = part.get_payload(decode=True).decode(errors="replace")[:500]
                        break
            else:
                body = msg.get_payload(decode=True).decode(errors="replace")[:500]
            results.append({
                "id": mid.decode(),
                "from": msg["From"],
                "subject": msg["Subject"],
                "body": body,
            })
        imap.logout()
        return results
```

#### DriveCheck (persistent)

```python
class DriveCheck(BaseCheck):
    """Check Google Drive for new/modified files."""

    name = "drive"
    interval = 600  # every 10 minutes

    def __init__(self, config: NousConfig, gdrive: GoogleDriveIntegration):
        self.gdrive = gdrive
        self.last_check_time: datetime | None = None

    async def run(self) -> CheckResult:
        findings = []
        cutoff = self.last_check_time or (datetime.now(UTC) - timedelta(hours=1))
        self.last_check_time = datetime.now(UTC)

        modified = await self.gdrive.list_modified_since(cutoff)
        for file in modified:
            findings.append(Finding(
                source="drive",
                summary=f"Modified: {file['name']} ({file['mimeType']})",
                urgency="normal",
                needs_action=False,
                raw_data=file,
                check_name=self.name,
            ))
        return CheckResult(has_updates=bool(findings), findings=findings)
```

#### HealthCheck (permanent, persistent)

```python
class HealthCheck(BaseCheck):
    """Internal health monitoring. System-installed, can't be removed.
    
    Checks:
    - Decisions needing outcome review
    - Censors with high false positive rate
    - Fact staleness
    - Procedure effectiveness drift
    - Token budget consumption rate
    """

    name = "health"
    interval = 3600  # every hour

    def __init__(self, heart: Heart, brain: Brain):
        self.heart = heart
        self.brain = brain

    async def run(self) -> CheckResult:
        findings = []

        # Check for decisions needing outcome review
        pending = await self.brain.get_pending_reviews(older_than_days=7)
        if pending:
            findings.append(Finding(
                source="health",
                summary=f"{len(pending)} decisions pending outcome review (>7 days)",
                urgency="low",
                needs_action=True,
                raw_data={"decision_ids": [d.id for d in pending]},
                check_name=self.name,
            ))

        # Check censor false positive rate
        censors = await self.heart.get_censors()
        noisy = [c for c in censors
                 if c.activation_count > 10
                 and c.false_positive_count / c.activation_count > 0.3]
        if noisy:
            findings.append(Finding(
                source="health",
                summary=f"{len(noisy)} censors have >30% false positive rate",
                urgency="normal",
                needs_action=True,
                raw_data={"censor_ids": [c.id for c in noisy]},
                check_name=self.name,
            ))

        # Check fact staleness
        stale_count = await self.heart.count_stale_facts(older_than_days=30)
        if stale_count > 50:
            findings.append(Finding(
                source="health",
                summary=f"{stale_count} facts older than 30 days without access",
                urgency="low",
                needs_action=False,
                raw_data={"stale_count": stale_count},
                check_name=self.name,
            ))

        # Check procedure effectiveness drift
        try:
            low_eff = await self.heart.procedures.get_low_effectiveness(threshold=0.5)
            if low_eff:
                findings.append(Finding(
                    source="health",
                    summary=f"{len(low_eff)} procedures below 50% effectiveness",
                    urgency="low",
                    needs_action=True,
                    raw_data={"procedure_ids": [p.id for p in low_eff]},
                    check_name=self.name,
                ))
        except Exception:
            pass  # Method may not exist yet

        return CheckResult(has_updates=bool(findings), findings=findings)
```

#### SelfInitiatedCheck (permanent, persistent) — NEW

```python
class SelfInitiatedCheck(BaseCheck):
    """Proactive autonomous work detection.
    
    This is where Nous goes beyond KAIROS. Instead of only executing
    user-defined prompts on a schedule, Nous can identify work it
    should initiate on its own based on:
    
    - Pending follow-ups from past conversations
    - Research topics it was asked to track
    - Scheduled reports that are due
    - Pattern-detected opportunities for proactive assistance
    """

    name = "self-initiated"
    interval = 1800  # every 30 minutes

    def __init__(self, heart: Heart, brain: Brain):
        self.heart = heart
        self.brain = brain

    async def run(self) -> CheckResult:
        findings = []

        # Check for pending follow-ups tagged in facts
        follow_ups = await self.heart.facts.search(
            query="follow-up pending action TODO",
            category="rule",
            limit=5,
        )
        for fact in follow_ups:
            if self._looks_like_pending(fact.content):
                findings.append(Finding(
                    source="self",
                    summary=f"Pending follow-up: {fact.content[:100]}",
                    urgency="low",
                    needs_action=True,
                    raw_data={"fact_id": str(fact.id), "content": fact.content},
                    check_name=self.name,
                ))

        # Check for overdue scheduled reports
        overdue = await self.heart.schedules.get_overdue()
        for sched in overdue:
            findings.append(Finding(
                source="self",
                summary=f"Overdue schedule: {sched.task[:100]}",
                urgency="normal",
                needs_action=True,
                raw_data={"schedule_id": str(sched.id)},
                check_name=self.name,
            ))

        return CheckResult(has_updates=bool(findings), findings=findings)

    def _looks_like_pending(self, content: str) -> bool:
        """Simple heuristic for detecting pending items."""
        markers = ["TODO", "follow up", "pending", "need to", "should check", "remind"]
        return any(m.lower() in content.lower() for m in markers)
```

---

## Configuration

New config fields in `NousConfig` / `Settings`:

```python
# Heartbeat core
heartbeat_enabled: bool = True
heartbeat_tick_interval: int = 30           # base loop tick (seconds)
heartbeat_quiet_hours: tuple[int, int] = (23, 8)  # start, end (user TZ)
heartbeat_user_timezone: str = "America/New_York"
heartbeat_daily_token_budget: int = 50_000  # max tokens/day for heartbeat cognitive sessions

# Email check
heartbeat_email_enabled: bool = True
heartbeat_email_interval: int = 180     # seconds
heartbeat_email_imap_host: str = "imap.gmail.com"

# Drive check
heartbeat_drive_enabled: bool = True
heartbeat_drive_interval: int = 600     # seconds

# Health check (permanent — always enabled when heartbeat is on)
heartbeat_health_interval: int = 3600   # seconds

# Self-initiated check (permanent)
heartbeat_self_initiated_interval: int = 1800  # seconds
```

Environment variables:
```
NOUS_HEARTBEAT_ENABLED=true
NOUS_HEARTBEAT_TICK_INTERVAL=30
NOUS_HEARTBEAT_QUIET_START=23
NOUS_HEARTBEAT_QUIET_END=8
NOUS_HEARTBEAT_TIMEZONE=America/New_York
NOUS_HEARTBEAT_DAILY_TOKEN_BUDGET=50000
```

---

## Integration Points

### main.py startup

```python
# After existing setup (sleep_handler, subtask_pool, task_scheduler)
heartbeat_runner = None
if settings.heartbeat_enabled:
    from nous.heartbeat.runner import HeartbeatRunner
    from nous.heartbeat.registry import CheckRegistry
    from nous.heartbeat.checks import (
        EmailCheck, DriveCheck, HealthCheck, SelfInitiatedCheck,
    )
    
    registry = CheckRegistry()
    
    # Permanent checks (system-installed, can't be user-removed)
    registry.register(
        HealthCheck(heart, brain),
        permanent=True,
    )
    registry.register(
        SelfInitiatedCheck(heart, brain),
        permanent=True,
    )
    
    # Optional checks
    if settings.heartbeat_email_enabled:
        registry.register(EmailCheck(settings))
    if settings.heartbeat_drive_enabled and gdrive:
        registry.register(DriveCheck(settings, gdrive))
    
    heartbeat_runner = HeartbeatRunner(
        config=settings,
        registry=registry,
        runner=runner,
        telegram_bot=telegram_bot,
        heart=heart,
        brain=brain,
        bus=bus,
    )
    asyncio.create_task(heartbeat_runner.start(), name="heartbeat")
```

### Runner.process_heartbeat_message()

New method on Runner that opens a special heartbeat session:

```python
async def process_heartbeat_message(
    self, message: str, metadata: dict | None = None
):
    """Process heartbeat findings through the cognitive pipeline.
    
    Sessions are tagged with source=heartbeat for:
    - Token budget tracking (KAIROS workload attribution pattern)
    - Execution ledger differentiation
    - Lower priority than user-initiated sessions
    """
    session = await self.create_session(
        source="heartbeat",
        metadata={"type": "heartbeat", "automated": True, **(metadata or {})},
    )
    try:
        response = await self.process_turn(session.id, message)
        # Log to execution ledger
        if self.ledger:
            await self.ledger.record_action(
                session_id=session.id,
                action_type="heartbeat_triage",
                input_text=message,
                output_text=response,
                metadata=metadata,
            )
        return response
    finally:
        await self.end_session(session.id)
```

### REST API

```
GET  /heartbeat/status     → { enabled, last_run, run_count, tokens_used_today,
                                budget_remaining, checks: [{name, active, interval,
                                last_run, failures, permanent, next_due}] }
POST /heartbeat/trigger    → Force immediate tick (like /sleep/trigger)
PUT  /heartbeat/config     → Update intervals, quiet hours, budget at runtime
POST /heartbeat/check/:name/trigger → Force a specific check to run now
POST /heartbeat/check/:name/reset   → Reset circuit breaker for a failed check
```

### Dashboard Integration (F021)

Add a Heartbeat panel to the Memory Dashboard:

- Real-time check status (green/yellow/red based on failures)
- Findings timeline (last 24h of findings with urgency coloring)
- Token budget gauge (daily usage vs. budget)
- Cognitive session log (heartbeat-initiated sessions)
- Check interval tuning UI

---

## Phases

### Phase 1: Core Loop + Health Check
- HeartbeatRunner with tick loop, quiet hours, budget tracking
- CheckRegistry with permanent/session-only distinction
- HealthCheck (permanent) — decisions, censors, facts
- REST status/trigger endpoints
- Event bus integration for ledger recording
- Missed-check detection on startup
- Config in Settings + env vars

### Phase 2: Email + Drive + Notifications
- EmailCheck with IMAP polling
- DriveCheck using existing gdrive integration
- Telegram push notifications with urgency tiers
- Cognitive triage for actionable findings
- Dashboard panel

### Phase 3: Self-Initiated Work + Procedure Learning
- SelfInitiatedCheck — detect pending follow-ups, overdue schedules
- Convert check configs to procedures in Heart
- F012 K-line learning optimizes check intervals & urgency rules
- Nous can create new checks via `learn_skill`
- User-defined checks via Telegram: "check X every Y minutes"

### Phase 4: Calendar + Adaptive Scheduling + Multi-Agent
- CalendarCheck (Google Calendar API)
- Adaptive scheduling: check more often when Tim is active, less when quiet
- Integration with F024 multi-agent: heartbeat findings can spawn subtasks
- Cross-check correlation: "new email + calendar event = meeting prep needed"

---

## Risks & Mitigations

- **Token burn from unnecessary cognitive sessions** → Hybrid: thin checks first, LLM only when needed. Daily token budget cap.
- **IMAP connection failures crashing loop** → Per-check circuit breaker (5 failures → disable). Each check is isolated.
- **Quiet hours misconfiguration** → Default to conservative (11PM-8AM), runtime-adjustable via REST.
- **Heartbeat session conflicts with user sessions** → Heartbeat sessions are short-lived, tagged as automated, lower priority.
- **Notification spam** → Urgency classification + batching + quiet hours + seen_id dedup.
- **Stale seen_ids memory leak** → Prune seen_ids older than 24h on each tick (explicit fix from v1).
- **Budget exhaustion** → Daily cap with warning notification when 80% consumed. Urgent findings still notify (just don't open cognitive session).
- **Downtime gap** → Missed-check detection on startup with immediate catch-up run.

---

## Open Questions (Updated)

1. **~~Should heartbeat sessions count toward token budgets?~~** → Yes, with a separate daily cap (resolved: `heartbeat_daily_token_budget`).
2. **~~Should findings be stored as facts?~~** → No. Log in execution ledger only. Cognitive sessions that _act_ on findings may create facts as a side effect.
3. **Should Nous learn urgency classification?** → Phase 3: start with rules, back with procedure for F012 learning.
4. **Multi-agent heartbeat?** → Phase 4: heartbeat findings can spawn F024 subtasks, but one heartbeat runner per Nous instance.
5. **Should heartbeat integrate with existing TaskScheduler?** → No. TaskScheduler handles user-defined `schedule_task` cron jobs. Heartbeat is a separate system for proactive monitoring. They coexist: TaskScheduler fires prompts on schedule, Heartbeat watches the world and decides what to do.
6. **Should checks be able to read each other's findings?** → Phase 4: cross-check correlation. For now, checks are independent.

---

## Appendix: KAIROS vs Nous Heartbeat Comparison

- **KAIROS cron** — General-purpose prompt scheduler. Fires any prompt on a cron schedule. User-driven. Session-only or durable. 7-day auto-expiry. Anti-thundering-herd jitter.
- **Nous heartbeat** — Purpose-built autonomous monitoring. Thin checks → cognitive triage. System-driven (permanent checks) + user-driven (optional checks). Budget-conscious. Procedure-learnable. Cognitively integrated (full pipeline: censors, ledger, facts).

KAIROS is a better _scheduler_. Nous is a better _autonomous agent_. The heartbeat system should not try to be a generic cron — that's what `schedule_task` already does. Instead, it should be the mechanism that makes Nous genuinely proactive.
