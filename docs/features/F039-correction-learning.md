# F039 — Correction Learning Pipeline

**Status:** Implemented (PR #303)
**Inspired by:** Databricks MemAlign paper — dual-memory correction learning
**Branch:** `claude-job-33b2aaba`

## Overview

MemAlign-inspired correction learning pipeline that detects when users correct the AI and extracts generalizable principles, storing them as facts (preferences/rules) and optional censors (for "never do X" patterns). Operates in two modes: real-time inline detection during conversation and batch extraction post-session.

## Architecture

### 3-Component Design

1. **Inline Correction Detection** (`monitor.py` — `detect_and_extract_correction`)
   - Runs in `post_turn` after each AI response
   - Pattern-matches user messages against 15 correction indicators (e.g., "no, actually", "that's wrong", "i meant", "never do that")
   - On match → LLM micro-call to extract the principle
   - Stores result as a fact and optionally as a censor

2. **Batch Correction Extractor** (`handlers/correction_extractor.py` — `CorrectionExtractor`)
   - Listens to `outcome_signals_detected` event
   - Filters for `type == "corrected"` signals
   - Uses LLM micro-call with episode summary + transcript tail + evidence
   - Extracts: principle, subject, is_censor, censor_pattern, confidence
   - Dual-write: every correction → fact + optional censor (for "never do" patterns)

3. **Feedback-Aware Rubric Evaluation** (`handlers/rubric_evolver.py`)
   - Integrates correction signals into the self-evaluation rubric system
   - Correction frequency influences dimension weights over time

### Dual-Write Pattern

Every extracted correction is stored in two places:
- **Fact** — the generalizable principle (category: `rule` or `preference`, subject extracted from context)
- **Censor** (optional) — if the correction is a "never do X" pattern, a `warn`-level censor is created with the extracted trigger pattern

## Configuration

```python
# Settings
correction_extraction_enabled: bool = True  # Feature gate
```

Environment variable: `NOUS_CORRECTION_EXTRACTION_ENABLED`

## Files Changed

| File | Change |
|------|--------|
| `nous/cognitive/layer.py` | Added F039 inline correction detection in `post_turn` |
| `nous/cognitive/monitor.py` | Added `detect_and_extract_correction()` method + correction patterns |
| `nous/handlers/correction_extractor.py` | New — batch correction extractor handler |
| `nous/handlers/rubric_evolver.py` | New — feedback-aware rubric evolution |
| `nous/cognitive/rubric.py` | Rubric manager (F024 Phase 3b, used by F039) |
| `nous/config.py` | Added `correction_extraction_enabled` setting |
| `nous/main.py` | Handler registration |
| `tests/test_correction_extractor.py` | Unit tests for batch extractor |
| `tests/test_inline_correction.py` | Unit tests for inline detection |

## Correction Patterns Detected

```python
_CORRECTION_PATTERNS = [
    "no, actually", "that's wrong", "that's not right", "not what i",
    "you misunderstood", "i meant", "correction:", "no no",
    "wrong,", "that's incorrect", "don't do that", "never do that",
    "stop doing", "i said", "i already told you",
]
```

## Event Flow

```
User correction message
  → post_turn (inline path)
    → pattern match → LLM micro-call → fact + optional censor

Session end → outcome_signals_detected event
  → CorrectionExtractor.handle (batch path)
    → filter corrected signals → LLM micro-call → fact + optional censor
```

## Test Coverage

- `tests/test_correction_extractor.py` — batch extraction with mocked LLM
- `tests/test_inline_correction.py` — inline detection patterns and extraction
- 17/17 tests passing

## Dependencies

- F024 (Critic Agent) — rubric system integration
- Event bus (`outcome_signals_detected`)
- Background LLM client for micro-calls
