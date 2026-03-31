# F034: Heartbeat — Proactive Monitoring

**Status:** Draft
**Author:** Emerson (spec), Tim (architecture review)
**Created:** 2026-03-31
**Dependencies:** F026 (Execution Ledger), F031 (Censor Middleware), F033 (Multi-Tier Search)

## Problem

Nous is purely reactive. If no one sends a message, nothing happens. The sleep cycle handles memory maintenance, but Nous has no way to:

- Check email and notify Tim about important messages
- Monitor Google Drive for new or modified files
- Track calendar events and send reminders
- Watch for external events that need attention
- Perform periodic self-health checks (censor stats, decision review, memory drift)

OpenClaw solves this with a heartbeat loop + HEARTBEAT.md config. Nous needs its own version — one that fits the cognitive architecture rather than bolting on a dumb polling loop.

## Design Principles

1. **Hybrid approach:** Thin checks (no LLM) for polling, cognitive sessions only when something is found
2. **Procedure-driven:** Checks are defined as procedures, so Nous can learn to improve them via F012
3. **Ledger-integrated:** All heartbeat activity recorded in the execution ledger (F026)
4. **Censor-aware:** Cognitive sessions triggered by heartbeat go through the full pipeline
5. **Budget-conscious:** Don't burn tokens checking empty inboxes

## Architecture

```
┌─────────────────────────────────────────────────┐
│                   main.py                        │
│                                                  │
│  ┌──────────┐  ┌──────────┐  ┌───────────────┐  │
│  │ Telegram  │  │  Sleep   │  │  Heartbeat    │  │
│  │   Bot     │  │  Cycle   │  │  Runner       │  │
│  └──────────┘  └──────────┘  └───────┬───────┘  │
│                                      │           │
│                              ┌───────▼───────┐   │
│                              │  Check Registry│   │
│                              │  (thin checks) │   │
│                              └───────┬───────┘   │
│                                      │           │
│                          has findings?│           │
│                              ┌───────▼───────┐   │
│                              │    Triage      │   │
│                              │  (cognitive    │   │
│                              │   session)     │   │
│                              └───────┬───────┘   │
│                                      │           │
│                          ┌───────────┼──────┐    │
│                          ▼           ▼      ▼    │
│                       Notify      Act     Store  │
│                      (Telegram) (tools)  (facts) │
└─────────────────────────────────────────────────┘
```

## Components

### 1. HeartbeatRunner

The main loop. Runs as an `asyncio.Task` alongside the Telegram bot and sleep cycle.

```python
# nous/heartbeat.py

class HeartbeatRunner:
    """Periodic external-world monitoring loop."""

    def __init__(self, config: NousConfig, registry: CheckRegistry,
                 runner: Runner, telegram_bot: TelegramBot):
        self.interval = config.heartbeat_interval  # seconds, default 300
        self.quiet_hours = config.heartbeat_quiet_hours  # e.g. (23, 8) = 11PM-8AM
        self.registry = registry
        self.runner = runner
        self.telegram_bot = telegram_bot
        self.last_run: datetime | None = None
        self.run_count = 0

    async def start(self):
        """Main heartbeat loop."""
        while True:
            try:
                if not self._in_quiet_hours():
                    await self._tick()
                    self.run_count += 1
                    self.last_run = datetime.now(UTC)
            except Exception as e:
                logger.error(f"Heartbeat tick failed: {e}", exc_info=True)
                # Never crash the loop — log and continue

            await asyncio.sleep(self.interval)

    async def _tick(self):
        """Run all registered checks, triage findings."""
        findings: list[Finding] = []

        for check in self.registry.get_due_checks():
            try:
                result = await asyncio.wait_for(
                    check.run(),
                    timeout=check.timeout  # per-check timeout, default 30s
                )
                if result.has_updates:
                    findings.extend(result.findings)
                check.mark_success()
            except asyncio.TimeoutError:
                logger.warning(f"Check {check.name} timed out")
                check.mark_failure("timeout")
            except Exception as e:
                logger.error(f"Check {check.name} failed: {e}")
                check.mark_failure(str(e))

        if findings:
            await self._triage(findings)

    async def _triage(self, findings: list[Finding]):
        """Decide how to handle findings — notify, act, or both."""
        # Group by urgency
        urgent = [f for f in findings if f.urgency == "high"]
        normal = [f for f in findings if f.urgency == "normal"]
        low = [f for f in findings if f.urgency == "low"]

        # Urgent: notify immediately + open cognitive session
        if urgent:
            summary = format_findings(urgent)
            await self.telegram_bot.send_push(f"⚡ {summary}")
            await self._cognitive_triage(urgent)

        # Normal: batch notify + optional cognitive session
        if normal:
            summary = format_findings(normal)
            await self.telegram_bot.send_push(f"📬 {summary}")
            # Only open cognitive session if action is needed
            actionable = [f for f in normal if f.needs_action]
            if actionable:
                await self._cognitive_triage(actionable)

        # Low: just log to ledger, no notification
        if low:
            for f in low:
                logger.info(f"Heartbeat low-priority: {f.summary}")

    async def _cognitive_triage(self, findings: list[Finding]):
        """Open a session and let the cognitive pipeline process findings."""
        context = "\n".join(f"- [{f.source}] {f.summary}" for f in findings)
        message = (
            f"Heartbeat found {len(findings)} items requiring attention:\n"
            f"{context}\n\n"
            f"Review and take appropriate action."
        )
        # This goes through the full pipeline: censors, ledger, etc.
        await self.runner.process_heartbeat_message(message)

    def _in_quiet_hours(self) -> bool:
        """Respect quiet hours (user's timezone)."""
        now = datetime.now(self.config.user_timezone)
        start, end = self.quiet_hours
        if start > end:  # crosses midnight (e.g., 23-8)
            return now.hour >= start or now.hour < end
        return start <= now.hour < end
```

