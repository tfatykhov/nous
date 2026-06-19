<script lang="ts">
  import { apiGet } from '../lib/api';
  import { makePollStore } from '../lib/stores/registry';
  import { usePoll } from '../lib/poll';
  import type { DagDashboardData, DagActiveDag, DagRecentDag } from '../lib/types/api';
  import StatGrid from '../lib/ui/StatGrid.svelte';
  import DataTable from '../lib/ui/DataTable.svelte';
  import StaleBadge from '../lib/ui/StaleBadge.svelte';
  import Chart from '../lib/viz/Chart.svelte';
  import BottomSheet from '../lib/ui/BottomSheet.svelte';
  import DagViz, { type DagNode, type DagEdge } from '../lib/viz/Dag.svelte';

  const store = usePoll(
    makePollStore<DagDashboardData>(
      (signal) => apiGet<DagDashboardData>('/dashboard/dag?limit=20', { signal }),
      15_000,
    ),
  );

  // ── Selected active DAG for graph view ───────────────────────────────────
  let selectedDagId = $state<string | null>(null);
  let selectedNode = $state<DagNode | null>(null);
  let sheetOpen = $state(false);

  function selectDag(dag: DagActiveDag) {
    selectedDagId = selectedDagId === dag.id ? null : dag.id;
    selectedNode = null;
  }

  function onNodeClick(node: DagNode) {
    selectedNode = node;
    sheetOpen = true;
  }

  // Close graph when data refreshes and the selected DAG disappears
  $effect(() => {
    const d = $store.data;
    if (!d || !selectedDagId) return;
    const stillActive = d.active_dags.some((dag) => dag.id === selectedDagId);
    if (!stillActive) {
      selectedDagId = null;
      selectedNode = null;
    }
  });

  // ── Derived: selected DAG object ─────────────────────────────────────────
  let activeDag = $derived(
    $store.data?.active_dags.find((d) => d.id === selectedDagId) ?? null,
  );

  // ── Formatters ────────────────────────────────────────────────────────────

  function fmtAgo(iso: string | null | undefined): string {
    if (!iso) return '--';
    const diff = (Date.now() - new Date(iso).getTime()) / 1000;
    if (diff < 30) return 'just now';
    const mins = Math.floor(diff / 60);
    if (mins < 1) return Math.floor(diff) + 's ago';
    if (mins < 60) return mins + 'm ago';
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return hrs + 'h ago';
    return Math.floor(hrs / 24) + 'd ago';
  }

  function fmtDuration(seconds: number | null | undefined): string {
    if (seconds == null || seconds === 0) return '--';
    if (seconds < 60) return Math.round(seconds) + 's';
    if (seconds < 3600) return (seconds / 60).toFixed(1) + 'm';
    return (seconds / 3600).toFixed(1) + 'h';
  }

  function fmtTokens(n: number): string {
    if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + 'M';
    if (n >= 1_000) return (n / 1_000).toFixed(1) + 'K';
    return String(n);
  }

  function dagDuration(dag: DagRecentDag): string {
    if (!dag.created_at || !dag.completed_at) return '--';
    const secs = (new Date(dag.completed_at).getTime() - new Date(dag.created_at).getTime()) / 1000;
    return fmtDuration(secs);
  }

  // ── Status badge ──────────────────────────────────────────────────────────
  const STATUS_COLOR: Record<string, string> = {
    pending: '#6b6b8a',
    ready: '#22d3ee',
    running: '#fbbf24',
    awaiting_check: '#f59e0b',
    completed: '#4ade80',
    failed: '#f87171',
    blocked: '#991b1b',
    cancelled: '#4b4b5a',
    partial: '#fb923c',
  };

  function statusColor(s: string): string {
    return STATUS_COLOR[s] ?? '#6b6b8a';
  }

  // ── Stat grid ─────────────────────────────────────────────────────────────
  let stats = $derived(() => {
    const s = $store.data?.stats;
    if (!s) return [];
    const successPct = s.success_rate != null ? Math.round(s.success_rate * 100) + '%' : '--';
    return [
      { label: 'Active DAGs', value: String(s.active_count) },
      { label: 'Nodes (24 h)', value: String(s.nodes_completed_24h) },
      { label: 'Success Rate', value: successPct },
      { label: 'Avg Duration', value: fmtDuration(s.avg_completion_seconds) },
    ];
  });

  // ── Active DAGs table rows ────────────────────────────────────────────────
  let activeRows = $derived(
    ($store.data?.active_dags ?? []).map((dag) => {
      const completed = dag.nodes.filter((n) => n.status === 'completed').length;
      const total = dag.nodes.length;
      const pct = total > 0 ? Math.round((completed / total) * 100) : 0;
      return {
        _dag: dag,
        name: dag.name,
        status: dag.status,
        source: dag.source,
        progress: `${completed}/${total}`,
        pct,
        created: fmtAgo(dag.created_at),
      };
    }),
  );

  // ── Budget chart (active DAGs with a token_budget > 0) ───────────────────
  let budgetDags = $derived(
    ($store.data?.active_dags ?? []).filter((d) => d.token_budget > 0),
  );

  let budgetChartData = $derived(() => {
    const dags = budgetDags;
    if (dags.length === 0) return null;
    return {
      labels: dags.map((d) => d.name),
      datasets: [
        {
          label: 'Consumed',
          data: dags.map((d) => d.tokens_consumed),
          backgroundColor: 'rgba(251,191,36,0.7)',
        },
        {
          label: 'Budget',
          data: dags.map((d) => d.token_budget - d.tokens_consumed),
          backgroundColor: 'rgba(255,255,255,0.08)',
        },
      ],
    };
  });

  // ── Recent DAGs table ─────────────────────────────────────────────────────
  const recentCols = [
    { key: 'name', label: 'Name' },
    { key: 'statusBadge', label: 'Status' },
    { key: 'nodes', label: 'Nodes' },
    { key: 'tokens', label: 'Tokens' },
    { key: 'completed', label: 'Completed' },
  ];

  let recentRows = $derived(
    ($store.data?.recent_dags ?? []).map((dag) => ({
      _dag: dag,
      name: dag.name,
      statusBadge: dag.status,
      nodes: `${dag.completed_count}/${dag.node_count}`,
      tokens: fmtTokens(dag.tokens_consumed),
      completed: fmtAgo(dag.completed_at),
    })),
  );

  // ── Nodes cast to DagNode shape for the viz ───────────────────────────────
  function toVizNodes(dag: DagActiveDag): DagNode[] {
    return dag.nodes.map((n) => ({
      id: n.id,
      name: n.name,
      status: n.status,
      node_type: n.node_type,
      wave: n.wave,
      started_at: n.started_at ?? undefined,
      completed_at: n.completed_at ?? undefined,
      tokens_used: n.tokens_used,
      description: n.description,
      result: n.result,
      error: n.error,
    }));
  }

  function toVizEdges(dag: DagActiveDag): DagEdge[] {
    return dag.edges.map((e) => ({
      from_node_id: e.from_node_id,
      to_node_id: e.to_node_id,
      edge_type: e.edge_type ?? undefined,
    }));
  }
