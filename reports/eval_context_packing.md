# End-to-end context packing eval — 5 scenarios

- judge: `claude-sonnet-4-6`
- top_k: 10
- sufficiency: **0/5 (0%)**

## Per-scenario

| name | sufficient | n_results | reason |
|---|---|---:|---|
| f042_finding | FAIL | 30 | The assembled context contains information about cross-encoder reranking (F042) being integrated, high-value, and enabled, but does not mention the corpus-dependent nature of its performance — specifically that it helps on Nous-shape data but regresses on LongMemEval. That key finding is absent. |
| f058_reason | FAIL | 25 | The assembled context contains no mention of Brier score 0.252 at random baseline or ~20% systemic overconfidence on prod decisions, which are the specific facts needed to answer why confidence was calibrated. |
| ce_recommendation | FAIL | 29 | The assembled context confirms cross-encoder reranking (F042) is enabled on Tim's live Nous instance, but contains no information about the +4% MRR measurement or the corpus-dependent finding that would justify a production recommendation. |
| calibration_factor | FAIL | 27 | The assembled context contains no mention of a calibration factor of 0.7627 or any reference to 401 reviewed production decisions. |
| edge_audit | FAIL | 30 | The assembled context contains no information about the F022 edge audit findings, specifically nothing about 0.70 precision on informed_by/related_to edges or empty content being the dominant cause. The only F022-related fact mentions a graph edge breakdown prior to an F022 fix, but does not describe the audit findings requested. |