### 2. CheckRegistry

Manages registered checks with individual schedules.

```python
class CheckRegistry:
    """Registry of thin checks with independent schedules."""

    def __init__(self):
        self.checks: list[BaseCheck] = []

    def register(self, check: BaseCheck):
        self.checks.append(check)

    def get_due_checks(self) -> list[BaseCheck]:
        """Return checks that are due to run based on their interval."""
        now = datetime.now(UTC)
        return [c for c in self.checks if c.active and c.is_due(now)]
```

### 3. BaseCheck Protocol

```python
@dataclass
class Finding:
    source: str          # "email", "drive", "calendar", "health"
    summary: str         # human-readable one-liner
    urgency: str         # "high", "normal", "low"
    needs_action: bool   # should we open a cognitive session?
    raw_data: dict       # source-specific payload

class BaseCheck(ABC):
    name: str
    interval: int        # seconds between runs (independent of heartbeat interval)
    timeout: int = 30    # per-check timeout
    active: bool = True
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
            logger.warning(f"Check {self.name} disabled after {self.max_failures} failures")

    @abstractmethod
    async def run(self) -> CheckResult: ...
```

### 4. Concrete Checks

#### EmailCheck

```python
class EmailCheck(BaseCheck):
    """Check for unread emails via IMAP. No LLM needed."""

    name = "email"
    interval = 180  # every 3 minutes

    def __init__(self, config: NousConfig):
        self.host = config.email_imap_host
        self.user = config.email_user
        self.password = config.email_password
        self.seen_ids: set[str] = set()  # track already-reported

    async def run(self) -> CheckResult:
        findings = []
        # Run IMAP in executor (blocking I/O)
        messages = await asyncio.get_event_loop().run_in_executor(
            None, self._fetch_unread
        )
        for msg in messages:
            if msg["id"] not in self.seen_ids:
                self.seen_ids.add(msg["id"])
                urgency = self._classify_urgency(msg)
                findings.append(Finding(
                    source="email",
                    summary=f"From: {msg['from']} — {msg['subject']}",
                    urgency=urgency,
                    needs_action=(urgency != "low"),
                    raw_data=msg,
                ))
        return CheckResult(has_updates=bool(findings), findings=findings)

    def _classify_urgency(self, msg: dict) -> str:
        """Simple rule-based urgency. No LLM."""
        sender = msg["from"].lower()
        # Tim is always high priority
        if "tfatykhov" in sender:
            return "high"
        # Known contacts = normal
        if any(k in sender for k in ["maechkina"]):
            return "normal"
        return "low"

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

#### DriveCheck

```python
class DriveCheck(BaseCheck):
    """Check Google Drive for new/modified files. Uses F224 gdrive integration."""

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
                needs_action=False,  # inform only unless shared with specific intent
                raw_data=file,
            ))
        return CheckResult(has_updates=bool(findings), findings=findings)
```

#### HealthCheck

```python
class HealthCheck(BaseCheck):
    """Internal health monitoring. Checks censors, decisions, memory."""

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
            ))

        return CheckResult(has_updates=bool(findings), findings=findings)
