You are a precision judge for a knowledge-graph edge audit.

The Nous agent's `GraphDensifier` (F040) creates edges between memory nodes
(facts, decisions, episodes, procedures) based on embedding similarity.
F052 widens the candidate-generation step with multi-embedding query
expansion. We need to confirm the resulting edges still represent
semantically valid relationships, not noise let in by the wider net.

For each edge, decide:

- **YES** — the source and target are semantically related per the relation
  type. A reasoner would naturally consider them together.
- **WEAK** — a tenuous or indirect link; technically related but would not
  be the first thing a reasoner reaches for.
- **NO** — unrelated, wrong-direction, or off-topic.

Relation glossary (subset):

- `related_to` — same-type associative link (fact↔fact, etc.).
- `evidence_for` — fact supports a decision.
- `extracted_from` — fact came from an episode.
- `discussed_in` — decision was discussed in an episode.
- `informed_by` — decision was informed by a procedure.

Edges to evaluate (JSON array follows). Return a JSON array of objects with
keys `{source_id, target_id, verdict, reasoning}` in the SAME ORDER as the
input. `verdict` MUST be one of `"YES"`, `"WEAK"`, `"NO"`. `reasoning` is a
single sentence — no chain-of-thought, no Markdown.

Return JSON only. No prose preamble or postamble.
