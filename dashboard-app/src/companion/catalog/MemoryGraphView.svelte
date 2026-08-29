<script lang="ts">
  // nous-core MemoryGraph — hand-rolled SVG radial graph (the companion
  // entry ships no vendor JS; cytoscape/d3 are dashboard-only script tags).
  // Focus node at the center, everything else on rings. Tapping a node
  // calls the agent-side expandGraphNode function over POST /a2ui/call and
  // merges the returned neighborhood into the surface's LOCAL data model —
  // exploration state is per-client by design.
  import { store } from '../store.svelte';
  import { transport } from '../transport';
  import { resolveDynamic } from '../functions';
  import type { Scope } from '../pointer';
  import type { A2uiComponent } from '../store.svelte';

  interface GNode {
    id: string;
    type?: string;
    label?: string;
  }
  interface GEdge {
    source: string;
    target: string;
    relation?: string;
    weight?: number;
  }

  let {
    surfaceId,
    comp,
    scope = null,
  }: {
    surfaceId: string;
    comp: A2uiComponent;
    scope?: Scope | null;
    depth?: number;
    ancestors?: readonly string[];
  } = $props();

  const W = 440;
  const H = 360;

  let busy = $state(false);
  let error = $state('');
  let selected = $state('');
  const expanded = new Set<string>();

  const ctx = $derived({ dataModel: store.surfaces[surfaceId]?.dataModel ?? {}, scope });
  const nodes = $derived.by(() => {
    const raw = resolveDynamic(comp.nodes, ctx);
    if (!Array.isArray(raw)) return [];
    // Dedupe on the render key, exactly like edges below: seed nodes arrive
    // from params unchecked, and a duplicate id in a keyed each is a Svelte
    // crash, not a cosmetic glitch.
    const seen = new Set<string>();
    const out: GNode[] = [];
    for (const n of raw as GNode[]) {
      if (!n || typeof n.id !== 'string' || seen.has(n.id)) continue;
      seen.add(n.id);
      out.push(n);
    }
    return out;
  });
  const edges = $derived.by(() => {
    const raw = resolveDynamic(comp.edges, ctx);
    if (!Array.isArray(raw)) return [];
    // Same duplicate-render-key guard as DagGraphView: the expand merge
    // dedupes what IT adds, but seed edges arrive from params unchecked.
    const seen = new Set<string>();
    const out: GEdge[] = [];
    for (const e of raw as GEdge[]) {
      if (!e || !e.source || !e.target) continue;
      const key = `${e.source}→${e.target}:${e.relation ?? ''}`;
      if (seen.has(key)) continue;
      seen.add(key);
      out.push(e);
    }
    return out;
  });
  const focusId = $derived(
    typeof comp.focusNodeId === 'string' && comp.focusNodeId ? comp.focusNodeId : (nodes[0]?.id ?? ''),
  );

  // Radial layout: focus centered, others on one ring (two interleaved
  // radii once it gets crowded so labels stop colliding).
  const positions = $derived.by(() => {
    const map = new Map<string, { x: number; y: number }>();
    const others = nodes.filter((n) => n.id !== focusId);
    map.set(focusId, { x: W / 2, y: H / 2 });
    const base = Math.min(W, H) / 2 - 52;
    others.forEach((n, i) => {
      const r = others.length > 9 && i % 2 === 1 ? base * 0.58 : base;
      const angle = (2 * Math.PI * i) / Math.max(others.length, 1) - Math.PI / 2;
      map.set(n.id, { x: W / 2 + r * Math.cos(angle), y: H / 2 + r * Math.sin(angle) });
    });
    return map;
  });

  const TYPE_COLORS: Record<string, string> = {
    fact: 'var(--accent)',
    decision: '#a78bfa',
    episode: 'var(--green)',
    procedure: 'var(--yellow)',
    chunk: 'var(--muted)',
  };

  function short(label: string | undefined, id: string): string {
    const text = label || id.slice(0, 8);
    return text.length > 18 ? text.slice(0, 17) + '…' : text;
  }

  async function expand(node: GNode) {
    if (busy) return;
    selected = node.id;
    if (expanded.has(node.id)) return;
    busy = true;
    error = '';
    const res = await transport.callAgentFunction(surfaceId, 'expandGraphNode', {
      nodeId: node.id,
      nodeType: node.type ?? 'fact',
    });
    busy = false;
    if (!res.ok) {
      error = res.message;
      return;
    }
    expanded.add(node.id);
    const value = res.value as { nodes?: GNode[]; edges?: GEdge[] } | undefined;
    const dm = store.surfaces[surfaceId]?.dataModel as
      | { nodes?: GNode[]; edges?: GEdge[] }
      | undefined;
    if (!dm || !value) return;
    // Merge into the reactive data model in place; dedupe so re-expansion
    // and overlapping neighborhoods never duplicate.
    const dmNodes = (dm.nodes ??= []);
    const dmEdges = (dm.edges ??= []);
    const knownN = new Set(dmNodes.map((n) => n.id));
    for (const n of value.nodes ?? []) {
      if (n?.id && !knownN.has(n.id)) {
        knownN.add(n.id);
        dmNodes.push(n);
      }
    }
    const key = (e: GEdge) => `${e.source}→${e.target}:${e.relation ?? ''}`;
    const knownE = new Set(dmEdges.map(key));
    for (const e of value.edges ?? []) {
      if (e?.source && e?.target && !knownE.has(key(e))) {
        knownE.add(key(e));
        dmEdges.push(e);
      }
    }
  }
