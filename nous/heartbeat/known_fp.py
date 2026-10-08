"""Known heartbeat false positives — rules that auto-close a finding at ingest.

Findings Nous has already diagnosed as recurring false positives cost the owner
a companion tap every time they come back. A rule here closes them at ingest
instead: the finding is still tracked (state ``auto_closed_known_fp`` with the
rule id, counted per rule), it just never reaches triage or a card.

Rules live in a plain JSON file (``NOUS_HEARTBEAT_KNOWN_FP_PATH``) so they can
be edited without a code change. The file is re-read when its mtime changes.
A missing file means no rules. An unreadable or malformed file keeps the last
good rule set (a half-written file must not reopen every closed finding at
once) and logs a warning; it never raises into the heartbeat. See
``docs/examples/known_false_positives.example.json`` for the schema.

Safety: a rule is matched only up to and including its ``review_by`` date.
After that it stops matching (the finding flows normally) and logs one
warning, so no rule is permanent. Urgency is not checked here; the
FindingStore never auto-closes high-urgency or escalated findings.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from nous.heartbeat.schemas import Finding

logger = logging.getLogger(__name__)

# Agent-authored companion items are tracked as "agent:<check>:<digest>"
# (nous/a2ui/tools.py), so a rule's `check` also matches that namespace.
_AGENT_CHECK_PREFIX = "agent:"


@dataclass(frozen=True)
class KnownFalsePositiveRule:
    """One known-false-positive rule. Every criterion given must match."""

    id: str
    reason: str
    added_by: str
    review_by: date
    check: str | None = None  # exact check_name, or the agent:<check>: namespace
    contains: str | None = None  # case-insensitive substring of the summary
    regex: re.Pattern[str] | None = None  # case-insensitive search on the summary

    def matches(self, finding: Finding) -> bool:
        """True if ``finding`` meets every criterion this rule sets."""
        if self.check is not None:
            name = finding.check_name
            if name != self.check and not name.startswith(f"{_AGENT_CHECK_PREFIX}{self.check}:"):
                return False
        if self.contains is not None and self.contains.casefold() not in finding.summary.casefold():
            return False
        if self.regex is not None and self.regex.search(finding.summary) is None:
            return False
        return True

    def expired(self, today: date) -> bool:
        """True once ``today`` is past ``review_by`` (the date itself still matches)."""
        return today > self.review_by


def parse_rule(raw: Any) -> KnownFalsePositiveRule:
    """Build a rule from one JSON object. Raises ValueError if it is invalid."""
    if not isinstance(raw, dict):
        raise ValueError("rule must be a JSON object")

    def _text(key: str, *, required: bool) -> str | None:
        value = raw.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            if required:
                raise ValueError(f"'{key}' is required")
            return None
        if not isinstance(value, str):
            raise ValueError(f"'{key}' must be a string")
        return value.strip()

    rule_id = _text("id", required=True)
    reason = _text("reason", required=True)
    added_by = _text("added_by", required=True)
    review_by_text = _text("review_by", required=True)
    match = raw.get("match")
    if not isinstance(match, dict):
        raise ValueError("'match' must be an object")
    unknown = set(match) - {"check", "contains", "regex"}
    if unknown:
        raise ValueError(f"unknown match keys {sorted(unknown)}")
    check = match.get("check")
    contains = match.get("contains")
    pattern = match.get("regex")
    for key, value in (("check", check), ("contains", contains), ("regex", pattern)):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError(f"match.{key} must be a non-empty string")
    if check is None and contains is None and pattern is None:
        raise ValueError("match needs at least one of check / contains / regex")
    try:
        review_by = date.fromisoformat(review_by_text)  # type: ignore[arg-type]
    except ValueError as exc:
        raise ValueError(f"'review_by' must be YYYY-MM-DD: {exc}") from exc
    try:
        regex = re.compile(pattern, re.IGNORECASE) if pattern is not None else None
    except re.error as exc:
        raise ValueError(f"match.regex does not compile: {exc}") from exc
    return KnownFalsePositiveRule(
        id=rule_id,  # type: ignore[arg-type]
        reason=reason,  # type: ignore[arg-type]
        added_by=added_by,  # type: ignore[arg-type]
        review_by=review_by,
        check=check,
        contains=contains,
        regex=regex,
    )


def _today() -> date:
    return datetime.now(UTC).date()


class KnownFalsePositiveRules:
    """The live rule set, loaded from a JSON file and re-read on change."""

    def __init__(
        self,
        path: str | None = None,
        *,
        rules: Iterable[KnownFalsePositiveRule] = (),
        today: Callable[[], date] = _today,
    ) -> None:
        self._path = path
        self._rules: list[KnownFalsePositiveRule] = list(rules)
        self._today = today
        self._loaded_mtime: int | None = None
        self._missing_logged = False
        self._expired_warned: set[KnownFalsePositiveRule] = set()

    @property
    def rules(self) -> list[KnownFalsePositiveRule]:
        """The current rule set (after picking up any file change)."""
        self._maybe_reload()
        return list(self._rules)

    def match(self, finding: Finding) -> KnownFalsePositiveRule | None:
        """Return the first unexpired rule that matches ``finding``, else None.

        Never raises: a rule-file problem must not break a heartbeat tick.
        """
        try:
            self._maybe_reload()
            today = self._today()
            for rule in self._rules:
                if not rule.matches(finding):
                    continue
                if rule.expired(today):
                    if rule not in self._expired_warned:
                        self._expired_warned.add(rule)
                        logger.warning(
                            "Known-FP rule %r passed its review_by date (%s) and no longer "
                            "auto-closes findings; matching findings now go to triage again. "
                            "Review it and extend review_by or delete it.",
                            rule.id,
                            rule.review_by.isoformat(),
                        )
                    continue
                return rule
        except Exception:
            logger.warning("Known-FP rule matching failed; treating finding as unmatched", exc_info=True)
        return None

    def _maybe_reload(self) -> None:
        if not self._path:
            return
        try:
            mtime = os.stat(self._path).st_mtime_ns
        except FileNotFoundError:
            if not self._missing_logged:
                self._missing_logged = True
                logger.warning("Known-FP rule file %s not found; no heartbeat findings will be auto-closed", self._path)
            self._rules = []
            self._loaded_mtime = None
            return
        except OSError:
            logger.warning("Known-FP rule file %s unreadable; keeping the last good rules", self._path, exc_info=True)
            return
        self._missing_logged = False
        if mtime == self._loaded_mtime:
            return
        # Recorded before parsing so a bad file warns once, not every tick.
        self._loaded_mtime = mtime
        try:
            with open(self._path, encoding="utf-8") as fh:
                data = json.load(fh)
            raw_rules = data.get("rules") if isinstance(data, dict) else None
            if not isinstance(raw_rules, list):
                raise ValueError('top level must be an object with a "rules" list')
        except Exception as exc:
            logger.warning(
                "Known-FP rule file %s is invalid (%s); keeping the last good rules (%d)",
                self._path,
                exc,
                len(self._rules),
            )
            return
        rules: list[KnownFalsePositiveRule] = []
        seen_ids: set[str] = set()
        for index, raw in enumerate(raw_rules):
            try:
                rule = parse_rule(raw)
                if rule.id in seen_ids:
                    raise ValueError(f"duplicate id {rule.id!r}")
            except ValueError as exc:
                logger.warning("Known-FP rule file %s: skipping rule #%d: %s", self._path, index, exc)
                continue
            seen_ids.add(rule.id)
            rules.append(rule)
        self._rules = rules
        logger.info("Loaded %d known-FP rule(s) from %s", len(rules), self._path)
