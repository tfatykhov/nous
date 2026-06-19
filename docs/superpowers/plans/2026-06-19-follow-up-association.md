# F083 Follow-up Association Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Nous resolve cross-session follow-ups ("the second option you mentioned", "continue what we were doing", "did that work?") from memory instead of asking the user to clarify.

**Architecture:** Three flag-gated layers in the cognitive context-assembly path. **A** = prior-session context reaches the prompt (A1 un-zero the conversation-frame episode budget; A2 inject the most-recent episode's full summary on a **verified first turn** of a new session). **B** = richer summaries via an `open_threads` dimension on the episode summarizer. **C** = detection + behavior (C1 a **first-turn-gated** deictic detector that raises `temporal_recency`; C2 a "recall before clarifying" instruction). A1/C1/C2 ship default-ON behind kill-switches; A2/B land dark (default OFF); the A2/B flag-default is decided from local-instance evidence.

**Tech Stack:** Python 3.12, pydantic-settings, SQLAlchemy async, Starlette, pytest + pytest-asyncio (real Postgres via docker-compose), `urllib` probe script against the live local instance (`192.168.1.141:8383`).

**Spec:** `docs/superpowers/specs/2026-06-19-follow-up-association-design.md`
**FORGE:** analysis `0c76ee32`, spec/plan `ab018bba`, review `b4d94716`

---

## Review revisions folded in (3-agent review `b4d94716`, all P1s verified against HEAD)

- **R1 [P1] A1×C1 interaction:** `intent.py:233` rescue is `current_ep_budget == 0`; A1 sets `episodes=600`, so C1's recency boost would never lift it. Fix: `== 0` → `< 1000` (T2). Spec data-flow + §6 gate corrected.
- **R2 [P1] C1 cross-session only:** the referent-conflation risk is same-session. C1 moves **out of `IntentClassifier.classify` into `pre_turn`**, gated on `is_first_turn`, with a narrowed regex; `_RECAP_PATTERNS` is **not** broadened (explicit recaps already work any-time). (T4)
- **R3 [P1] A2 reliable first-turn:** don't infer first-turn from empty `conversation_messages` (also true after LRU-evict + restore-miss). Use `is_first_turn = session_id not in self._active_episodes`, captured at the top of `pre_turn` and threaded into `build()`. (T2/T5)
- **R4 [P1] Settings DI:** `layer.py:235` constructs `IntentClassifier()` with no settings → kill-switches would read a divergent fresh `Settings()`. Explicit step: `IntentClassifier(settings=self._settings)`. (T2)
- **R5 [P1] B truncation:** prod `_summarize_single` `max_tokens=1500` already carries the F075 temporal block; adding `open_threads` risks JSON truncation → whole summary lost. Bump to `3000` when `episode_open_threads` is on (mirrors `extraction_coverage_broadened`). (T6)
- **R6 [P1] `open_threads` type safety:** guard non-list/null/non-str at both the merge `extend` (`.extend(None)` crashes) and the A2 read (`threads[:5]` on a str silently char-joins). `isinstance(list)` + per-entry `str` filter. (T5/T6)
- **R7 [P1] Test correctness:** `plan_retrieval(signals, input_text)` not `(text, frame)`; `FrameSelection` needs `frame_id+frame_name+confidence+match_method` and lives in `schemas.py` — reuse the existing `tests/test_intent.py` `_frame()` helper; replace phantom `_build_*_for_test` helpers with a real extracted `_build_summary_prompt` + a direct `_merge_summaries` call; pin real `build()` args + fixtures. (all test steps)
- **R8 [P2] A2 unranked-tier injection:** the temporal tier bypasses relevance floor / dedup / recency-resolver / contradiction. Bounded by first-turn + single most-recent episode + real (non-fabricated) content; documented as an intentional recency-only tier, and A2 routes through the existing `_is_system_episode` filter. Future option (recency-resolver routing) noted, not built.
- **R9 [P2] A2 try/except:** narrow so a malformed `open_threads` cannot nuke the whole temporal tier; guard the render explicitly. (T5)
- **R10 [P2] merge dict:** show the **full** `_merge_summaries` return dict so `outcome_rationale`/`topics` aren't dropped. (T6)
- **R11 [P3] token math / cache:** prod runs `NOUS_BUDGET_SCALE_ENABLED=true` + a ≥200k window → `_scaled_budget` ×1.5, so "600/1000" episode tokens are ~900/1500 in prod. A2 rides the **dynamic** tier (already volatile) — it does **not** bust the cached static prefix; spec risk row corrected.

### Re-review revisions (re-review `5ed05ab0`, 2 P1 + 1 P2 + 1 P3 introduced by R1–R11, verified vs HEAD)

- **R12 [P2] regex/test mismatch:** `did (?:that|it) work` does NOT match "did that **fix** work?" (the test/probe phrase). Broadened to `did (?:that|it)(?:\s+\w+){0,2}\s+work\b`. (T4)
- **R13 [P1] A2 `elif` dead-coded:** on a C1 follow-up `temporal_boost` is also True, so `if temporal_boost ... elif inject_full` never reaches A2. Reordered: `if inject_full and idx==0 ... elif temporal_boost ...`. (T5)
- **R14 [P2] A2 restart gap:** `warm_active_episode` runs at `layer.py:756` (after `build()`), so the first post-restart turn of an ongoing session would see `is_first_turn=True`. Fix: call `await self.warm_active_episode(session_id)` BEFORE capturing `is_first_turn`. (T5)
- **R15 [P3] background turns:** subtask/heartbeat turns go through `pre_turn` and create `_active_episodes` entries → A2/C1 could fire. Fix: `is_first_turn = (... ) and not is_subtask`. (T5)
- **R10 sharpened:** the real `_merge_summaries` return uses inline `summaries[-1].get("outcome"/"outcome_rationale")` and `sorted(merged_topics)` — the plan now shows the exact dict (no `merged_outcome`/`merged_outcome_rationale` NameError, no raw-set `topics`). (T6)

---

## Flag register (final)

| Flag (field on `Settings`) | Default | Layer |
|---|---|---|
| `followup_episode_budget_enabled` (`NOUS_FOLLOWUP_EPISODE_BUDGET_ENABLED`) | `True` | A1 |
| `followup_deictic_detection_enabled` (`NOUS_FOLLOWUP_DEICTIC_DETECTION_ENABLED`) | `True` | C1 |
| `recall_before_clarify_prompt` (`NOUS_RECALL_BEFORE_CLARIFY_PROMPT`) | `True` | C2 |
| `followup_first_turn_episode` (`NOUS_FOLLOWUP_FIRST_TURN_EPISODE`) | `False` | A2 |
| `episode_open_threads` (`NOUS_EPISODE_OPEN_THREADS`) | `False` | B |

## File structure

| File | Responsibility | Tasks |
|---|---|---|
| `nous/config.py` | 5 new `Settings` fields | T1 |
| `nous/cognitive/schemas.py:135` | A1 conversation-frame default budget | T2 |
| `nous/cognitive/intent.py` | A1 budget override (kill-switch) + R1 rescue fix | T2 |
| `nous/cognitive/layer.py` | DI fix; `is_first_turn`; C1 deictic detector + regex const | T2, T4, T5 |
| `nous/cognitive/context.py` | C2 section + A2 first-turn injection (`is_first_turn` param) | T3, T5 |
| `nous/handlers/episode_summarizer.py` | B `open_threads` instruction + schema + merge + max_tokens | T6 |
| `tests/test_followup_association.py` | unit + DB-backed integration | T2–T6 |
| `scripts/diag/followup_probe.py` | local-instance acceptance harness | T7 |
| `docs/features/INDEX.md`, `CLAUDE.md` | flag docs + shipped row | T8 |

> **Verified facts (HEAD):** `IntentClassifier.plan_retrieval(self, signals, input_text="")` (`intent.py:146`); `FrameSelection` requires `frame_id, frame_name, confidence, match_method` (`schemas.py:89-95`); `IntentClassifier()` built at `layer.py:235` (no settings); the rescue is `intent.py:231-234`; `_temporal_boost` derives at `layer.py:607-608` and is passed to `build(..., temporal_boost=...)` at `layer.py:670`; `build()` has `conversation_messages` + `temporal_boost` params (`context.py:133-137`); episode creation is gated on `session_id not in self._active_episodes` (`layer.py:755`) and happens AFTER the `build()` call (so `is_first_turn` is observable at the top of `pre_turn`); `EpisodeSummarizer` has `self._settings` (`episode_summarizer.py:181`); `_summarize_single` prompt assembly + `max_tokens` at `episode_summarizer.py:544-555`; `_merge_summaries` at `:616-658`; `list_episodes` already coerces `structured_summary` to dict-or-None (`episodes.py:403`).

---

## Task 0: Branch + reachability

- [ ] **Step 1: Create the feature branch**

```bash
git checkout -b feat/F083-follow-up-association
```

- [ ] **Step 2: Confirm the local instance is reachable**

Run:
```bash
py -c "import urllib.request; r=urllib.request.urlopen('http://192.168.1.141:8383/health',timeout=10); print(r.status)"
```
Expected: `200` (else fall back to a `POST /chat` smoke per `reports/_nous_consult_recall.py`). If unreachable, STOP and tell the user — the acceptance harness (T7) needs it.

---

## Task 1: Settings flags

**Files:** Modify `nous/config.py` (after `recall_parent_episode_truncate`, ~line 1314)

- [ ] **Step 1: Write the failing test**

`tests/test_followup_association.py`:
```python
from nous.config import Settings


def test_followup_flags_defaults():
    s = Settings()
    assert s.followup_episode_budget_enabled is True
    assert s.followup_deictic_detection_enabled is True
    assert s.recall_before_clarify_prompt is True
    assert s.followup_first_turn_episode is False
    assert s.episode_open_threads is False
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_followup_association.py::test_followup_flags_defaults -v`
Expected: FAIL — `AttributeError`

- [ ] **Step 3: Add the fields**

```python
    # --- F083 Follow-up Association ---
    followup_episode_budget_enabled: bool = Field(
        default=True,
        description=(
            "F083 A1 kill-switch. When true, the 'conversation' frame gets a non-zero "
            "episode retrieval budget so semantic episode recall fires for follow-ups. "
            "Set false to restore episodes=0."
        ),
    )
    followup_deictic_detection_enabled: bool = Field(
        default=True,
        description=(
            "F083 C1 kill-switch. When true, on the FIRST turn of a new session a deictic/"
            "continuation follow-up ('continue what we were doing', 'the second option you "
            "mentioned') raises temporal_recency, flipping the episode-budget rescue + temporal_boost."
        ),
    )
    recall_before_clarify_prompt: bool = Field(
        default=True,
        description=(
            "F083 C2. When true, inject a static instruction to call recall_deep/recall_recent "
            "to resolve a referent before asking the user to clarify."
        ),
    )
    followup_first_turn_episode: bool = Field(
        default=False,
        description=(
            "F083 A2 (land-dark). When true, on a verified first turn of a new session the temporal "
            "tier injects the most-recent episode's FULL summary (+ open_threads) instead of titles. "
            "Flip default after local-instance validation."
        ),
    )
    episode_open_threads: bool = Field(
        default=False,
        description=(
            "F083 B (land-dark). When true, the episode summarizer extracts an 'open_threads' array "
            "(unfinished items / next steps) into structured_summary. Flip default after validation."
        ),
    )
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_followup_association.py::test_followup_flags_defaults -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add nous/config.py tests/test_followup_association.py
git commit -m "feat(F083): add follow-up association settings flags"
```

---

## Task 2: Layer A1 — non-zero conversation-frame episode budget (+ DI fix + R1 rescue fix)

**Files:**
- Modify `nous/cognitive/schemas.py:135`
- Modify `nous/cognitive/intent.py` (`__init__`, conversation override ~220-226, rescue ~231-234)
- Modify `nous/cognitive/layer.py:235` (DI)
- Test `tests/test_followup_association.py`

- [ ] **Step 1: Write the failing tests** (reuse the real frame helper from `tests/test_intent.py`)

```python
from nous.cognitive.schemas import ContextBudget, FrameSelection
from nous.cognitive.intent import IntentClassifier
from nous.config import Settings


def _frame(frame_id="conversation"):
    # Mirror tests/test_intent.py: FrameSelection requires these 4 fields.
    return FrameSelection(frame_id=frame_id, frame_name=frame_id.title(),
                          confidence=0.9, match_method="pattern")


def test_conversation_frame_default_episode_budget_nonzero():
    assert ContextBudget.for_frame("conversation").episodes == 600  # was 0


def test_intent_conversation_override_episodes_when_enabled():
    clf = IntentClassifier(settings=Settings())  # A1 default ON
    signals = clf.classify("let's keep chatting about the weather", _frame())
    plan = clf.plan_retrieval(signals, input_text="let's keep chatting about the weather")
    assert plan.budget_overrides.get("episodes") == 600


def test_intent_conversation_override_episodes_when_disabled():
    clf = IntentClassifier(settings=Settings(followup_episode_budget_enabled=False))
    signals = clf.classify("let's keep chatting", _frame())
    plan = clf.plan_retrieval(signals, input_text="let's keep chatting")
    assert plan.budget_overrides.get("episodes") == 0


def test_rescue_lifts_above_a1_floor():
    # R1: with temporal_recency>0.5 the rescue must lift 600 -> 1000 (was gated on ==0).
    clf = IntentClassifier(settings=Settings())
    signals = clf.classify("let's keep chatting", _frame())
    signals.temporal_recency = 0.6
    plan = clf.plan_retrieval(signals, input_text="x")
    assert plan.budget_overrides.get("episodes") == 1000
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_followup_association.py -k "episode_budget or conversation_override or rescue_lifts" -v`
Expected: FAIL

- [ ] **Step 3: schemas.py:135 — bump the conversation default**

```python
            "conversation": cls(total=3000, decisions=500, facts=500, procedures=0, episodes=600, conversation_window=3),
```

- [ ] **Step 4: intent.py — add settings handle + kill-switch override + R1 rescue fix**

Add to `IntentClassifier.__init__` (create one if absent):
```python
    def __init__(self, settings=None):
        from nous.config import Settings
        self._settings = settings or Settings()
```
Conversation override (`intent.py:220-226`):
```python
        if signals.frame_type == "conversation":
            ep_budget = 600 if self._settings.followup_episode_budget_enabled else 0
            plan.budget_overrides = {
                "decisions": 500,
                "facts": 500,
                "procedures": 0,
                "episodes": ep_budget,
            }
        elif signals.frame_type == "decision":
            plan.budget_overrides = {"decisions": 3500, "procedures": 2000}
```
R1 rescue fix (`intent.py:233`): change the guard so a non-zero A1 floor still gets lifted:
```python
        if signals.temporal_recency > 0.5:
            current_ep_budget = plan.budget_overrides.get("episodes", None)
            if current_ep_budget is not None and current_ep_budget < 1000:
                plan.budget_overrides["episodes"] = 1000
```

- [ ] **Step 5: layer.py:235 — pass the app-wide settings (R4)**

```python
        self._intent_classifier = IntentClassifier(settings=settings)
```
(`CognitiveLayer.__init__` already holds `settings`; this is the only construction site — `grep -n "IntentClassifier(" nous/` to confirm.)

- [ ] **Step 6: Run to verify pass + no regressions**

Run: `uv run pytest tests/test_followup_association.py tests/test_intent.py -v`
Expected: PASS (confirm existing `IntentClassifier()` test calls still work via the `settings=None` default)

- [ ] **Step 7: Commit**

```bash
git add nous/cognitive/schemas.py nous/cognitive/intent.py nous/cognitive/layer.py tests/test_followup_association.py
git commit -m "feat(F083): un-zero conversation episode budget + rescue<1000 + settings DI (A1)"
```

- [ ] **Step 8: LOCAL-INSTANCE VERIFY (A1 isolated)** — `NOUS_FOLLOWUP_EPISODE_BUDGET_ENABLED=true`, all other F083 flags OFF, restart. Run the T7 probe; confirm via the `context.py:997` log line that `budget.episodes > 0` for conversation-frame follow-ups and no guard-metric regression. Record outcome.

---

## Task 3: Layer C2 — recall-before-clarify instruction

**Files:** Modify `nous/cognitive/context.py` (after the anti-hallucination block, ~line 263); add a `SECTION_TIERS` entry. Test `tests/test_followup_association.py`.

- [ ] **Step 1: Write the failing test** (uses the real `build()` signature + a DB fixture — mirror `tests/test_context*.py`)

```python
import pytest


@pytest.mark.asyncio
async def test_recall_before_clarify_section_present(context_engine, frame_conv):
    # context_engine / frame_conv: fixtures mirroring the existing context tests.
    res = await context_engine(recall_before_clarify_prompt=True).build(
        "agent", "sess-new", "what about that?", frame_conv, conversation_messages=None,
    )
    assert "before asking" in res.system_prompt.lower() and "clarify" in res.system_prompt.lower()


@pytest.mark.asyncio
async def test_recall_before_clarify_absent_when_off(context_engine, frame_conv):
    res = await context_engine(recall_before_clarify_prompt=False).build(
        "agent", "sess-new", "what about that?", frame_conv, conversation_messages=None,
    )
    assert "before asking" not in res.system_prompt.lower()
```

> If `tests/test_context*.py` has no reusable engine fixture, add a module-level fixture in this test file that builds `ContextEngine` against the docker-compose Postgres exactly as those tests do (real DB, per CLAUDE.md). Do NOT mock the DB.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_followup_association.py -k recall_before_clarify -v`
Expected: FAIL

- [ ] **Step 3: Implement the section + tier entry**

Add to `SECTION_TIERS` (`context.py:32-41`): `"Recall Before Clarifying": "static",`. Then after the anti-hallucination `sections.append(...)` (after line 263):
```python
        # F083 C2: recall-before-clarify cue. Static → caches in the stable prefix.
        if self._settings.recall_before_clarify_prompt:
            recall_first = (
                "Before asking the user to clarify a referent — a pronoun, "
                '"that", "the thing/option you mentioned", or a continuation of '
                "earlier work — first call recall_deep or recall_recent to resolve "
                "it from your memory of prior sessions. Only ask the user to "
                "clarify if recall returns nothing relevant."
            )
            sections.append(
                ContextSection(
                    priority=2,
                    label="Recall Before Clarifying",
                    content=recall_first,
                    token_estimate=self._estimate_tokens(recall_first),
                    tier=SECTION_TIERS.get("Recall Before Clarifying", "static"),
                )
            )
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_followup_association.py -k recall_before_clarify -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add nous/cognitive/context.py tests/test_followup_association.py
git commit -m "feat(F083): recall-before-clarify instruction section (C2)"
```

- [ ] **Step 6: LOCAL-INSTANCE VERIFY (C2 isolated)** — flag ON, A2/B OFF, restart, run probe set; confirm the model calls a recall tool before clarifying on deictic probes. Record outcome.

---

## Task 4: Layer C1 — first-turn-gated deictic detector (in `pre_turn`)

**Files:**
- Modify `nous/cognitive/layer.py` — add `_DEICTIC_FOLLOWUP` module const near `_RECAP_PATTERNS` (~line 64); add the first-turn-gated boost in `pre_turn` (between `plan_retrieval` at :604 and the `_is_recap` block at :606). **Do NOT broaden `_RECAP_PATTERNS`** (R2) and **do NOT touch `classify`** (R2/R7).
- Test `tests/test_followup_association.py`

> `is_first_turn` is computed in T5 Step 3 at the top of `pre_turn`. T4 and T5 both edit `pre_turn`; implement T4's boost referencing the `is_first_turn` local that T5 introduces (do T5 Step 3's capture line first if implementing T4 standalone).

- [ ] **Step 1: Write the failing tests**

```python
from nous.cognitive.layer import _DEICTIC_FOLLOWUP


def test_deictic_matches_cross_session_referents():
    for s in ["what about the second option you mentioned?",
              "can you continue what we were doing?",
              "did that fix work?"]:
        assert _DEICTIC_FOLLOWUP.search(s), s


def test_deictic_does_not_match_same_session_coding():
    # Hard negatives — these are normal same-session instructions, NOT follow-ups.
    for s in ["continue the loop until done",
              "use the first argument",
              "write a python function to reverse a string",
              "what about performance?"]:
        assert not _DEICTIC_FOLLOWUP.search(s), s
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_followup_association.py -k deictic -v`
Expected: FAIL — `ImportError: _DEICTIC_FOLLOWUP`

- [ ] **Step 3: Add the narrowed regex const** (`nous/cognitive/layer.py`, near `_RECAP_PATTERNS`)

```python
# F083 C1: cross-session deictic/continuation follow-up markers. Deliberately
# narrow — must NOT match same-session coding instructions ("continue the loop",
# "use the first argument"). Requires a referent noun tied to prior discussion.
_DEICTIC_FOLLOWUP = re.compile(
    r"\b(?:"
    r"that (?:option|approach|idea|fix|bug) (?:you|we)\b"
    r"|the (?:second|first|other|previous|last) (?:option|approach|idea) (?:you|we)\b"
    r"|(?:option|approach|idea) you (?:mentioned|suggested|proposed)\b"
    r"|you (?:mentioned|suggested|proposed) (?:earlier|before|last time)\b"
    r"|continue what we (?:were|had been) (?:doing|working)\b"
    r"|(?:pick up |continue )?where we left off\b"
    r"|did (?:that|it)(?:\s+\w+){0,2}\s+work\b"   # R12: also "did that fix/change work"
    r"|as we discussed (?:earlier|before|last time)\b"
    r")",
    re.IGNORECASE,
)
```

(`re` is already imported in `layer.py`.)

- [ ] **Step 4: Add the first-turn-gated boost in `pre_turn`** (after `plan = ...plan_retrieval(...)` at `layer.py:604`)

```python
        # F083 C1: on the FIRST turn of a new session, a cross-session deictic
        # follow-up raises recency so episodes are retrieved + temporal_boost fires.
        # Gated on is_first_turn (captured at the top of pre_turn) so same-session
        # references — already in live history — never pull cross-session episodes.
        if (is_first_turn and self._settings.followup_deictic_detection_enabled
                and _DEICTIC_FOLLOWUP.search(user_input)):
            signals.temporal_recency = max(signals.temporal_recency, 0.6)
            plan = self._intent_classifier.plan_retrieval(signals, input_text=user_input)
```

- [ ] **Step 5: Run to verify pass**

Run: `uv run pytest tests/test_followup_association.py -k deictic tests/test_intent.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add nous/cognitive/layer.py tests/test_followup_association.py
git commit -m "feat(F083): first-turn-gated deictic follow-up detector (C1)"
```

- [ ] **Step 7: LOCAL-INSTANCE VERIFY (C1 isolated)** — flag ON (A2/B OFF), restart. Run probe set; confirm deictic probes on a NEW session flip `temporal_boost` (episode summaries injected, not titles) and `budget.episodes==1000` (R1 fix); confirm the hard negatives (T7) and a SAME-session deictic ("apply that fix" mid-conversation) do NOT trigger. Record outcome.

---

## Task 5: Layer A2 — first-turn last-episode full-summary injection

**Files:**
- Modify `nous/cognitive/layer.py` — capture `is_first_turn` at the top of `pre_turn`; pass it to `build()`
- Modify `nous/cognitive/context.py` — `build()` gains `is_first_turn: bool = False`; temporal-tier injection (~907-938)
- Test `tests/test_followup_association.py`

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.asyncio
async def test_first_turn_injects_full_summary_when_enabled(context_engine, frame_conv, seed_episode):
    eng = context_engine(followup_first_turn_episode=True)
    res = await eng.build("agent", "sess-new", "continue what we were doing", frame_conv,
                          conversation_messages=None, is_first_turn=True)
    # Assert a PREFIX (A2 truncates to recall_parent_episode_truncate); not the whole string.
    assert seed_episode.structured_summary["summary"][:120] in res.system_prompt


@pytest.mark.asyncio
async def test_no_full_summary_when_not_first_turn(context_engine, frame_conv, seed_episode):
    eng = context_engine(followup_first_turn_episode=True)
    res = await eng.build("agent", "sess-new", "continue", frame_conv,
                          conversation_messages=["prior turn"], is_first_turn=False)
    assert seed_episode.structured_summary["summary"][:120] not in res.system_prompt


@pytest.mark.asyncio
async def test_flag_off_first_turn_titles_only(context_engine, frame_conv, seed_episode):
    # R-flag-off: with A2 OFF, a first turn behaves byte-identically to pre-F083 (titles-only).
    eng = context_engine(followup_first_turn_episode=False)
    res = await eng.build("agent", "sess-new", "continue", frame_conv,
                          conversation_messages=None, is_first_turn=True)
    assert seed_episode.structured_summary["summary"][:120] not in res.system_prompt
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_followup_association.py -k "first_turn or titles_only" -v`
Expected: FAIL (`build()` has no `is_first_turn` kwarg)

- [ ] **Step 3: Capture `is_first_turn` in `pre_turn` + thread to `build()`**

Near the top of `pre_turn` (before the episode-creation block at `layer.py:755`), FIRST warm the active-episode map from DB, THEN capture the signal, AND-ing in `not is_subtask`:
```python
        # F083 A2/C1 (R14): warm first so a post-restart ongoing session is not
        # mis-seen as a first turn. warm_active_episode is idempotent (returns the
        # cached value if present) and only restores ongoing (ended_at IS NULL,
        # active=True) episodes — so a just-ENDED session correctly stays first-turn.
        await self.warm_active_episode(session_id)
        # First turn iff no active episode exists yet. Survives LRU eviction (this
        # map is session-lived, cleared only at end_session). R15: exclude
        # subtask/background turns from follow-up injection entirely.
        is_first_turn = (session_id not in self._active_episodes) and not is_subtask
```
> If `warm_active_episode(session_id)` is already called later in `pre_turn` (it is, ~`layer.py:756`), this early call is a cheap idempotent no-op there; do not remove the later call.
Add `is_first_turn=is_first_turn` to the `self._context.build(...)` call (`layer.py:660-673`):
```python
                    temporal_boost=_temporal_boost,  # 008.6
                    is_first_turn=is_first_turn,      # F083 A2
                    critic_skills=_critic_skills,
                    epistemic_class=epistemic_class,
                )
```

- [ ] **Step 4: Add the `is_first_turn` param + A2 injection in `context.py`**

`build()` signature (`context.py:125-137`): add `is_first_turn: bool = False,`.

In the temporal-tier loop (replace the body-line block at `context.py:924-926`):
```python
                    inject_full = self._settings.followup_first_turn_episode and is_first_turn
                    for idx, e in enumerate(recent):
                        title = e.title or (e.summary[:60] if e.summary else "Untitled")
                        time_str = e.started_at.strftime("%b %d %H:%M")
                        recent_lines.append(f"- [{time_str}] {title}")
                        # R13: A2 must take PRECEDENCE over the temporal_boost body line.
                        # On a C1 follow-up temporal_boost is ALSO True, so an `elif` here
                        # would dead-code A2 exactly when it should fire. inject_full first.
                        if inject_full and idx == 0:
                            # F083 A2: most-recent episode's FULL summary on a verified
                            # first turn. structured_summary is dict-or-None (episodes.py:403).
                            struct = getattr(e, "structured_summary", None) or {}
                            full_summary = struct.get("summary") or e.summary
                            if full_summary and full_summary != title:
                                trunc = self._settings.recall_parent_episode_truncate
                                recent_lines.append(f"  {full_summary[:trunc]}")
                            # R6/R9: guard open_threads shape; never let it break the tier.
                            threads = struct.get("open_threads")
                            if isinstance(threads, list):
                                items = [str(t) for t in threads if isinstance(t, (str, int, float))][:5]
                                if items:
                                    recent_lines.append("  Open threads: " + "; ".join(items))
                        elif temporal_boost and e.summary and e.summary != e.title:
                            recent_lines.append(f"  {e.summary[:200]}")
```

> Flag-OFF safety (R-flag-off): `inject_full` is False ⇒ the `if inject_full...` branch is skipped ⇒ the existing `elif temporal_boost...` body at `context.py:925-926` runs unchanged ⇒ byte-identical to pre-F083. Do not alter the `elif` body.

- [ ] **Step 5: Run to verify pass**

Run: `uv run pytest tests/test_followup_association.py -k "first_turn or titles_only" -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add nous/cognitive/layer.py nous/cognitive/context.py tests/test_followup_association.py
git commit -m "feat(F083): verified-first-turn last-episode full-summary injection (A2, default OFF)"
```

- [ ] **Step 7: LOCAL-INSTANCE VERIFY (A2 flag ON)** — `NOUS_FOLLOWUP_FIRST_TURN_EPISODE=true`, restart. New-session first turn shows the most-recent episode's full summary; a SECOND turn in that session does NOT re-inject it. Record outcome (feeds Decision Gate).

---

## Task 6: Layer B — `open_threads` summarizer dimension

**Files:** Modify `nous/handlers/episode_summarizer.py` (`_OPEN_THREADS_INSTRUCTION` const; append + `max_tokens` bump in `_summarize_single` ~544-555; `_merge_summaries` ~616-658). Test `tests/test_followup_association.py`.

- [ ] **Step 1: Write the failing tests** (test the REAL `_merge_summaries`; extract a real prompt helper — no phantom `_for_test`)

```python
from nous.handlers.episode_summarizer import EpisodeSummarizer, _OPEN_THREADS_INSTRUCTION


def _summarizer(open_threads=False):
    # Build without running __init__ DB wiring, mirroring tests/test_f025_chunked.py.
    s = EpisodeSummarizer.__new__(EpisodeSummarizer)
    from nous.config import Settings
    s._settings = Settings(episode_open_threads=open_threads)
    return s


def test_open_threads_in_prompt_when_enabled():
    s = _summarizer(open_threads=True)
    prompt = s._build_summary_prompt(transcript="t", decision_context="", started_at=None)
    assert "open_threads" in prompt and _OPEN_THREADS_INSTRUCTION in prompt


def test_open_threads_absent_when_disabled():
    s = _summarizer(open_threads=False)
    prompt = s._build_summary_prompt(transcript="t", decision_context="", started_at=None)
    assert "open_threads" not in prompt  # byte-identical to pre-F083 base prompt


def test_merge_preserves_open_threads_and_required_keys():
    s = _summarizer()
    merged = s._merge_summaries([
        {"title": "p1", "summary": "a", "key_points": [], "outcome": "partial",
         "outcome_rationale": "r1", "topics": ["t"], "candidate_facts": [], "open_threads": ["finish auth"]},
        {"title": "p2", "summary": "b", "key_points": [], "outcome": "partial",
         "outcome_rationale": "r2", "topics": ["u"], "candidate_facts": [], "open_threads": ["write tests"]},
    ])
    assert merged["open_threads"] == ["finish auth", "write tests"]
    for k in ("title", "summary", "outcome", "outcome_rationale", "topics", "candidate_facts"):
        assert k in merged


def test_merge_open_threads_typesafe():
    s = _summarizer()
    merged = s._merge_summaries([
        {"summary": "a", "candidate_facts": [], "open_threads": None},
        {"summary": "b", "candidate_facts": [], "open_threads": "not a list"},
        {"summary": "c", "candidate_facts": [], "open_threads": ["ok"]},
    ])
    assert merged["open_threads"] == ["ok"]  # null + str silently ignored, no crash
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_followup_association.py -k "open_threads or merge" -v`
Expected: FAIL

- [ ] **Step 3: Extract a prompt helper + add the instruction const**

Add near `_F075_TEMPORAL_INSTRUCTION` (~line 90):
```python
# F083 B: appended to _SUMMARY_PROMPT when settings.episode_open_threads is True.
# CONCATENATED (single braces), mirroring _F075_TEMPORAL_INSTRUCTION. Respects the
# NO PADDING rule: empty/omitted when nothing is genuinely unfinished.
_OPEN_THREADS_INSTRUCTION = """

OPEN THREADS (F083):
If the TRANSCRIPT above leaves work unfinished — a next step the user intended, a
question left open, a task started but not completed — add a top-level "open_threads"
array, each entry one short actionable phrase:

  "open_threads": ["finish wiring the auth callback", "decide on retry budget"]

Return an empty array (or omit) when nothing is unfinished. Do NOT invent or pad."""
```

Refactor `_summarize_single`'s prompt assembly (`episode_summarizer.py:544-555`) into a pure helper so it's testable without an LLM call:
```python
    def _build_summary_prompt(self, transcript, decision_context, started_at):
        prompt = _SUMMARY_PROMPT.format(transcript=transcript, decision_context=decision_context)
        if getattr(self._settings, "temporal_extraction_enabled", False):
            if started_at is not None:
                prompt = _F075_EPISODE_TS_BLOCK.format(iso=started_at.isoformat()) + prompt
            prompt = prompt + _F075_TEMPORAL_INSTRUCTION
        if getattr(self._settings, "extraction_coverage_broadened", False):
            prompt = prompt + _COVERAGE_EXPANSION_INSTRUCTION
        if getattr(self._settings, "episode_open_threads", False):
            prompt = prompt + _OPEN_THREADS_INSTRUCTION
        return prompt

    def _summary_max_tokens(self):
        # R5: open_threads competes with F075 events for output budget; raise the
        # ceiling so a long transcript's JSON doesn't truncate (whole summary lost).
        if getattr(self._settings, "extraction_coverage_broadened", False):
            return 3000
        if getattr(self._settings, "episode_open_threads", False):
            return 3000
        return 1500
```
Then in `_summarize_single`, replace the inline assembly (lines 544-555) with:
```python
        prompt = self._build_summary_prompt(transcript, decision_context, started_at)
        max_tokens = self._summary_max_tokens()
```
(Preserve the existing `_COVERAGE_EXPANSION_INSTRUCTION` behavior — it now lives in the helper.)

- [ ] **Step 4: Type-safe merge (R6/R10) — full dict, no elision**

In `_merge_summaries` (`episode_summarizer.py:616-658`), add before the return:
```python
        merged_open_threads: list = []
        for s in summaries:
            v = s.get("open_threads")
            if isinstance(v, list):
                merged_open_threads.extend(
                    str(t) for t in v if isinstance(t, (str, int, float))
                )
```
And add `"open_threads": merged_open_threads[:10],` to the returned dict. **R10: the real return (`episode_summarizer.py:650-658`) uses INLINE expressions, not `merged_*` vars — `outcome`/`outcome_rationale` come from `summaries[-1].get(...)` and `topics` is `sorted(merged_topics)` (a set). Reproduce it EXACTLY and add only the one key:**
```python
        return {
            "title": summaries[0].get("title", "Multi-part episode"),
            "summary": " ".join(merged_summary_parts),
            "key_points": merged_key_points[:10],
            "candidate_facts": merged_candidate_facts,
            "outcome": summaries[-1].get("outcome", "informational"),
            "outcome_rationale": summaries[-1].get("outcome_rationale", ""),
            "topics": sorted(merged_topics),
            "open_threads": merged_open_threads[:10],   # F083 B — the only new key
        }
```
(Do NOT substitute `merged_outcome`/`merged_outcome_rationale` — they don't exist and would `NameError`; do NOT return a raw `merged_topics` set — keep `sorted(...)` or JSON serialization breaks.)

- [ ] **Step 5: Run to verify pass**

Run: `uv run pytest tests/test_followup_association.py -k "open_threads or merge" -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add nous/handlers/episode_summarizer.py tests/test_followup_association.py
git commit -m "feat(F083): open_threads summarizer dimension, type-safe, max_tokens bump (B, default OFF)"
```

- [ ] **Step 7: LOCAL-INSTANCE VERIFY (B flag ON)** — `NOUS_EPISODE_OPEN_THREADS=true` (+ A2 ON), restart. Re-summarize a seed episode with unfinished work (trigger sleep/summarize), confirm `structured_summary.open_threads` populated AND the summary dict still parses on a long transcript (no truncation). Then run a continuation probe; confirm A2 injects the threads. Record outcome (feeds Decision Gate).

---

## Task 7: Local-instance acceptance probe harness

**Files:** Create `scripts/diag/followup_probe.py`

- [ ] **Step 1: Write the harness** (note the hard negatives from the review)

```python
"""F083 follow-up association probe — drives the LIVE local Nous instance.

For each probe: send SEED (session A), then the FOLLOW-UP as a NEW session (session B).
Score recall-precedes-clarification: PASS if the follow-up resolves the referent from
prior-session context OR calls a recall tool before clarifying; FAIL if it asks the user
to clarify without recalling first. Negatives SHOULD ask for clarification.

Usage: py scripts/diag/followup_probe.py --label baseline
"""
import argparse, json, time, urllib.request

BASE = "http://192.168.1.141:8383"

PROBES = [
    {"id": "deictic_option", "seed": "Give me two options for caching: Redis or in-memory LRU.",
     "followup": "what about the second option you mentioned?", "negative": False},
    {"id": "continuation", "seed": "Let's start refactoring the auth module; first extract the token parser.",
     "followup": "can you continue what we were doing?", "negative": False},
    {"id": "outcome_check", "seed": "Apply the fix to the retry budget in the worker pool.",
     "followup": "did that work?", "negative": False},
    # Hard negatives (review R2): same-session-style / fresh inputs that must NOT pull cross-session episodes.
    {"id": "neg_ambiguous", "seed": "Tell me about Postgres indexes.",
     "followup": "what about the other thing?", "negative": True},
    {"id": "neg_fresh_task", "seed": "Summarize HNSW indexing.",
     "followup": "write a python function to reverse a string", "negative": True},
    {"id": "neg_same_session_phrasing", "seed": "Explain async generators.",
     "followup": "use the first argument", "negative": True},
]


def chat(message, session_id):
    body = json.dumps({"message": message, "session_id": session_id,
                       "user_id": "claude-code", "user_display_name": "F083 probe"}).encode()
    req = urllib.request.Request(f"{BASE}/chat", data=body, headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=300).read())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    args = ap.parse_args()
    results = []
    for p in PROBES:
        chat(p["seed"], f"f083-seed-{p['id']}-{args.label}")
        time.sleep(2)
        r = chat(p["followup"], f"f083-followup-{p['id']}-{args.label}")
        resp = (r.get("response") or "")
        clarify = any(k in resp.lower() for k in
                      ["could you clarify", "which ", "what do you mean", "not sure what you", "can you specify"])
        results.append({"id": p["id"], "negative": p["negative"], "frame": r.get("frame"),
                        "asked_clarification": clarify, "response": resp[:1200]})
        print(f"{p['id']}: frame={r.get('frame')} clarify={clarify}")
    out = f"reports/f083_probe_{args.label}.json"
    open(out, "w", encoding="utf-8").write(json.dumps(results, indent=2))
    print("saved", out)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run the BASELINE (HEAD defaults, before enabling any layer)**

Run: `py scripts/diag/followup_probe.py --label baseline`
Expected: writes `reports/f083_probe_baseline.json`. Read it manually — non-negatives likely show `asked_clarification=true`.

- [ ] **Step 3: Commit**

```bash
git add scripts/diag/followup_probe.py
git commit -m "test(F083): local-instance follow-up probe harness with hard negatives"
```

> `asked_clarification` is a coarse signal; the acceptance read is MANUAL (did the follow-up resolve the referent from prior-session context? did the negatives correctly clarify-or-decline rather than confabulate from an unrelated episode?). Each per-layer VERIFY step re-runs this with a distinct `--label` after toggling ONE flag + restart.

---

## Task 8: Docs

**Files:** Modify `docs/features/INDEX.md` (F083 row), `CLAUDE.md` (5 flag rows + shipped row)

- [ ] **Step 1:** Add the 5 env-var rows + an F083 shipped-table row to `CLAUDE.md`.
- [ ] **Step 2:** Add the `INDEX.md` F083 row (A1/C1/C2 ON, A2/B dark).
- [ ] **Step 3: Commit**

```bash
git add docs/features/INDEX.md CLAUDE.md
git commit -m "docs(F083): follow-up association flags + shipped row"
```

---

## Decision Gate: A2 + B flag defaults (the user's mandate)

After all per-layer LOCAL-INSTANCE VERIFY steps:

1. Compare `reports/f083_probe_baseline.json` vs the A2-on and B-on runs.
2. **Flip `NOUS_FOLLOWUP_FIRST_TURN_EPISODE` → default `True`** only if A2-on improves recall-precedes-clarification on non-negative probes, with no guard-metric regression AND **zero negative-control false positives** (no confabulation from an unrelated injected episode).
3. **Flip `NOUS_EPISODE_OPEN_THREADS` → default `True`** only if B-on (with A2) measurably improves the *continuation* probe AND no summary-truncation observed on long transcripts (R5).
4. If a layer shows no win, it stays default-OFF (land-dark), pending a fuller F051-harness A/B.
5. Record the verdict in FORGE (`review_outcome`) + a memory entry; update `config.py` defaults + `CLAUDE.md` if flipping. Surface the probe evidence to the user before any flip.

---

## Self-Review (post-revision)

- **Spec coverage:** A1→T2, A2→T5, B→T6, C1→T4, C2→T3, validation→T7 + per-layer verify, decision gate→dedicated section, flags→T1, docs→T8. All 11 review revisions (R1–R11) mapped to a task.
- **Placeholder scan:** test fixtures (`context_engine`, `frame_conv`, `seed_episode`) are explicitly instructed to mirror existing `tests/test_context*.py`/`tests/test_intent.py` patterns against the real Postgres — not hidden TODOs. No `...`-as-arg remains (real `build()` positional args pinned).
- **Type consistency:** `is_first_turn` introduced in T5 Step 3 and consumed in T4 Step 4 + T5 Step 4 (cross-task dependency noted in T4 header); `_DEICTIC_FOLLOWUP` defined T4, used T4; flag field names consistent T1↔usage; `_build_summary_prompt`/`_summary_max_tokens`/`_merge_summaries` are real (extracted) seams, no `_for_test` phantoms; `open_threads` read (T5) and write/merge (T6) both `isinstance(list)`-guarded.
- **R1 verified:** `intent.py:233` `== 0` → `< 1000`; the `test_rescue_lifts_above_a1_floor` test asserts the fix.
