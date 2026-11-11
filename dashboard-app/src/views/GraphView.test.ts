import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, cleanup, waitFor } from '@testing-library/svelte';
import { tick } from 'svelte';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import GraphView from './GraphView.svelte';
import { setLook } from '../lib/look';
import { currentRoute } from '../lib/router';
import { DARK_SEL, PAPER_SEL, parseBlocks, styleBlocks } from '../lib/testing/css';

// The Graph on the paper look: node, edge, ring and legend colours are tokens. Cytoscape draws on a canvas, so its
// colours are resolved against the graph's own element; a look switch restyles in place and never re-runs the layout
// (which is random: re-running it would move every node). Every value below is made up.
const BLOCKS = parseBlocks(readFileSync(join(__dirname, '..', 'app.css'), 'utf-8'));
const DARK = BLOCKS.get(DARK_SEL) ?? {};
const PAPER = BLOCKS.get(PAPER_SEL) ?? {};

const HOSTILE = '<img src=x onerror="alert(1)">';
const LONG = 'L'.repeat(300);
const GRAPH = {
  nodes: [
    { id: 'n-fact', type: 'fact', label: 'a fact', category: 'general' },
    { id: 'n-episode', type: 'episode', label: HOSTILE, category: null },
    { id: 'n-decision', type: 'decision', label: LONG, category: null },
    { id: 'n-procedure', type: 'procedure', label: 'a procedure', category: null },
    { id: 'n-chunk', type: 'chunk', label: 'a chunk', category: null },
  ],
  edges: [
    { id: 'e1', source: 'n-fact', target: 'n-episode', source_type: 'fact', target_type: 'episode', relation: 'supports', weight: 0.8, auto_linked: true, extraction_method: 'heuristic' },
    { id: 'e2', source: 'n-decision', target: 'n-fact', source_type: 'decision', target_type: 'fact', relation: 'informed_by', weight: 0.5, auto_linked: false, extraction_method: null },
    { id: 'e3', source: 'n-chunk', target: 'n-procedure', source_type: 'chunk', target_type: 'procedure', relation: 'part_of', weight: 0.4, auto_linked: true, extraction_method: 'deterministic' },
    { id: 'e4', source: 'n-fact', target: 'n-chunk', source_type: 'fact', target_type: 'chunk', relation: 'contradicts', weight: 0.3, auto_linked: true, extraction_method: 'inferred' },
  ],
  stats: {
    total_edges: 4, displayed_edges: 4, node_count: 5, orphan_counts: { facts: 1 }, all_links: true,
    recent_edges: 4, linked_edges: 0, links_per_memory: 0, recent_from: null, recent_to: null,
  },
};
const DETAIL = {
  found: true,
  node: { id: 'n-episode', type: 'episode', content: HOSTILE, category: null, created_at: null },
  connection_count: 2,
  connections_truncated: false,
  connections: [
    { edge_id: 'e1', neighbor_id: 'n-fact', neighbor_type: 'fact', neighbor_label: 'a fact', neighbor_active: true, relation: 'supports', direction: 'in', weight: 0.8, extraction_method: 'heuristic', auto_linked: true },
    { edge_id: 'e9', neighbor_id: 'n-decision', neighbor_type: 'decision', neighbor_label: LONG, neighbor_active: true, relation: 'supports', direction: 'out', weight: 0.2, extraction_method: null, auto_linked: false },
  ],
};

type Ele = { data: Record<string, any>; classes: Set<string> };

/** A recording stand-in for cytoscape: it keeps the elements, and counts style() and layout() calls. */
function fakeCytoscape() {
  let elements: Ele[] = [];
  const handlers: Record<string, (evt: any) => void> = {};
  const runs = vi.fn();
  const cy = {
    on: vi.fn((ev: string, _sel: string, fn: (evt: any) => void) => { handlers[ev] = fn; }),
    destroy: vi.fn(),
    json: vi.fn(({ elements: els }: { elements: any[] }) => { elements = els.map((e) => ({ data: { ...e.data }, classes: new Set() })); }),
    layout: vi.fn(() => ({ run: runs })),
    style: vi.fn(),
    batch: (fn: () => void) => fn(),
    nodes: () => wrapAll(elements.filter((e) => e.data.source == null)),
    edges: () => wrapAll(elements.filter((e) => e.data.source != null)),
    elements: () => wrapAll(elements),
    get current() { return elements; },
  };
  function wrap(e: Ele): any {
    const w: any = {
      id: () => e.data.id,
      data: (k: string, v?: unknown) => { if (v === undefined) return e.data[k]; e.data[k] = v; return w; },
      style: () => w,
      addClass: (c: string) => { c.split(' ').forEach((x) => e.classes.add(x)); return w; },
      removeClass: (c: string) => { c.split(' ').forEach((x) => e.classes.delete(x)); return w; },
      source: () => wrap(elements.find((x) => x.data.id === e.data.source)!),
      target: () => wrap(elements.find((x) => x.data.id === e.data.target)!),
    };
    return w;
  }
  function wrapAll(list: Ele[]) {
    return { forEach: (fn: (w: any) => void) => list.forEach((e) => fn(wrap(e))), removeClass: (c: string) => list.forEach((e) => wrap(e).removeClass(c)) };
  }
  const factory = vi.fn((opts: { elements: any[]; style: any[] }) => {
    elements = opts.elements.map((e) => ({ data: { ...e.data }, classes: new Set() }));
    return cy;
  });
  return { factory, cy, handlers, tap: (id: string) => handlers.tap({ target: wrap(elements.find((e) => e.data.id === id)!) }) };
}

