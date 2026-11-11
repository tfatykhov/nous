<script lang="ts">
  import { fmtUtc } from '../lib/harness';
  import { apiGet } from '../lib/api';
  import { makePollStore } from '../lib/stores/registry';
  import { usePoll } from '../lib/poll';
  import { graphSliceText } from '../lib/graphSlice';
  import type {
    GraphData,
    GraphEdgeData,
    GraphNodeData,
    GraphConnection,
    GraphNodeDetail,
  } from '../lib/types/api';
  import StatGrid from '../lib/ui/StatGrid.svelte';
  import StaleBadge from '../lib/ui/StaleBadge.svelte';
  import BottomSheet from '../lib/ui/BottomSheet.svelte';
  import { Button } from '../lib/ui/button';
  import GraphViz, { type GraphNode } from '../lib/viz/Graph.svelte';
  import { cssColor } from '../lib/chartColors';
  import { readToken } from '../lib/overviewCharts';

  const store = usePoll(
    makePollStore<GraphData>(
      (signal) => apiGet<GraphData>('/dashboard/graph?limit=500', { signal }),
      0, // fetch-once; user-triggered refresh only
    ),
  );

  // ── Filter state (component-local) ──────────────────────────────────────
  const ALL_TYPES = ['fact', 'episode', 'decision', 'procedure', 'chunk'] as const;
  type NodeType = typeof ALL_TYPES[number];

  let checkedTypes = $state<Record<NodeType, boolean>>({
    fact: true,
    episode: true,
    decision: true,
    procedure: true,
    chunk: true,
  });
  let minEdges = $state(0);
  let searchQuery = $state('');

  // ── Node detail (BottomSheet) ────────────────────────────────────────────
  // The sheet shows full content + ALL connections, fetched lazily from
  // /dashboard/graph/node/{id}. selectedHead holds the lightweight {id,type,label}
  // picked from either a Cytoscape tap or a connection drill-through, so the
  // header renders instantly while the detail loads.
  type NodeHead = { id: string; type: string; label: string };
  let selectedHead = $state<NodeHead | null>(null);
  let sheetOpen = $state(false);
  let detail = $state<GraphNodeDetail | null>(null);
  let detailLoading = $state(false);
  let detailError = $state(false);

  let detailReq = 0; // monotonic guard against out-of-order responses
  let detailAbort: AbortController | null = null;

  async function loadDetail(head: NodeHead) {
    selectedHead = head;
    sheetOpen = true;
    detail = null;
    detailError = false;
    detailLoading = true;

    detailAbort?.abort();
    const ac = new AbortController();
    detailAbort = ac;
    const req = ++detailReq;
    try {
      const data = await apiGet<GraphNodeDetail>(
        `/dashboard/graph/node/${encodeURIComponent(head.id)}?type=${encodeURIComponent(head.type)}`,
        { signal: ac.signal, retries: 1 },
      );
      if (req !== detailReq) return; // a newer request superseded this one
      detail = data;
    } catch (err) {
      if (req !== detailReq) return;
      // 404 = node hard-deleted but still edge-referenced; treat as "not found".
      detail = { found: false };
      detailError = (err as { status?: number }).status !== 404;
    } finally {
      if (req === detailReq) detailLoading = false;
    }
  }

  function onNodeClick(node: GraphNode) {
    loadDetail({ id: node.id, type: node.type, label: node.label });
  }

  function drillTo(c: GraphConnection) {
    loadDetail({ id: c.neighbor_id, type: c.neighbor_type, label: c.neighbor_label });
  }

  // Connections grouped by relation, largest group first; items keep the
  // backend's weight-desc order within each group.
  let groupedConnections = $derived.by(() => {
    const conns = detail?.connections ?? [];
    const groups = new Map<string, GraphConnection[]>();
    for (const c of conns) {
      const arr = groups.get(c.relation);
      if (arr) arr.push(c);
      else groups.set(c.relation, [c]);
    }
    return [...groups.entries()]
      .map(([relation, items]) => ({ relation, items }))
      .sort((a, b) => b.items.length - a.items.length);
  });

  // ── Derived: hiddenTypes set ─────────────────────────────────────────────
  let hiddenTypes = $derived(
    new Set(
      (Object.keys(checkedTypes) as NodeType[]).filter((t) => !checkedTypes[t]),
    ),
  );

  // ── Derived: build Cytoscape elements from backend data ─────────────────
  //
  // graph.js computes edge_count per node from the edge list, then embeds it
  // in the Cytoscape node data so style selectors (degree-based sizing,
  // label threshold, min-edges filter) can read it via node.data('edge_count').
  // We replicate that mapping here.

  let elements = $derived(() => {
    const data = $store.data;
    if (!data) return [];

    // edge_count per node id
    const edgeCounts: Record<string, number> = {};
    for (const e of data.edges) {
      edgeCounts[e.source] = (edgeCounts[e.source] ?? 0) + 1;
      edgeCounts[e.target] = (edgeCounts[e.target] ?? 0) + 1;
    }

    const nodes = data.nodes.map((n: GraphNodeData) => ({
      data: {
        id: n.id,
        label: n.label ?? '',
        type: n.type,
        category: n.category ?? '',
        edge_count: edgeCounts[n.id] ?? 0,
        created_at: null, // not in backend response; kept for BottomSheet compat
      },
    }));

    const edges = data.edges.map((e: GraphEdgeData) => ({
      data: {
        id: e.id,
        source: e.source,
        target: e.target,
        relation: e.relation,
        weight: e.weight ?? 0.5,
        extraction_method: e.extraction_method ?? 'heuristic',
      },
    }));

    return [...nodes, ...edges];
  });

  // ── Derived: stat cards ──────────────────────────────────────────────────
  let statCards = $derived(() => {
    const s = $store.data?.stats;
    if (!s) return [];
    const orphanTotal = Object.values(s.orphan_counts).reduce((a, b) => a + b, 0);
    return [
      { label: 'Nodes', value: String(s.node_count) },
      { label: 'Total edges', value: String(s.total_edges) },
      { label: 'Shown edges', value: String(s.displayed_edges) },
      { label: 'Orphans', value: String(orphanTotal) },
    ];
  });

  // ── Cytoscape style (mirrors graph.js) ──────────────────────────────────
  // Cytoscape draws on a canvas and cannot read var(): the stylesheet's colours are read off the graph's own element.
  // Node and edge fills come from each element's `color` data.
  function cytoscapeStyle(el: Element) {
    const c = (token: string) => cssColor(el, token);
    /** A number token on the graph's element (the edge opacities); `fallback`, the :root value, when unset (jsdom). */
    const n = (token: string, fallback: number) => {
      const v = parseFloat(el.isConnected ? readToken(el, token) : '');
      return Number.isFinite(v) ? v : fallback;
    };
    return [
    {
      selector: 'node',
      style: {
        'background-color': 'data(color)',
        'border-width': 1,
        'border-color': c('--gv-node-border'),
        width: 'mapData(edge_count, 0, 30, 14, 44)',
        height: 'mapData(edge_count, 0, 30, 14, 44)',
        label: '',
        'font-size': '9px',
        color: c('--muted'),
        'text-valign': 'bottom',
        'text-margin-y': 4,
        'text-outline-color': c('--gv-outline'),
        'text-outline-width': 2,
      },
    },
    {
      selector: 'node[edge_count >= 3]',
      style: {
        label: (ele: any) => (ele.data('label') ?? '').slice(0, 30),
      },
    },
    {
      selector: 'node:selected',
      style: { 'border-width': 3, 'border-color': c('--gv-select') },
    },
    {
      selector: 'node.faded',
      style: { opacity: 0.15 },
    },
    {
      selector: 'node.searchHit',
      style: { 'border-width': 2, 'border-color': c('--gv-select') },
    },
    {
      selector: 'edge',
      style: {
        'curve-style': 'bezier',
        'line-color': 'data(color)',
        opacity: n('--gv-edge-opacity', 0.75),
        width: 'mapData(weight, 0, 1, 1, 4)',
        'target-arrow-color': 'data(color)',
        'target-arrow-shape': 'triangle',
        'arrow-scale': 0.7,
      },
    },
    {
      selector: 'edge[extraction_method = "inferred"]',
      style: { opacity: n('--gv-edge-opacity-inferred', 0.5), 'line-style': 'dashed' },
    },
    {
      selector: 'edge.faded',
      style: { opacity: 0.05 },
    },
    ];
  }

  // Cytoscape layout (cose — bundled, no plugin needed)
  const cytoscapeLayout = {
    name: 'cose',
    animate: false,
    randomize: true,
    componentSpacing: 80,
    nodeRepulsion: () => 80000,
    nodeOverlap: 12,
    idealEdgeLength: () => 80,
    edgeElasticity: () => 100,
    nestingFactor: 1.2,
    gravity: 80,
    numIter: 1500,
    initialTemp: 200,
    coolingFactor: 0.95,
    minTemp: 1.0,
  };

  // ── Helpers ──────────────────────────────────────────────────────────────

  // A node type's and a relation's colour, as a token: var() works as is in the legend, the badge and the dots, and
  // Graph.svelte resolves it for the canvas. Graph's own tokens (--gv-*), not the legacy --*-color ones, whose fact
  // and episode are the other way round.
  const RELATIONS = new Set([
    'related_to', 'extracted_from', 'supports', 'informed_by', 'evidence_for', 'contradicts',
    'supersedes', 'caused_by', 'discussed_in', 'part_of', 'summarized_by',
  ]);

  function typeColor(type: string): string {
    return (ALL_TYPES as readonly string[]).includes(type) ? `var(--gv-${type})` : 'var(--gv-other)';
  }

  function relationColor(relation: string): string {
    return RELATIONS.has(relation) ? `var(--rel-${relation.replace(/_/g, '-')})` : 'var(--gv-other)';
  }

  function colorFor(kind: 'node' | 'edge', key: string): string {
    return kind === 'node' ? typeColor(key) : relationColor(key);
  }

  function fmtDate(iso: string | null | undefined): string {
    if (!iso) return '--';
    try {
      return fmtUtc(iso);
    } catch {
      return iso;
    }
  }

  function capitalize(s: string): string {
    return s.charAt(0).toUpperCase() + s.slice(1);
  }

  function fmtWeight(w: number | null): string {
    return w == null ? '--' : w.toFixed(2);
  }

  function fmtRelation(r: string): string {
    return r.replace(/_/g, ' ');
  }
