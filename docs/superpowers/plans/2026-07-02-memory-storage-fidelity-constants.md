# Memory Storage Fidelity — Non-Configurable Constants Fix Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate silent information loss at the memory write path under the principle **"capture losslessly, bound processing, gate admission, cap retrieval"** — capture-side truncations become generous sanity bounds, processing costs get explicit bounds at the seams that own them, and admission gates become configurable and observable — without touching retrieval ranking (covered by the 2026-07-01 A/B-gated plan).

**Architecture:** The 2026-07-02 scan's constants fall into four classes, only three of which are legitimate: (1) **capture truncations** destroy information at the cheapest point to preserve it — these become sanity bounds (8000 chars) since `episodes.transcript` (F025 P3-C) persists raw text for future re-derivation; (2) **derivation budgets** (LLM max_tokens / input caps over *preserved* raw) are legitimate and ship the architecture-reviewed improved values; (3) **admission gates** are retrieval-precision defenses (36% of decisions are autopilot noise per the 2026-06-14 Brain audit) and keep current values, now configurable; (4) the two costs the old capture caps were *indirectly* controlling — summarizer chunk-call count and F067 chunk-embedding volume — get **explicit caps at their own seams** instead (`episode_summary_max_chunks`, `episode_chunk_max_per_episode`). Rollback for any single value is one env-var pin — no flags, no migration. Reviewed 2026-07-02 (code-architect, APPROVE_WITH_REVISIONS) + lossless-capture revision (user decision, FORGE fc0712de).

**Tech Stack:** Python 3.12+, pydantic-settings (`NOUS_` prefix), pytest + pytest-asyncio, docker-compose local instance for the probe.

**Source:** 2026-07-02 multi-agent scan (5 finders + 5 adversarial config-wiring verifiers, 114 confirmed non-configurable constants, 13 claims refuted). Full verified inventory: `docs/reviews/2026-07-02-nonconfigurable-constants-scan.json`; triage below.

## Global Constraints

- **Improved defaults ship directly** for fidelity caps and LLM budgets (user decision 2026-07-02; architecture-reviewed). The three **destructive/admission gates keep current values** (`episode_dedup_threshold` 0.85 / 48h / `episode_min_content_length` 200, `fact_min_content_chars` 30, `fact_supersession_threshold` 0.80) — changing what gets deleted/rejected requires dedicated evidence. Rollback for any regression = pin that one env var to the old literal (listed in Task 9).
- **No new boolean Settings fields** — all new fields are ints/floats, so the bare-MagicMock fixture trap (memory `feedback_f071_shipped`) does not apply. If a task finds it needs a boolean, it must pin it `False` in `tests/test_streaming.py::_make_mock_settings` and siblings.
- **Do not touch retrieval ranking constants** (`retrieval_pipeline.py` seed caps `[:3]`/`limit=2`, `stale_penalty=0.3`, spreading `0.1` floor). Those require the retrieval A/B instrument from `docs/superpowers/plans/2026-07-01-memory-retrieval-fixes-ab-gated.md` Phase 0 and are explicitly out of scope here (see "Not in this plan").
- All tests run against real Postgres where they need the DB (`docker compose up -d postgres`), per project convention; pure config/wiring tests may use unit-level fakes that already exist in the test suite.
- Commit style: `feat:`/`fix:` prefixes, feature branch `feat/memory-fidelity-constants`, one logical change per commit.
- CLAUDE.md env-var table must be updated for every new setting (Task 9).

---

## Triage — why these 12 constants (and not the other 102)

The scan confirmed 114 non-configurable constants (7 high / 64 medium / 43 low impact). Making all 114 configurable is config sprawl, not improvement. Selection rule: **(a)** the constant destroys information at capture/persist/derive time (loss is permanent — downstream can never recover it), **(b)** changing it has a plausible fidelity win with bounded cost, and **(c)** it is not owned by the retrieval A/B plan.

**In scope (12 findings → 18 settings, incl. 2 new downstream bounds):**

| # | Site | Constant | Loss it causes |
|---|------|----------|----------------|
| 1 | `cognitive/layer.py:462,1287` | `[:500]` per transcript message | Sole source for stored episode transcript, LLM summary, extracted facts, F067 chunks — 80–90% of long messages lost at capture |
| 2 | `cognitive/layer.py:1874` | `reflection[:500]` | Session's distilled lesson truncated before `heart.episodes.lessons_learned` |
| 3 | `cognitive/layer.py:821,825` | `user_input[:200]` | Episode summary seed + dedup probe — 200-char stub is the searchable summary/embedding until the summarizer rewrites it |
| 4 | `cognitive/layer.py:1783` + `:49` | `> 0.85` / 48h / `_MIN_CONTENT_LENGTH=200` | Episode-level admission: whole sessions (transcript+facts+chunks) silently never captured |
| 5 | `cognitive/monitor.py:328,345,353` | `[:1000]` input, `max_tokens=512`, `<30` principle gate | F039 correction facts — user explicitly teaches the agent and the lesson is truncated/dropped (F031 bug class) |
| 6 | `handlers/episode_summarizer.py:585-586` | `3000/1500` max_tokens | The single most upstream derivation seam; JSON tail truncation silently drops `candidate_facts` |
| 7 | `handlers/knowledge_extractor.py:181,203` | `[:2000]`/msg, `[:12000]` total | Pre-compaction extraction sees a head-slice of messages that are about to be destroyed — unrecoverable |
| 8 | `handlers/sleep_handler.py:709` | `ep.summary[:200]` | Sleep reflection derives cross-episode patterns from 200-char snippets of 150-word summaries |
| 9 | `handlers/sleep_handler.py:1026` | `fact[:500]` | SUPERSEDE/REMOVE/MERGE verdicts (destructive) computed from half-visible facts |
| 10 | `heart/facts.py:506` (+437) | `< 30` chars hard reject | Terse high-value facts ("v0.2 shipped") silently rejected on every write path |
| 11 | `heart/facts.py:945` | `> 0.80` supersession | Deactivates old facts; sibling threshold `fact_native_cosine_threshold` IS configurable, this one isn't |
| 12 | `brain/graph_linker.py:194` + `handlers/decision_graph_linker.py:87` | `timedelta(days=30)` | Decisions older than 30 days can structurally NEVER receive `evidence_for` edges — permanent graph blind spot |

