<script lang="ts">
  import { apiGet } from '../lib/api';
  import { makePollStore } from '../lib/stores/registry';
  import { usePoll } from '../lib/poll';
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
  import GraphViz, { type GraphNode } from '../lib/viz/Graph.svelte';

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
        color: typeColor(n.type),
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
        color: relationColor(e.relation),
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
      { label: 'Total Edges', value: String(s.total_edges) },
      { label: 'Shown Edges', value: String(s.displayed_edges) },
      { label: 'Orphans', value: String(orphanTotal) },
    ];
  });

  // ── Cytoscape style (mirrors graph.js) ──────────────────────────────────
  const cytoscapeStyle = [
    {
      selector: 'node',
      style: {
        'background-color': 'data(color)',
        'border-width': 1,
        'border-color': 'rgba(0,0,0,0.3)',
        width: 'mapData(edge_count, 0, 30, 14, 44)',
        height: 'mapData(edge_count, 0, 30, 14, 44)',
        label: '',
        'font-size': '9px',
        color: '#6b6b8a',
        'text-valign': 'bottom',
        'text-margin-y': 4,
        'text-outline-color': '#0a0a0f',
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
      style: { 'border-width': 3, 'border-color': '#fbbf24' },
    },
    {
      selector: 'node.faded',
      style: { opacity: 0.15 },
    },
    {
      selector: 'node.searchHit',
      style: { 'border-width': 2, 'border-color': '#fbbf24' },
    },
    {
      selector: 'edge',
      style: {
        'curve-style': 'bezier',
        'line-color': 'data(color)',
        opacity: 0.5,
        width: 'mapData(weight, 0, 1, 1, 4)',
        'target-arrow-color': 'data(color)',
        'target-arrow-shape': 'triangle',
        'arrow-scale': 0.7,
      },
    },
    {
      selector: 'edge[extraction_method = "inferred"]',
      style: { opacity: 0.3, 'line-style': 'dashed' },
    },
    {
      selector: 'edge.faded',
      style: { opacity: 0.05 },
    },
  ];

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

  function typeColor(type: string): string {
    const map: Record<string, string> = {
      fact: '#34d399',
      episode: '#60a5fa',
      decision: '#a78bfa',
      procedure: '#fbbf24',
      chunk: '#06b6d4',
    };
    return map[type] ?? '#6b6b8a';
  }

  function relationColor(relation: string): string {
    const map: Record<string, string> = {
      related_to: '#7c6af7',
      extracted_from: '#60a5fa',
      supports: '#34d399',
      informed_by: '#a78bfa',
      evidence_for: '#fbbf24',
      contradicts: '#f87171',
      supersedes: '#fb923c',
      caused_by: '#e2e2f0',
      discussed_in: '#6b6b8a',
      part_of: '#22d3ee',
      summarized_by: '#06b6d4',
    };
    return map[relation] ?? '#6b6b8a';
  }

  function fmtDate(iso: string | null | undefined): string {
    if (!iso) return '--';
    try {
      return new Date(iso).toLocaleDateString();
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
    <h1 class="view-title">Knowledge Graph</h1>
    <p class="view-subtitle">Memory nodes and relationship edges across all types</p>
  </div>
  <div class="head-actions">
    <StaleBadge state={$store} />
    <button class="btn-refresh" onclick={() => store.refresh()}>Refresh</button>
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
        <span class="type-badge" style:background="{typeColor(head.type)}20" style:color={typeColor(head.type)}>
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
                  <span class="conn-dir" title={c.direction === 'out' ? 'this → neighbor' : 'neighbor → this'}>
                    {c.direction === 'out' ? '→' : '←'}
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
  .view-head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 1rem;
    margin-bottom: 1.25rem;
  }

  .view-title {
    font-size: 1.375rem;
    font-weight: 700;
    color: var(--text);
    margin: 0 0 0.25rem;
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

  .btn-refresh {
    padding: 0.25rem 0.75rem;
    font-size: 0.75rem;
    font-weight: 500;
    border: 1px solid var(--border);
    border-radius: var(--radius-sm, 6px);
    background: var(--surface);
    color: var(--text);
    cursor: pointer;
    white-space: nowrap;
  }

  .btn-refresh:hover {
    background: var(--surface-hover);
  }

  .state-msg {
    margin-top: 2rem;
    text-align: center;
    color: var(--muted);
    font-size: 0.875rem;
  }

  .state-msg.error {
    color: var(--red, #f87171);
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
    border: 1px solid var(--border);
    border-radius: var(--radius-sm, 8px);
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
    background: rgba(17, 17, 24, 0.6);
  }

  .search-input {
    padding: 0.25rem 0.625rem;
    font-size: 0.8125rem;
    border: 1px solid var(--border);
    border-radius: var(--radius-sm, 6px);
    background: var(--bg, #0f172a);
    color: var(--text);
    width: 180px;
    outline: none;
  }

  .search-input:focus {
    border-color: var(--accent);
  }

  .type-filters {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
  }

  .type-check {
    display: flex;
    align-items: center;
    gap: 0.3rem;
    font-size: 0.75rem;
    color: var(--muted);
    cursor: pointer;
    user-select: none;
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
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }

  .type-badge {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 0.6875rem;
    font-weight: 600;
    border: 1px solid transparent;
  }

  .mono {
    font-family: ui-monospace, monospace;
    font-size: 0.8125rem;
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
    background: var(--surface-hover, rgba(255, 255, 255, 0.06));
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
    background: var(--surface-hover, rgba(255, 255, 255, 0.06));
  }

  .conn-item.inactive {
    opacity: 0.55;
  }

  .conn-dir {
    color: var(--muted);
    font-family: ui-monospace, monospace;
    flex-shrink: 0;
  }

  .conn-type-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    flex-shrink: 0;
  }

  .conn-label {
    flex: 1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .conn-flag {
    font-size: 0.625rem;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--red, #f87171);
    border: 1px solid var(--red, #f87171);
    border-radius: 3px;
    padding: 0 0.25rem;
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

  .conn-weight {
    font-family: ui-monospace, monospace;
  }
</style>