```

## Configuration

New config fields in `NousConfig`:

```python
# Heartbeat
heartbeat_enabled: bool = True
heartbeat_interval: int = 300           # base loop interval (seconds)
heartbeat_quiet_hours: tuple[int, int] = (23, 8)  # start, end (user TZ)
heartbeat_user_timezone: str = "America/New_York"

# Email check
heartbeat_email_enabled: bool = True
heartbeat_email_interval: int = 180     # seconds
heartbeat_email_imap_host: str = "imap.gmail.com"

# Drive check
heartbeat_drive_enabled: bool = True
heartbeat_drive_interval: int = 600     # seconds

# Health check
heartbeat_health_enabled: bool = True
heartbeat_health_interval: int = 3600   # seconds
```

Environment variables:
```
NOUS_HEARTBEAT_ENABLED=true
NOUS_HEARTBEAT_INTERVAL=300
NOUS_HEARTBEAT_QUIET_START=23
NOUS_HEARTBEAT_QUIET_END=8
NOUS_HEARTBEAT_TIMEZONE=America/New_York
```

## Integration Points

### main.py startup

```python
# After existing setup
if config.heartbeat_enabled:
    registry = CheckRegistry()

    if config.heartbeat_email_enabled:
        registry.register(EmailCheck(config))
    if config.heartbeat_drive_enabled:
        registry.register(DriveCheck(config, gdrive))
    if config.heartbeat_health_enabled:
        registry.register(HealthCheck(heart, brain))

    heartbeat = HeartbeatRunner(config, registry, runner, telegram_bot)
    asyncio.create_task(heartbeat.start())
```

### Runner.process_heartbeat_message()

New method on Runner that opens a special heartbeat session:

```python
async def process_heartbeat_message(self, message: str):
    """Process heartbeat findings through the cognitive pipeline."""
    session = await self.create_session(
        source="heartbeat",
        metadata={"type": "heartbeat", "automated": True}
    )
    try:
        response = await self.process_turn(session.id, message)
        # Log to execution ledger
        await self.ledger.record_action(
            session_id=session.id,
            action_type="heartbeat_triage",
            input_text=message,
            output_text=response,
        )
    finally:
        await self.end_session(session.id)
```

### REST API

```
GET  /heartbeat/status     → { enabled, last_run, run_count, checks: [...] }
POST /heartbeat/trigger    → Force immediate tick (like /sleep/trigger)
PUT  /heartbeat/config     → Update intervals, quiet hours at runtime
```

### Execution Ledger (F026)

All heartbeat actions — checks run, findings, notifications sent, cognitive sessions opened — are recorded in the ledger. This provides:
- Audit trail of what Nous checked and when
- Evidence for claim verification (did Nous actually check email, or hallucinate checking?)
- Data for procedure learning (which checks produce useful findings?)

## Phases

### Phase 1: Core Loop + Email
- HeartbeatRunner, CheckRegistry, BaseCheck
- EmailCheck with IMAP
- Telegram notification
- REST status/trigger endpoints
- Config in NousConfig

### Phase 2: Drive + Health
- DriveCheck using existing gdrive integration
- HealthCheck (censors, decisions, facts)
- Cognitive triage for actionable findings

### Phase 3: Procedure Learning
- Convert check configs to procedures in Heart
- F012 K-line learning can optimize check intervals
- Nous can create new checks via `learn_skill`

### Phase 4: Calendar + Custom Checks
- CalendarCheck (Google Calendar API)
- User-defined checks via procedure creation
- Adaptive scheduling (check more often when active, less when quiet)

## Risks & Mitigations

| Risk | Mitigation |
|------|------------|
| Token burn from unnecessary cognitive sessions | Hybrid: thin checks first, LLM only when needed |
| IMAP connection failures crashing loop | Per-check circuit breaker (5 failures → disable) |
| Quiet hours misconfiguration | Default to conservative (11PM-8AM), runtime-adjustable |
| Heartbeat session conflicts with user sessions | Heartbeat sessions are short-lived, tagged as automated |
| Notification spam | Urgency classification + batching + quiet hours |
| Stale seen_ids memory leak | Prune seen_ids older than 24h on each tick |

## Open Questions

1. **Should heartbeat sessions count toward token budgets?** Probably yes, with a separate daily cap.
2. **Should findings be stored as facts?** Probably not — they're ephemeral. Log in ledger only.
3. **Should Nous learn urgency classification?** Phase 3 — start with rules, let F029 trajectory learning improve.
4. **Multi-agent heartbeat?** If Nous gets agent-per-task (F024 DAG), should each agent have its own heartbeat? Probably overkill for now.
