import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, cleanup, waitFor, fireEvent } from '@testing-library/svelte';
import { tick } from 'svelte';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import GraphView from './GraphView.svelte';
import { currentRoute } from '../lib/router';
import { ROOT_SEL, block, parseBlocks, styleBlocks } from '../lib/testing/css';

// The Graph: node, link and legend colours are tokens. The 3D library draws on a WebGL canvas, so its colours are
// resolved against the graph's own element; the layout runs once per fetch (it is a simulation: re-running it would
// move every memory). The library is a recording stand-in here, and every value below is made up.
const BLOCKS = parseBlocks(readFileSync(join(__dirname, '..', 'app.css'), 'utf-8'));
const PAPER = block(BLOCKS, ROOT_SEL, 50);

const HOSTILE = '<img src=x onerror="alert(1)">';
const LONG = 'L'.repeat(300);
const NOW = new Date().toISOString();
const GRAPH = {
  nodes: [
    { id: 'n-fact', type: 'fact', label: 'a fact', category: 'general', created_at: NOW },
    { id: 'n-episode', type: 'episode', label: HOSTILE, category: null, created_at: NOW },
    { id: 'n-decision', type: 'decision', label: LONG, category: null, created_at: NOW },
    { id: 'n-procedure', type: 'procedure', label: 'a procedure', category: null, created_at: null },
    { id: 'n-chunk', type: 'chunk', label: 'a chunk', category: null, created_at: NOW },
  ],
  edges: [
    { id: 'e1', source: 'n-fact', target: 'n-episode', source_type: 'fact', target_type: 'episode', relation: 'supports', weight: 0.8, auto_linked: true, extraction_method: 'heuristic' },
    { id: 'e2', source: 'n-decision', target: 'n-fact', source_type: 'decision', target_type: 'fact', relation: 'informed_by', weight: 0.5, auto_linked: false, extraction_method: null },
    { id: 'e3', source: 'n-chunk', target: 'n-procedure', source_type: 'chunk', target_type: 'procedure', relation: 'part_of', weight: 0.4, auto_linked: true, extraction_method: 'deterministic' },
    { id: 'e4', source: 'n-fact', target: 'n-chunk', source_type: 'fact', target_type: 'chunk', relation: 'contradicts', weight: 0.3, auto_linked: true, extraction_method: 'inferred' },
  ],
  stats: {
    total_edges: 40, total_nodes: 50, displayed_edges: 4, node_count: 5, largest_piece: 4, orphan_counts: { facts: 1 },
    chunks_included: false, edges_truncated: false,
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

/** What the stand-in for 3d-force-graph recorded: every setter's arguments by name, and the data it was given. */
const lib = vi.hoisted(() => ({
  made: 0,
  fail: false,
  calls: {} as Record<string, any[][]>,
  data: { nodes: [] as any[], links: [] as any[] },
  controls: { autoRotate: false, autoRotateSpeed: 0, addEventListener: () => {} } as Record<string, any>,
  scene: { fog: null as any },
}));

vi.mock('three', () => ({
  Fog: class { color: string; near: number; far: number; constructor(c: string, n: number, f: number) { this.color = c; this.near = n; this.far = f; } },
}));
vi.mock('3d-force-graph', () => ({
  default: class {
    constructor() {
      if (lib.fail) throw new Error('no WebGL');
      lib.made += 1;
      const force = { strength: () => force, distance: () => force };
      const special: Record<string, (...a: any[]) => any> = {
        d3Force: (_name: string, fn?: unknown) => (fn === undefined ? force : proxy),
        renderer: () => ({ setPixelRatio: () => {} }),
        scene: () => lib.scene,
        controls: () => lib.controls,
        camera: () => ({ fov: 50, aspect: 1, position: { length: () => 1000 } }),
        getGraphBbox: () => ({ x: [-100, 100], y: [-50, 50], z: [-80, 80] }),
        graphData: (d?: { nodes: any[]; links: any[] }) => {
          if (d === undefined) return lib.data;
          // As the library does: the link ends become the node objects.
          const byId = new Map(d.nodes.map((n) => [n.id, n]));
          lib.data = { nodes: d.nodes, links: d.links.map((l) => Object.assign(l, { source: byId.get(l.source), target: byId.get(l.target) })) };
          return proxy;
        },
      };
      const proxy: any = new Proxy({}, {
        get: (_t, name: string) => (...args: any[]) => {
          (lib.calls[name] ??= []).push(args);
          return special[name] ? special[name](...args) : proxy;
        },
      });
      return proxy;
    }
  },
}));

/** The accessor the graph was last given for `name` (nodeColor, linkColor, nodeLabel, onNodeClick…). */
const last = (name: string) => lib.calls[name]?.at(-1)?.[0];
const node = (id: string) => lib.data.nodes.find((n) => n.id === id);
const link = (id: string) => lib.data.links.find((l) => l.id === id);
const loads = () => (lib.calls.graphData ?? []).filter((a) => a.length > 0).length;

let fetched: string[] = [];
function install(detail: unknown = DETAIL) {
  fetched = [];
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    fetched.push(url);
    const body = url.startsWith('/dashboard/graph/node/') ? detail : url.startsWith('/dashboard/graph') ? GRAPH : null;
    if (!body) return { ok: false, status: 404, json: async () => ({}) } as Response;
    return { ok: true, status: 200, json: async () => structuredClone(body) } as Response; // a new object per response, as parsing gives
  }));
}

