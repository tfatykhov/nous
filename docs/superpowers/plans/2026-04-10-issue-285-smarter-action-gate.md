# Issue #285: Make ActionGate Duplicate Detection Smarter

## Problem

ActionGate Tier 2 (consistency check) uses overly simplistic duplicate detection that produces false positives, blocking legitimate tool calls. Two root causes:

1. **`_args_similar()` returns True if ANY shared key matches** — `action=disable` on two different checks triggers a false positive
2. **`_summarize_args()` only captures the FIRST matching key** — loses distinguishing information (e.g., `name` field)

## Evidence

```
T9 heartbeat_check_manage action=disable name=ci-schema-sync-monitor (success)
T9 heartbeat_check_manage action=disable name=pr-fixes-monitor [BLOCKED: Duplicate]
```

## Files to Modify

| File | Changes |
|------|---------|
| `nous/cognitive/execution_ledger.py` | Fix `_summarize_args` to capture ALL key args; add `heartbeat_check_manage` and `heartbeat_check_create` to registries; add `send_file` to registries |
| `nous/cognitive/action_gate.py` | Fix `_args_similar` to require ALL shared keys to match; add turn-distance window to `_consistency_check` |
| `nous/config.py` | Add `action_gating_turn_window` config (default 5) |
| `tests/test_action_gate.py` | Update tests for ALL-key-match semantics; add multi-target and turn-window tests |
| `tests/test_execution_integrity.py` | Update any tests affected by `_summarize_args` changes |

## Implementation

### Phase 1: Fix `_summarize_args` in `execution_ledger.py`

**Current behavior** (line ~288-301):
```python
if key_names:
    for name in key_names:
        if name in args:
            result[name] = str(args[name])[:80]
            break  # Only first matching key  <-- BUG
else:
    for k, v in args.items():
        result[k] = str(v)[:80]
        break  # Only first arg  <-- BUG
```

**New behavior**: Capture ALL matching key args, not just the first. For fallback, capture all args (up to 5 to prevent bloat).

```python
if key_names:
    for name in key_names:
        if name in args:
            result[name] = str(args[name])[:80]
    # No break — capture all matching key args
else:
    for k, v in list(args.items())[:5]:
        result[k] = str(v)[:80]
    # Capture up to 5 args for unknown tools
```

**Add to `_KEY_ARGS`**:
```python
"heartbeat_check_manage": ["action", "name"],
"heartbeat_check_create": ["name", "prompt"],
"send_file": ["path", "file_path"],
```

**Add to `WRITE_TOOLS`**:
```python
"heartbeat_check_manage",
"heartbeat_check_create",
"send_file",
```

### Phase 2: Fix `_args_similar` in `action_gate.py`

**Current behavior** (line ~199-228): Returns True if ANY shared key has matching value.

**New behavior**: Returns True only if ALL shared keys match. If no shared keys, returns False (unchanged).

```python
def _args_similar(self, prior_args, new_args) -> bool:
    shared_keys = set(prior_args) & set(new_args)
    if not shared_keys:
        return False

    for key in shared_keys:
        prior_val = prior_args[key].strip().lower()
        new_val = new_args[key].strip().lower()

        if key in PATH_KEYS:
            prior_val = prior_val.rstrip("/").removeprefix("./")
            new_val = new_val.rstrip("/").removeprefix("./")

        if prior_val != new_val:
            return False  # ANY difference means not similar

    return True  # ALL shared keys match
```

### Phase 3: Add turn-distance window to `_consistency_check`

**Current behavior**: Checks last 20 actions of same tool name.

**New behavior**: Also filter by turn distance — only consider actions within `action_gating_turn_window` turns.

```python
def _consistency_check(self, tool_name, tool_input, ledger) -> GateResult:
    new_key_args = ledger._summarize_args(tool_name, tool_input)
    turn_window = self._settings.action_gating_turn_window
    min_turn = ledger.current_turn - turn_window

    recent = [
        a for a in ledger.actions[-20:]
        if a.tool_name == tool_name
        and a.status == "success"
        and a.turn >= min_turn
    ]
    # ... rest unchanged
```

### Phase 4: Add config in `config.py`

```python
action_gating_turn_window: int = 5  # Only block duplicates within this many turns
```

### Phase 5: Update tests

**Tests to update in `test_action_gate.py`**:
- `test_any_key_match_triggers_similar` → rename and invert: now ALL keys must match, so mixed match returns False
- `test_one_side_missing_key` → still True (only shared key `path` matches)
- Add: `test_all_shared_keys_must_match` — same tool, same action, different name → not similar
- Add: `test_multi_target_disable_not_blocked` — heartbeat_check_manage with different names passes
- Add: `test_same_bash_different_target_not_blocked` — bash with different commands passes
- Add: `test_exact_duplicate_still_blocked` — exact same args still caught
- Add: `test_turn_window_allows_old_duplicates` — action beyond turn window passes
- Add: `test_turn_window_blocks_recent_duplicates` — action within turn window blocked

**Tests to check in `test_execution_integrity.py`**:
- Any test relying on single-key `_summarize_args` output

## Acceptance Criteria (from issue)

- [x] Disabling two different heartbeat checks in the same turn should not be blocked
- [x] Running similar bash commands with different targets should not be blocked
- [x] Genuine duplicates (exact same tool + exact same args) should still be caught
- [x] Add tests for multi-target scenarios

## Risk Assessment

- **LOW risk**: The change is strictly more permissive (fewer false positives). The only risk is missing genuine duplicates, but requiring ALL shared keys to match is still a strong signal.
- Turn-distance window prevents stale matches from blocking current work.
- All existing tests will be reviewed and updated to match new semantics.
