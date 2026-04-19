# Recent Changes

**Date Range:** April 5 – April 17, 2026  
**Commits Covered:** ~30 commits from main branch

---

## Cross-Encoder Reranking & Graph Intelligence (F042, F043, F045)

- `be29768` feat(F042): cross-encoder reranking stage in recall_deep (#312)
- `751b16e` Merge pull request #311 from tfatykhov/feature/F042-cross-encoder-reranking
- `128461a` F042: Cross-Encoder Reranking Stage spec
- `ef3ac60` feat(F043): cross-encoder reranking for sleep-cycle graph backfill (#314)
- `9f14360` fix(F042): install [rerank] extra in Dockerfile + persistent HF cache volume (#313)
- `6d3e66c` feat(F045): CE-aware cosine thresholds + content-length guard (#315)
- `4ca4e4f` docs(F043): link PR #314 in shipped table
- `c96b000` docs(F045): link PR #315 in shipped table

## Graph Densification (F040)

- `091d8e4` Merge pull request #307 from tfatykhov/feat/f040-graph-densification
- `326c63d` feat(F040): switch backfill to hybrid search (vector + keyword via RRF)
- `f02811b` feat(F040): add density dashboard frontend tab
- `78e7148` feat(F040): add graph density dashboard query and REST endpoint
- `bc1e6c6` feat(F040): wire graph densifier and reverse linkers in main.py
- `22780d5` feat(F040): add DecisionGraphLinker reverse-linking handler
- `b6b68aa` feat(F040): add ProcedureGraphLinker handler
- `6fef206` feat(F040): emit procedure_stored event in ProcedureManager._store()
- `6dd8ffa` feat(F040): add semantic episode↔episode linking to EpisodeSummarizer
- `a65053c` feat(F040): add graph densification phase to sleep handler
- `d7cd537` feat(F040): implement GraphDensifier orphan backfill engine
- `b37ec15` feat(F040): add edge_confidence scoring and create_edge helper
- `856df84` feat(F040): add graph densification config settings
- `f7a98ae` docs(F040): update CLAUDE.md and feature index for graph densification
- `a084909` fix(F040): fix review P1s — backfill return dict, bus emission for decision/procedure events
- `67ec57a` fix(F040): update sleep handler tests for graph_densification phase count

## Bug Fixes & Stability

- `764f890` fix(config+tests): remove redundant validation_alias and fix 6 SQLite test failures (#318)
- `b24b3af` fix(tools): surface entity IDs in recall_deep + broaden anti-hallucination prompt (#317)
- `b9aec93` fix: increase subtask_max_timeout to 900s and add env var overrides
- `dd6e297` fix(streaming): keep /chat/stream alive during long pre_turn, compaction, and tool runs
- `0b75947` Merge pull request #309 from tfatykhov/fix/sse-keepalive-long-tool-runs

## Features & Enhancements

- `fb4fff3` feat: F038 Unified DAG Orchestration with Dashboard (#289)
- `92df4a8` fix: make ActionGate duplicate detection smarter (#285)
- `ffb87a7` feat: F027 supersession detection and principled forgetting (#287)
- `7131550` feat: F034 Phase 5 followup — expand sqlite patches, remove integration gates (#276)
- `8cdeac9` feat: F037 utility-boosted procedure retrieval (#278)
- `847cd8e` feat: add on_complete callback to dynamic heartbeat checks (#273)
- `bcbf21d` feat: F038 memory quality & context loading fixes (#258)
- `675e5e0` feat(dashboard): mobile UI overhaul — responsive layout, typography, accessibility
- `1ee2a21` feat(f036): Prompt Cache Optimization — 3-tier system prompt split (#253)
- `ab50e18` feat(f034.5): Dynamic Heartbeat Checks (#252)

## Documentation & Specs

- `62e5fd0` F014: Frame Reasoning Scaffolds spec (v4)
- `b8f8f5c` Merge pull request #310 from tfatykhov/spec/F041-snn-sleep-densification
- `8fab9c4` F041: Update spec with fact-checked analysis of actual replay_12pct_stc.h5
- `fbbae25` spec(F041): SNN Sleep Densification — tinyHippo-driven graph augmentation
- `77a3df2` F038: Unified DAG Orchestration feature spec (#286)
- `97da5db` docs: F027 spec review and corrections (v3) (#279)
- `f61c662` spec: F026.1 Action Gate Enhancements — change-aware duplicate detection

## Infrastructure & Testing

- `3400403` feat: install GitHub CLI in Docker image
- `189c1eb` feat: add claude-runner non-root user to Dockerfile (#299)
- `9c47f03` test: add comprehensive unit tests for cognitive frames module
- `22c412b` test: add comprehensive unit tests for guardrails module
- `8d2f3da` Add tests for action_gate and execution_ledger (#261)
- `f199db8` fix: update 132 CI test failures to match current production code
- `d71a136` fix: set NOUS_TEST_DB=postgres in CI and fix tests.* imports

---

**Summary:** Major focus on cross-encoder reranking improvements (F042-F045), comprehensive graph densification system (F040), DAG orchestration, and significant test coverage improvements. Multiple bug fixes for streaming, configuration, and test stability.
