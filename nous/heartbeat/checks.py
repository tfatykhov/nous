"""Built-in heartbeat checks (F034).

HealthCheck — system health indicators (stale facts, unreviewed decisions, etc.)
SelfInitiatedCheck — pending actions, due schedules
EmailCheck — optional IMAP email polling
"""

from __future__ import annotations

import asyncio
import imaplib
import logging
from datetime import UTC, datetime

from nous.brain import Brain
from nous.config import Settings
from nous.heart import Heart
from nous.heartbeat.registry import BaseCheck
from nous.heartbeat.schemas import CheckResult, Finding

logger = logging.getLogger(__name__)


class HealthCheck(BaseCheck):
    """Periodic system health check (permanent).

    Checks for unreviewed decisions, high-false-positive censors,
    stale facts, and low-effectiveness procedures.
    """

    name = "health"
    timeout = 30

    def __init__(self, heart: Heart, brain: Brain, settings: Settings) -> None:
        super().__init__()
        self._heart = heart
        self._brain = brain
        self.interval = settings.heartbeat_health_interval

    async def run(self) -> CheckResult:
        findings: list[Finding] = []

        # 1. Unreviewed decisions
        try:
            unreviewed = await self._brain.get_unreviewed(max_age_days=7)
            if unreviewed:
                findings.append(Finding(
                    source="brain",
                    summary=f"{len(unreviewed)} decisions pending review (oldest 7 days)",
                    urgency="normal",
                    needs_action=True,
                    raw_data={"count": len(unreviewed)},
                ))
        except Exception:
            logger.debug("HealthCheck: get_unreviewed failed", exc_info=True)

        # 2. High false-positive censors
        try:
            censors = await self._heart.censors.list_active()
            high_fp = [c for c in censors if (c.false_positive_count or 0) > 5]
            if high_fp:
                findings.append(Finding(
                    source="censors",
                    summary=f"{len(high_fp)} censors with high false-positive counts",
                    urgency="normal",
                    needs_action=True,
                    raw_data={"censor_ids": [str(c.id) for c in high_fp]},
                ))
        except Exception:
            logger.debug("HealthCheck: list_active censors failed", exc_info=True)

        # 3. Stale facts
        try:
            stale_count = await self._heart.facts.count_stale(older_than_days=30)
            if stale_count > 10:
                findings.append(Finding(
                    source="facts",
                    summary=f"{stale_count} facts not accessed in 30+ days",
                    urgency="low",
                    needs_action=False,
                    raw_data={"count": stale_count},
                ))
        except Exception:
            logger.debug("HealthCheck: count_stale failed", exc_info=True)

        # 4. Low-effectiveness procedures
        try:
            low_procs = await self._heart.procedures.get_low_effectiveness(threshold=0.5)
            if low_procs:
                findings.append(Finding(
                    source="procedures",
                    summary=f"{len(low_procs)} procedures below 50% effectiveness",
                    urgency="normal",
                    needs_action=True,
                    raw_data={"procedure_ids": [str(p.id) for p in low_procs]},
                ))
        except Exception:
            logger.debug("HealthCheck: get_low_effectiveness failed", exc_info=True)

        return CheckResult(
            has_updates=bool(findings),
            findings=findings,
        )


class SelfInitiatedCheck(BaseCheck):
    """Check for pending actions and due schedules (permanent).

    Searches facts for follow-up markers and checks schedules
    that are overdue.
    """

    name = "self_initiated"
    timeout = 20

    def __init__(self, heart: Heart, brain: Brain, settings: Settings) -> None:
        super().__init__()
        self._heart = heart
        self._brain = brain
        self.interval = settings.heartbeat_self_initiated_interval

    async def run(self) -> CheckResult:
        findings: list[Finding] = []

        # 1. Search for pending action facts
        try:
            pending_facts = await self._heart.facts.search(
                "follow-up pending action TODO",
                category="rule",
                limit=5,
            )
            for fact in pending_facts:
                if self._looks_like_pending(fact.content):
                    findings.append(Finding(
                        source="facts",
                        summary=f"Pending action: {fact.content[:100]}",
                        urgency="normal",
                        needs_action=True,
                        raw_data={"fact_id": str(fact.id)},
                    ))
        except Exception:
            logger.debug("SelfInitiatedCheck: fact search failed", exc_info=True)

        # 2. Due schedules
        try:
            now = datetime.now(UTC)
            due_schedules = await self._heart.schedules.get_due(now)
            if due_schedules:
                findings.append(Finding(
                    source="schedules",
                    summary=f"{len(due_schedules)} schedule(s) past due",
                    urgency="normal",
                    needs_action=True,
                    raw_data={"count": len(due_schedules)},
                ))
        except Exception:
            logger.debug("SelfInitiatedCheck: get_due failed", exc_info=True)

        return CheckResult(
            has_updates=bool(findings),
            findings=findings,
        )

    @staticmethod
    def _looks_like_pending(content: str) -> bool:
        """Simple heuristic to detect pending action markers."""
        lower = content.lower()
        markers = ["todo", "follow-up", "pending", "action needed", "remind me", "need to"]
        return any(m in lower for m in markers)


