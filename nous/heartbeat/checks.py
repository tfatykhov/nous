"""Built-in heartbeat checks (F034 + F034.2).

HealthCheck — system health indicators (stale facts, unreviewed decisions, etc.)
SelfInitiatedCheck — pending actions, due schedules, promise tracking, temporal awareness
EmailCheck — optional IMAP email polling with LLM classification + sender reputation
DriveCheck — Google Drive monitoring with significance scoring + cross-reference
"""

from __future__ import annotations

import asyncio
import imaplib
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from typing import Any

from nous.brain import Brain
from nous.brain.embeddings import EmbeddingProvider
from nous.config import Settings
from nous.heart import Heart
from nous.heartbeat.registry import BaseCheck
from nous.heartbeat.schemas import CheckResult, Finding, TunableParam

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# HealthCheck
# ------------------------------------------------------------------


class HealthCheck(BaseCheck):
    """Periodic system health check (permanent).

    Checks for unreviewed decisions, high-false-positive censors,
    stale facts, and low-effectiveness procedures.

    F034.2: Uses tunable parameters for thresholds.
    """

    name = "health"
    timeout = 30

    def __init__(self, heart: Heart, brain: Brain, settings: Settings) -> None:
        super().__init__()
        self._heart = heart
        self._brain = brain
        self.interval = settings.heartbeat_health_interval
        self._params = {
            "stale_decision_days": TunableParam("stale_decision_days", 7, 3, 30, 1),
            "stale_fact_days": TunableParam("stale_fact_days", 30, 7, 90, 5),
            "low_effectiveness_threshold": TunableParam("low_effectiveness_threshold", 0.5, 0.3, 0.8, 0.05),
            "max_findings_per_run": TunableParam("max_findings_per_run", 10, 3, 25, 1),
        }

    async def run(self) -> CheckResult:
        findings: list[Finding] = []
        max_findings = int(self.get_param_value("max_findings_per_run"))

        # 1. Unreviewed decisions
        try:
            stale_days = int(self.get_param_value("stale_decision_days"))
            unreviewed = await self._brain.get_unreviewed(max_age_days=stale_days)
            if unreviewed and len(findings) < max_findings:
                findings.append(Finding(
                    source="brain",
                    summary=f"{len(unreviewed)} decisions pending review (oldest {stale_days} days)",
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
            if high_fp and len(findings) < max_findings:
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
            stale_fact_days = int(self.get_param_value("stale_fact_days"))
            stale_count = await self._heart.facts.count_stale(older_than_days=stale_fact_days)
            if stale_count > 10 and len(findings) < max_findings:
                findings.append(Finding(
                    source="facts",
                    summary=f"{stale_count} facts not accessed in {stale_fact_days}+ days",
                    urgency="low",
                    needs_action=False,
                    raw_data={"count": stale_count},
                ))
        except Exception:
            logger.debug("HealthCheck: count_stale failed", exc_info=True)

        # 4. Low-effectiveness procedures
        try:
            threshold = self.get_param_value("low_effectiveness_threshold")
            low_procs = await self._heart.procedures.get_low_effectiveness(threshold=threshold)
            if low_procs and len(findings) < max_findings:
                findings.append(Finding(
                    source="procedures",
                    summary=f"{len(low_procs)} procedures below {threshold:.0%} effectiveness",
                    urgency="normal",
                    needs_action=True,
                    raw_data={"procedure_ids": [str(p.id) for p in low_procs]},
                ))
        except Exception:
            logger.debug("HealthCheck: get_low_effectiveness failed", exc_info=True)

        # Enforce max_findings limit
        findings = findings[:max_findings]

        return CheckResult(
            has_updates=bool(findings),
            findings=findings,
        )


# ------------------------------------------------------------------
# SelfInitiatedCheck
# ------------------------------------------------------------------

# F047: _OBSERVATION_PATTERNS now owned by nous.heart.actionability.
# Re-exported here for backward-compat with any callers still importing
# from this module. Authoritative source: nous/heart/actionability.py.
from nous.heart.actionability import _OBSERVATION_PATTERNS  # noqa: E402, F401

PENDING_PROTOTYPES = [
    "I need to follow up on this",
    "This task is waiting for completion",
    "Tim asked me to do this and I haven't yet",
    "This should be revisited soon",
    "Action required but not yet taken",
]

# #369: episode summaries that open as a question or conversational filler are
# not commitments — _promise_scan skips them before flagging.
_QUESTION_PREFIX = re.compile(
    r"^\s*(what|how|where|when|why|who|is|are|do|does|did|can|could|should|would|hey|ok|okay)\b",
    re.IGNORECASE,
)


class SelfInitiatedCheck(BaseCheck):
    """Check for pending actions and due schedules (permanent).

    F034.2: Uses embedding-based search, promise tracking, and temporal
    awareness with graceful degradation to keyword search.
    """

    name = "self_initiated"
    timeout = 20

    def __init__(
        self,
        heart: Heart,
        brain: Brain,
        settings: Settings,
        embeddings: EmbeddingProvider | None = None,
    ) -> None:
        super().__init__()
        self._heart = heart
        self._brain = brain
        self.interval = settings.heartbeat_self_initiated_interval
        self._embeddings = embeddings
        self._prototype_cache: list[list[float]] | None = None
        self._params = {
            "similarity_threshold": TunableParam("similarity_threshold", 0.75, 0.6, 0.9, 0.02),
            "lookback_days": TunableParam("lookback_days", 14, 3, 30, 1),
            "max_pending_items": TunableParam("max_pending_items", 5, 2, 15, 1),
            # #369: upper bound on the age-based promise heuristic (hours).
            # Episodes older than this are too old to be actionable.
            "max_stale_age_hours": TunableParam("max_stale_age_hours", 336, 72, 720, 24),
        }

    async def _ensure_prototypes(self) -> list[list[float]]:
        """Embed prototype strings on first call, cache result."""
        if self._prototype_cache is not None:
            return self._prototype_cache
        if self._embeddings is None:
            return []
        try:
            self._prototype_cache = await self._embeddings.embed_batch(PENDING_PROTOTYPES)
        except Exception:
            logger.debug("SelfInitiatedCheck: prototype embedding failed", exc_info=True)
            self._prototype_cache = []
        return self._prototype_cache

    async def _embedding_search(self) -> list[Finding]:
        """Search recent facts using cosine similarity against pending prototypes."""
        if self._embeddings is None:
            return []

        prototypes = await self._ensure_prototypes()
        if not prototypes:
            return []

        findings: list[Finding] = []
        threshold = self.get_param_value("similarity_threshold")
        lookback_days = int(self.get_param_value("lookback_days"))
        max_items = int(self.get_param_value("max_pending_items"))

        # Search facts using each prototype query
        seen_fact_ids: set[str] = set()
        for i, proto_text in enumerate(PENDING_PROTOTYPES):
            if len(findings) >= max_items:
                break
            try:
                results = await self._heart.facts.search(
                    proto_text,
                    limit=5,
                )
                for fact in results:
                    fid = str(fact.id)
                    if fid in seen_fact_ids:
                        continue
                    seen_fact_ids.add(fid)

                    # Skip person/identity category facts — never actionable.
                    # Lowercase tags for comparison; schema doesn't enforce casing.
                    fact_category = getattr(fact, "category", None) or ""
                    fact_tags_lower = {t.lower() for t in (getattr(fact, "tags", None) or [])}
                    if fact_category == "person" or fact_tags_lower & {"resolved", "identity"}:
                        continue

                    # F047: Prefer persisted actionable verdict.
                    # Fallback path for NULL (unclassified) rows uses
                    # positive-wins logic (action patterns beat observation
                    # patterns) — fixing the PR #335 review P1.
                    score = getattr(fact, "score", 0.0) or 0.0
                    actionable = getattr(fact, "actionable", None)

                    if actionable == True:  # noqa: E712 — SQLite may return 1/0
                        is_pending = True
                    elif actionable == False:  # noqa: E712
                        is_pending = False
                    else:
                        # Legacy fallback — row hasn't been classified yet.
                        # Positive action wins over observation substring.
                        if self._looks_like_pending(fact.content):
                            is_pending = True
                        elif self._is_observation(fact.content):
                            is_pending = False
                        else:
                            is_pending = score >= threshold

                    if is_pending:
                        findings.append(Finding(
                            source="facts",
                            summary=f"Pending action: {fact.content[:100]}",
                            urgency="normal",
                            needs_action=True,
                            raw_data={"fact_id": fid, "detection": "embedding"},
                        ))
                        if len(findings) >= max_items:
                            break
            except Exception:
                logger.debug("SelfInitiatedCheck: embedding search failed for prototype %d", i, exc_info=True)

        return findings

    async def _promise_scan(self) -> list[Finding]:
        """Search recent episode summaries for unresolved commitments."""
        findings: list[Finding] = []
        max_items = int(self.get_param_value("max_pending_items"))
        max_stale_hours = float(self.get_param_value("max_stale_age_hours"))

        promise_queries = [
            "I'll look into",
            "let me research",
            "I'll draft",
            "unfinished commitment",
            "ongoing task not completed",
        ]

        seen_episode_ids: set[str] = set()
        for query in promise_queries:
            if len(findings) >= max_items:
                break
            try:
                episodes = await self._heart.search_episodes(query, limit=3)
                for ep in episodes:
                    eid = str(ep.id)
                    if eid in seen_episode_ids:
                        continue
                    seen_episode_ids.add(eid)

                    # Check if episode is ongoing or old enough to be stale.
                    # #369: the age-based heuristic is bounded above — beyond
                    # max_stale_age_hours it's too old to be actionable.
                    # outcome=ongoing is an explicit state and is NOT capped.
                    is_ongoing = getattr(ep, "outcome", None) == "ongoing"
                    started = getattr(ep, "started_at", None)
                    is_stale = False
                    if started:
                        age_hours = (datetime.now(UTC) - started).total_seconds() / 3600
                        is_stale = 48 < age_hours <= max_stale_hours

                    if is_ongoing or is_stale:
                        summary_text = getattr(ep, "summary", None) or getattr(ep, "title", "")
                        # #369: questions / conversational openers are not
                        # commitments — skip before flagging.
                        if _QUESTION_PREFIX.match(summary_text):
                            continue
                        findings.append(Finding(
                            source="episodes",
                            summary=f"Unresolved commitment: {summary_text[:100]}",
                            urgency="normal",
                            needs_action=True,
                            raw_data={
                                "episode_id": eid,
                                "detection": "promise_scan",
                                "outcome": getattr(ep, "outcome", None),
                            },
                        ))
                        if len(findings) >= max_items:
                            break
            except Exception:
                logger.debug("SelfInitiatedCheck: promise scan failed for '%s'", query, exc_info=True)

        return findings

    async def _temporal_scan(self) -> list[Finding]:
        """Parse explicit temporal markers from facts for approaching deadlines."""
        findings: list[Finding] = []

        try:
            from dateutil import parser as dateutil_parser  # type: ignore[import-untyped]
        except ImportError:
            logger.debug("SelfInitiatedCheck: dateutil not available, skipping temporal scan")
            return findings

        max_items = int(self.get_param_value("max_pending_items"))
        lookback_days = int(self.get_param_value("lookback_days"))

        # Search for facts with temporal language
        temporal_queries = ["by Friday", "next week", "deadline", "due date", "end of week", "before Monday"]
        now = datetime.now(UTC)
        upcoming_window = timedelta(days=3)  # Surface items due within 3 days

        seen_fact_ids: set[str] = set()
        for query in temporal_queries:
            if len(findings) >= max_items:
                break
            try:
                results = await self._heart.facts.search(query, limit=3)
                for fact in results:
                    fid = str(fact.id)
                    if fid in seen_fact_ids:
                        continue
                    seen_fact_ids.add(fid)

                    # Try to extract a date from the fact content
                    parsed_date = self._try_parse_date(dateutil_parser, fact.content)
                    if parsed_date is not None:
                        delta = parsed_date - now
                        if timedelta(0) <= delta <= upcoming_window:
                            findings.append(Finding(
                                source="facts",
                                summary=f"Approaching deadline: {fact.content[:100]}",
                                urgency="high" if delta.days <= 1 else "normal",
                                needs_action=True,
                                raw_data={
                                    "fact_id": fid,
                                    "detection": "temporal",
                                    "parsed_date": parsed_date.isoformat(),
                                },
                            ))
                            if len(findings) >= max_items:
                                break
            except Exception:
                logger.debug("SelfInitiatedCheck: temporal scan failed for '%s'", query, exc_info=True)

        return findings

    @staticmethod
    def _try_parse_date(dateutil_parser: Any, text: str) -> datetime | None:
        """Try to extract a date from text using dateutil fuzzy parsing."""
        try:
            parsed, _ = dateutil_parser.parse(text, fuzzy_with_tokens=True)
            # Only return if it's in the future
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            if parsed > datetime.now(UTC):
                return parsed
        except (ValueError, OverflowError, TypeError):
            pass
        return None

    async def run(self) -> CheckResult:
        findings: list[Finding] = []
        max_items = int(self.get_param_value("max_pending_items"))

        # 1. Try embedding-based search first (if embeddings available)
        if self._embeddings is not None:
            try:
                embedding_findings = await self._embedding_search()
                findings.extend(embedding_findings)
            except Exception:
                logger.debug("SelfInitiatedCheck: embedding search failed, falling back", exc_info=True)

        # 2. Fall back to / supplement with keyword-based search
        if len(findings) < max_items:
            try:
                pending_facts = await self._heart.facts.search(
                    "follow-up pending action TODO",
                    category="rule",
                    limit=5,
                )
                seen_ids = {f.raw_data.get("fact_id") for f in findings}
                for fact in pending_facts:
                    if len(findings) >= max_items:
                        break
                    fid = str(fact.id)
                    if fid in seen_ids:
                        continue
                    # F047: honour persisted verdict in the keyword fallback
                    # too — otherwise classifier's actionable=False on rule
                    # facts would leak through here despite being suppressed
                    # on the embedding path.
                    actionable = getattr(fact, "actionable", None)
                    if actionable == False:  # noqa: E712 — SQLite 0/1
                        continue
                    if actionable == True or self._looks_like_pending(fact.content):  # noqa: E712
                        findings.append(Finding(
                            source="facts",
                            summary=f"Pending action: {fact.content[:100]}",
                            urgency="normal",
                            needs_action=True,
                            raw_data={"fact_id": fid, "detection": "keyword"},
                        ))
            except Exception:
                logger.debug("SelfInitiatedCheck: keyword fact search failed", exc_info=True)

        # 3. Promise tracking
        if len(findings) < max_items:
            try:
                promise_findings = await self._promise_scan()
                remaining = max_items - len(findings)
                findings.extend(promise_findings[:remaining])
            except Exception:
                logger.debug("SelfInitiatedCheck: promise scan failed", exc_info=True)

        # 4. Temporal awareness
        if len(findings) < max_items:
            try:
                temporal_findings = await self._temporal_scan()
                remaining = max_items - len(findings)
                findings.extend(temporal_findings[:remaining])
            except Exception:
                logger.debug("SelfInitiatedCheck: temporal scan failed", exc_info=True)

        # 5. Due schedules
        if len(findings) < max_items:
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

        # Enforce max limit
        findings = findings[:max_items]

        return CheckResult(
            has_updates=bool(findings),
            findings=findings,
        )

    @staticmethod
    def _looks_like_pending(content: str) -> bool:
        """Detect actionable pending items, rejecting observations/descriptions.

        Uses positive + negative patterns. Positive match is required.
        Negative patterns only reject when no positive match is found.
        """
        lower = content.lower()

        # Positive patterns — action-oriented language (checked first)
        action_patterns = [
            "todo",
            "follow-up on",
            "follow up on",
            "action needed",
            "remind me",
            "i need to",
            "need to finish",
            "need to complete",
            "need to send",
            "need to review",
            "needs to review",
            "need to check",
            "need to restart",
            "needs to be",
            "should follow up",
            "must complete",
            "waiting for response",
            "hasn't been done",
            "not yet completed",
            "pending review",
            "pending approval",
        ]
        has_action = any(p in lower for p in action_patterns)

        if has_action:
            return True

        # Negative patterns — observational/descriptive language
        # Only checked when no positive match (positive wins)
        has_observation = any(p in lower for p in _OBSERVATION_PATTERNS)
        if has_observation:
            return False

        # No positive match and no negative match — not pending
        return False

    @staticmethod
    def _is_observation(content: str) -> bool:
        """Detect observational/descriptive content that should not be flagged."""
        return any(p in content.lower() for p in _OBSERVATION_PATTERNS)


# ------------------------------------------------------------------
# EmailCheck
# ------------------------------------------------------------------


class EmailCheck(BaseCheck):
    """Optional IMAP email polling check.

    F034.2: Tiered classification (sender reputation -> LLM -> keywords),
    budget-aware LLM calls, sender reputation learning.
    """

    name = "email"
    timeout = 30
    urgent_override = True  # runs even during quiet hours

    def __init__(
        self,
        settings: Settings,
        llm_callable: Callable[..., Any] | None = None,
        budget_check: Callable[[], bool] | None = None,
    ) -> None:
        super().__init__()
        self.interval = settings.heartbeat_email_interval
        self._host = settings.heartbeat_email_imap_host
        self._user = settings.email_user
        self._password = settings.email_password
        self._seen_ids: dict[str, datetime] = {}

        # F034.2: LLM classification support
        self._llm_callable = llm_callable
        self._budget_check = budget_check

        # F034.2: Sender reputation (sender -> list of past classifications, max 10)
        self._sender_reputation: dict[str, list[tuple[str, datetime]]] = {}

        self._params = {
            "sender_reputation_weight": TunableParam("sender_reputation_weight", 0.5, 0.0, 1.0, 0.05),
            "llm_classification_budget": TunableParam("llm_classification_budget", 500, 200, 2000, 50),
        }

    async def run(self) -> CheckResult:
        if not self._user or not self._password:
            return CheckResult()

        self._prune_seen()

        try:
            messages = await asyncio.to_thread(self._fetch_unseen)
        except Exception:
            logger.warning("EmailCheck: IMAP fetch failed", exc_info=True)
            raise

        new_count = sum(1 for mid, _, _ in messages if mid not in self._seen_ids)
        logger.info(
            "EmailCheck: fetched %d unseen, %d new (seen cache: %d)",
            len(messages), new_count, len(self._seen_ids),
        )

        findings: list[Finding] = []
        for msg_id, subject, sender in messages:
            if msg_id in self._seen_ids:
                continue
            self._seen_ids[msg_id] = datetime.now(UTC)

            urgency = await self._classify_email(subject, sender)
            logger.info("EmailCheck: new email [%s] from %s — %s", urgency, sender, subject[:60])
            findings.append(Finding(
                source="email",
                summary=f"New email from {sender}: {subject[:80]}",
                urgency=urgency,
                needs_action=True,
                raw_data={"message_id": msg_id, "subject": subject, "sender": sender},
            ))

        return CheckResult(
            has_updates=bool(findings),
            findings=findings,
        )

    async def _classify_email(self, subject: str, sender: str, body_preview: str = "") -> str:
        """Tiered email classification: reputation -> LLM -> keywords.

        Returns urgency string: "high", "normal", or "low".
        """
        # Tier 0: Sender reputation (if enough history)
        rep = self._get_sender_reputation(sender)
        if rep is not None:
            return rep

        # Tier 1: LLM classification (if available and budget allows)
        if self._llm_callable and (self._budget_check is None or self._budget_check()):
            try:
                result = await self._llm_classify(subject, sender, body_preview)
                self._update_reputation(sender, result)
                return result
            except Exception:
                logger.debug("LLM email classification failed, falling back to keywords")

        # Tier 2: Keyword heuristics (always available)
        result = self._keyword_classify(subject, sender)
        self._update_reputation(sender, result)
        return result

    def _get_sender_reputation(self, sender: str) -> str | None:
        """Check if we have enough reputation data for this sender.

        Returns the dominant classification if 5+ consistent entries,
        None otherwise (indicating LLM/keyword fallback needed).
        """
        entries = self._sender_reputation.get(sender, [])
        if len(entries) < 5:
            return None

        # Count classifications
        counts: dict[str, int] = {}
        for classification, _ in entries:
            counts[classification] = counts.get(classification, 0) + 1

        # If dominant classification is >60% of entries, use it
        total = len(entries)
        for classification, count in counts.items():
            if count / total >= 0.6:
                return classification

        return None

    def _update_reputation(self, sender: str, classification: str) -> None:
        """Update sender reputation with a new classification."""
        now = datetime.now(UTC)
        if sender not in self._sender_reputation:
            self._sender_reputation[sender] = []

        entries = self._sender_reputation[sender]

        # 30-day decay: remove old entries
        cutoff = now - timedelta(days=30)
        entries[:] = [(c, t) for c, t in entries if t > cutoff]

        # Add new entry, cap at 10
        entries.append((classification, now))
        if len(entries) > 10:
            entries[:] = entries[-10:]

    async def _llm_classify(self, subject: str, sender: str, body_preview: str = "") -> str:
        """Use LLM for email classification. Returns urgency string."""
        prompt = (
            "Classify this email. Reply with ONE word: urgent, actionable, informational, spam.\n"
            f"From: {sender}\n"
            f"Subject: {subject}\n"
            f"Preview: {body_preview[:200]}"
        )

        response = await self._llm_callable(prompt)  # type: ignore[misc]
        response_lower = response.strip().lower()

        # Map LLM response to urgency
        if "urgent" in response_lower:
            return "high"
        elif "actionable" in response_lower:
            return "normal"
        elif "spam" in response_lower:
            return "low"
        elif "informational" in response_lower:
            return "low"
        else:
            # Unknown response, default to normal
            return "normal"

    @staticmethod
    def _keyword_classify(subject: str, sender: str) -> str:
        """Rule-based urgency classification. All emails are at least normal."""
        lower = subject.lower()
        if any(w in lower for w in ["urgent", "critical", "emergency", "asap"]):
            return "high"
        if any(w in lower for w in ["newsletter", "unsubscribe", "digest", "weekly update"]):
            return "low"
        return "normal"

    def _fetch_unseen(self) -> list[tuple[str, str, str]]:
        """Synchronous IMAP fetch — runs in thread."""
        messages: list[tuple[str, str, str]] = []
        mail = imaplib.IMAP4_SSL(self._host)
        try:
            mail.login(self._user, self._password)
            mail.select("INBOX")
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


# ------------------------------------------------------------------
# DriveCheck
# ------------------------------------------------------------------

# Default folder-to-project mapping (empty, user configures via config/REST)
DEFAULT_FOLDER_MAP: dict[str, str] = {}


class DriveCheck(BaseCheck):
    """Check Google Drive for recently modified files.

    F034.2: Adds significance scoring, folder mapping, and
    conversation cross-reference via Heart episodes.
    """

    name = "drive"
    timeout = 30

    def __init__(
        self,
        settings: Settings,
        heart: Heart | None = None,
    ) -> None:
        super().__init__()
        self.interval = settings.heartbeat_drive_interval
        self._last_check_time: datetime | None = None
        self._heart = heart

        # Lazy-init GDrive to avoid crashing if creds are missing
        self._gdrive = None

        self._params = {
            "significance_threshold": TunableParam("significance_threshold", 1.0, 0.0, 2.0, 0.5),
            "cross_reference_lookback_hours": TunableParam("cross_reference_lookback_hours", 48, 6, 168, 6),
        }

    def _ensure_gdrive(self) -> None:
        if self._gdrive is None:
            from nous.integrations.gdrive import GDrive
            self._gdrive = GDrive()

    async def run(self) -> CheckResult:
        try:
            self._ensure_gdrive()
        except Exception:
            logger.warning("DriveCheck: GDrive init failed", exc_info=True)
            raise

        cutoff = self._last_check_time or (datetime.now(UTC) - timedelta(hours=1))
        self._last_check_time = datetime.now(UTC)

        # Query Drive for files modified since cutoff
        cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M:%S")
        query = f"modifiedTime > '{cutoff_str}' and trashed = false"

        try:
            files = await asyncio.to_thread(
                self._gdrive.list_files, query=query
            )
        except Exception:
            logger.warning("DriveCheck: list_files failed", exc_info=True)
            raise

        sig_threshold = self.get_param_value("significance_threshold")

        findings: list[Finding] = []
        for f in files:
            significance = self._score_significance(f)
            sig_value = {"low": 0.0, "normal": 1.0, "high": 2.0}.get(significance, 1.0)

            # Skip files below significance threshold
            if sig_value < sig_threshold:
                continue

            file_name = f.get("name", "?")
            mime_type = f.get("mimeType", "?")

            # Build summary with context
            summary = f"Modified: {file_name} ({mime_type})"

            # Cross-reference with recent conversations
            context = await self._contextualize(file_name)
            if context:
                summary = f"{summary} — {context}"

            # Map urgency from significance
            urgency: str = "low"
            if significance == "high":
                urgency = "normal"
            elif significance == "normal":
                urgency = "low"

            findings.append(Finding(
                source="drive",
                summary=summary,
                urgency=urgency,
                needs_action=significance == "high",
                raw_data={**f, "significance": significance},
            ))

        return CheckResult(
            has_updates=bool(findings),
            findings=findings,
        )

    @staticmethod
    def _score_significance(file_data: dict) -> str:
        """Score file modification significance based on metadata.

        Returns "low", "normal", or "high".
        """
        mime_type = file_data.get("mimeType", "")

        # New files shared by someone else are high significance
        sharing_user = file_data.get("sharingUser")
        if sharing_user:
            return "high"

        # Google Docs/Sheets/Slides edits are normally significant
        if mime_type.startswith("application/vnd.google-apps."):
            # Check if recently created (might be auto-save)
            created = file_data.get("createdTime", "")
            modified = file_data.get("modifiedTime", "")
            if created and modified and created != modified:
                return "normal"
            return "low"  # Same-day creation, likely auto-save

        # Images and PDFs are normally significant
        if mime_type.startswith("image/") or mime_type == "application/pdf":
            return "normal"

        # Everything else is low
        return "low"

    async def _contextualize(self, file_name: str) -> str | None:
        """Search recent conversations for references to this file."""
        if self._heart is None:
            return None

        try:
            episodes = await self._heart.search_episodes(file_name, limit=3)
            if episodes:
                ep = episodes[0]
                score = getattr(ep, "score", 0.0) or 0.0
                if score > 0.7:
                    summary_text = getattr(ep, "summary", None) or getattr(ep, "title", "")
                    if summary_text:
                        return f"Related to recent conversation: {summary_text[:100]}"
        except Exception:
            logger.debug("DriveCheck: contextualize failed for '%s'", file_name, exc_info=True)

        return None


# ------------------------------------------------------------------
# BehaviorDriftCheck (F035.3)
# ------------------------------------------------------------------


class BehaviorDriftCheck(BaseCheck):
    """Periodic behavioral drift detection (F035.3).

    Captures metric snapshots, stores to DB, compares against
    rolling 7-day baseline using z-score analysis.
    """

    name = "behavior_drift"
    timeout = 30

    def __init__(self, heart: Heart, brain: Brain, settings: Settings, bus_stats: Any = None, db: Any = None) -> None:
        super().__init__()
        self._heart = heart
        self._brain = brain
        self._settings = settings
        self._bus_stats = bus_stats
        self._db = db
        from nous.observability.drift import DriftDetector
        self._detector = DriftDetector()
        self._last_snapshot: Any = None  # BehaviorSnapshot
        self._last_anomalies: list[dict] = []  # Serialized anomalies for DB persistence
        self.interval = getattr(settings, 'drift_detection_interval', 3600)

    async def run(self) -> CheckResult:
        from nous.observability.snapshots import BehaviorSnapshot
        findings: list[Finding] = []
        try:
            snapshot = await self._capture_snapshot()
            baseline = await self._load_baseline(hours=168)
            self._last_anomalies = []
            if baseline:
                anomalies = self._detector.detect(snapshot, baseline)
                self._last_anomalies = [
                    {"metric": a.metric, "current": a.current, "mean": a.mean,
                     "stddev": a.stddev, "z_score": a.z_score, "direction": a.direction,
                     "severity": a.severity}
                    for a in anomalies
                ]
                for a in anomalies:
                    findings.append(Finding(
                        source="drift",
                        summary=f"{a.metric}: {a.current} ({a.direction} from {a.mean} +/- {a.stddev})",
                        urgency="high" if a.severity == "alert" else "normal",
                        needs_action=a.severity == "alert",
                        raw_data={"metric": a.metric, "current": a.current, "mean": a.mean, "stddev": a.stddev, "z_score": a.z_score},
                    ))
            await self._store_snapshot(snapshot)
            self._last_snapshot = snapshot
        except Exception:
            logger.exception("BehaviorDriftCheck failed")
        return CheckResult(has_updates=bool(findings), findings=findings)

    async def _capture_snapshot(self):
        from nous.observability.snapshots import BehaviorSnapshot
        now = datetime.now(UTC)
        fact_count = episode_count = censor_count = procedure_count = 0
        if self._db:
            try:
                async with self._db.session() as session:
                    from sqlalchemy import text
                    result = await session.execute(text(
                        "SELECT "
                        "(SELECT COUNT(*) FROM heart.facts WHERE active = true) AS facts, "
                        "(SELECT COUNT(*) FROM heart.episodes) AS episodes, "
                        "(SELECT COUNT(*) FROM heart.censors WHERE active = true) AS censors, "
                        "(SELECT COUNT(*) FROM heart.procedures WHERE active = true) AS procedures"
                    ))
                    row = result.fetchone()
                    if row:
                        fact_count, episode_count, censor_count, procedure_count = row.facts, row.episodes, row.censors, row.procedures
            except Exception:
                logger.debug("Snapshot: DB query failed", exc_info=True)

        bus_data = self._bus_stats.to_dict() if self._bus_stats else {}
        handlers = bus_data.get("handlers", {})
        total_errors = sum(h.get("errors", 0) for h in handlers.values())
        total_invocations = sum(h.get("invocations", 0) for h in handlers.values())
        error_rate = total_errors / total_invocations if total_invocations else 0.0

        prev = self._last_snapshot
        return BehaviorSnapshot(
            timestamp=now,
            fact_count=fact_count, fact_count_delta=fact_count - (prev.fact_count if prev else fact_count),
            episode_count=episode_count, episode_count_delta=episode_count - (prev.episode_count if prev else episode_count),
            active_censor_count=censor_count, active_censor_delta=censor_count - (prev.active_censor_count if prev else censor_count),
            procedure_count=procedure_count, decision_count=0,
            events_processed=bus_data.get("total_processed", 0),
            events_dropped=bus_data.get("total_dropped", 0),
            handler_error_count=total_errors, handler_error_rate=round(error_rate, 4),
            turns_processed=bus_data.get("event_counts", {}).get("turn_completed", 0),
        )

    async def _store_snapshot(self, snapshot) -> None:
        if not self._db:
            return
        try:
            import json
            async with self._db.session() as session:
                from sqlalchemy import text
                await session.execute(text(
                    "INSERT INTO nous_system.behavior_snapshots (agent_id, timestamp, metrics, anomalies) "
                    "VALUES (:aid, :ts, :metrics, :anomalies)"
                ), {"aid": self._settings.agent_id, "ts": snapshot.timestamp,
                    "metrics": json.dumps(snapshot.to_metrics_dict()),
                    "anomalies": json.dumps(self._last_anomalies)})
                await session.commit()
        except Exception:
            logger.debug("Snapshot store failed", exc_info=True)

    async def _load_baseline(self, hours: int = 168) -> list:
        if not self._db:
            return []
        try:
            import json as _json
            from nous.observability.snapshots import BehaviorSnapshot
            async with self._db.session() as session:
                from sqlalchemy import text
                now = datetime.now(UTC)
                cutoff = now - timedelta(hours=hours)
                result = await session.execute(text(
                    "SELECT timestamp, metrics FROM nous_system.behavior_snapshots "
                    "WHERE agent_id = :aid AND timestamp > :cutoff ORDER BY timestamp"
                ), {"aid": self._settings.agent_id, "cutoff": cutoff})
                rows = result.fetchall()
            snapshots = []
            for row in rows:
                metrics = row.metrics if isinstance(row.metrics, dict) else _json.loads(row.metrics)
                # Build snapshot from stored metrics, defaulting missing keys to 0
                kwargs: dict[str, Any] = {"timestamp": row.timestamp}
                for k in BehaviorSnapshot.__dataclass_fields__:
                    if k == "timestamp" or k == "interval_changes":
                        continue
                    kwargs[k] = metrics.get(k, 0)
                snapshots.append(BehaviorSnapshot(**kwargs))
            return snapshots
        except Exception:
            logger.debug("Baseline load failed", exc_info=True)
            return []
