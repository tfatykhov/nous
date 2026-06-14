# LLM calls & prompt-caching audit — prod (`nous-default`), 2026-06-14

Read-only audit of how Nous assembles LLM calls and whether context is grouped
to preserve the Anthropic prompt cache. Two halves: **(A) measure prod cache
performance** from `nous_system.context_log`, **(B) trace the prompt-assembly +
`cache_control` placement** for stable-before-volatile discipline.

## Verdict

**The cache discipline is fundamentally sound and prod is healthy (88.2% hit
rate).** One real, quantified inefficiency: **frame-scoped tool schemas bust the
entire cached prefix on every frame change** (~10% of all cache-creation tokens,
avoidable). Everything else — tier grouping, block ordering, within-turn reuse —
is correct.

## A. Prod measurement (`context_log`, 2026-05-14 → 06-14, 857 calls w/ token data)

| metric | value |
|---|---|
| overall hit rate (`cache_read / total_input`) | **88.2%** |
| cache-creation share | 11.8% |
| non-cached input share | ~0.0% (1,292 tok total — just user-message text) |
| by type: `subtask` | n=489, 89.6% hit |
| by type: `chat` | n=368, 86.1% hit |

Within-turn tool loops cache **excellently** (87–98% per call after the turn's
first call). The cache prefix is *not* being broken mid-turn by volatile content —
the grouping works.

## B. Assembly & `cache_control` placement (correct)

System blocks, in order (`runner.py:707-786`):
0. Claude Code preamble — always cached
1. **static** identity — always cached
1b. **semi_stable** — cached via single-breakpoint strategy (only when its hash
    matches the previous turn)
2. **dynamic** — never cached
…then `messages[]`, with `cache_control` on the last user message.

Tier classification (`context.py:32-42`, `SECTION_TIERS`):
- **static**: Identity, Context Safety, Procedure Awareness, Procedure Catalog
- **semi_stable**: User Profile, Active Censors, Current Frame
- **dynamic** (default): Date/Time, Working Memory, Relevant Facts, Related
  Decisions, Procedures, Recent Conversations, Past Episodes, Epistemic Routing,
  Cached Results — plus runner extras (frame instructions, ledger, corrections,
  diagnostic nudges, telegram format).

All volatile, per-turn content is correctly in `dynamic` (uncached). Stable
content is in `static`. Ordering within a tier is deterministic
(`sorted(sections, key=priority)`, `context.py:1005`). Block order is
stable→semi→volatile→messages. **No volatile leak into the cached prefix.**

## C. The one real finding: frame-scoped tools bust the whole prefix

**Root cause.** `FRAME_TOOLS` (`runner.py:92-100`) gives each frame a different
tool array: conversation=27, question=19, decision=14, creative=8, debug=24,
task=all. Tools sit at the **front of the cacheable prefix** (before the system
blocks). When the frame changes between turns, the tools array changes →
everything downstream (tools + all system blocks) busts → the turn's first call
is a **full re-creation** (`cache_read=0`, full `cache_creation`).

In interactive chat the frame changes almost every turn
(question→task→conversation→decision), so most turns pay a full prefix rewrite on
their first call.

**Evidence.** Of 32 full-prefix re-creations that had a prior in-session call:
- 13 were >5 min apart → legitimate 5-min cache TTL expiry (not a bug)
- **19 were ≤5 min (cache still warm) → real busts; 17 of 19 coincide with a
  frame change**

**Quantified cost.** ~**423,865 cache-creation tokens (10.1% of all
cache-creation)** over the one-month window are frame-change busts —
re-created at 1.25× base that could have been 0.1× cache reads.

**Note:** F036's "tool schema cache" (`tools.py:120-144`) is only **process-side
Python memoization** (avoids rebuilding the schema list). It does **nothing** for
the Anthropic-side prefix cache; the cross-frame API bust is unaddressed.

**Two same-frame warm busts** (`0e1e11a6` t12, t14) are intra-turn (same turn
number) re-creations — likely mid-loop message rewrite (SmartCompress/pruning),
1.4% of cc; not pursued.

## Fix options (frame-scoped tools)

1. **Stable tool superset every turn** — stop frame-filtering the API `tools`
   array; keep `FRAME_TOOLS` only for the *textual* frame instructions.
   Eliminates the bust. Cost: slightly larger tool schema per call, but those
   tokens become 0.1× cached reads → net large win. Behavioral change: the model
   can call any tool in any frame (today `task` frame already does this, and
   frame *instructions* already steer tool use), so read-only frames lose their
   hard tool gate.
2. **`cache_control` breakpoint inside the tools array** — order tools as
   [stable common subset][frame tail], breakpoint after the common subset. More
   surgical but only partially works: system blocks are downstream of the
   variable tail, so they still re-cache.
3. **Leave as-is** — accept the 10% creation overhead; within-turn caching
   already keeps the aggregate at 88%.

**Recommendation: Option 1.** Biggest win, simplest, and the only real cost is
removing per-frame tool gating — a soft guardrail that frame *instructions*
already duplicate. It's a behavioral change (read-only frames gain action tools),
so it's the user's call.
