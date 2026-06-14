# F053 edge-precision audit

_Generated: 2026-06-13 23:59:51 UTC_
_since: (all edges)_  
_sample-limit-per-type: 30_  
_gate threshold (precision >= 0.75 per type)_

| relation | n | YES | WEAK | NO | PARSE_ERROR | precision | gate |
|----------|---|-----|------|----|-------------|-----------|------|
| co_occurred | 30 | 18 | 4 | 8 | 0 | 0.60 | FAIL |
| discussed_in | 2 | 1 | 1 | 0 | 0 | 0.50 | UNDERPOWERED |
| evidence_for | 30 | 23 | 4 | 3 | 0 | 0.77 | PASS |
| extracted_from | 30 | 13 | 14 | 3 | 0 | 0.43 | FAIL |
| happened_before | 8 | 5 | 1 | 2 | 0 | 0.62 | UNDERPOWERED |
| informed_by | 26 | 22 | 2 | 2 | 0 | 0.85 | PASS |
| related_to | 18 | 16 | 2 | 0 | 0 | 0.89 | PASS |
| supersedes | 30 | 28 | 1 | 1 | 0 | 0.93 | PASS |

**Overall gate**: FAIL (all gate-eligible relations meet precision >= 0.75)

## Sample of NO + WEAK verdicts (for spot-check)

- **discussed_in** [WEAK] 0b767cca-2a66-4013-b203-23e9b25e5090 -> 505cdc19-edcb-4d38-b395-9cae66cbf932: The fact describes quantitative recall_deep results about 'context pruning' while the episode only records the instruction to run recall_deep, with no actual results shown, making the link indirect ra
- **evidence_for** [NO] a783b25f-5073-42dd-a1d7-c85a5c5bbe6b -> 570c6d0a-22ab-4531-9332-2504c80c7200: The fact describes F023 in live enforcement mode with threshold 0.60, while the target decision describes it in shadow mode with threshold 0.55 — the fact explicitly supersedes and contradicts the dec
- **evidence_for** [NO] e3f3c746-e740-4c64-89b2-53a2967c9129 -> 60bae0f1-74b6-4fc0-a4bd-1fa671b10dec: The May 7 decision sweep covers recent pending decisions with no substantive overlap with the April 7 sweep's specific resolved items (Docker auth, F054 deployment, stress-test jobs), making these two
- **evidence_for** [WEAK] c189e947-b5e5-4231-81b6-d729b3d83df2 -> e7bee491-b4f7-4a1c-811c-104e5ff4c632: The fact shows a self-eval score trajectory ending at 7.5, while the decision captures an earlier self-eval at 7.0 with identified weaknesses — related as the same metric over time but the fact doesn'
- **evidence_for** [WEAK] f2ca4815-b870-40d1-89f8-c76fb3abebd1 -> d09c22cb-5933-4734-a6f1-d45459443b93: Both concern Tim's arXiv agent-memory paper collection (venue audit vs. fact-check audit), making them related research-verification activities on the same repository, but the fact's venue audit does 
- **evidence_for** [NO] 1a94627c-aeb2-4017-b942-ff5018913fea -> 2e6017b5-54d6-447b-96f8-ea97cd3fc5d8: The May 9 decision sweep records stable carry-forward items with no connection to the April 3 sweep's specific resolved tasks (procedure bugs, F014 reviews, awesome-agent-memory fixes), making these u
- **evidence_for** [WEAK] fe934301-1cbd-4ac4-830c-5be123df1f1d -> f49fa17b-8130-45c5-8900-b6ce221c4a80: Both facts describe weekly AI layoff reports sent to Tim, making them related instances of the same recurring task, but the specific companies and dates differ enough that one does not directly eviden
- **evidence_for** [WEAK] 68a41635-b871-48b5-b813-272f8505548c -> 851e4099-850d-4cbf-a3a9-f5f00ae60a92: Both decisions are decision sweep records triaging pending items (May 8 and April 3 evening respectively), making them related as instances of the same process, but their specific resolved items are e
- **happened_before** [NO] c189e947-b5e5-4231-81b6-d729b3d83df2 -> a74945f7-0216-482b-a35c-ab80fd68414b: A self-evaluation score trajectory (May 20) and a research document about decentralized multi-host agent orchestration (May 23) share only a rough date proximity and have no meaningful semantic or cau
- **happened_before** [WEAK] be266c5c-9fb9-4e89-9d70-213aa7a0f462 -> 224053db-84a1-4688-918c-f80628810c30: Both are procedural lessons about tool-use and workflow efficiency identified around the same period, but they address distinct problems (versioned-artifact iteration vs. tool selection) with no direc