**Not in this plan (documented decisions, not omissions):**
- **Retrieval-side constants** (`retrieval_pipeline.py:456/469/510/519` seed caps, `:648` activation floor, `:1201` stale penalty, `tools.py:1176` recall_recent `[:150]`, `heart.py:936` / `search.py:236` pool multipliers) → owned by the 2026-07-01 A/B-gated plan; changing them without the Phase-0 measurement instrument is blind tuning.
- **Two real bugs found by the scan, not config issues** — file as separate issues: (a) spreading-activation results carry a contentless `str(nid)[:8]` placeholder into the LLM (`retrieval_pipeline.py:654` — content never hydrated); (b) `run_python`'s in-scope `recall_deep` is silently facts-only with `limit=5` (`tools.py:3234`) while its schema implies full recall.
- **Brain decision autopilot constants** (`layer.py:1180` `[:500]` description, `:1157` `thinking[:2000]`, `brain.py:306/319/329` noise gates) → the 2026-06-14 Brain audit found 36% of decisions are autopilot noise; increasing their fidelity is not clearly an improvement. Revisit after the triage-as-decision fix decided there.
- **Working-memory caps** (`layer.py:1628`, `:1586`, `residual_activation.py:260 [:160]`) → working memory is short-lived scratch space swept by F049; fidelity loss is bounded by design.
- **All 43 low-impact findings** (MCP limits, `mcp.py:327` name`[:100]`, pre-prune salvage caps, etc.) → cost/benefit doesn't clear the bar; inventory retained in the scan artifact for future reference.

---

## Recommended defaults (architecture-reviewed 2026-07-02)

