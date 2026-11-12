import { describe, it, expect } from 'vitest';
import { graphSliceText } from './graphSlice';
import type { GraphStats } from './types/api';

const stats = (over: Partial<GraphStats> = {}): GraphStats => ({
  total_edges: 66536, total_nodes: 24238, displayed_edges: 5033, node_count: 1500, largest_piece: 1470,
  orphan_counts: {}, chunks_included: false, edges_truncated: false, ...over,
});

describe('graphSliceText', () => {
  it('says how much of the graph is drawn, how it was chosen, and that chunks are left out', () => {
    expect(graphSliceText(stats())).toBe(
      '1,500 of 24,238 linked memories and 5,033 of 66,536 links, grown outward from the newest along their ' +
      'strongest links. Chunks are left out.',
    );
  });
  it('drops the chunks sentence when chunks are drawn, and says when the link cap was reached', () => {
    expect(graphSliceText(stats({ chunks_included: true }))).not.toContain('Chunks');
    expect(graphSliceText(stats({ edges_truncated: true }))).toContain(' Only the strongest links among them are drawn.');
  });
  it('says "every linked memory" when the whole graph fits, with one link in the singular', () => {
    expect(graphSliceText(stats({ total_nodes: 2, node_count: 2, total_edges: 1, displayed_edges: 1, chunks_included: true })))
      .toBe('Every linked memory: 2, with 1 link.');
  });
  it('falls back to a plain description with nothing drawn', () => {
    expect(graphSliceText(stats({ total_edges: 0, total_nodes: 0, displayed_edges: 0, node_count: 0 })))
      .toBe('Memories and the links between them.');
  });
});