</script>

<div class="graph">
  <svg viewBox="0 0 {W} {H}" role="img" aria-label="memory graph">
    {#each edges as edge (edge.source + edge.target + (edge.relation ?? ''))}
      {@const a = positions.get(edge.source)}
      {@const b = positions.get(edge.target)}
      {#if a && b}
        <line
          x1={a.x}
          y1={a.y}
          x2={b.x}
          y2={b.y}
          stroke="var(--border)"
          stroke-width={Math.max(1, (edge.weight ?? 0.5) * 2.5)}
        >
          <title>{edge.relation ?? 'related'}</title>
        </line>
      {/if}
    {/each}
    {#each nodes as node (node.id)}
      {@const p = positions.get(node.id)}
      {#if p}
        <g
          class="node"
          class:selected={node.id === selected}
          role="button"
          tabindex="0"
          aria-label={node.label ?? node.id}
          onclick={() => void expand(node)}
          onkeydown={(e) => {
            if (e.key === 'Enter' || e.key === ' ') void expand(node);
          }}
        >
          <circle
            cx={p.x}
            cy={p.y}
            r={node.id === focusId ? 14 : 9}
            fill={TYPE_COLORS[node.type ?? ''] ?? 'var(--muted)'}
            stroke={node.id === selected ? 'var(--text)' : 'transparent'}
            stroke-width="2"
          >
            <title>{node.type ?? '?'}: {node.label ?? node.id}</title>
          </circle>
          <text x={p.x} y={p.y + (node.id === focusId ? 28 : 22)} text-anchor="middle">
            {short(node.label, node.id)}
          </text>
        </g>
      {/if}
    {/each}
  </svg>
  <div class="status">
    {#if busy}
      <span class="muted">expanding…</span>
    {:else if error}
      <span class="err" role="alert">{error}</span>
    {/if}
  </div>
</div>

<style>
  .graph {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    padding: 0.4rem;
  }
  svg {
    display: block;
    width: 100%;
    height: auto;
  }
  .node {
    cursor: pointer;
  }
  .node:hover circle {
    stroke: var(--accent);
  }
  text {
    fill: var(--muted);
    font-size: 11px;
  }
  .node.selected text {
    fill: var(--text);
  }
  .status {
    min-height: 1.1rem;
    font-size: 0.8rem;
    padding: 0 0.3rem;
  }
  .muted {
    color: var(--muted);
  }
  .err {
    color: var(--red);
  }
</style>
