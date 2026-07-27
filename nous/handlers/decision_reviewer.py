"""Decision Review Loop — auto-reviews decisions with verifiable outcomes.

Listens to: session_ended + periodic sweep
Tier 1: Signal-based auto-review (Error, Episode, FileExists, GitHub)

Part of spec 008.5.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

if TYPE_CHECKING:
    from nous.brain.schemas import DecisionSummary
    from nous.events import Event

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ReviewResult + ReviewSignal protocol
# ---------------------------------------------------------------------------


@dataclass
class ReviewResult:
    """Outcome from a review signal check."""

    result: str  # "success" | "partial" | "failure"
    explanation: str
    confidence: float  # 0.0-1.0
    signal_type: str


class ReviewSignal(Protocol):
    """Protocol for outcome detection signals."""

    async def check(self, decision: DecisionSummary) -> ReviewResult | None: ...


# ---------------------------------------------------------------------------
# ErrorSignal
# ---------------------------------------------------------------------------

class ErrorSignal:
    """Auto-fail decisions recorded with very low confidence.

    Audit HD-4 (2026-06-09): the former error-keyword branch matched the
    decision *description* (the plan text) — so a decision *about* a bug
    ("fix the login error") was auto-labelled `failure`. The description is
    the intended action, not the outcome, so that branch corrupted Brain
    calibration and has been removed. Only the low-confidence prior remains.

    2026-07-27: that remaining prior was measuring the wrong number. F058
    applies temperature scaling (`NOUS_CONFIDENCE_CALIBRATION_FACTOR`,
    default 0.7627) at write time, so `confidence` is the *calibrated* value
    and the 0.4 test silently became "raw < 0.5245". 37 prod decisions were
    auto-failed purely by that scaling — including one recorded at an
    honest 0.5. The threshold is a statement about what the author claimed,
    so it compares `confidence_raw` (the agent's own claim, preserved by
    F058) and falls back to `confidence` only for pre-F058 rows, where the
    stored value *is* the raw one. That also unwinds the circularity flagged
    here previously: the label no longer derives from the very number F058
    is trying to calibrate.
    """

    async def check(self, decision: DecisionSummary) -> ReviewResult | None:
        # Pre-F058 rows have confidence_raw IS NULL — there the stored
        # `confidence` IS the raw claim, so the fallback is exact.
        stated = decision.confidence_raw
        if stated is None:
            stated = decision.confidence
        if stated is not None and stated < 0.4:
            return ReviewResult(
                result="failure",
                explanation=f"Low confidence ({stated:.2f}) indicates uncertain/failed decision",
                confidence=0.9,
                signal_type="error",
            )
        return None


# ---------------------------------------------------------------------------
# EpisodeSignal
# ---------------------------------------------------------------------------

_EPISODE_OUTCOME_MAP = {
    "success": "success",
    "partial": "partial",
    "failure": "failure",
    "abandoned": "failure",
}


class EpisodeSignal:
    """Map linked episode outcome to decision outcome."""

    def __init__(self, brain):
        self._brain = brain

    async def check(self, decision: DecisionSummary) -> ReviewResult | None:
        try:
            episode = await self._brain.get_episode_for_decision(decision.id)
        except Exception:
            return None
        if episode is None:
            return None
        mapped = _EPISODE_OUTCOME_MAP.get(episode.outcome)
        if mapped is None:
            return None
        return ReviewResult(
            result=mapped,
            explanation=f"Linked episode outcome: {episode.outcome}",
            confidence=0.8,
            signal_type="episode",
        )


# ---------------------------------------------------------------------------
# FileExistsSignal
# ---------------------------------------------------------------------------

_FILE_PATH_PATTERN = re.compile(r"(?:docs|nous|tests|sql)/[\w./\-]+\.\w+")


class FileExistsSignal:
    """Check if files mentioned in decision description exist on disk."""

    async def check(self, decision: DecisionSummary) -> ReviewResult | None:
        matches = _FILE_PATH_PATTERN.findall(decision.description)
        if not matches:
            return None
        for path_str in matches:
            if Path(path_str).exists():
                return ReviewResult(
                    result="success",
                    explanation=f"Referenced file exists: {path_str}",
                    confidence=0.7,
                    signal_type="file_exists",
                )
        return None


# ---------------------------------------------------------------------------
# GitHubSignal
# ---------------------------------------------------------------------------

_PR_PATTERN = re.compile(r"(?:PR\s*#?|#)(\d+)", re.IGNORECASE)


class GitHubSignal:
    """Check GitHub PR status for decisions mentioning PRs."""

    def __init__(self, http_client, github_token: str, repo: str = "tfatykhov/nous"):
        self._http = http_client
        self._token = github_token
        self._repo = repo

    async def check(self, decision: DecisionSummary) -> ReviewResult | None:
        if not self._token:
            return None
        match = _PR_PATTERN.search(decision.description)
        if not match:
            return None
        pr_number = match.group(1)
        try:
            resp = await self._http.get(
                f"https://api.github.com/repos/{self._repo}/pulls/{pr_number}",
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Accept": "application/vnd.github.v3+json",
                },
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
            if data.get("merged"):
                return ReviewResult(
                    result="success",
                    explanation=f"PR #{pr_number} merged",
                    confidence=0.85,
                    signal_type="github",
                )
            if data.get("state") == "closed":
                return ReviewResult(
                    result="failure",
                    explanation=f"PR #{pr_number} closed without merge",
                    confidence=0.85,
                    signal_type="github",
                )
            return None
        except Exception:
            logger.warning("GitHub API check failed for PR #%s", pr_number)
            return None


# ---------------------------------------------------------------------------
# DecisionReviewer handler
# ---------------------------------------------------------------------------

CONFIDENCE_THRESHOLD = 0.7


class DecisionReviewer:
    """Auto-reviews decisions with verifiable outcomes.

    Listens to: session_ended
    Tier 1: Signal-based auto-review
    """

    def __init__(self, brain, settings, bus, http_client=None):
        self._brain = brain
        self._settings = settings
        self._bus = bus

        # Audit HD-4 (2026-06-09): EpisodeSignal and FileExistsSignal are no
        # longer wired. EpisodeSignal mapped the (currently hardcoded
        # "success") episode outcome onto every decision made during the
        # session — a category error: a session ending does not mean a specific
        # decision in it succeeded. FileExistsSignal labelled a decision
        # `success` whenever any path it mentioned existed on disk, even if the
        # file pre-existed and the decision never touched it. Both fed Brain
        # calibration false labels. The classes are retained (unit-tested) but
        # only outcome-grounded signals are wired: a low-confidence prior and
        # GitHub PR state (the one truly external source of truth).
        self._signals: list = [
            ErrorSignal(),
        ]
        if getattr(settings, "github_token", ""):
            self._signals.append(
                GitHubSignal(http_client, settings.github_token)
            )

        self._sweep_task: asyncio.Task | None = None

        bus.on("session_ended", self.handle)

    async def start(self) -> None:
        """Start the periodic sweep loop."""
        interval = getattr(self._settings, "decision_sweep_interval", 3600)
        self._sweep_task = asyncio.create_task(
            self._sweep_loop(interval), name="decision-review-sweep"
        )
        logger.info("Decision review sweep started (interval=%ds)", interval)

    async def stop(self) -> None:
        """Stop the periodic sweep loop."""
        if self._sweep_task:
            self._sweep_task.cancel()
            try:
                await self._sweep_task
            except asyncio.CancelledError:
                pass
            self._sweep_task = None

    async def _sweep_loop(self, interval: int) -> None:
        """Periodic loop: sleep → sweep unreviewed decisions → repeat."""
        while True:
            try:
                await asyncio.sleep(interval)
                results = await self.sweep()
                if results:
                    logger.info("Periodic sweep reviewed %d decision(s)", len(results))
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Periodic decision review sweep failed")

    async def handle(self, event: Event) -> None:
        """Review decisions from the ended session, then sweep older ones."""
        session_id = event.data.get("session_id") or event.session_id
        if not session_id:
            return

        try:
            decisions = await self._brain.get_session_decisions(session_id)
            for decision in decisions:
                if decision.reviewed_at is not None:
                    continue
                outcome = await self._check_signals(decision)
                if outcome:
                    await self._brain.review(
                        decision.id,
                        outcome=outcome.result,
                        result=outcome.explanation,
                        reviewer="auto",
                    )
        except Exception:
            logger.exception("Error reviewing session %s decisions", session_id)

        try:
            await self.sweep()
        except Exception:
            logger.exception("Error in decision review sweep")

    async def sweep(self, max_age_days: int = 30) -> list[ReviewResult]:
        """Sweep all unreviewed decisions. Standalone for future scheduler."""
        unreviewed = await self._brain.get_unreviewed(max_age_days=max_age_days)
        results = []
        for decision in unreviewed:
            outcome = await self._check_signals(decision)
            if outcome:
                await self._brain.review(
                    decision.id,
                    outcome=outcome.result,
                    result=outcome.explanation,
                    reviewer="auto",
                )
                results.append(outcome)
        return results

    async def _check_signals(self, decision) -> ReviewResult | None:
        """Run signals in order. First confident match wins."""
        for signal in self._signals:
            try:
                result = await signal.check(decision)
                if result and result.confidence >= CONFIDENCE_THRESHOLD:
                    return result
            except Exception:
                logger.warning(
                    "Signal %s failed for decision %s",
                    type(signal).__name__,
                    decision.id,
                )
        return None
