# Implementation Plan: Issue #216 — Inject Skill Catalog into Critic Prompt

**Issue:** F024: Inject actual skill catalog into Critic classification prompt
**Date:** 2026-03-31
**Status:** Draft

## Problem

The Critic LLM classification prompt (`critic.py:48-66`) includes `skills: list of skill names to activate` in its expected JSON output, but provides **no actual skill catalog** to the prompt. The Critic hallucinates skill names (e.g., `"research"`, `"data_analysis"`) that don't match any registered procedure names. Additionally, `CriticResult.skills` is populated at `critic.py:237` but **never consumed** in `layer.py`.

## Design

### Approach

1. **Build a compact skill catalog** from registered procedures at classification time
2. **Inject it into the Critic prompt** via a new `{skill_catalog}` placeholder
3. **Validate Critic output** — filter returned skill names to only those in the catalog
4. **Wire activation** — consume `CriticResult.skills` in `layer.py` to activate matched procedures

### Key Design Decisions

- **Query at classify-time, not cached** — Procedures can be added/retired dynamically. The Critic call already has ~500ms budget; one DB query adds <5ms.
- **Pass ProcedureManager to CriticAgent** — CriticAgent gets a reference to the procedure manager (via Heart) during initialization, matching how layer.py already receives Heart.
- **Compact catalog format** — `"name — description (effectiveness: 85%)"` per line. Minimizes token usage while giving the LLM enough signal for routing.
- **Activation in advised mode only** — Skills are only activated when critic_mode is "advised". In shadow mode, skills are logged but not activated (consistent with frame behavior).
- **Empty catalog = empty skills** — If no procedures exist, the prompt says "No skills registered" and the Critic should return an empty skills list.

## Changes

### Phase A: CriticAgent receives procedure access (critic.py)

**File: `nous/cognitive/critic.py`**

1. **Update `__init__`** (line 68): Accept optional `procedure_manager` parameter
   ```python
   def __init__(self, settings: Settings, procedure_manager: ProcedureManager | None = None) -> None:
       self._settings = settings
       self._api: Any = None
       self._procedure_manager = procedure_manager
       ...
   ```

2. **Add `_build_skill_catalog` method** (new, after line 79):
   ```python
   async def _build_skill_catalog(self) -> tuple[str, set[str]]:
       """Build compact skill catalog from registered procedures.
       Returns (formatted_catalog, set_of_valid_names)."""
       if self._procedure_manager is None:
           return "No skills registered.", set()
       try:
           summaries, _total = await self._procedure_manager.list_all(
               limit=50, active_only=True,
           )
           if not summaries:
               return "No skills registered.", set()
           valid_names = set()
           lines = []
           for s in summaries:
               valid_names.add(s.name)
               eff = f" (effectiveness: {s.effectiveness:.0%})" if s.effectiveness is not None else ""
               lines.append(f"- {s.name} — {s.description or 'No description'}{eff}")
           return "\n".join(lines), valid_names
       except Exception:
           logger.warning("Failed to build skill catalog for Critic")
           return "No skills registered.", set()
   ```

3. **Update `_CLASSIFICATION_PROMPT`** (line 48-66): Add skill catalog section
   ```python
   _CLASSIFICATION_PROMPT = """\
   You are the Critic Agent for Nous, a cognitive AI system. Your role is to
   analyze the user's message and decide how Nous should process it.

   AVAILABLE FRAMES:
   {available_frames}

   AVAILABLE SKILLS:
   {skill_catalog}

   USER MESSAGE:
   {user_message}

   DECIDE:
   1. complexity: "simple" | "moderate" | "complex"
   2. routing: "single" (one frame, best choice)
   3. frames: list with exactly 1 frame name (the best choice for this message)
   4. skills: list of skill names from AVAILABLE SKILLS to activate (empty if none relevant, ONLY use names listed above)
   5. rationale: brief explanation of why this frame
   6. per_frame_instructions: {{}} (reserved for future phases)

   Respond ONLY with valid JSON. No markdown, no explanation outside the JSON."""
   ```

4. **Update `classify` method** (line 118-212): Build catalog and pass to prompt
   ```python
   async def classify(self, user_message, heuristic_frame, available_frames, tool_call_history=None):
       # ... (passthrough + api check unchanged) ...

       # Build skill catalog
       skill_catalog, valid_skill_names = await self._build_skill_catalog()

       prompt = self._CLASSIFICATION_PROMPT.format(
           available_frames="\n".join(f"- {f}" for f in available_frames),
           skill_catalog=skill_catalog,
           user_message=user_message,
       )

       # ... (LLM call unchanged) ...

       parsed = self._parse_classification(raw_text, heuristic_frame, valid_skill_names)
       # ...
   ```

