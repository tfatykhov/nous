# Implementation Plan: F034.1/F034.2/F034.3 — Heartbeat Evolution

**Date:** 2026-04-03
**Specs:** F034.1 Finding Lifecycle, F034.2 Intelligent Checks, F034.3 Self-Tuning Heartbeat
**Approach:** Single consolidated delivery (all three specs)

## Scope Decisions

### Included (Full Implementation)
- **F034.1**: FindingStore, fingerprinting, state machine, escalation policy, daily digest, REST endpoints, runner integration
- **F034.2 Section 1**: Smart SelfInitiatedCheck (1a embedding prototypes, 1b promise tracking, 1c temporal awareness)
- **F034.2 Section 2**: LLM-powered EmailCheck (2a tiered classification, 2b sender reputation)
- **F034.2 Section 3**: DriveCheck improvements (3a folder mapping, 3b significance scoring, 3c conversation cross-reference)
- **F034.3**: Outcome signals, tunable parameter framework, tuning engine, guardrails + rollback, tuning report, REST endpoints

### Excluded (Phase 2 — explicitly deferred in specs)
- **F034.2 Section 3d**: Content-aware summaries (expensive API + LLM, spec says "do last")
- **F034.3 Section 3.7**: Per-finding-type tuning (spec says "Phase 2 ambition")

## File Plan

### New Files
| File | Purpose | Est. Lines |
|------|---------|-----------|
| `nous/heartbeat/finding_store.py` | FindingStore, TrackedFinding, EscalationPolicy, FindingAction enum | ~250 |
| `nous/heartbeat/tuner.py` | HeartbeatTuner, outcome tracking, parameter adjustment, rollback | ~300 |
| `tests/test_heartbeat_lifecycle.py` | F034.1 tests: fingerprinting, state machine, escalation, digest, REST | ~350 |
| `tests/test_heartbeat_intelligent.py` | F034.2 tests: embedding checks, LLM email, drive context | ~300 |
| `tests/test_heartbeat_tuner.py` | F034.3 tests: outcome signals, tuning engine, guardrails, rollback | ~350 |

### Modified Files
| File | Changes |
|------|---------|
| `nous/heartbeat/schemas.py` | Add TrackedFinding, FindingAction, OutcomeSignal, TunableParam, EscalationConfig |
| `nous/heartbeat/registry.py` | Extend BaseCheck with tunable_params(), get_param(), set_param(), fingerprint_key() |
| `nous/heartbeat/checks.py` | F034.2 upgrades to all 3 checks + fingerprint_key overrides |
| `nous/heartbeat/runner.py` | Integrate FindingStore into _triage(), add daily digest scheduling, outcome tracking |
| `nous/heartbeat/__init__.py` | Export new classes |
| `nous/config.py` | New config fields for escalation, tuning, embedding prototypes |
| `nous/api/rest.py` | 6 new REST endpoints for findings + tuning |

## Implementation Steps

### Step 1: Schemas & Data Structures (F034.1 + F034.3 foundations)

**File: `nous/heartbeat/schemas.py`**

Add to existing file:
```python
class FindingAction(str, Enum):
    TRIAGE = "triage"
    SUPPRESS = "suppress"
    ESCALATE = "escalate"

class OutcomeSignal(str, Enum):
    STRONG_POSITIVE = "strong_positive"
    POSITIVE = "positive"
    WEAK_NEGATIVE = "weak_negative"
    NEGATIVE = "negative"
    STRONG_NEGATIVE = "strong_negative"
    NEUTRAL = "neutral"

@dataclass
class EscalationConfig:
    low_to_normal_hours: int = 72
    normal_to_high_hours: int = 24
    high_realert_hours: int = 12
    accumulation_threshold: int = 5

@dataclass
class TunableParam:
    name: str
    value: float
    min_val: float
    max_val: float
    step: float
    pinned: bool = False  # manual override, skip auto-tuning

@dataclass
class TrackedFinding:
    finding: Finding
    fingerprint: str
    state: Literal["new", "suppressed", "acknowledged", "resolved"] = "new"
    first_seen: datetime
    last_seen: datetime
    seen_count: int = 1
    escalated: bool = False
    resolved_at: datetime | None = None
    outcome: OutcomeSignal | None = None
    outcome_at: datetime | None = None
```