/** A paper <main> with the paper ground's tokens set on it (jsdom has no stylesheet), as App renders it. */
function paperMain(): HTMLElement {
  const main = document.createElement('main');
  main.classList.add('page-ground');
  for (const [k, v] of Object.entries(PAPER)) if (k.startsWith('--')) main.style.setProperty(k, v);
  document.body.appendChild(main);
  return main;
}

beforeEach(() => {
  currentRoute.set('graph');
  Object.assign(lib, { made: 0, fail: false, calls: {}, data: { nodes: [], links: [] }, scene: { fog: null } });
  lib.controls.autoRotate = false;
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  document.body.innerHTML = '';
});

describe('Graph on the paper look', () => {
  it('the legend names the five types in words, each with a dot in its --gv token; chunks start unticked', async () => {
    install();
    const { container } = render(GraphView);
    await screen.findByLabelText('Search graph nodes');
    const labels = Array.from(container.querySelectorAll('.type-filters .type-check'));
    expect(labels.map((l) => l.textContent?.trim())).toEqual(['Fact', 'Episode', 'Decision', 'Procedure', 'Chunk']);
    expect(labels.map((l) => l.querySelector('.type-dot')?.getAttribute('style'))).toEqual(
      ['fact', 'episode', 'decision', 'procedure', 'chunk'].map((t) => `background: var(--gv-${t});`),
    );
    expect(labels.map((l) => (l.querySelector('input') as HTMLInputElement).checked)).toEqual([true, true, true, true, false]);
  });

  it('with no stylesheet (jsdom) the colours fall back to the :root values, and the layout runs once', async () => {
    install();
    render(GraphView);
    await waitFor(() => expect(loads()).toBe(1));
    expect(lib.made).toBe(1);
    // A memory made today keeps its type's colour; one with no date is a quarter of the way to the card's surface.
    expect(['n-fact', 'n-episode', 'n-decision', 'n-chunk'].map((id) => last('nodeColor')(node(id)))).toEqual(
      ['--gv-fact', '--gv-episode', '--gv-decision', '--gv-chunk'].map((t) => PAPER[t]),
    );
    expect(last('nodeColor')(node('n-procedure'))).not.toBe(PAPER['--gv-procedure']);
    expect(last('backgroundColor')).toBe(PAPER['--surface']);
    expect(lib.scene.fog.color).toBe(PAPER['--surface']); // distance fades into the card
    expect(last('linkColor')(link('e1'))).toBe(PAPER['--gv-other']); // every fibre is one quiet ink at rest
  });

  it('on the page ground the nodes take its tokens; search and the type boxes repaint, they never lay out again', async () => {
    install();
    render(GraphView, { target: paperMain() });
    await waitFor(() => expect(loads()).toBe(1));
    expect(last('nodeColor')(node('n-fact'))).toBe(PAPER['--gv-fact']);

    await fireEvent.input(screen.getByLabelText('Search graph nodes'), { target: { value: 'a fact' } });
    await tick();
    expect(last('nodeColor')(node('n-fact'))).toBe(PAPER['--gv-fact']); // the match keeps its colour
    expect(last('nodeColor')(node('n-episode'))).not.toBe(PAPER['--gv-episode']); // the rest fade

    // A refetch keeps the search: the new drawing's matches are found again.
    await fireEvent.click(screen.getByRole('button', { name: '600' }));
    await waitFor(() => expect(loads()).toBe(2));
    expect(last('nodeColor')(node('n-fact'))).toBe(PAPER['--gv-fact']);
    expect(last('nodeColor')(node('n-episode'))).not.toBe(PAPER['--gv-episode']);

    await fireEvent.click(screen.getByLabelText(/Episode/));
    await tick();
    expect(last('nodeVisibility')(node('n-episode'))).toBe(false);
    expect(last('nodeVisibility')(node('n-fact'))).toBe(true);
    expect(last('linkVisibility')(link('e1'))).toBe(false); // a link to a hidden memory is hidden too
    expect(last('linkVisibility')(link('e2'))).toBe(true);
    // Before the layout the library hands a link over with its ends still ids: it is judged the same way.
    expect(last('linkVisibility')({ id: 'e2', source: 'n-decision', target: 'n-fact', relation: 'informed_by' })).toBe(true);
    expect(last('linkVisibility')({ id: 'e1', source: 'n-fact', target: 'n-episode', relation: 'supports' })).toBe(false);
    expect(last('linkVisibility')({ id: 'ex', source: 'n-fact', target: 'gone', relation: 'supports' })).toBe(false);
    expect(loads()).toBe(2);
    expect(lib.made).toBe(1);
  });

  it('a tapped memory lights the links its activation crosses in their relation colours, and opens its sheet', async () => {
    install();
    render(GraphView);
    await waitFor(() => expect(loads()).toBe(1));
    last('onNodeClick')(node('n-episode'));
    expect(last('linkColor')(link('e1'))).toBe(PAPER['--rel-supports']); // one step: episode to fact
    expect(last('linkColor')(link('e2'))).toBe(PAPER['--rel-informed-by']); // two steps: fact to decision
    expect(last('linkColor')(link('e3'))).not.toBe(PAPER['--rel-part-of']); // three steps away: not crossed
    expect(last('nodeColor')(node('n-fact'))).toBe(PAPER['--gv-fact']);
    expect(lib.calls.cameraPosition.at(-1)![1]).toBe(node('n-episode')); // the camera moves in on it
    await screen.findByLabelText('Episode detail');
    // The sheet covers the graph, so no activation is sent yet; it crosses the links once the sheet is closed.
    await new Promise((r) => setTimeout(r, 1000));
    expect(lib.calls.emitParticle).toBeUndefined();
    await fireEvent.keyDown(document.activeElement ?? document.body, { key: 'Escape' });
    await waitFor(() => expect((lib.calls.emitParticle ?? []).map((a) => a[0].id)).toEqual(['e1']));
    await waitFor(() => expect((lib.calls.emitParticle ?? []).map((a) => a[0].id)).toEqual(['e1', 'e2']), { timeout: 2000 });

    last('onBackgroundClick')();
    expect(last('linkColor')(link('e1'))).toBe(PAPER['--gv-other']);
  });

  it('a hover label is written as HTML by the library: hostile memory text reaches it escaped, long text cut', async () => {
    install();
    render(GraphView);
    await waitFor(() => expect(loads()).toBe(1));
    const hostile = last('nodeLabel')(node('n-episode'));
    expect(hostile).not.toMatch(/[<>"]/);
    expect(hostile).toContain('&lt;img');
    expect(last('nodeLabel')(node('n-decision')).length).toBeLessThan(100);
  });

  it('the endpoint chooses the memories: the size and the chunks box refetch, with their parameters', async () => {
    install();
    render(GraphView);
    await waitFor(() => expect(loads()).toBe(1));
    expect(fetched).toEqual(['/dashboard/graph?limit=1500']);
    expect(screen.getByRole('button', { name: '1,500' }).getAttribute('aria-pressed')).toBe('true');

    await fireEvent.click(screen.getByRole('button', { name: '600' }));
    await waitFor(() => expect(fetched.at(-1)).toBe('/dashboard/graph?limit=600'));
    await fireEvent.click(screen.getByLabelText(/Chunk/));
    await waitFor(() => expect(fetched.at(-1)).toBe('/dashboard/graph?limit=600&chunks=1'));
    await waitFor(() => expect(loads()).toBe(3)); // each fetch lays the graph out once
    expect(lib.made).toBe(1);
  });

  it('"Turn slowly" drives the slow turn, and the header says what part of the graph is drawn', async () => {
    install();
    render(GraphView);
    await waitFor(() => expect(loads()).toBe(1));
    await waitFor(() => expect(lib.controls.autoRotate).toBe(true));
    // It holds still under a mouse pointer, so a memory can be read and tapped; a touch never parks it.
    const brain = screen.getByRole('application');
    await fireEvent.pointerEnter(brain, { pointerType: 'mouse' });
    await waitFor(() => expect(lib.controls.autoRotate).toBe(false));
    await fireEvent.pointerLeave(brain);
    await waitFor(() => expect(lib.controls.autoRotate).toBe(true));
    await fireEvent.click(screen.getByLabelText(/Turn slowly/));
    await waitFor(() => expect(lib.controls.autoRotate).toBe(false));
    expect(document.body.textContent).toContain('5 of 50 linked memories and 4 of 40 links');
    expect(document.body.textContent).toContain('Chunks are left out.');
    expect(document.body.textContent).toContain('80%'); // four of the five drawn memories are one piece
  });

  it('without WebGL the screen says so in words, and keeps its controls', async () => {
    install();
    lib.fail = true;
    render(GraphView);
    await screen.findByText('The 3D graph could not start in this browser. It needs WebGL.');
    expect(screen.getByLabelText('Search graph nodes')).toBeTruthy();
  });

  it('the node sheet: hostile and long labels render as text; connections say "to" and "from" in words', async () => {
    install();
    render(GraphView);
    await waitFor(() => expect(loads()).toBe(1));
    last('onNodeClick')(node('n-episode'));
    const sheet = await screen.findByLabelText('Episode detail');
    await waitFor(() => expect(sheet.querySelectorAll('button.conn-item')).toHaveLength(2));
    expect(sheet.classList.contains('page-ground')).toBe(true); // the page ground, though portalled
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

  it('the type badge: the colour\'s 12.5% tint; the text the colour mixed 70% with the ink, 4.5:1 or more (review I3)', () => {
    const css = styleBlocks(readFileSync(join(__dirname, 'GraphView.svelte'), 'utf-8'));
    expect(css).toMatch(/\.type-badge \{[^}]*background: color-mix\(in srgb, var\(--type\) 12\.5%, transparent\);[^}]*color: color-mix\(in srgb, var\(--type\) 70%, var\(--text\)\);/);
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
    for (const f of ['GraphView.svelte', '../lib/viz/BrainGraph.svelte', '../lib/brainGraph.ts']) {
      const src = readFileSync(join(__dirname, f), 'utf-8');
      expect(src, f).not.toMatch(/#[0-9a-fA-F]{3,8}\b|rgba?\(/);
      expect(src, f).not.toContain('{@html');
    }
  });

  it('source contract (jsdom cannot lay out; the 390 px overflow check in the screenshot round is the proof): the header rule has flex-wrap: wrap', () => {
    const css = styleBlocks(readFileSync(join(__dirname, 'GraphView.svelte'), 'utf-8'));
    expect(css).toMatch(/\.view-head \{[^}]*flex-wrap: wrap;/);
  });
});