</script>

<div class="view-head">
  <div>
    <h1 class="view-title">Graph</h1>
    <p class="view-subtitle">
      {$store.data ? graphSliceText($store.data.stats) : 'Memory nodes and the links between them.'}
    </p>
  </div>
  <div class="head-actions">
    <StaleBadge state={$store} />
    <Button variant="outline" size="lg" class="px-4 max-md:min-h-11" onclick={() => store.refresh()}>Refresh</Button>
  </div>
</div>

{#if $store.loading && $store.data === null}
  <p class="state-msg">Loading graph data…</p>
{:else if $store.error && $store.data === null}
  <p class="state-msg error">Failed to load graph data — <button class="link-btn" onclick={() => store.refresh()}>retry</button></p>
{:else if $store.data}
  {@const data = $store.data}

  <!-- Stat cards -->
  <StatGrid stats={statCards()} />

  <!-- Graph panel -->
  <section class="chart-card graph-card">
    <!-- Controls bar -->
    <div class="controls-bar">
      <!-- Search -->
      <input
        class="search-input"
        type="search"
        placeholder="Search nodes…"
        bind:value={searchQuery}
        aria-label="Search graph nodes"
      />

      <!-- Type checkboxes -->
      <div class="type-filters">
        {#each ALL_TYPES as type}
          <label class="type-check">
            <input
              type="checkbox"
              bind:checked={checkedTypes[type]}
            />
            <span class="type-dot" style:background={typeColor(type)}></span>
            {capitalize(type)}
          </label>
        {/each}
      </div>

      <!-- Min edges slider -->
      <label class="slider-wrap">
        <span class="slider-label">Min edges: {minEdges}</span>
        <input
          type="range"
          min="0"
          max="10"
          bind:value={minEdges}
          class="range-slider"
        />
      </label>
    </div>

    {#if data.nodes.length === 0}
      <p class="empty-state">
        No graph edges yet. As Nous learns facts and makes decisions, connections will appear here.
      </p>
    {:else}
      <GraphViz
        elements={elements()}
        layout={cytoscapeLayout}
        style={cytoscapeStyle}
        {colorFor}
        {onNodeClick}
        {searchQuery}
        {hiddenTypes}
        {minEdges}
      />
      <p class="graph-hint">Tap a node for details · Scroll to zoom · Drag to pan</p>
    {/if}
  </section>
{/if}

<!-- Node detail bottom sheet -->
<BottomSheet bind:open={sheetOpen} title={selectedHead ? capitalize(selectedHead.type) + ' Detail' : 'Node Detail'}>
  {#if selectedHead}
    {@const head = selectedHead}
    <div class="node-detail">
      <!-- Meta rows: render from detail when loaded, else from the head -->
      <div class="node-row">
        <span class="detail-label">Type</span>
        <span class="type-badge" style:--type={typeColor(head.type)}>
          {head.type}
        </span>
      </div>
      {#if detail?.node?.category}
        <div class="node-row">
          <span class="detail-label">Category</span>
          <span>{detail.node.category}</span>
        </div>
      {/if}
      <div class="node-row">
        <span class="detail-label">ID</span>
        <span class="mono">{head.id.slice(0, 8)}…</span>
      </div>
      {#if detail?.node?.created_at}
        <div class="node-row">
          <span class="detail-label">Created</span>
          <span>{fmtDate(detail.node.created_at)}</span>
        </div>
      {/if}

      <!-- Full content -->
      <div class="detail-section">
        <div class="detail-label">Content</div>
        {#if detailLoading && !detail}
          <div class="detail-text muted-text">Loading…</div>
        {:else if detail?.found && detail.node}
          <div class="detail-text">{detail.node.content || head.label || '(empty)'}</div>
        {:else if detailError}
          <div class="detail-text muted-text">Failed to load — <button class="link-btn" onclick={() => loadDetail(head)}>retry</button></div>
        {:else}
          <!-- 404 / not found: fall back to the truncated graph label -->
          <div class="detail-text">{head.label || '(node not found)'}</div>
        {/if}
      </div>

      <!-- Connections -->
      <div class="detail-section">
        <div class="detail-label">
          Connections{#if detail?.found}<span class="conn-count"> · {detail.connection_count}{#if detail.connections_truncated}+{/if}</span>{/if}
        </div>
        {#if detail?.connections_truncated}
          <div class="detail-text muted-text">Showing the 200 strongest connections.</div>
        {/if}

        {#if detailLoading && !detail}
          <div class="detail-text muted-text">Loading connections…</div>
        {:else if detail?.found && (detail.connection_count ?? 0) === 0}
          <div class="detail-text muted-text">No connections. This node is an orphan — still retrievable via vector + keyword search, just not graph-linked.</div>
        {:else if detail?.found}
          {#each groupedConnections as group (group.relation)}
            <div class="conn-group">
              <div class="conn-group-head">
                <span class="rel-dot" style:background={relationColor(group.relation)}></span>
                <span class="rel-name">{fmtRelation(group.relation)}</span>
                <span class="rel-count">{group.items.length}</span>
              </div>
              {#each group.items as c (c.edge_id)}
                <button
                  class="conn-item"
                  class:inactive={!c.neighbor_active}
                  onclick={() => drillTo(c)}
                  title="Open {c.neighbor_type}: {c.neighbor_label}"
                >
                  <span class="conn-dir" title={c.direction === 'out' ? 'from this node to the neighbour' : 'from the neighbour to this node'}>
                    {c.direction === 'out' ? 'to' : 'from'}
                  </span>
                  <span class="conn-type-dot" style:background={typeColor(c.neighbor_type)}></span>
                  <span class="conn-label">{c.neighbor_label || '(empty)'}</span>
                  {#if !c.neighbor_active}<span class="conn-flag">inactive</span>{/if}
                  <span class="conn-meta">
                    {#if c.extraction_method}<span class="conn-method">{c.extraction_method}</span>{/if}
                    <span class="conn-weight">{fmtWeight(c.weight)}</span>
                  </span>
                </button>
              {/each}
            </div>
          {/each}
        {/if}
      </div>
    </div>
  {/if}
</BottomSheet>

<style>
  /* Colours and structure are tokens only (hexGuard): today's values under the dark look, rounded white cards and
     pills on the paper ground. Paper-only chrome (the title size, chips as pills, the mono font) sits under a
     paper-ground selector, so the dark look keeps its shapes and fonts. */
  .view-head {
    display: flex;
    flex-wrap: wrap; /* at phone width the actions drop below the title rather than widen the page */
    align-items: flex-start;
    justify-content: space-between;
    gap: 1rem;
    margin-bottom: 1.25rem;
  }

  .view-title {
    font-size: 30px;
    font-weight: 650;
    color: var(--text);
    margin: 0 0 0.25rem;
    letter-spacing: -0.022em;
    line-height: 1.15;
  }

  .view-subtitle {
    font-size: 0.8125rem;
    color: var(--muted);
    margin: 0;
  }

  .head-actions {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex-shrink: 0;
  }

  .state-msg {
    margin-top: 2rem;
    text-align: center;
    color: var(--muted);
    font-size: 0.875rem;
  }

  .state-msg.error {
    color: var(--err-text);
  }

  .link-btn {
    background: none;
    border: none;
    color: var(--accent);
    cursor: pointer;
    font-size: inherit;
    padding: 0;
    text-decoration: underline;
  }

  /* ── Graph card ── */
  .chart-card {
    background: var(--surface);
    border: var(--card-border);
    border-radius: var(--card-radius);
    box-shadow: var(--card-shadow);
    padding: 1.25rem;
    margin-top: 1.25rem;
  }

  .graph-card {
    padding: 0;
    overflow: hidden;
  }

  /* ── Controls bar ── */
  .controls-bar {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.75rem;
    padding: 0.75rem 1rem;
    border-bottom: 1px solid var(--border);
    background: var(--surface-hover);
  }

  .search-input {
    padding: 0.3125rem 0.875rem;
    font-size: 0.8125rem;
    border: 1px solid var(--border);
    border-radius: var(--ctl-radius);
    background: var(--surface);
    color: var(--text);
    width: 180px;
    outline: none;
  }

  .search-input:focus {
    border-color: var(--accent);
  }

  .search-input:focus-visible {
    outline: 2px solid var(--focus-ring);
    outline-offset: 1px;
  }

  .type-filters {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
  }

  /* Each type filter is a pill: the box, the dot and the word inside one rounded chip. */
  .type-check {
    display: flex;
    align-items: center;
    gap: 0.3rem;
    font-size: 0.75rem;
    color: var(--text);
    cursor: pointer;
    user-select: none;
    padding: 0.1875rem 0.625rem 0.1875rem 0.4375rem;
    border-radius: 999px;
    background: var(--surface);
    border: 1px solid var(--border);
  }

  .type-check input {
    accent-color: var(--accent);
  }

  .type-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    flex-shrink: 0;
  }

  .slider-wrap {
    display: flex;
    flex-direction: column;
    gap: 0.125rem;
    font-size: 0.75rem;
    color: var(--muted);
  }

  .slider-label {
    white-space: nowrap;
  }

  .range-slider {
    width: 100px;
    accent-color: var(--accent);
  }

  .empty-state {
    color: var(--muted);
    font-size: 0.875rem;
    padding: 3rem 1.25rem;
    text-align: center;
  }

  .graph-hint {
    text-align: center;
    font-size: 0.6875rem;
    color: var(--muted);
    padding: 0.375rem 0.5rem;
    margin: 0;
    border-top: 1px solid var(--border);
  }

  /* ── Node detail (bottom sheet) ── */
  .node-detail {
    display: flex;
    flex-direction: column;
    gap: 0.625rem;
    font-size: 0.875rem;
  }

  .node-row {
    display: flex;
    align-items: center;
    gap: 0.75rem;
  }

  .node-row .detail-label {
    min-width: 5rem;
    flex-shrink: 0;
  }

  .detail-label {
    font-size: 0.6875rem;
    font-weight: 600;
    color: var(--muted);
    text-transform: none;
    letter-spacing: 0;
  }

  /* The type's colour (--type, set inline) on its own 12.5% tint (the old hex `20` alpha). */
  /* The node colours are 3:1 marks, not text: the badge's word is mixed toward the ink (4.5:1 or more). */
  .type-badge {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 999px;
    font-size: 0.6875rem;
    font-weight: 600;
    border: 1px solid transparent;
    background: color-mix(in srgb, var(--type) 12.5%, transparent);
    color: color-mix(in srgb, var(--type) 70%, var(--text));
  }

  .mono {
    font-family: ui-monospace, monospace;
    font-size: 0.8125rem;
  }

  .conn-dir,
  .conn-weight {
    font-family: ui-monospace, monospace;
  }

  .mono, .conn-weight {
    font-family: var(--font-mono);
  }

  .detail-section {
    margin-top: 0.25rem;
  }

  .detail-text {
    margin-top: 0.25rem;
    font-size: 0.8125rem;
    color: var(--text);
    white-space: pre-wrap;
    word-break: break-word;
    overflow-wrap: anywhere;
    line-height: 1.5;
  }

  .muted-text {
    color: var(--muted);
  }

  /* ── Connections ── */
  .conn-count {
    color: var(--muted);
    font-weight: 500;
  }

  .conn-group {
    margin-top: 0.5rem;
  }

  .conn-group-head {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    margin: 0.5rem 0 0.25rem;
    font-size: 0.75rem;
    color: var(--text);
  }

  .rel-dot {
    width: 8px;
    height: 8px;
    border-radius: 2px;
    flex-shrink: 0;
  }

  .rel-name {
    font-weight: 600;
  }

  .rel-count {
    color: var(--muted);
    font-size: 0.6875rem;
    background: var(--surface-hover);
    border-radius: 999px;
    padding: 0 0.4rem;
    line-height: 1.4;
  }

  .conn-item {
    display: flex;
    align-items: center;
    gap: 0.45rem;
    width: 100%;
    text-align: left;
    background: none;
    border: none;
    border-radius: var(--radius-sm, 6px);
    padding: 0.3rem 0.4rem;
    cursor: pointer;
    color: var(--text);
    font-size: 0.8125rem;
  }

  .conn-item:hover {
    background: var(--surface-hover);
  }

  .conn-item.inactive {
    opacity: 0.55;
  }

  /* The direction is a word ("to" or "from"), muted, in a fixed width so the rows line up. */
  .conn-dir {
    color: var(--muted);
    flex-shrink: 0;
    min-width: 2.25rem;
    font-size: 0.6875rem;
    font-family: var(--font-ui);
  }

  .conn-type-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    flex-shrink: 0;
  }

  .conn-label {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .conn-flag {
    font-size: 0.625rem;
    text-transform: none;
    letter-spacing: 0;
    color: var(--err-text);
    border: 1px solid var(--red);
    border-radius: 999px;
    padding: 0 0.375rem;
    flex-shrink: 0;
  }

  .conn-meta {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    flex-shrink: 0;
    color: var(--muted);
    font-size: 0.6875rem;
  }

  .conn-method {
    font-style: italic;
  }
</style>
