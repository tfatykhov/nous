"""Check registry and base check ABC (F034).

Manages registered checks, tracks schedules, and provides
circuit-breaker logic for failing checks.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import UTC, datetime

from nous.heartbeat.schemas import CheckResult, TunableParam

logger = logging.getLogger(__name__)


class BaseCheck(ABC):
    """Abstract base for heartbeat checks."""

    name: str = "unnamed"
    interval: int = 3600  # seconds between runs
    timeout: int = 30  # max seconds per run
    active: bool = True
    urgent_override: bool = False  # if True, runs even during quiet hours

    def __init__(self) -> None:
        self.last_run: datetime | None = None
        self.consecutive_failures: int = 0
        self.max_failures: int = 3
        # Audit HB-8 (2026-06-09): timestamp the breaker opening so it can
        # auto-recover via a half-open trial instead of staying open until a
        # manual REST reset (a transient outage would otherwise kill the check
        # permanently — e.g. a 9-min IMAP blip disabling the 180s email check).
        self._breaker_opened_at: datetime | None = None
        self._params: dict[str, TunableParam] = {}  # F034.3: tunable params

    @property
    def breaker_cooldown_seconds(self) -> float:
        """How long the breaker stays open before a half-open trial run.

        Proportional to the check interval (min 5 min) so fast checks recover
        quickly and slow checks don't thrash.
        """
        return max(self.interval * 3, 300)

    def is_due(self, now: datetime | None = None) -> bool:
        """Check if this check is due to run."""
        if not self.active:
            return False
        now = now or datetime.now(UTC)
        if self.consecutive_failures >= self.max_failures:
            # Circuit breaker open — HB-8: allow a single half-open trial once
            # the cooldown has elapsed. mark_success() closes it; mark_failure()
            # re-arms the cooldown for another attempt later.
            #
            # Review follow-up: derive the open time lazily from last_run when
            # _breaker_opened_at is unset. _breaker_opened_at is in-memory only,
            # so a DynamicCheckLoader re-sync (which copies consecutive_failures
            # + last_run onto a fresh object but not _breaker_opened_at) would
            # otherwise leave the breaker open with no timestamp -> permanently
            # not-due, re-introducing the exact bug HB-8 fixed for dynamic
            # checks. Falling back to last_run also covers any future
            # restore-from-persistence path.
            opened_at = self._breaker_opened_at or self.last_run
            if opened_at is None:
                return False
            open_for = (now - opened_at).total_seconds()
            return open_for >= self.breaker_cooldown_seconds
        if self.last_run is None:
            return True
        elapsed = (now - self.last_run).total_seconds()
        return elapsed >= self.interval

    def mark_success(self) -> None:
        """Record a successful run."""
        self.last_run = datetime.now(UTC)
        self.consecutive_failures = 0
        self._breaker_opened_at = None

    def mark_failure(self) -> None:
        """Record a failed run."""
        self.last_run = datetime.now(UTC)
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.max_failures:
            # (Re)arm the cooldown each time we fail while open so a failed
            # half-open trial waits another cooldown before the next attempt.
            self._breaker_opened_at = self.last_run
            logger.warning(
                "Check '%s' circuit breaker opened after %d consecutive failures "
                "(half-open retry in %.0fs)",
                self.name, self.consecutive_failures, self.breaker_cooldown_seconds,
            )

    def reset_circuit_breaker(self) -> None:
        """Manually reset the circuit breaker."""
        self.consecutive_failures = 0
        self._breaker_opened_at = None

    def tunable_params(self) -> dict[str, TunableParam]:
        """Return tunable parameters. Override in subclasses to define params."""
        return self._params

    def get_param(self, name: str) -> TunableParam | None:
        """Get a tunable parameter (returns full TunableParam, not just value)."""
        return self._params.get(name)

    def get_param_value(self, name: str) -> float:
        """Get parameter value as float. Returns 0 if not found."""
        p = self._params.get(name)
        return p.value if p else 0

    def set_param(self, name: str, value: float) -> bool:
        """Set a tunable parameter value (within bounds). Returns False if pinned or not found."""
        if name not in self._params:
            return False
        p = self._params[name]
        if p.pinned:
            return False
        clamped = max(p.min_val, min(p.max_val, value))
        self._params[name] = TunableParam(
            name=p.name, value=clamped,
            min_val=p.min_val, max_val=p.max_val,
            step=p.step, pinned=p.pinned,
        )
        return True

    @abstractmethod
    async def run(self) -> CheckResult:
        """Execute the check and return results."""
        ...


class CheckRegistry:
    """Registry of heartbeat checks with permanent/removable distinction."""

    def __init__(self) -> None:
        self._checks: dict[str, BaseCheck] = {}
        self._permanent: set[str] = set()

    def register(self, check: BaseCheck, permanent: bool = False) -> None:
        """Register a check. Permanent checks cannot be unregistered."""
        self._checks[check.name] = check
        if permanent:
            self._permanent.add(check.name)
        logger.info("Registered heartbeat check: %s (permanent=%s)", check.name, permanent)

    def unregister(self, name: str) -> bool:
        """Unregister a check. Returns False if check is permanent."""
        if name in self._permanent:
            logger.warning("Cannot unregister permanent check: %s", name)
            return False
        if name in self._checks:
            del self._checks[name]
            return True
        return False

    def get_due_checks(self, now: datetime | None = None) -> list[BaseCheck]:
        """Get all checks that are due to run."""
        now = now or datetime.now(UTC)
        return [c for c in self._checks.values() if c.is_due(now)]

    def get_check(self, name: str) -> BaseCheck | None:
        """Get a check by name."""
        return self._checks.get(name)

    def all_checks(self) -> list[BaseCheck]:
        """Return all registered checks."""
        return list(self._checks.values())

    def get_status(self) -> dict:
        """Get status of all registered checks."""
        return {
            name: {
                "active": check.active,
                "interval": check.interval,
                "last_run": check.last_run.isoformat() if check.last_run else None,
                "consecutive_failures": check.consecutive_failures,
                "max_failures": check.max_failures,
                "circuit_breaker_open": check.consecutive_failures >= check.max_failures,
                "permanent": name in self._permanent,
                "urgent_override": check.urgent_override,
            }
            for name, check in self._checks.items()
        }