class EmailCheck(BaseCheck):
    """Optional IMAP email polling check.

    Uses imaplib in asyncio.to_thread() to avoid blocking.
    Tracks seen message IDs with 24h pruning.
    """

    name = "email"
    timeout = 30
    urgent_override = True  # runs even during quiet hours

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self.interval = settings.heartbeat_email_interval
        self._host = settings.heartbeat_email_imap_host
        self._user = settings.email_user
        self._password = settings.email_password
        self._seen_ids: dict[str, datetime] = {}

    async def run(self) -> CheckResult:
        if not self._user or not self._password:
            return CheckResult()

        self._prune_seen()

        try:
            messages = await asyncio.to_thread(self._fetch_unseen)
        except Exception:
            logger.warning("EmailCheck: IMAP fetch failed", exc_info=True)
            raise

        findings: list[Finding] = []
        for msg_id, subject, sender in messages:
            if msg_id in self._seen_ids:
                continue
            self._seen_ids[msg_id] = datetime.now(UTC)

            urgency = self._classify_urgency(subject, sender)
            findings.append(Finding(
                source="email",
                summary=f"New email from {sender}: {subject[:80]}",
                urgency=urgency,
                needs_action=urgency != "low",
                raw_data={"message_id": msg_id, "subject": subject, "sender": sender},
            ))

        return CheckResult(
            has_updates=bool(findings),
            findings=findings,
        )

    def _fetch_unseen(self) -> list[tuple[str, str, str]]:
        """Synchronous IMAP fetch — runs in thread."""
        messages: list[tuple[str, str, str]] = []
        mail = imaplib.IMAP4_SSL(self._host)
        try:
            mail.login(self._user, self._password)
            mail.select("INBOX", readonly=True)
            _status, data = mail.search(None, "UNSEEN")
            if not data or not data[0]:
                return messages

            msg_ids = data[0].split()[-5:]  # Last 5 unseen
            for mid in msg_ids:
                _status, msg_data = mail.fetch(mid, "(BODY[HEADER.FIELDS (SUBJECT FROM MESSAGE-ID)])")
                if msg_data and msg_data[0] and isinstance(msg_data[0], tuple):
                    raw = msg_data[0][1]
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8", errors="replace")
                    subject = ""
                    sender = ""
                    msg_uid = mid.decode() if isinstance(mid, bytes) else str(mid)
                    for line in raw.split("\r\n"):
                        if line.lower().startswith("subject:"):
                            subject = line[8:].strip()
                        elif line.lower().startswith("from:"):
                            sender = line[5:].strip()
                        elif line.lower().startswith("message-id:"):
                            msg_uid = line[11:].strip()
                    messages.append((msg_uid, subject, sender))
        finally:
            try:
                mail.logout()
            except Exception:
                pass
        return messages

    def _prune_seen(self) -> None:
        """Remove seen IDs older than 24 hours."""
        now = datetime.now(UTC)
        self._seen_ids = {
            k: v for k, v in self._seen_ids.items()
            if (now - v).total_seconds() < 86400
        }

    @staticmethod
    def _classify_urgency(subject: str, sender: str) -> str:
        """Simple rule-based urgency classification."""
        lower = subject.lower()
        if any(w in lower for w in ["urgent", "critical", "emergency", "asap"]):
            return "high"
        if any(w in lower for w in ["important", "action required", "deadline"]):
            return "normal"
        return "low"
