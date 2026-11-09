import { describe, it, expect } from 'vitest';
import { dateSpan, graphSliceText } from './graphSlice';
import type { GraphStats } from './types/api';

const stats = (over: Partial<GraphStats> = {}): GraphStats => ({
  total_edges: 134838, displayed_edges: 1640, node_count: 901, orphan_counts: {},
  recent_edges: 666, linked_edges: 974, links_per_memory: 3,
  recent_from: '2026-11-08T03:10:00+00:00', recent_to: '2026-11-09T16:25:17+00:00', ...over,
});

describe('dateSpan', () => {
  it('names one day, a span in one month, and a span across months (UTC)', () => {
    expect(dateSpan('2026-11-09T01:00:00+00:00', '2026-11-09T23:00:00+00:00')).toBe('Nov 9');
    expect(dateSpan('2026-11-07T02:44:52+00:00', '2026-11-09T16:25:17+00:00')).toBe('Nov 7–9');
    expect(dateSpan('2026-10-30T10:00:00+00:00', '2026-11-02T10:00:00+00:00')).toBe('Oct 30 – Nov 2');
  });
  it('gives nothing for a missing or unreadable date', () => {
    expect(dateSpan(null, '2026-11-09T00:00:00+00:00')).toBe('');
    expect(dateSpan('nope', '2026-11-09T00:00:00+00:00')).toBe('');
  });
});

describe('graphSliceText', () => {
  it('says the drawing is the newest links plus their best-linked neighbours, and how much is shown', () => {
    expect(graphSliceText(stats())).toBe(
      'The newest 666 links (Nov 8–9), plus up to 3 links from each of their memories to its best-linked ' +
      'neighbours. 1,640 of 134,838 links shown.',
    );
  });
  it('says "all" when every link is drawn', () => {
    expect(graphSliceText(stats({ total_edges: 12, displayed_edges: 12, recent_edges: 12, linked_edges: 0 })))
      .toBe('All 12 links between memories.');
  });
  it('drops the neighbour clause when no linked edge was added', () => {
    expect(graphSliceText(stats({ displayed_edges: 10, recent_edges: 10, linked_edges: 0 })))
      .toBe('The newest 10 links (Nov 8–9). 10 of 134,838 links shown.');
  });
  it('falls back to a plain description with nothing drawn', () => {
    expect(graphSliceText(stats({ total_edges: 0, displayed_edges: 0, recent_edges: 0, linked_edges: 0 })))
      .toBe('Memory nodes and the links between them.');
  });
});