Reviewed by a code-architect agent against actual code paths (chunked summarization loop `episode_summarizer.py:532-543`, F067 `chunk_text` geometry, F027 disambiguation band, 2026-06-13 edge-precision audit), verdict APPROVE_WITH_REVISIONS; then revised to the **lossless-capture model** (user decision 2026-07-02): capture caps stop rationing information (the reviewer's cost concerns — summarizer call count, F067 embedding volume — are real, but the fix is bounding those seams directly, not starving capture; in dollars, lossless capture is ~$0.45/session worst case vs ~$0.06, and F067 embeddings are ~$0.0007).

| Setting | Old literal | **Ships as** | Rationale / review note |
|---------|------------|--------------|------------------------|
| `transcript_message_max_chars` | 500 | **8000** | **Sanity bound, not a fidelity ration** (lossless-capture revision; reviewer's 1000 superseded). ~2000 tokens/message — only paste-bombs hit it; real messages are captured whole. Downstream cost is now bounded at its own seams by the two new caps below. |
| `episode_lessons_max_chars` | 500 | **8000** | Sanity bound (same revision). No downstream LLM trigger; K-line learner (`procedure_learner.py:386`) and context engine handle any length. |
| `episode_summary_max_chunks` | *(new — unbounded today)* | **4** | **NEW downstream bound.** Chunked summarization (`_chunk_transcript` → one LLM call per 16000-char chunk) had no call-count cap. 4 chunks = 64K chars summarized ≈ $0.25/session ceiling on Sonnet. Selection is head+tail (first 3 + final chunk — consistent with F025 P3-B's first-title/last-outcome merge); dropped chunks are logged and the raw transcript column retains everything for re-derivation. 0 = unlimited. |
| `episode_chunk_max_per_episode` | *(new — unbounded today)* | **100** | **NEW downstream bound.** F067 `_chunk_and_store_transcript` had no chunk-count cap; 100 chunks ≈ 52K chars chunked ≈ $0.0003 embedding ceiling per episode. Tail beyond the cap stays raw in `episodes.transcript`. 0 = unlimited. |
| `episode_seed_summary_chars` | 200 | **500** | Longer dedup-probe embedding is *more specific* → the 0.85 threshold gets effectively stricter → fewer false-dedup suppressions. Summary stub later overwritten by the summarizer. |
| `episode_dedup_threshold` | 0.85 | **0.85 (keep)** | Admission gate — no evidence to move it. |
| `episode_dedup_window_hours` | 48 | **48 (keep)** | Same. |
| `episode_min_content_length` | 200 | **200 (keep)** | Lowering risks storing one-word exchanges as episodes that consume summarizer calls. |
| `correction_input_max_chars` | 1000 | **2000** | Reviewer counter (proposed 4000): the correction principle is almost always in the first 1–2 sentences; 4000 dilutes signal and doubles prompt cost for no gain. |
| `correction_max_tokens` | 512 | **1024** | F031 bug class (300→800 fixed 91% silent merge failures); max_tokens is a cap, not a spend. |
| `correction_min_principle_chars` | 30 | **20** | Admits terse genuine corrections ("Always use uv, not pip." = 24 chars); still blocks whitespace/noise. Downstream `fact_min_content_chars` floor applies separately. |
| `episode_summary_max_tokens` | auto (3000/1500) | **0 = auto (keep)** | Flat 3000 would double output budget for flag-off deployments; prod already gets 3000 via `extraction_coverage_broadened=true`. Field exists as operator override. |
| `knowledge_extractor_max_chars` | 12000 | **24000** | Fires once per compaction (rare); under-capture is permanent loss — asymmetric payoff. Per-message `[:2000]` unchanged, so growth = more messages covered, not longer ones. |
| `sleep_reflection_summary_chars` | 200 | **500** | Captures ~70% of a typical summary vs ~28% now; +~750 tokens total across 10 episodes — negligible. |
| `sleep_contradiction_fact_chars` | 500 | **1000** | Destructive SUPERSEDE/REMOVE/MERGE verdicts should see whole facts; matches the call's existing `max_tokens=1000`. |
| `fact_min_content_chars` | 30 | **30 (keep)** | Lowering without evidence risks one-word noise on every write path. |
| `fact_supersession_threshold` | 0.80 | **0.80 (keep)** | Raising pushes more pairs into F027 LLM disambiguation — cost, not safety. |
| `graph_link_candidate_window_days` | 30 | **60** | Reviewer counter (proposed 90): `evidence_for` precision is 0.70 (2026-06-13 audit); doubling coverage bounds candidate growth, final cosine gate unchanged. 90 can follow measurement. |

**Operator note (must land in CLAUDE.md, Task 9):** with lossless capture, per-session summarizer and F067 costs are governed by `episode_summary_max_chunks` and `episode_chunk_max_per_episode` — NOT by the capture caps. Operators tuning cost should touch those two; pinning `NOUS_TRANSCRIPT_MESSAGE_MAX_CHARS` down re-introduces permanent information loss and should be a last resort.

---

## File Structure

- `nous/config.py` — one new "Memory fidelity caps (2026-07-02)" block, 14 fields (Task 1)
- `nous/cognitive/layer.py` — wire settings #1–4 (Task 2)
- `nous/cognitive/monitor.py` — wire setting #5 (Task 3)
- `nous/handlers/episode_summarizer.py` — wire setting #6 (Task 4)
- `nous/handlers/knowledge_extractor.py` — wire setting #7 (Task 5)
- `nous/handlers/sleep_handler.py` — wire settings #8–9 (Task 6)
- `nous/heart/facts.py` — wire settings #10–11 (Task 7)
- `nous/brain/graph_linker.py`, `nous/handlers/decision_graph_linker.py` — wire setting #12 (Task 8)
- `tests/test_memory_fidelity_settings.py` — new; config defaults + env override (Task 1)
- Existing per-subsystem test files — one wiring test each (Tasks 2–8)
- `CLAUDE.md` — env-var table rows (Task 9)

---

### Task 1: Settings block

**Files:**
- Modify: `nous/config.py` (append to the Settings class, near the existing `transcript_max_chars` field at ~line 330 for locality)
- Create: `tests/test_memory_fidelity_settings.py`

**Interfaces:**
- Produces (consumed by Tasks 2–8): the following `Settings` fields, all read as `self._settings.<name>` / `settings.<name>` — defaults per the "Recommended defaults" table:
  - `transcript_message_max_chars: int = 8000`  (was literal 500; sanity bound)
  - `episode_lessons_max_chars: int = 8000`  (was 500; sanity bound)
  - `episode_summary_max_chunks: int = 4`  (NEW downstream bound; 0 = unlimited)
  - `episode_chunk_max_per_episode: int = 100`  (NEW downstream bound; 0 = unlimited)
  - `episode_seed_summary_chars: int = 500`  (was 200)
  - `episode_dedup_threshold: float = 0.85`  (keep)
  - `episode_dedup_window_hours: int = 48`  (keep)
  - `episode_min_content_length: int = 200`  (keep)
  - `correction_input_max_chars: int = 2000`  (was 1000)
  - `correction_max_tokens: int = 1024`  (was 512)
  - `correction_min_principle_chars: int = 20`  (was 30)
  - `episode_summary_max_tokens: int = 0`  (0 = auto: keep today's 3000-extended/1500-base logic)
  - `knowledge_extractor_max_chars: int = 24000`  (was 12000)
  - `sleep_reflection_summary_chars: int = 500`  (was 200)
  - `sleep_contradiction_fact_chars: int = 1000`  (was 500)
  - `fact_min_content_chars: int = 30`  (keep)
  - `fact_supersession_threshold: float = 0.80`  (keep)
  - `graph_link_candidate_window_days: int = 60`  (was 30; 0 = no time cutoff)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memory_fidelity_settings.py
"""2026-07-02 memory-fidelity scan: new Settings fields for previously hardcoded constants."""
import pytest

from nous.config import Settings


def _settings(**env):
    return Settings(_env_file=None, **env)


def test_fidelity_defaults_match_reviewed_values():
    """Defaults per the 2026-07-02 architecture-reviewed table (improved, not
    behavior-preserving, except the three destructive/admission gates)."""
    s = _settings()
    assert s.transcript_message_max_chars == 8000   # was literal 500 — sanity bound
    assert s.episode_lessons_max_chars == 8000      # was 500 — sanity bound
    assert s.episode_summary_max_chunks == 4        # NEW downstream bound (0=unlimited)
    assert s.episode_chunk_max_per_episode == 100   # NEW downstream bound (0=unlimited)
    assert s.episode_seed_summary_chars == 500      # was 200
    assert s.episode_dedup_threshold == 0.85        # gate — keep
    assert s.episode_dedup_window_hours == 48       # gate — keep
    assert s.episode_min_content_length == 200      # gate — keep
    assert s.correction_input_max_chars == 2000     # was 1000
    assert s.correction_max_tokens == 1024          # was 512 (F031 precedent)
    assert s.correction_min_principle_chars == 20   # was 30
    assert s.episode_summary_max_tokens == 0        # 0 = auto (3000 extended / 1500 base)
    assert s.knowledge_extractor_max_chars == 24000  # was 12000
    assert s.sleep_reflection_summary_chars == 500  # was 200
    assert s.sleep_contradiction_fact_chars == 1000  # was 500
    assert s.fact_min_content_chars == 30           # gate — keep
    assert s.fact_supersession_threshold == 0.80    # gate — keep
    assert s.graph_link_candidate_window_days == 60  # was 30


def test_fidelity_env_override(monkeypatch):
    monkeypatch.setenv("NOUS_TRANSCRIPT_MESSAGE_MAX_CHARS", "2000")
    monkeypatch.setenv("NOUS_FACT_SUPERSESSION_THRESHOLD", "0.9")
    monkeypatch.setenv("NOUS_GRAPH_LINK_CANDIDATE_WINDOW_DAYS", "0")
    s = _settings()
    assert s.transcript_message_max_chars == 2000
    assert s.fact_supersession_threshold == 0.9
    assert s.graph_link_candidate_window_days == 0


def test_fidelity_bounds_rejected():
    with pytest.raises(Exception):
        _settings(transcript_message_max_chars=0)   # ge=50
    with pytest.raises(Exception):
        _settings(fact_supersession_threshold=1.5)  # le=1.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_memory_fidelity_settings.py -v`
Expected: FAIL with `AttributeError` / pydantic `ValidationError` (fields don't exist).

- [ ] **Step 3: Add the fields to Settings**

In `nous/config.py`, directly below the existing `transcript_max_chars: int = 16000` field (~line 330):

```python
    # ── Memory fidelity caps (2026-07-02 non-configurable-constants scan) ──
    # Lossless-capture model: capture-side caps are SANITY BOUNDS (paste-bomb
    # protection), never fidelity rations — episodes.transcript keeps raw text
    # for re-derivation. Processing cost is bounded at its own seams:
    # episode_summary_max_chunks (LLM call count) and
    # episode_chunk_max_per_episode (F067 embedding volume). Destructive and
    # admission gates keep their prior literals.
    transcript_message_max_chars: int = Field(
        default=8000, ge=50,
        description="SANITY per-message bound when capturing User:/Assistant: lines into the episode transcript (layer.py capture seam — sole source for stored transcript, summary, facts, F067 chunks). Was hardcoded 500. Tune cost via episode_summary_max_chunks / episode_chunk_max_per_episode, not this.",
    )
    episode_lessons_max_chars: int = Field(
        default=8000, ge=50,
        description="SANITY bound on the end-of-session reflection stored as episodes.lessons_learned. Was hardcoded 500.",
    )
    episode_summary_max_chunks: int = Field(
        default=4, ge=0,
        description="Max transcript chunks (each <= transcript_max_chars) summarized per episode — bounds summarizer LLM call count. Selection is head+tail (first N-1 + final chunk); dropped chunks are logged and remain raw in episodes.transcript. 0 = unlimited (pre-2026-07-02 behavior).",
    )
    episode_chunk_max_per_episode: int = Field(
        default=100, ge=0,
        description="F067: max chunks embedded into heart.episode_chunks per episode — bounds embedding volume. Tail beyond the cap stays raw in episodes.transcript. 0 = unlimited (pre-2026-07-02 behavior).",
    )
    episode_seed_summary_chars: int = Field(
        default=500, ge=50,
        description="Chars of the first user message used as the episode's seed summary AND its dedup embedding probe. Was hardcoded 200.",
    )
    episode_dedup_threshold: float = Field(
        default=0.85, ge=0.0, le=1.0,
        description="Cosine threshold above which a new episode is treated as a duplicate and not created.",
    )
    episode_dedup_window_hours: int = Field(
        default=48, ge=1,
        description="Lookback window for episode-duplicate detection.",
    )
    episode_min_content_length: int = Field(
        default=200, ge=0,
        description="Min combined user+assistant chars for a single-turn no-tool session to keep its episode (below = soft-deleted as trivial).",
    )
    correction_input_max_chars: int = Field(
        default=2000, ge=100,
        description="F039: chars of the user message and AI response shown to the correction-extraction LLM. Was hardcoded 1000.",
    )
    correction_max_tokens: int = Field(
        default=1024, ge=256,
        description="F039: output budget for correction extraction. Raised from hardcoded 512 (F031 bug class: truncated JSON silently drops the correction).",
    )
    correction_min_principle_chars: int = Field(
        default=20, ge=0,
        description="F039: min length of an extracted principle before it is stored as a fact (below = silently dropped). Was hardcoded 30, which dropped terse corrections like 'Always use uv, not pip.' (24 chars).",
    )
    episode_summary_max_tokens: int = Field(
        default=0, ge=0,
        description="Override for the episode-summarization LLM max_tokens. 0 = auto (3000 when coverage/open-threads prompts are on, else 1500).",
    )
    knowledge_extractor_max_chars: int = Field(
        default=24000, ge=1000,
        description="Pre-compaction fact extraction: total chars of the doomed-message snapshot shown to the LLM (head-truncated). Was hardcoded 12000; fires once per compaction, under-capture is permanent loss.",
    )
    sleep_reflection_summary_chars: int = Field(
        default=500, ge=50,
        description="Per-episode summary chars fed to the sleep reflection LLM. Was hardcoded 200 (~28% of a typical summary).",
    )
    sleep_contradiction_fact_chars: int = Field(
        default=1000, ge=100,
        description="Per-fact chars shown to the contradiction-resolution LLM (verdicts are destructive: SUPERSEDE/REMOVE/MERGE). Was hardcoded 500; 1000 matches the call's max_tokens.",
    )
    fact_min_content_chars: int = Field(
        default=30, ge=0,
        description="F038-1.2 hard floor: facts shorter than this are rejected before dedup/admission on every write path.",
    )
    fact_supersession_threshold: float = Field(
        default=0.80, ge=0.0, le=1.0,
        description="Same-subject supersession cosine gate in _supersede_same_subject (deactivates the old fact). Sibling of fact_native_cosine_threshold.",
    )
    graph_link_candidate_window_days: int = Field(
        default=60, ge=0,
        description="Recency window for graph-link candidates (fact→decision evidence_for at learn time; decision→fact/episode at record time). Was hardcoded 30; 60 doubles coverage with bounded candidate growth (evidence_for precision 0.70, 2026-06-13 audit). 0 = no time cutoff.",
    )
```

(`Field` is already imported in `nous/config.py`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_memory_fidelity_settings.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add nous/config.py tests/test_memory_fidelity_settings.py
git commit -m "feat: add memory-fidelity Settings block for previously hardcoded constants (2026-07-02 scan)"
```

---

### Task 2: Wire cognitive/layer.py (transcript capture, lessons, episode seed + admission)

**Files:**
- Modify: `nous/cognitive/layer.py:49` (`_MIN_CONTENT_LENGTH`), `:462`, `:821`, `:825`, `:1287`, `:1743`/`:1864` (module-constant call sites), `:1779-1783`, `:1874`
- Test: `tests/test_cognitive_layer.py` (append; follow the file's existing fixture pattern for constructing `CognitiveLayer` — it already injects a settings object)

**Interfaces:**
- Consumes: Task 1 fields. `CognitiveLayer` already holds `self._settings` (used elsewhere in the file, e.g. the F083 budget logic) — no constructor change.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cognitive_layer.py` (adapt fixture names to the file's existing helpers — it already builds a layer with a real/injectable settings object):

```python
class TestMemoryFidelityCaps:
    """2026-07-02 scan: capture-time truncations honor Settings."""

    async def test_transcript_capture_honors_message_cap(self, layer_factory):
        layer = layer_factory(settings_overrides={"transcript_message_max_chars": 2000})
        long_input = "x" * 3000
        # pre_turn appends "User: <input[:cap]>"
        await layer.pre_turn(session_id="s1", agent_id="a", user_input=long_input)
        meta = layer._session_metadata["s1"]
        assert meta.transcript[-1] == f"User: {'x' * 2000}"

    async def test_lessons_cap_honored(self, layer_factory):
        layer = layer_factory(settings_overrides={"episode_lessons_max_chars": 1000})
        # end_session with a 1500-char reflection must pass a 1000-char lesson
        # to heart.end_episode — assert via the mocked heart call args.
        ...

    async def test_trivial_gate_uses_setting(self, layer_factory):
        layer = layer_factory(settings_overrides={"episode_min_content_length": 0})
        # With the gate at 0, a tiny single-turn session must NOT be soft-deleted.
        ...
```

(Write the `...` bodies out fully during implementation using the file's established mock-heart pattern; each asserts the mocked `Heart` method received the capped/uncapped value.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_cognitive_layer.py -k FidelityCaps -v`
Expected: FAIL (assertions see 500-char truncation / old gate).

- [ ] **Step 3: Implement the wiring**

```python
# layer.py:462  (pre_turn)
meta.transcript.append(f"User: {user_input[:self._settings.transcript_message_max_chars]}")

# layer.py:1287 (post_turn)
meta.transcript.append(
    f"Assistant: {turn_result.response_text[:self._settings.transcript_message_max_chars]}"
)

# layer.py:1874 (end_session)
lessons = [reflection[:self._settings.episode_lessons_max_chars]]

# layer.py:821/825 (episode creation) — one local for both uses
seed = user_input[:self._settings.episode_seed_summary_chars]
if await self._is_duplicate_episode(seed, session=session):
    ...
    summary=seed,

# layer.py:1779-1783 (_is_duplicate_episode)
#   hours=48        -> hours=self._settings.episode_dedup_window_hours
#   > 0.85          -> > self._settings.episode_dedup_threshold

# layer.py:49 — keep the module constant as documentation-free fallback removal:
# delete `_MIN_CONTENT_LENGTH = 200` and replace its two uses
# (:1743 significance gate, :1864 trivial-episode gate) with
# self._settings.episode_min_content_length
```

Grep for any remaining `_MIN_CONTENT_LENGTH` references (tests may import it) and update them.

- [ ] **Step 4: Run the full layer test file**

Run: `uv run pytest tests/test_cognitive_layer.py -v`
Expected: all pass (existing tests unaffected because defaults equal old literals).

- [ ] **Step 5: Commit**

```bash
git add nous/cognitive/layer.py tests/test_cognitive_layer.py
git commit -m "feat: wire transcript/lessons/episode-admission caps in cognitive layer to Settings"
```

---

### Task 3: Wire cognitive/monitor.py (F039 correction extraction)

**Files:**
- Modify: `nous/cognitive/monitor.py:328-329` (input caps), `:345` (max_tokens), `:353` (principle gate)
- Test: `tests/test_monitor.py` (append, following its existing F039 test pattern with a fake LLM client)

**Interfaces:**
- Consumes: `correction_input_max_chars`, `correction_max_tokens`, `correction_min_principle_chars`. `TurnMonitor` already holds `self._settings` (it reads `self._settings.background_model` at `:342`).

- [ ] **Step 1: Write the failing test** — assert (a) the prompt passed to the fake `call_background_llm` contains chars past position 1000 when `correction_input_max_chars=5000`; (b) `max_tokens` kwarg equals `settings.correction_max_tokens`; (c) a 25-char principle is stored when `correction_min_principle_chars=20` (and dropped at the default 30).

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_monitor.py -k correction -v` → FAIL.

- [ ] **Step 3: Implement**

```python
# monitor.py:326-329
cap = self._settings.correction_input_max_chars
prompt = (
    "The user corrected the AI in this exchange:\n\n"
    f"User: {user_message[:cap]}\n"
    f"AI response: {ai_response[:cap]}\n\n"
    ...
)

# monitor.py:345
max_tokens=self._settings.correction_max_tokens,

# monitor.py:353
if not principle or len(principle) < self._settings.correction_min_principle_chars:
    return None
```

**Defaults note:** all three values ship raised/lowered per the "Recommended defaults" table — input cap 1000→2000 (reviewer counter from 4000: the principle is almost always in the first 1–2 sentences), `max_tokens` 512→1024 (identical failure mode to F031, where `max_tokens=300` silently killed 91% of merges and the fix to 800 shipped as a default change in PR #526), principle floor 30→20 (admits terse corrections like "Always use uv, not pip." = 24 chars). Cost impact is negligible: this call only fires on detected corrections (rare).

- [ ] **Step 4: Run** — `uv run pytest tests/test_monitor.py -v` → all pass.

- [ ] **Step 5: Commit**

```bash
git add nous/cognitive/monitor.py tests/test_monitor.py
git commit -m "feat: make F039 correction-extraction caps configurable; raise max_tokens 512->1024 (F031 bug class)"
```

---

### Task 4: Wire episode_summarizer — max_tokens override + the two new downstream bounds

**Files:**
- Modify: `nous/handlers/episode_summarizer.py:577-586` (`_summary_max_tokens`), the chunk-summarization loop at `:532-543`, and `_chunk_and_store_transcript` at `:282-288`
- Test: `tests/test_episode_summarizer.py` (append)

**Interfaces:**
- Consumes: `episode_summary_max_tokens` (0 = auto), `episode_summary_max_chunks` (0 = unlimited), `episode_chunk_max_per_episode` (0 = unlimited).

- [ ] **Step 1: Write the failing test**

```python
def test_summary_max_tokens_override(summarizer_factory):
    s = summarizer_factory(settings_overrides={"episode_summary_max_tokens": 5000})
    assert s._summary_max_tokens() == 5000

def test_summary_max_tokens_auto_preserved(summarizer_factory):
    s = summarizer_factory(settings_overrides={"episode_summary_max_tokens": 0,
                                               "extraction_coverage_broadened": True})
    assert s._summary_max_tokens() == 3000
    s2 = summarizer_factory(settings_overrides={"episode_summary_max_tokens": 0,
                                                "extraction_coverage_broadened": False,
                                                "episode_open_threads": False})
    assert s2._summary_max_tokens() == 1500

def test_summary_chunk_cap_head_plus_tail(summarizer_factory):
    """6 chunks, cap 4 -> summarize chunks [0, 1, 2, 5] (first cap-1 + final)."""
    s = summarizer_factory(settings_overrides={"episode_summary_max_chunks": 4})
    chunks = [f"chunk-{i}" for i in range(6)]
    assert s._select_chunks(chunks) == ["chunk-0", "chunk-1", "chunk-2", "chunk-5"]

def test_summary_chunk_cap_zero_is_unlimited(summarizer_factory):
    s = summarizer_factory(settings_overrides={"episode_summary_max_chunks": 0})
    chunks = [f"chunk-{i}" for i in range(6)]
    assert s._select_chunks(chunks) == chunks

def test_summary_chunk_cap_noop_under_cap(summarizer_factory):
    s = summarizer_factory(settings_overrides={"episode_summary_max_chunks": 4})
    chunks = ["a", "b"]
    assert s._select_chunks(chunks) == chunks

async def test_f067_chunk_count_cap(summarizer_factory):
    """Transcript long enough for 10 chunks, cap 3 -> only 3 rows inserted."""
    s = summarizer_factory(settings_overrides={"episode_chunk_max_per_episode": 3,
                                               "episode_chunks_enabled": True})
    # drive _chunk_and_store_transcript with a fake embedder + captured INSERTs,
    # following the file's existing F067 test pattern; assert 3 chunks embedded
    # and the truncation WARN log fired.
    ...
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_episode_summarizer.py -k max_tokens -v` → FAIL.

- [ ] **Step 3: Implement**

```python
def _summary_max_tokens(self) -> int:
    """Return the max_tokens budget for a single summarization LLM call.

    F083 R5: the extended output schema (coverage facts or open_threads)
    needs more headroom so a long transcript's JSON doesn't truncate.
    2026-07-02: operator override via episode_summary_max_tokens (0 = auto).
    """
    override = getattr(self._settings, "episode_summary_max_tokens", 0)
    if override:
        return override
    if (getattr(self._settings, "extraction_coverage_broadened", False)
            or getattr(self._settings, "episode_open_threads", False)):
        return 3000
    return 1500

def _select_chunks(self, chunks: list[str]) -> list[str]:
    """Bound summarizer LLM call count per episode (2026-07-02 lossless-capture).

    Head+tail selection — first cap-1 chunks plus the FINAL chunk, matching
    the first-title/last-outcome strategy of _merge_summaries (F025 P3-B).
    Dropped chunks stay raw in episodes.transcript for re-derivation.
    """
    cap = self._settings.episode_summary_max_chunks
    if cap <= 0 or len(chunks) <= cap:
        return chunks
    logger.warning(
        "Episode transcript spans %d chunks; summarizing %d (first %d + final). "
        "Raw transcript is preserved; raise episode_summary_max_chunks to widen.",
        len(chunks), cap, cap - 1,
    )
    return chunks[: cap - 1] + [chunks[-1]]
```

In the chunk-summarization loop (`:532-543`), apply the selection once after `_chunk_transcript` returns:

```python
chunks = self._chunk_transcript(transcript, max_chars=max_chars)
chunks = self._select_chunks(chunks)
```

In `_chunk_and_store_transcript` (`:282-288`), cap the F067 chunk list right after `chunk_text`:

```python
chunk_cap = self._settings.episode_chunk_max_per_episode
if chunk_cap > 0 and len(chunks) > chunk_cap:
    logger.warning(
        "F067: episode %s produced %d chunks; embedding first %d "
        "(episode_chunk_max_per_episode). Tail remains in episodes.transcript.",
        episode_id, len(chunks), chunk_cap,
    )
    chunks = chunks[:chunk_cap]
```

(Chunk indices stay contiguous from 0, so the `(episode_id, chunk_index)` ON CONFLICT idempotency is unaffected.)

- [ ] **Step 4: Run** — `uv run pytest tests/test_episode_summarizer.py -v` → all pass.

- [ ] **Step 5: Commit** — `git commit -m "feat: episode summary max_tokens override + summarizer chunk-call cap + F067 per-episode chunk cap"`

---

### Task 5: Wire knowledge_extractor snapshot cap

**Files:**
- Modify: `nous/handlers/knowledge_extractor.py:203` (`[:12000]`) — note `:181` per-message `[:2000]` and tool-part `[:500]` stay hardcoded (they shape the snapshot's internal balance, not its total budget; revisit only if the probe in Task 9 shows per-message loss dominates).
- Test: `tests/test_knowledge_extractor.py` (append)

- [ ] **Step 1: Failing test** — with `knowledge_extractor_max_chars=100`, assert the text passed to the fake LLM is ≤ 100 chars; with a 20k-char conversation and the default, assert exactly 12000.
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement** — replace the literal slice at `:203` with `[:self._settings.knowledge_extractor_max_chars]`.
- [ ] **Step 4: Run** — pass.
- [ ] **Step 5: Commit** — `git commit -m "feat: configurable pre-compaction extraction snapshot budget"`

---

### Task 6: Wire sleep_handler reflection + contradiction input caps

**Files:**
- Modify: `nous/handlers/sleep_handler.py:709` (`ep.summary[:200]`), `:1026` (fact `[:500]` — locate the exact slice in `_resolve_contradictions`' prompt construction; there is a sibling in the F027 cluster-merge path, wire both if they share the constant)
- Test: `tests/test_sleep_handler.py` (append)

- [ ] **Step 1: Failing test** — build the reflection prompt with `sleep_reflection_summary_chars=400` and a 600-char episode summary; assert 400 chars survive. Same shape for the contradiction prompt with `sleep_contradiction_fact_chars=1000`.
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement** — `ep.summary[:self._settings.sleep_reflection_summary_chars]` and `fact.content[:self._settings.sleep_contradiction_fact_chars]` (both facts of the pair).
- [ ] **Step 4: Run** — pass. Also run `uv run pytest tests/ -k "sleep" -v` to catch the F035.6 audit tests.
- [ ] **Step 5: Commit** — `git commit -m "feat: configurable sleep reflection/contradiction LLM input caps"`

---

### Task 7: Wire facts.py admission floor + supersession threshold (+ rejection visibility)

**Files:**
- Modify: `nous/heart/facts.py:506` and the mirrored check at `:437`; `:945`
- Test: `tests/test_facts.py` or the existing F038 gate test file (append)

**Interfaces:**
- Consumes: `fact_min_content_chars`, `fact_supersession_threshold`. `FactsManager` already holds settings (it reads `fact_native_cosine_threshold`).

- [ ] **Step 1: Failing test** — (a) with `fact_min_content_chars=10`, a 15-char fact passes the floor (reaches dedup); with default 30 it returns `FactRejected`; (b) supersession triggers at 0.82 similarity with default threshold but not with `fact_supersession_threshold=0.90`.
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement**

```python
# facts.py:506 (and the :437 mirror)
min_chars = self._settings.fact_min_content_chars
if min_chars and len(input.content.strip()) < min_chars:
    logger.info(  # was silent; rejection visibility for the Task 9 probe
        "Fact rejected by min-content floor (%d < %d): %.60s",
        len(input.content.strip()), min_chars, input.content,
    )
    return FactRejected(
        content=input.content,
        composite_score=0.0,
        threshold=0.0,
        scores={},
        explanation=f"Content too short (< {min_chars} chars)",
    )

# facts.py:945
if similarity > self._settings.fact_supersession_threshold:
```

Keep the F027 disambiguation band's upper bound (`<= 0.95`) as-is — it is `fact_native_cosine_threshold`'s domain and already configurable.

- [ ] **Step 4: Run** — `uv run pytest tests/ -k "facts" -v` → pass.
- [ ] **Step 5: Commit** — `git commit -m "feat: configurable fact min-content floor + supersession threshold, log floor rejections"`

---

### Task 8: Wire graph-link candidate windows

**Files:**
- Modify: `nous/brain/graph_linker.py:194` (`cutoff = datetime.now(UTC) - timedelta(days=30)`), `nous/handlers/decision_graph_linker.py:87` (same pattern)
- Test: `tests/test_graph_linker.py` (append)

**Interfaces:**
- Consumes: `graph_link_candidate_window_days` (0 = no cutoff). `GraphLinker` holds `self.settings`; `decision_graph_linker` reads settings the same way — verify at implementation time and thread it if that handler builds its own cutoff without settings access.

- [ ] **Step 1: Failing test** — with `graph_link_candidate_window_days=0`, the candidate SQL must not contain a `created_at >=` clause (or the cutoff param must be far-past); with `=7`, cutoff is 7 days ago. Test via the generated SQL/params, matching the file's existing test style.
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement**

```python
window_days = self.settings.graph_link_candidate_window_days
if window_days > 0:
    cutoff = datetime.now(UTC) - timedelta(days=window_days)
else:
    cutoff = datetime.min.replace(tzinfo=UTC)  # no cutoff — keeps SQL shape identical
```

Same change at `decision_graph_linker.py:87`.

- [ ] **Step 4: Run** — pass.
- [ ] **Step 5: Commit** — `git commit -m "feat: configurable graph-link candidate recency window (0=unbounded)"`

---

### Task 9: Fidelity probe, docs, and recommended prod flips

**Files:**
- Modify: `CLAUDE.md` (env-var table), `.env.example` if present
- No prod changes in this task — it produces the *recommendation*, the operator flips.

- [ ] **Step 1: Full test suite**

Run: `uv run pytest tests/ -x -q`
Expected: green. Any existing test asserting an old literal (500-char transcript lines, 30-char principle floor, 30-day window) must be updated to read the setting — those diffs are the expected fallout of the default raises, not regressions.

- [ ] **Step 2: Before/after fidelity probe on the local instance**

Method (mirrors the F031 6/6 probe and the extraction-coverage A/B). **Arm A pins the OLD literals via env; arm B is the new defaults** — this validates the shipped defaults themselves:
1. `docker compose up -d` with arm-A env (old behavior — old literals, downstream caps disabled):
   ```
   NOUS_TRANSCRIPT_MESSAGE_MAX_CHARS=500
   NOUS_EPISODE_LESSONS_MAX_CHARS=500
   NOUS_EPISODE_SUMMARY_MAX_CHUNKS=0
   NOUS_EPISODE_CHUNK_MAX_PER_EPISODE=0
   NOUS_EPISODE_SEED_SUMMARY_CHARS=200
   NOUS_CORRECTION_INPUT_MAX_CHARS=1000
   NOUS_CORRECTION_MAX_TOKENS=512
   NOUS_KNOWLEDGE_EXTRACTOR_MAX_CHARS=12000
   NOUS_SLEEP_REFLECTION_SUMMARY_CHARS=200
   NOUS_SLEEP_CONTRADICTION_FACT_CHARS=500
   NOUS_GRAPH_LINK_CANDIDATE_WINDOW_DAYS=30
   ```
2. Drive 5 scripted sessions through `/chat` with long messages (>3000 chars each, containing seeded facts in the tail past char 500 — e.g. "the deploy key rotates on 2026-08-15" at char ~2500).
3. `POST /sleep/trigger`, then query: stored `heart.episodes.transcript` lengths, `lessons_learned` lengths, and whether tail-seeded facts appear in `heart.facts`.
4. Restart with NO fidelity env vars set (arm B = shipped defaults: lossless capture + downstream bounds). Repeat 2–3 on fresh sessions.
5. Success gate: tail-seeded facts recovered in arm B but not arm A; arm-B stored transcripts contain the full seeded messages (no mid-message cuts); **cost guard:** episode-summarizer LLM calls per session ≤ `episode_summary_max_chunks` (4) and `heart.episode_chunks` rows per episode ≤ `episode_chunk_max_per_episode` (100) — the caps, not capture starvation, are now the cost-control points; record both counts for the CLAUDE.md note.

- [ ] **Step 3: Update CLAUDE.md env-var table** — one row per new setting, copying the Field descriptions and old literals. MUST include the lossless-capture operator note: per-session summarizer/F067 cost is governed by `NOUS_EPISODE_SUMMARY_MAX_CHUNKS` and `NOUS_EPISODE_CHUNK_MAX_PER_EPISODE`; pinning `NOUS_TRANSCRIPT_MESSAGE_MAX_CHARS` down re-introduces permanent information loss and is a last resort.

- [ ] **Step 4: Record the rollback pins** (in the PR description):

```
# Any single regression rolls back with one env pin to the old behavior:
NOUS_TRANSCRIPT_MESSAGE_MAX_CHARS=500    # old capture literal
NOUS_EPISODE_LESSONS_MAX_CHARS=500       # old capture literal
NOUS_EPISODE_SUMMARY_MAX_CHUNKS=0        # old behavior = no summarizer call cap
NOUS_EPISODE_CHUNK_MAX_PER_EPISODE=0     # old behavior = no F067 chunk cap
NOUS_EPISODE_SEED_SUMMARY_CHARS=200
NOUS_CORRECTION_INPUT_MAX_CHARS=1000
NOUS_CORRECTION_MAX_TOKENS=512
NOUS_CORRECTION_MIN_PRINCIPLE_CHARS=30
NOUS_KNOWLEDGE_EXTRACTOR_MAX_CHARS=12000
NOUS_SLEEP_REFLECTION_SUMMARY_CHARS=200
NOUS_SLEEP_CONTRADICTION_FACT_CHARS=500
NOUS_GRAPH_LINK_CANDIDATE_WINDOW_DAYS=30
# Gates shipped unchanged (no rollback needed): episode_dedup_threshold /
# episode_dedup_window_hours / episode_min_content_length /
# fact_min_content_chars / fact_supersession_threshold / episode_summary_max_tokens
```

- [ ] **Step 5: Commit + PR**

```bash
git add CLAUDE.md
git commit -m "docs: env-var table for memory-fidelity settings + recommended prod flips"
# open PR per repo workflow (feature branch, codex review)
```

---

## Self-Review

- **Coverage vs scan:** all 7 high-impact confirmed findings are addressed (items 1, 5–12 in the triage table) or explicitly routed (retrieval-side → 2026-07-01 plan; two bugs → separate issues). The 64 medium findings are each either in the 12-constant scope or in a named not-in-plan bucket with a reason.
- **Placeholder scan:** Tasks 2, 3, 5, 6, 7, 8 describe tests in prose with exact assertions rather than full code blocks — acceptable because each names the exact file, the fixture pattern to copy, and the assertion; the implementer writes them against the file's established test helpers. Task 1 and 4 have complete code.
- **Type consistency:** every settings field consumed in Tasks 2–8 is defined in Task 1 with the same name and type; `episode_summary_max_tokens=0` sentinel semantics match between Task 1's description and Task 4's implementation.
- **Defaults consistency:** the "Recommended defaults" table, Task 1's Interfaces list, Task 1's Field definitions, and Task 1's default-assertion test all carry the same 18 values (2 sanity bounds at 8000, 2 new downstream caps, 8 raised derive-budgets/windows, 6 kept gates — verified line-by-line). The three destructive/admission gates and the `episode_summary_max_tokens` auto sentinel are unchanged in behavior. Every changed default has a one-env-var rollback pin listed in Task 9 Step 4 (the two new caps roll back to 0 = old unbounded behavior).
