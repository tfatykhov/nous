import { describe, it, expect } from 'vitest';
import { activate, ageFade, buildBrain, escapeHtml, frameFor, hoverLabel, mixHex, type BrainLink } from './brainGraph';
import type { GraphData } from './types/api';

const NOW = Date.parse('2026-11-12T12:00:00Z');
const edge = (id: string, source: string, target: string, relation = 'related_to') =>
  ({ id, source, target, source_type: 'fact', target_type: 'fact', relation, weight: 1, auto_linked: true, extraction_method: null });
const nodeOf = (id: string, created_at: string | null = null) => ({ id, type: 'fact', label: id, category: null, created_at });
const STATS = {
  total_edges: 0, total_nodes: 0, displayed_edges: 0, node_count: 0, largest_piece: 0, orphan_counts: {},
  chunks_included: false, edges_truncated: false,
};
const link = (id: string, source: string, target: string): BrainLink => ({ id, source, target, relation: 'related_to' });

describe('buildBrain', () => {
  it('counts each memory\'s drawn links, ages it in whole days, and leaves out an edge with a missing end', () => {
    const data: GraphData = {
      nodes: [nodeOf('a', '2026-11-12T00:00:00Z'), nodeOf('b', '2026-11-02T11:00:00Z'), nodeOf('c'), nodeOf('d', 'not a date')],
      edges: [edge('1', 'a', 'b'), edge('2', 'a', 'c'), edge('3', 'a', 'ghost')],
      stats: STATS,
    };
    const { nodes, links } = buildBrain(data, NOW);
    expect(links.map((l) => l.id)).toEqual(['1', '2']);
    expect(nodes.map((n) => [n.id, n.degree, n.ageDays])).toEqual([['a', 2, 0], ['b', 1, 10], ['c', 1, null], ['d', 0, null]]);
  });
  it('never gives a memory from the future a negative age', () => {
    const data: GraphData = { nodes: [nodeOf('a', '2026-12-01T00:00:00Z')], edges: [], stats: STATS };
    expect(buildBrain(data, NOW).nodes[0].ageDays).toBe(0);
  });
});

describe('activate', () => {
  // a - b - c - d, and a - e
  const links = [link('ab', 'a', 'b'), link('bc', 'b', 'c'), link('cd', 'c', 'd'), link('ae', 'a', 'e')];
  it('reaches the neighbours in one step and theirs in two, and no further', () => {
    const act = activate('a', links);
    expect([...act.hop1].sort()).toEqual(['b', 'e']);
    expect([...act.hop2]).toEqual(['c']);
    expect(act.first.map((l) => l.id).sort()).toEqual(['ab', 'ae']);
    expect(act.second.map((l) => l.id)).toEqual(['bc']);
    expect(act.capped).toBe(false);
  });
  it('follows a link in either direction, and reads ends the library has swapped for node objects', () => {
    const swapped: BrainLink[] = [{ id: 'ab', source: { id: 'a' } as any, target: { id: 'b' } as any, relation: 'related_to' }];
    expect([...activate('b', swapped).hop1]).toEqual(['a']);
  });
  it('stops the second step at the cap and says so', () => {
    const star = [link('ah', 'a', 'h'), ...Array.from({ length: 10 }, (_, i) => link(`h${i}`, 'h', `x${i}`))];
    const act = activate('a', star, 4);
    expect(act.hop2.size).toBe(4);
    expect(act.second).toHaveLength(4);
    expect(act.capped).toBe(true);
  });
  it('a memory with no drawn link activates nothing; a self-link is not a neighbour', () => {
    expect(activate('z', links).hop1.size).toBe(0);
    expect(activate('a', [link('aa', 'a', 'a')]).hop1.size).toBe(0);
  });
});

describe('hover labels', () => {
  it('escape every character HTML reads', () => {
    expect(escapeHtml(`<a href="x" onclick='y'>&`)).toBe('&lt;a href=&quot;x&quot; onclick=&#39;y&#39;&gt;&amp;');
  });
  it('name the type, cut long text, and fall back to the type alone', () => {
    expect(hoverLabel({ type: 'fact', label: 'short' })).toBe('fact: short');
    expect(hoverLabel({ type: 'fact', label: 'x'.repeat(200) })).toBe(`fact: ${'x'.repeat(79)}…`);
    expect(hoverLabel({ type: 'episode', label: '' })).toBe('episode');
    expect(hoverLabel({ type: 'fact', label: '<script>' })).toBe('fact: &lt;script&gt;');
  });
});

describe('colour and framing sums', () => {
  it('a memory fades with age to half at most; an undated one sits between', () => {
    expect([ageFade(0), ageFade(30), ageFade(60), ageFade(900), ageFade(null)]).toEqual([0, 0.25, 0.5, 0.5, 0.25]);
  });
  it('mixes two six-digit colours, and leaves anything else alone', () => {
    const black = '#' + '0'.repeat(6);
    const white = '#' + 'f'.repeat(6);
    expect(mixHex(black, white, 0)).toBe(black);
    expect(mixHex(black, white, 1)).toBe(white);
    expect(mixHex(black, white, 0.5)).toBe('#' + '80'.repeat(3));
    expect(mixHex('var(--x)', white, 0.5)).toBe('var(--x)');
    expect(mixHex(black, '', 0.5)).toBe(black);
  });
  it('frames the mass by its height on a wide screen and by its width on a narrow one', () => {
    const box = { x: [-100, 100] as [number, number], y: [-50, 50] as [number, number], z: [-10, 10] as [number, number] };
    const wide = frameFor(box, 90, 4); // tan(45°) = 1
    expect(wide.center).toEqual({ x: 0, y: 0, z: 0 });
    expect(wide.distance).toBeCloseTo(50 * 0.92 + 10);
    expect(frameFor(box, 90, 0.5).distance).toBeCloseTo(200 * 0.92 + 10);
  });
});