Add `fingerprint()` method to `Finding` class:
```python
def fingerprint(self) -> str:
    """Stable hash for dedup. Strips volatile parts (counts, timestamps)."""
    import hashlib, re
    normalized = re.sub(r'\d+', 'N', self.summary)
    key = f"{self.check_name}:{self.source}:{normalized}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]
```

### Step 2: FindingStore (F034.1 core)

**New file: `nous/heartbeat/finding_store.py`**

```python
class FindingStore:
    def __init__(self, escalation_config: EscalationConfig | None = None):
        self._findings: dict[str, TrackedFinding] = {}
        self._escalation = escalation_config or EscalationConfig()

    def ingest(self, finding: Finding) -> FindingAction:
        """Route finding through state machine. Returns action to take."""

    def acknowledge(self, fingerprint: str) -> bool:
        """Mark finding as triaged → daily digest only."""

    def resolve(self, fingerprint: str) -> bool:
        """Mark finding as resolved."""

    def record_outcome(self, fingerprint: str, signal: OutcomeSignal):
        """Record outcome signal for tuning engine."""

    def get_digest_items(self) -> list[TrackedFinding]:
        """Acknowledged but unresolved findings for daily digest."""

    def get_outcomes_for_check(self, check_name: str, since: timedelta) -> list[TrackedFinding]:
        """Get findings with outcomes for a specific check (for tuner)."""

    def prune(self, resolved_ttl_days: int = 7):
        """Remove resolved findings older than TTL."""

    def _should_escalate(self, tracked: TrackedFinding) -> bool:
        """Check if finding should escalate based on age + policy."""

    def _check_accumulation_escalation(self, check_name: str) -> bool:
        """5+ acknowledged findings from same check → escalate collection."""

    def to_dict(self) -> list[dict]:
        """Serialize all findings for REST API."""

    def stats(self) -> dict:
        """Summary stats: counts by state, by check, etc."""
```

### Step 3: Extend BaseCheck with Tunable Parameters (F034.3 framework)

**File: `nous/heartbeat/registry.py`**

Add to BaseCheck:
```python
class BaseCheck(ABC):
    # ... existing ...

    def tunable_params(self) -> dict[str, TunableParam]:
        """Override to expose tunable parameters."""
        return {}

    def get_param(self, name: str) -> float:
        params = self.tunable_params()
        return params[name].value if name in params else 0

    def set_param(self, name: str, value: float):
        """Set a tunable parameter value (within bounds)."""
        params = self.tunable_params()
        if name in params:
            p = params[name]
            params[name] = TunableParam(
                name=p.name,
                value=max(p.min_val, min(p.max_val, value)),
                min_val=p.min_val, max_val=p.max_val,
                step=p.step, pinned=p.pinned,
            )

    def fingerprint_key(self, finding: Finding) -> str | None:
        """Override to customize finding fingerprinting for this check."""
        return None
```

Add `all_checks()` to CheckRegistry:
```python
def all_checks(self) -> list[BaseCheck]:
    return list(self._checks.values())
```

### Step 4: Upgrade Checks (F034.2)

**File: `nous/heartbeat/checks.py`**

#### 4a. HealthCheck — add tunable params + fingerprint
- Add `_params` dict with stale_decision_days, stale_fact_days, low_effectiveness_threshold, max_findings_per_run
- Use params in run() instead of hardcoded values
- Override `fingerprint_key()` to strip counts from findings

#### 4b. SelfInitiatedCheck — embedding + promise tracking + temporal
- Add EmbeddingProvider dependency (injected via __init__)
- Cache prototype embeddings on first run
- `_embedding_search()`: cosine similarity against recent facts
- `_promise_scan()`: scan episode summaries for unresolved commitments
- `_temporal_scan()`: dateutil parsing for explicit deadlines
- Add tunable params: similarity_threshold, lookback_days, max_pending_items
- Falls back to keyword matching if embeddings unavailable

