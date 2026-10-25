import { describe, it, expect } from 'vitest';
import { legYield, unattributedSeed, SPREADING_STAGE } from './retrievalLegs';
import type { RetrievalExpansion, RetrievalLeg } from './types/api';

const leg = (name: string, n: number, extra: Partial<RetrievalLeg> = {}): RetrievalLeg => ({
  name, attempted: true, n_returned: n, n_deduped: 0, n_dropped: 0,
  score_min: null, score_max: null, error: null, skip_reason: null, ...extra,
});

const exp = (stage: string, seed_type = 'decision', hop = 1): RetrievalExpansion => ({
  seed_id: 's', seed_type, seed_score: null, neighbor_id: crypto.randomUUID(),
  neighbor_type: 'fact', stage, hop, edge_relation: 'spreading_activation',
  edge_weight: 0.25, extraction_method: null, path_strength: 0.25, won_best_path: true,
});

describe('legYield', () => {
  it('shows a mechanism leg its own yield and where it is counted', () => {
    const sa = leg('spreading_activation', 15, { counted_in: 'graph_expanded' });
    expect(legYield(sa, [sa], [])).toEqual({ n: 15, countedIn: 'graph_expanded', derived: false });
  });

  it('keeps a stored 0 from a mechanism that reports one (spreading fell back)', () => {
    const sa = leg('spreading_activation', 0, { counted_in: 'graph_expanded' });
    expect(legYield(sa, [sa], []).n).toBe(0);
  });

  it('derives an old spreading record from its expansions, not the stored 0', () => {
    // retrieval cc79bcc3: 11 hop-1 + 4 hop-2 spreading rows, leg stored as 0.
    const sa = leg('spreading_activation', 0);
    const legs = [leg('graph_expanded', 15), sa];
    const rows = [
      ...Array.from({ length: 11 }, () => exp(SPREADING_STAGE, 'multi', 1)),
      ...Array.from({ length: 4 }, () => exp(SPREADING_STAGE, 'multi', 2)),
      exp('stage2b_heart_graph_memory', 'episode', 1),
    ];
    expect(legYield(sa, legs, rows)).toEqual({ n: 15, countedIn: 'graph_expanded', derived: true });
  });

  it('gives an old 1-hop fallback record the graph_expanded count', () => {
    const bg = leg('brain_graph', 0);
    const legs = [leg('graph_expanded', 7), bg];
    expect(legYield(bg, legs, [exp('stage4_decision_1hop')]).n).toBe(7);
  });

  it('gives an old 1-hop marker 0 when spreading supplied graph_expanded', () => {
    const bg = leg('brain_graph', 0);
    expect(legYield(bg, [leg('graph_expanded', 3), bg], [exp(SPREADING_STAGE, 'multi')]).n).toBe(0);
  });

  it('leaves every other leg as stored, 0 included', () => {
    const hg = leg('heart_graph', 0);
    expect(legYield(hg, [hg], [])).toEqual({ n: 0, countedIn: null, derived: false });
  });
});

describe('unattributedSeed', () => {
  it('flags only the old multi-seed placeholder', () => {
    expect(unattributedSeed(exp(SPREADING_STAGE, 'multi', 2))).toBe(true);
    expect(unattributedSeed(exp(SPREADING_STAGE, 'decision', 2))).toBe(false);
  });
});
