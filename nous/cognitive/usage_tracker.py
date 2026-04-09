"""Track which retrieved context the agent actually references.

Provides a feedback loop: memories that get used are boosted in
future retrieval; memories that are retrieved but ignored get penalized.
Half-life: 7 days -- usage signal decays to 50% after a week.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass
class UsageRecord:
    """Single usage observation for a memory item."""

    memory_id: str
    memory_type: str
    retrieved_at: datetime
    was_referenced: bool
    overlap_score: float = 0.0


@dataclass
class MemoryUsageStats:
    """Aggregated usage stats for a memory item."""

    memory_id: str
    memory_type: str
    times_retrieved: int = 0
    times_referenced: int = 0
    usage_score: float = 0.0  # Decayed score
    last_retrieved: datetime | None = None


class UsageTracker:
    """Tracks context usage for feedback-driven retrieval.

    Stores in-memory for now (DB persistence in future iteration).
    Half-life: 7 days -- usage signal decays to 50% after a week.

    F11: Records indexed by memory_id for O(1) lookup.
    Capped at MAX_RECORDS=5000 with time-based pruning (>21 days).
    """

    HALF_LIFE_DAYS = 7.0
    MAX_RECORDS = 5000
    PRUNE_AGE_DAYS = 21  # 3x half-life, decay < 1%

    def __init__(self) -> None:
        # F11: Index records by memory_id for O(1) lookup per memory
        self._records: dict[str, list[UsageRecord]] = {}
        self._total_record_count: int = 0
        self._stats: dict[str, MemoryUsageStats] = {}

    def record_retrieval(
        self,
        memory_id: str,
        memory_type: str,
        was_referenced: bool,
        overlap_score: float = 0.0,
    ) -> None:
        """Record that a memory was retrieved and whether it was referenced."""
        now = datetime.now(UTC)
        record = UsageRecord(
            memory_id=memory_id,
            memory_type=memory_type,
            retrieved_at=now,
            was_referenced=was_referenced,
            overlap_score=overlap_score,
        )

        if memory_id not in self._records:
            self._records[memory_id] = []
        self._records[memory_id].append(record)
        self._total_record_count += 1

        # Update aggregate stats
        if memory_id not in self._stats:
            self._stats[memory_id] = MemoryUsageStats(
                memory_id=memory_id, memory_type=memory_type
            )
        stats = self._stats[memory_id]
        stats.times_retrieved += 1
        if was_referenced:
            stats.times_referenced += 1
        stats.last_retrieved = now
        stats.usage_score = self._compute_decayed_score(memory_id)

        # F11: Prune if over capacity
        if self._total_record_count > self.MAX_RECORDS:
            self._prune_old_records()

    def get_usage_score(self, memory_id: str) -> float:
        """Get current decayed usage score for a memory."""
        if memory_id not in self._stats:
            return 0.0
        return self._compute_decayed_score(memory_id)

    def get_boost_factor(self, memory_id: str) -> float:
        """Get retrieval boost factor based on usage history.

        Returns 1.0 for unknown memories (no boost/penalty).
        > 1.0 for frequently-referenced memories.
        < 1.0 for frequently-retrieved but rarely-referenced memories.

        Range: [0.3, 2.0]
        """
        stats = self._stats.get(memory_id)
        if stats is None or stats.times_retrieved < 2:
            return 1.0
        ref_rate = stats.times_referenced / stats.times_retrieved
        # Scale: 0% referenced -> 0.3x, 50% -> 1.15x, 100% -> 2.0x
        return 0.3 + ref_rate * 1.7

    def _compute_decayed_score(self, memory_id: str) -> float:
        """Compute usage score with exponential decay.

        F11: Only iterates records for this specific memory_id (O(k) not O(n)).
        """
        records = self._records.get(memory_id, [])
        if not records:
            return 0.0

        now = datetime.now(UTC)
        score = 0.0
        for record in records:
            age_days = (now - record.retrieved_at).total_seconds() / 86400
            decay = math.exp(-0.693 * age_days / self.HALF_LIFE_DAYS)
            value = 1.0 if record.was_referenced else 0.0
            score += value * decay
        return score

    def _prune_old_records(self) -> None:
        """Remove records older than PRUNE_AGE_DAYS (F11)."""
        now = datetime.now(UTC)
        cutoff_seconds = self.PRUNE_AGE_DAYS * 86400
        pruned_total = 0

        empty_keys: list[str] = []
        for memory_id, records in self._records.items():
            original_len = len(records)
            self._records[memory_id] = [
                r
                for r in records
                if (now - r.retrieved_at).total_seconds() < cutoff_seconds
            ]
            pruned_total += original_len - len(self._records[memory_id])
            if not self._records[memory_id]:
                empty_keys.append(memory_id)

        for key in empty_keys:
            del self._records[key]
            self._stats.pop(key, None)

        self._total_record_count -= pruned_total

    @staticmethod
    def compute_overlap(context_text: str, response_text: str) -> float:
        """Compute containment coefficient between context and response.

        F9: Uses |A intersection B| / min(|A|, |B|) instead of Jaccard.
        Jaccard systematically undercounts for asymmetric-length texts.
        Example: 2000-word context, 100-word response referencing all 100 words
        -> Jaccard = 100/2000 = 0.05. Containment = 100/100 = 1.0.
        """
        ctx_words = set(re.findall(r"\b\w{3,}\b", context_text.lower()))
        resp_words = set(re.findall(r"\b\w{3,}\b", response_text.lower()))
        if not ctx_words or not resp_words:
            return 0.0
        intersection = ctx_words & resp_words
        return len(intersection) / min(len(ctx_words), len(resp_words))