#### 4c. EmailCheck — LLM classification + sender reputation
- `_llm_classify()`: compact prompt → haiku model → urgent/actionable/informational/spam
- `_keyword_classify()`: existing logic (Tier 1 fallback)
- `_sender_reputation`: dict[str, list[str]] tracking sender → classifications
- Budget-gated: only call LLM if token budget available
- Replace `_seen_ids` with FindingStore integration (use message_id in fingerprint)
- Add tunable params: sender_reputation_weight, llm_classification_budget

#### 4d. DriveCheck — folder mapping + significance + cross-reference
- `_folder_map`: configurable dict of folder prefix → project name
- `_score_significance()`: new file=high, shared=high, own edit=normal, minor=low
- `_contextualize()`: search recent episodes via heart.search() for file mentions
- Add tunable params: significance_threshold, cross_reference_lookback_hours
- Enrich finding summary with project context + conversation cross-reference

### Step 5: Runner Integration (F034.1 + F034.3)

**File: `nous/heartbeat/runner.py`**

Changes:
1. Add `FindingStore` as constructor dependency
2. Modify `_triage()` to route through FindingStore:
   - `ingest()` each finding → TRIAGE/SUPPRESS/ESCALATE
   - SUPPRESS → skip (log debug)
   - ESCALATE → upgrade urgency, proceed to triage
   - After triage → `acknowledge()` the finding
3. Add `_daily_digest()` method:
   - Collect acknowledged items from FindingStore
   - Group by check_name
   - Send single Telegram summary
   - Mark escalation warnings (⬆️)
4. Add digest scheduling to `_loop()`:
   - Track `_last_digest_date`
   - At configured hour (default 09:00), fire digest if items exist
5. Add outcome tracking:
   - After cognitive triage, if session modifies related data → strong_positive/positive
   - On manual resolve via REST → positive
   - Periodic sweep: acknowledged findings with no action after 72h → weak_negative

### Step 6: HeartbeatTuner (F034.3 core)

**New file: `nous/heartbeat/tuner.py`**

```python
class HeartbeatTuner:
    LEARNING_RATE = 0.1
    MIN_SAMPLES = 10

    async def tune(self, finding_store: FindingStore, registry: CheckRegistry) -> TuningReport:
        """Run tuning pass over all checks."""

    def _compute_adjustments(self, check: BaseCheck, outcomes: list[TrackedFinding]) -> dict[str, str]:
        """Determine relax/tighten per parameter."""

    def _apply_adjustment(self, check: BaseCheck, param: str, direction: str) -> tuple[float, float]:
        """Apply bounded adjustment. Returns (old_value, new_value)."""

    def _check_rollback(self, check: BaseCheck, pre_snapshot: dict, post_outcomes: list) -> bool:
        """If post-adjustment negative rate increased >20%, rollback."""

    def _generate_report(self, adjustments: dict) -> str:
        """Human-readable tuning report for Telegram/facts."""

@dataclass
class TuningReport:
    adjustments: list[TuningAdjustment]
    skipped_checks: list[str]  # insufficient data
    timestamp: datetime

@dataclass
class TuningAdjustment:
    check_name: str
    param_name: str
    old_value: float
    new_value: float
    direction: str  # "relax" | "tighten" | "rollback"
    sample_count: int
    positive_rate: float
    negative_rate: float
```

### Step 7: Config Fields

**File: `nous/config.py`**