</script>

<div class="view-head">
  <div>
    <h1 class="view-title">DAG Orchestrator</h1>
    <p class="view-subtitle">Unified execution DAGs, node progress, and graph visualization</p>
  </div>
  <StaleBadge state={$store} />
</div>

{#if $store.loading && $store.data === null}
  <p class="state-msg">Loading DAG data…</p>
{:else if $store.error && $store.data === null}
  <p class="state-msg error">Failed to load DAG data — retrying every 15 s</p>
{:else if $store.data}
  {@const data = $store.data}

  <!-- Stat cards -->
  <StatGrid stats={stats()} />

  <!-- Active DAGs -->
  <section class="chart-card">
    <h2 class="section-title">Active DAGs</h2>
    {#if data.active_dags.length === 0}
      <p class="empty-state">No active DAGs</p>
    {:else}
      <div class="table-wrap">
        <table class="dag-table">
          <thead>
            <tr>
              <th>Name</th>
              <th>Status</th>
              <th>Source</th>
              <th>Progress</th>
              <th>Created</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {#each activeRows as row (row._dag.id)}
              {@const isSelected = row._dag.id === selectedDagId}
              <tr class:selected={isSelected}>
                <td><strong>{row.name}</strong></td>
                <td>
                  <span
                    class="status-badge"
                    style:background="{statusColor(row.status)}20"
                    style:color={statusColor(row.status)}
                    style:border-color="{statusColor(row.status)}40"
                  >{row.status}</span>
                </td>
                <td class="muted small">{row.source}</td>
                <td class="progress-cell">
                  <div class="progress-bar">
                    <div class="progress-fill" style:width="{row.pct}%"></div>
                  </div>
                  <span class="small muted">{row.progress} nodes</span>
                </td>
                <td class="muted small">{row.created}</td>
                <td>
                  <button
                    class="btn-sm"
                    class:btn-active={isSelected}
                    onclick={() => selectDag(row._dag)}
                  >
                    {isSelected ? 'Hide Graph' : 'View Graph'}
                  </button>
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>

      <!-- Inline graph panel for selected DAG -->
      {#if activeDag}
        <div class="graph-panel">
          <div class="graph-header">
            <span class="graph-dag-name">{activeDag.name}</span>
            <button class="btn-sm" onclick={() => { selectedDagId = null; selectedNode = null; }}>
              Close
            </button>
          </div>
          <DagViz
            nodes={toVizNodes(activeDag)}
            edges={toVizEdges(activeDag)}
            onNodeClick={onNodeClick}
          />
          <p class="graph-hint">Tap a node for details</p>
        </div>
      {/if}
    {/if}
  </section>

  <!-- Token budget chart (only when active DAGs have budgets) -->
  {#if budgetDags.length > 0 && budgetChartData() !== null}
    <section class="chart-card">
      <h2 class="section-title">Token Budgets</h2>
      <Chart type="bar" data={budgetChartData()!} height="240px" options={{
        plugins: { legend: { labels: { color: '#94a3b8' } } },
        scales: {
          x: { stacked: true, ticks: { color: '#94a3b8' }, grid: { color: '#1e293b' } },
          y: { stacked: true, ticks: { color: '#94a3b8' }, grid: { color: '#1e293b' } },
        },
      }} />
    </section>
  {/if}

  <!-- Recent DAGs -->
  <section class="chart-card">
    <h2 class="section-title">Recent DAGs</h2>
    {#if data.recent_dags.length === 0}
      <p class="empty-state">No completed DAGs yet</p>
    {:else}
      <DataTable
        columns={recentCols}
        rows={recentRows.map((r) => ({
          ...r,
          statusBadge: r.statusBadge,
        }))}
        rowKey={(r) => r._dag.id}
        mode="scroll"
      >
        {#snippet detail(row)}
          {@const dag = (row as typeof recentRows[0])._dag}
          <div class="detail-grid">
            <div><span class="detail-label">Source</span><span>{dag.source || '--'}</span></div>
            <div><span class="detail-label">Duration</span><span>{dagDuration(dag)}</span></div>
            <div><span class="detail-label">Token Budget</span><span>{fmtTokens(dag.token_budget)}</span></div>
            <div><span class="detail-label">Created</span><span>{fmtAgo(dag.created_at)}</span></div>
          </div>
          {#if dag.result_summary}
            <div class="detail-section">
              <div class="detail-label">Result Summary</div>
              <div class="detail-text">{dag.result_summary}</div>
            </div>
          {/if}
          {#if dag.postmortem}
            <div class="detail-section">
              <div class="detail-label">Postmortem</div>
              <div class="detail-text detail-postmortem">{dag.postmortem}</div>
            </div>
          {/if}
        {/snippet}
      </DataTable>
    {/if}
  </section>
{/if}

<!-- Node detail bottom sheet -->
<BottomSheet bind:open={sheetOpen} title={selectedNode?.name ?? 'Node Detail'}>
  {#if selectedNode}
    {@const n = selectedNode}
    <div class="node-detail">
      <div class="node-detail-row">
        <span class="detail-label">Status</span>
        <span
          class="status-badge"
          style:background="{statusColor(n.status)}20"
          style:color={statusColor(n.status)}
          style:border-color="{statusColor(n.status)}40"
        >{n.status}</span>
      </div>
      {#if n.node_type}
        <div class="node-detail-row">
          <span class="detail-label">Type</span>
          <span>{n.node_type}</span>
        </div>
      {/if}
      {#if n.wave != null}
        <div class="node-detail-row">
          <span class="detail-label">Wave</span>
          <span>{n.wave}</span>
        </div>
      {/if}
      {#if n.tokens_used}
        <div class="node-detail-row">
          <span class="detail-label">Tokens</span>
          <span>{fmtTokens(n.tokens_used)}</span>
        </div>
      {/if}
      {#if n.started_at}
        <div class="node-detail-row">
          <span class="detail-label">Started</span>
          <span>{fmtAgo(n.started_at)}</span>
        </div>
      {/if}
      {#if n.completed_at}
        <div class="node-detail-row">
          <span class="detail-label">Completed</span>
          <span>{fmtAgo(n.completed_at)}</span>
        </div>
      {/if}
      {#if n.description}
        <div class="detail-section">
          <div class="detail-label">Description</div>
          <div class="detail-text">{n.description}</div>
        </div>
      {/if}
      {#if n.result}
        <div class="detail-section">
          <div class="detail-label">Result</div>
          <div class="detail-text">{n.result}</div>
        </div>
      {/if}
      {#if n.error}
        <div class="detail-section">
          <div class="detail-label">Error</div>
          <div class="detail-text error-text">{n.error}</div>
        </div>
      {/if}
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

  .chart-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm, 8px);
    padding: 1.25rem;
    margin-top: 1.25rem;
  }

  .section-title {
    font-size: 0.9375rem;
    font-weight: 600;
    color: var(--text);
    margin: 0 0 1rem;
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

  .empty-state {
    color: var(--muted);
    font-size: 0.875rem;
    padding: 1.5rem 0;
    text-align: center;
  }

  /* ── Active DAGs table ── */
  .table-wrap {
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
  }

  .dag-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.875rem;
  }

  .dag-table th,
  .dag-table td {
    text-align: left;
    padding: 0.625rem 0.75rem;
    border-bottom: 1px solid var(--border);
  }

  .dag-table th {
    color: var(--muted);
    font-size: 0.75rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }

  .dag-table tr:hover td {
    background: var(--surface-hover);
  }

  .dag-table tr.selected td {
    background: rgba(34, 211, 238, 0.04);
  }

  .muted {
    color: var(--muted);
  }

  .small {
    font-size: 0.75rem;
  }

  /* ── Status badge ── */
  .status-badge {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 0.6875rem;
    font-weight: 600;
    border: 1px solid transparent;
    white-space: nowrap;
  }

  /* ── Progress bar ── */
  .progress-cell {
    min-width: 120px;
  }

  .progress-bar {
    height: 4px;
    background: var(--border);
    border-radius: 2px;
    overflow: hidden;
    margin-bottom: 2px;
  }

  .progress-fill {
    height: 100%;
    background: var(--accent, #22d3ee);
    border-radius: 2px;
    transition: width 0.3s ease;
  }

  /* ── Graph panel ── */
  .graph-panel {
    margin-top: 1rem;
    border: 1px solid var(--border);
    border-radius: var(--radius-sm, 8px);
    overflow: hidden;
    background: var(--bg, #0f172a);
  }

  .graph-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0.625rem 0.875rem;
    border-bottom: 1px solid var(--border);
    background: var(--surface);
  }

  .graph-dag-name {
    font-size: 0.875rem;
    font-weight: 600;
    color: var(--text);
  }

  .graph-hint {
    text-align: center;
    font-size: 0.6875rem;
    color: var(--muted);
    padding: 0.375rem 0.5rem;
    margin: 0;
    border-top: 1px solid var(--border);
  }

  /* ── Buttons ── */
  .btn-sm {
    display: inline-flex;
    align-items: center;
    padding: 0.25rem 0.625rem;
    font-size: 0.75rem;
    font-weight: 500;
    border-radius: var(--radius-sm, 6px);
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--text);
    cursor: pointer;
    white-space: nowrap;
    line-height: 1.4;
  }

  .btn-sm:hover {
    background: var(--surface-hover);
  }

  .btn-sm.btn-active {
    background: rgba(34, 211, 238, 0.1);
    border-color: rgba(34, 211, 238, 0.4);
    color: #22d3ee;
  }

  /* ── Detail expand (recent DAGs) ── */
  .detail-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
    gap: 0.5rem 1rem;
    padding: 0.75rem 0;
  }

  .detail-grid > div {
    display: flex;
    flex-direction: column;
    gap: 0.125rem;
  }

  .detail-label {
    font-size: 0.6875rem;
    font-weight: 600;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }

  .detail-section {
    margin-top: 0.625rem;
  }

  .detail-text {
    margin-top: 0.25rem;
    font-size: 0.8125rem;
    color: var(--text);
    white-space: pre-wrap;
    word-break: break-word;
  }

  .detail-postmortem {
    border-left: 3px solid #fbbf24;
    padding-left: 0.625rem;
  }

  /* ── Node detail (bottom sheet) ── */
  .node-detail {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    font-size: 0.875rem;
  }

  .node-detail-row {
    display: flex;
    align-items: center;
    gap: 0.75rem;
  }

  .node-detail-row .detail-label {
    min-width: 5rem;
  }

  .error-text {
    color: var(--red, #f87171);
  }
</style>