let cyto: ReturnType<typeof fakeCytoscape>;
function install(detail: unknown = DETAIL) {
  cyto = fakeCytoscape();
  vi.stubGlobal('cytoscape', cyto.factory);
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    const body = url.startsWith('/dashboard/graph/node/') ? detail : url.startsWith('/dashboard/graph') ? GRAPH : null;
    if (!body) return { ok: false, status: 404, json: async () => ({}) } as Response;
    return { ok: true, status: 200, json: async () => body } as Response;
  }));
}

/** A paper <main> with the paper ground's tokens set on it (jsdom has no stylesheet), as App renders it. */
function paperMain(): HTMLElement {
  const main = document.createElement('main');
  main.setAttribute('data-ground', 'paper');
  for (const [k, v] of Object.entries(PAPER)) if (k.startsWith('--')) main.style.setProperty(k, v);
  document.body.appendChild(main);
  return main;
}

const nodeColor = (id: string) => cyto.cy.current.find((e) => e.data.id === id)?.data.color;
const edgeColor = (id: string) => cyto.cy.current.find((e) => e.data.id === id)?.data.color;
/** The style array cytoscape got last: the value of `prop` under `selector`. */
function styleValue(style: any[], selector: string, prop: string) {
  return style.find((s) => s.selector === selector)?.style[prop];
}

beforeEach(() => { currentRoute.set('graph'); });
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  setLook('dark');
  document.body.innerHTML = '';
});