Add:
```python
# F034.1 — Finding lifecycle
heartbeat_escalation_low_to_normal_hours: int = 72
heartbeat_escalation_normal_to_high_hours: int = 24
heartbeat_escalation_high_realert_hours: int = 12
heartbeat_escalation_accumulation_threshold: int = 5
heartbeat_digest_hour: int = 9  # local hour for daily digest
heartbeat_suppression_ttl_hours: int = 24

# F034.3 — Self-tuning
heartbeat_tuning_enabled: bool = False  # off by default until stable
heartbeat_tuning_interval_hours: int = 168  # weekly
heartbeat_tuning_min_samples: int = 10
heartbeat_tuning_learning_rate: float = 0.1
heartbeat_tuning_rollback_threshold: float = 0.2
```

### Step 8: REST Endpoints

**File: `nous/api/rest.py`**

New endpoints:
```
GET  /heartbeat/findings           — All tracked findings with state/age
POST /heartbeat/findings/{fp}/acknowledge — Manual acknowledge
POST /heartbeat/findings/{fp}/resolve     — Manual resolve
POST /heartbeat/findings/{fp}/dismiss     — Explicit dismiss (strong_negative)
PUT  /heartbeat/escalation-policy  — Update escalation thresholds
GET  /heartbeat/tuning-report      — Latest tuning report
POST /heartbeat/tune               — Force tuning pass
```

### Step 9: Tests

Three test files, each following existing test_heartbeat.py patterns:

**`tests/test_heartbeat_lifecycle.py`** (~350 lines):
- TestFindingFingerprint: stable hash, count-invariant, check-scoped
- TestFindingStore: ingest new→TRIAGE, ingest dup→SUPPRESS, ingest resolved→reopen
- TestEscalation: time-based escalation, accumulation escalation, bounds
- TestDailyDigest: collects acknowledged, groups by check, formats message
- TestFindingStoreIntegration: full lifecycle new→acknowledged→resolved
- TestOutcomeRecording: record outcomes, query by check, time filtering
- TestPruning: resolved TTL, active findings not pruned

**`tests/test_heartbeat_intelligent.py`** (~300 lines):
- TestEmbeddingSelfInitiated: prototype matching, fallback to keywords, filtering
- TestPromiseTracking: episode scan, unresolved commitments, age filtering
- TestTemporalAwareness: explicit dates, fuzzy dates, deadline surfacing
- TestLLMEmailClassification: tiered classification, budget gating, graceful degradation
- TestSenderReputation: learning, decay, unknown senders get LLM
- TestDriveSignificance: new file scoring, shared file scoring, folder mapping
- TestDriveCrossReference: conversation mentions, no match returns None

**`tests/test_heartbeat_tuner.py`** (~350 lines):
- TestTuningEngine: relax on high negatives, tighten on high positives, no change on balanced
- TestParameterBounds: cannot exceed min/max, learning rate respected
- TestRollback: auto-rollback on increased negatives, snapshot restore
- TestPinnedParams: manual overrides skip tuning
- TestMinSamples: insufficient data skips check
- TestTuningReport: format, includes all adjustments, skipped checks
- TestGuardrails: sensitivity floors, max step size

### Step 10: Exports & Wiring

- Update `nous/heartbeat/__init__.py` with new exports
- Wire FindingStore into HeartbeatRunner in `nous/main.py`
- Wire tuner scheduling (if enabled) into runner loop or event bus

## Implementation Order (for parallel agents)

**Phase A (independent, parallel):**
1. Step 1 (schemas) — foundational, no deps
2. Step 6 (tuner) — depends only on schemas + registry interfaces

**Phase B (after Phase A):**
3. Step 2 (FindingStore) — needs schemas
4. Step 3 (BaseCheck extensions) — needs schemas

**Phase C (after Phase B):**
5. Step 4 (check upgrades) — needs BaseCheck extensions + FindingStore
6. Step 5 (runner integration) — needs FindingStore
7. Step 7 (config) — can parallel with 5/6

**Phase D (after Phase C):**
8. Step 8 (REST endpoints) — needs all runtime components
9. Step 9 (tests) — needs all implementation
10. Step 10 (wiring + exports) — final integration

## Estimated Total
- ~900 lines new implementation
- ~1000 lines tests
- ~100 lines config/wiring changes
- **~2000 lines total**
