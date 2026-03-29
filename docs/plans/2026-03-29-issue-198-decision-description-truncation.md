# Fix Decision Description Truncation (Issue #198) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Increase decision description truncation from 200 to 500 characters so auto-recorded decisions aren't cut mid-word/sentence.

**Architecture:** Two code changes in `layer.py` at the decision-related truncation points (deliberation start and finalize). All other `[:200]` usages serve different purposes (episode seeds, tool arg previews, topic tracking) and remain unchanged. One existing test needs updating.

**Tech Stack:** Python, pytest

---

### Task 1: Update decision description truncation limits

**Files:**
- Modify: `nous/cognitive/layer.py:457` (deliberation start)
- Modify: `nous/cognitive/layer.py:652` (deliberation finalize)
- Modify: `tests/test_topic_persistence.py:101-105` (update test that validates truncation)
- Test: `tests/test_cognitive_layer.py` (new test for decision description length)

- [ ] **Step 1: Write failing test for decision finalize truncation**

In `tests/test_cognitive_layer.py`, find or create a test that verifies decision descriptions allow up to 500 chars. If no suitable test file exists for layer.py truncation behavior, add to `tests/test_topic_persistence.py`.

First, check what test infrastructure exists:

Run: `grep -n "finalize\|deliberat" tests/test_cognitive_layer.py | head -20`

The test should verify that a 400-char response_text is NOT truncated (it would be under the old 200 limit):

```python
def test_decision_description_allows_500_chars():
    """Issue #198: Decision descriptions should truncate at 500, not 200."""
    long_text = "x" * 400
    # Under old behavior, this would be truncated to 200
    result = long_text[:500]
    assert len(result) == 400  # preserved, not truncated
```

- [ ] **Step 2: Change line 457 — deliberation start description**

In `nous/cognitive/layer.py`, change line 457 from:
```python
agent_id, user_input[:200], frame,
```
to:
```python
agent_id, user_input[:500], frame,
```

- [ ] **Step 3: Change line 652 — deliberation finalize description**

In `nous/cognitive/layer.py`, change line 652 from:
```python
description=turn_result.response_text[:200],
```
to:
```python
description=turn_result.response_text[:500],
```

- [ ] **Step 4: Update topic persistence test**

In `tests/test_topic_persistence.py`, the test at line 101 validates truncation at 200 for topic extraction — this is UNCHANGED behavior (topic extraction still uses `[:200]`). Verify this test still passes as-is:

Run: `uv run pytest tests/test_topic_persistence.py::TestTopicResolution::test_truncation_at_200 -v`
Expected: PASS (topic extraction unchanged)

- [ ] **Step 5: Run full test suite to verify no regressions**

Run: `uv run pytest tests/ -v --timeout=120 -x`
Expected: All tests pass

- [ ] **Step 6: Commit**

```bash
git add nous/cognitive/layer.py
git commit -m "fix(brain): increase decision description truncation from 200 to 500 chars (#198)"
```