describe('Graph on the paper look', () => {
  it('the legend names the five types in words, each with a dot in its --gv token', async () => {
    install();
    const { container } = render(GraphView);
    await screen.findByLabelText('Search graph nodes');
    const labels = Array.from(container.querySelectorAll('.type-check'));
    expect(labels.map((l) => l.textContent?.trim())).toEqual(['Fact', 'Episode', 'Decision', 'Procedure', 'Chunk']);
    expect(labels.map((l) => l.querySelector('.type-dot')?.getAttribute('style'))).toEqual(
      ['fact', 'episode', 'decision', 'procedure', 'chunk'].map((t) => `background: var(--gv-${t});`),
    );
  });

  it('builds cytoscape with the dark colours under the dark look, and runs the layout once', async () => {
    install();
    render(GraphView);
    await waitFor(() => expect(cyto.factory).toHaveBeenCalledTimes(1));
    await tick();
    const created = cyto.factory.mock.calls[0][0];
    const color = (id: string) => created.elements.find((e: any) => e.data.id === id).data.color;
    expect(['n-fact', 'n-episode', 'n-decision', 'n-procedure', 'n-chunk'].map(color)).toEqual(
      ['#34d399', '#60a5fa', '#a78bfa', '#fbbf24', '#06b6d4'],
    );
    expect(['e1', 'e2', 'e3', 'e4'].map(color)).toEqual(['#34d399', '#a78bfa', '#22d3ee', '#f87171']);
    // Today's cytoscape style, value for value.
    expect(styleValue(created.style, 'node', 'border-color')).toBe(DARK['--gv-node-border']);
    expect(styleValue(created.style, 'node', 'color')).toBe('#8e8eab');
    expect(styleValue(created.style, 'node', 'text-outline-color')).toBe('#0a0a0f');
    expect(styleValue(created.style, 'node:selected', 'border-color')).toBe('#fbbf24');
    expect(styleValue(created.style, 'node.searchHit', 'border-color')).toBe('#fbbf24');
    expect(cyto.cy.layout).toHaveBeenCalledTimes(1);
    expect(cyto.cy.style).not.toHaveBeenCalled();
  });

  it('a look switch restyles nodes, edges and rings once, without re-running the layout', async () => {
    install();
    render(GraphView, { target: paperMain() });
    await waitFor(() => expect(cyto.factory).toHaveBeenCalledTimes(1));
    await tick();
    expect(nodeColor('n-fact')).toBe(PAPER['--gv-fact']); // the paper ground's tokens are on <main> in this test
    setLook('paper');
    await tick();
    expect(cyto.cy.style).toHaveBeenCalledTimes(1);
    expect(cyto.cy.layout).toHaveBeenCalledTimes(1);
    const style = cyto.cy.style.mock.calls[0][0];
    // Rings and labels stay visible on the white card.
    expect(styleValue(style, 'node:selected', 'border-color')).toBe('#14202b');
    expect(styleValue(style, 'node.searchHit', 'border-color')).toBe('#14202b');
    expect(styleValue(style, 'node', 'color')).toBe(PAPER['--muted']);
    expect(styleValue(style, 'node', 'text-outline-color')).toBe('#ffffff');
    expect(edgeColor('e4')).toBe(PAPER['--rel-contradicts']);
    expect(nodeColor('n-chunk')).toBe(PAPER['--gv-chunk']);
  });

  it('the node sheet: hostile and long labels render as text; connections say "to" and "from" in words', async () => {
    install();
    render(GraphView);
    await waitFor(() => expect(cyto.factory).toHaveBeenCalledTimes(1));
    cyto.tap('n-episode');
    const sheet = await screen.findByLabelText('Episode Detail');
    await waitFor(() => expect(sheet.querySelectorAll('button.conn-item')).toHaveLength(2));
    expect(sheet.getAttribute('data-ground')).toBe('paper'); // the route's ground, though portalled
    expect(sheet.textContent).toContain(HOSTILE);
    expect(sheet.querySelector('img')).toBeNull();
    expect(sheet.textContent).toContain(LONG);
    const dirs = Array.from(sheet.querySelectorAll('.conn-dir'));
    expect(dirs.map((d) => d.textContent?.trim())).toEqual(['from', 'to']);
    expect(dirs.map((d) => d.getAttribute('title'))).toEqual(['from the neighbour to this node', 'from this node to the neighbour']);
    expect(sheet.textContent).not.toMatch(/[→←]/);
    // The badge carries its type's colour as --type; the stylesheet draws the tint and the text from it.
    const badge = sheet.querySelector('.type-badge')!;
    expect(badge.getAttribute('style')).toBe('--type: var(--gv-episode);');
    expect(sheet.querySelector('.rel-dot')?.getAttribute('style')).toContain('var(--rel-supports)');
  });

  it('the type badge: the colour on its 12.5% tint under dark; on paper the text mixed 70% with the ink, 4.5:1 or more (review I3)', () => {
    const css = styleBlocks(readFileSync(join(__dirname, 'GraphView.svelte'), 'utf-8'));
    expect(css).toMatch(/\.type-badge \{[^}]*background: color-mix\(in srgb, var\(--type\) 12\.5%, transparent\);[^}]*color: var\(--type\);/);
    expect(css).toMatch(/\[data-ground="paper"\]\) \.type-badge \{\s*color: color-mix\(in srgb, var\(--type\) 70%, var\(--text\)\);/);
    // On paper the plain node colours are 3:1 marks (fact 3.06:1 as 11px text on its tint); mixed, every type passes.
    const rgb = (h: string) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
    const lum = (c: number[]) => {
      const [r, g, b] = c.map((v) => { const s = v / 255; return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4; });
      return 0.2126 * r + 0.7152 * g + 0.0722 * b;
    };
    const ratio = (a: number[], b: number[]) => { const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p); return (x + 0.05) / (y + 0.05); };
    const ink = rgb(PAPER['--text']);
    for (const t of ['fact', 'episode', 'decision', 'procedure', 'chunk']) {
      const c = rgb(PAPER[`--gv-${t}`]);
      const text = c.map((v, i) => 0.7 * v + 0.3 * ink[i]);
      const tintBg = c.map((v) => 0.125 * v + 0.875 * 255); // the sheet is white on paper
      expect(ratio(text, tintBg), t).toBeGreaterThanOrEqual(4.5);
    }
  });

  it('the sources hold no colour literal and no {@html', () => {
    for (const f of ['GraphView.svelte', '../lib/viz/Graph.svelte']) {
      const src = readFileSync(join(__dirname, f), 'utf-8');
      expect(src, f).not.toMatch(/#[0-9a-fA-F]{3,8}\b|rgba?\(/);
      expect(src, f).not.toContain('{@html');
    }
  });

  it('at phone width the header wraps instead of widening the page', () => {
    const css = styleBlocks(readFileSync(join(__dirname, 'GraphView.svelte'), 'utf-8'));
    expect(css).toMatch(/\.view-head \{[^}]*flex-wrap: wrap;/);
  });
});
