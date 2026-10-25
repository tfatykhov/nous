/**
 * How the Retrieval view reads a leg's yield and an expansion's seed.
 *
 * Spreading activation and the 1-hop fallback are MECHANISMS: both append to
 * one list whose candidates carry entry_leg "graph_expanded", so their rows are
 * counted under that leg. Since the per-mechanism count landed, the mechanism
 * leg reports its own yield with `counted_in: "graph_expanded"`. Rows written
 * before it carry no `counted_in` and a stored n_returned of 0 that never meant
 * "found nothing", so their yield is derived here from the same retrieval.
 */
import type { RetrievalExpansion, RetrievalLeg } from './types/api';

/** Mechanism legs whose rows are counted under graph_expanded. */
export const MECHANISM_LEGS = ['spreading_activation', 'brain_graph'] as const;

/** The F091 stage spreading activation records its expansions under. */
export const SPREADING_STAGE = 'stage4_spreading_activation';

export interface LegYield {
  /** Rows the leg produced, or null when unknown. */
  n: number | null;
  /** The leg these rows are counted under, when it is not this one. */
  countedIn: string | null;
  /** True when `n` was derived from the rest of the record, not stored. */
  derived: boolean;
}

/**
 * The yield to display for one leg. A mechanism leg from before
 * `counted_in` existed gets its yield from the record:
 * - spreading activation records exactly one expansion per row it appended;
 * - the 1-hop fallback owns the rest of graph_expanded (no such leg = 0).
 */
export function legYield(
  leg: RetrievalLeg,
  legs: RetrievalLeg[],
  expansions: RetrievalExpansion[],
): LegYield {
  const isMechanism = (MECHANISM_LEGS as readonly string[]).includes(leg.name);
  if (!isMechanism || leg.counted_in) {
    return { n: leg.n_returned, countedIn: leg.counted_in ?? null, derived: false };
  }
  const spreadRows = expansions.filter((e) => e.stage === SPREADING_STAGE).length;
  if (leg.name === 'spreading_activation') {
    return { n: spreadRows, countedIn: 'graph_expanded', derived: true };
  }
  const expanded = legs.find((l) => l.name === 'graph_expanded');
  // The pipeline emits a graph_expanded leg only when it holds rows (true of
  // every record since F091 shipped), so no such leg means it held none.
  const nExpanded = expanded ? expanded.n_returned : 0;
  return { n: Math.max(0, nExpanded - spreadRows), countedIn: 'graph_expanded', derived: true };
}

/**
 * True when an expansion row has no attributable seed: the placeholder
 * `seed_type: "multi"` that spreading activation wrote, under its first seed,
 * before it reported the seed of each winning path.
 */
export function unattributedSeed(e: RetrievalExpansion): boolean {
  return e.seed_type === 'multi';
}