5. **Update `_parse_classification`** (line 214-245): Accept valid_skill_names, filter output
   ```python
   def _parse_classification(self, raw_text, heuristic_frame, valid_skill_names=None):
       # ... (JSON parsing unchanged) ...
       raw_skills = data.get("skills", [])
       if valid_skill_names:
           skills = [s for s in raw_skills if s in valid_skill_names]
       else:
           skills = raw_skills
       return CriticResult(
           routing=RoutingMode.SINGLE_ADVISED,
           recommended_frame=recommended,
           rationale=data.get("rationale", ""),
           complexity=data.get("complexity", "moderate"),
           skills=skills,
       )
   ```

### Phase B: Wire CriticAgent initialization (layer.py, main.py)

**File: `nous/cognitive/layer.py`**

6. **Update Critic construction** — The CognitiveLayer doesn't construct the Critic; it receives it via `__init__`. The wiring happens in `main.py`.

**File: `nous/main.py`**

7. **Pass procedure_manager when constructing CriticAgent**:
   Find where CriticAgent is instantiated and pass `heart.procedures`:
   ```python
   critic = CriticAgent(settings, procedure_manager=heart.procedures)
   ```

### Phase C: Activate skills in layer.py

**File: `nous/cognitive/layer.py`** (after line 336, where critic_result is obtained)

8. **Activate matched procedures in advised mode**:
   ```python
   # F024/issue-216: Activate Critic-recommended skills
   if (self._settings.critic_mode == "advised"
           and critic_result.skills):
       activated_skill_ids = []
       for skill_name in critic_result.skills:
           try:
               proc = await self._heart.procedures.get_by_name(
                   skill_name, session=session,
               )
               if proc:
                   await self._heart.procedures.activate(
                       proc.id, session=session,
                   )
                   activated_skill_ids.append(str(proc.id))
                   logger.info(
                       "F024 Critic activated skill: %s (id=%s)",
                       skill_name, proc.id,
                   )
           except Exception:
               logger.warning("F024 Critic skill activation failed: %s", skill_name)
   ```

9. **Add activated skills to critic_classified event data** (line 379):
   ```python
   data={
       ...existing fields...
       "skills": critic_result.skills,
       "activated_skills": activated_skill_ids if self._settings.critic_mode == "advised" else [],
   },
   ```

### Phase D: Tests

**File: `tests/test_critic.py`**

10. **Test: skill catalog built from procedures**
    - Mock ProcedureManager.list_all returning 3 procedures
    - Verify `_build_skill_catalog` returns formatted catalog with correct names

11. **Test: skill catalog injected into prompt**
    - Mock ProcedureManager + API, verify prompt contains "AVAILABLE SKILLS" section

12. **Test: hallucinated skills are filtered out**
    - Return JSON with `skills: ["real_skill", "hallucinated_name"]`
    - Verify only "real_skill" survives in CriticResult.skills

13. **Test: empty procedure list produces "No skills registered"**

14. **Test: procedure_manager=None gracefully degrades**

15. **Test: valid_skill_names=None (backward compat) passes all skills through**

**File: `tests/test_layer_critic_skills.py`** (new, focused test file)

16. **Test: advised mode activates procedures for Critic skills**
17. **Test: shadow mode does NOT activate procedures**
18. **Test: unknown skill name in critic result is safely skipped**

## Files Changed

| File | Change Type | Lines |
|------|------------|-------|
| `nous/cognitive/critic.py` | Modified | ~40 lines added/changed |
| `nous/cognitive/layer.py` | Modified | ~20 lines added |
| `nous/main.py` | Modified | ~2 lines changed |
| `tests/test_critic.py` | Modified | ~80 lines added |
| `tests/test_layer_critic_skills.py` | New | ~60 lines |

## Risk Assessment

- **Low risk**: Changes are additive. Critic already has fallback paths for errors.
- **No migration needed**: No schema changes. Uses existing procedures API.
- **Backward compatible**: `procedure_manager=None` degrades to current behavior (no catalog, no filtering).
- **Performance**: One `list_all` query per Critic call adds <5ms to the ~500ms budget.

## Verification

1. All existing `test_critic.py` tests pass (no regressions)
2. New catalog tests pass
3. New filtering tests pass
4. New layer activation tests pass
5. `uv run pytest tests/test_critic.py tests/test_layer_critic_skills.py -v` green
