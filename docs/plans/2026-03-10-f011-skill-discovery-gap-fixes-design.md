# F011 Skill Discovery — Gap Fixes Design

**Date:** 2026-03-10
**Status:** Approved
**Depends on:** F011 (Skill Discovery v2) — shipped

## Problem

F011 skill discovery works on the happy path but has 8 gaps identified during validation. The most impactful: agent-created skills fail to register due to fragile frontmatter parsing, and even when registration succeeds, skills are nearly invisible at RECALL time because `_format_procedures` strips all useful context.

## Gaps Addressed

| # | Priority | Gap | Root Cause |
|---|----------|-----|------------|
| 1 | P1 | Inactive procedures surface in RECALL | `search_procedures` has no `active=true` filter |
| 2 | P1 | Skills invisible at RECALL — only name+domain shown | `_format_procedures` strips description |
| 3 | P2 | `requires` env var validation not implemented | Spec compliance gap |
| 4 | P1 | No provenance for inline/agent-created skills | `source_hint=None` for inline |
| 5 | P2 | Dedup uses fuzzy semantic search | Non-deterministic matching |
| 6 | P1 | LLM-generated frontmatter rejected with vague errors | Strict parser, no fallback |
| 7 | P2 | File path mismatch between write_file and learn_skill | Relative path resolution |
| 8 | P3 | No `encoding="utf-8"` on file open | System locale default |

## Design

### 1. Lenient Frontmatter Parser (Gap 6)

**File:** `nous/skills/parser.py`

Add a fallback parsing path. `parse()` tries strict first (existing regex). On failure, attempts lenient extraction:

- Strip leading whitespace/blank lines before `---`
- Accept `` ```yaml `` fenced blocks as frontmatter
- Accept missing closing `---` (treat EOF or first `##` as end of frontmatter)
- If still fails, raise `ValueError` with specific field-level feedback ("Missing 'name' field", "Frontmatter delimiters not found — expected `---` on first line")

Add `warnings: list[str]` field to `SkillManifest`. Auto-correction populates warnings. `learn_skill` includes warnings in the response text.

### 2. Active Filter in search_procedures (Gap 1)

**File:** `nous/heart/procedures.py`

Add `AND t.active = true` to the base `extra_where` in `_search()`. Inactive skills stop surfacing in RECALL. The retire method already sets `active=False`, so this filter immediately takes effect.

### 3. Richer Procedure Formatting (Gap 2)

**File:** `nous/cognitive/context.py`, `nous/heart/procedures.py`, `nous/heart/schemas.py`

Add `description: str` field to `ProcedureSummary`. Populate it from the ORM in `_to_summary()`. Change `_format_procedures` to:

```
- **{name}** ({domain}): {description} | activated {count}x{eff_str}
```

### 4. Inline Provenance (Gap 4)

**File:** `nous/api/tools.py`, `nous/skills/parser.py`

When `source == "inline"`, pass `source_hint="inline"` instead of `None`. In `to_procedure_input`, when source_url is `"inline"`:
- Write `source:inline` in implementation_notes
- Use tag `inline` instead of `local`

### 5. Deterministic Dedup (Gap 5)

**File:** `nous/api/tools.py`, `nous/heart/procedures.py`

Replace semantic search dedup with exact name query. Add `get_procedure_by_name(name) -> ProcedureDetail | None` to `ProcedureManager`:

```sql
SELECT * FROM heart.procedures WHERE name = :name AND agent_id = :agent_id AND active = true LIMIT 1
```

`learn_skill` calls this instead of `search_procedures` for dedup.

### 6. Requires Validation (Gap 3)

**File:** `nous/api/tools.py`

After parsing, check `os.environ.get(var)` for each item in `manifest.requires`. If any missing:
- Store with `active=False` (add optional `active` param to `ProcedureInput`)
- Response: "Skill registered as inactive (missing: X, Y)"

**Re-activation path D (primary):** Agent calls `learn_skill` again after env var is set. Dedup retires old inactive version, new registration re-checks requires.

**Re-activation path B (startup):** In `bootstrap.py`, on startup, query all inactive skill-tagged procedures. Re-check their `requires` (stored in `core_concepts`). Flip `active=True` for any now satisfied via `heart.procedures.activate()` or direct update.

### 7. File Encoding Fix (Gap 8)

**File:** `nous/api/tools.py`

Add `encoding="utf-8"` to `open(path)` at line 612.

## Files Modified

| File | Changes |
|------|---------|
| `nous/skills/parser.py` | Lenient fallback parser, warnings field on SkillManifest |
| `nous/api/tools.py` | Inline provenance, requires validation, deterministic dedup, encoding fix |
| `nous/heart/procedures.py` | Active filter in `_search`, `get_procedure_by_name`, description in `_to_summary` |
| `nous/heart/schemas.py` | `description` on `ProcedureSummary`, `active` param on `ProcedureInput` |
| `nous/cognitive/context.py` | Richer `_format_procedures` |
| `nous/skills/bootstrap.py` | Startup re-activation scan for inactive skills |
| `tests/test_skill_parser.py` | Lenient parser tests, warnings tests |
| `tests/` (existing) | Tests for active filter, dedup, requires, provenance |

## Testing Strategy

- Unit tests for lenient parser (missing delimiters, fenced blocks, whitespace)
- Unit tests for warnings propagation to learn_skill response
- Unit test for `get_procedure_by_name` exact match
- Unit test for `_search` active filter (inactive skills excluded)
- Unit test for requires validation (missing vars → inactive)
- Unit test for bootstrap re-activation scan
- Unit test for inline provenance tags
- Integration: agent creates inline skill → registers → surfaces in RECALL with description
