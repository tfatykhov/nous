<script lang="ts">
  import { apiGet } from '../lib/api';
  import { makePollStore } from '../lib/stores/registry';
  import { usePoll } from '../lib/poll';
  import type { DagDashboardData, DagActiveDag, DagRecentDag, DagActiveNode, DagWaiting } from '../lib/types/api';
  import { statusColor, badgeStyle, WAITING } from '../lib/status';
  import { fmtUtc, relUntil, isSoon } from '../lib/harness';
  import { pushCounts } from '../lib/stores/attention';
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

  // ── Status colours: ONE map shared with the graph (lib/status.ts) ───────────

  // Keep the nav badge in step with this tab (harness dashboard §3.4).
  $effect(() => {
    const d = $store.data;
    if (d) pushCounts({ questions: d.stats.waiting_count ?? 0 });
  });

  // ── Approval steps (harness dashboard §3.1 / §4) ─────────────────────────
  function fullNode(nodeId: string): DagActiveNode | null {
    for (const dag of $store.data?.active_dags ?? []) {
      const n = dag.nodes.find((x) => x.id === nodeId);
      if (n) return n;
    }
    return null;
  }

  function openWaiting(w: DagWaiting) {
    const n = fullNode(w.node_id);
    if (!n) return;
    selectedNode = {
      id: n.id, name: n.name, status: n.status, node_type: n.node_type, wave: n.wave,
      started_at: n.started_at ?? undefined, completed_at: n.completed_at ?? undefined,
      tokens_used: n.tokens_used, description: n.description, result: n.result, error: n.error,
    };
    sheetOpen = true;
  }

  function answerLine(a: NonNullable<DagActiveNode['approval']>): string {
    if (a.answer_source === 'companion') {
      const opt = a.options.find((o) => o.id === a.answer);
      const verdict = opt?.outcome === 'proceed' ? 'approved' : 'declined';
      return `${verdict} — '${a.answer_label}' in the companion${a.answered_by ? ` by ${a.answered_by}` : ''} at ${fmtUtc(a.answered_at)}`;
    }
    if (a.answer_source === 'deadline') {
      return `no answer by ${fmtUtc(a.deadline)}; default '${a.answer_label}' applied`;
    }
    return 'not answered yet';
  }

  function attemptLine(t: NonNullable<DagActiveNode['approval']>['attempts'][number]): string {
    if (t.answer_source === 'deadline') return `no answer — default '${t.label}' applied at ${fmtUtc(t.answered_at)}`;
    const verdict = t.outcome === 'proceed' ? 'approved' : 'declined';
    return `${verdict} — '${t.label}' in the companion${t.answered_by ? ` by ${t.answered_by}` : ''} at ${fmtUtc(t.answered_at)}`;
  }

  /** Why an active DAG is not moving — the "Now" column. */
  function nowOf(dag: DagActiveDag): { text: string; kind: 'waiting' | 'held' | 'plain' } {
    if (dag.waiting > 0) {
      const q = dag.nodes.find((n) => n.status === 'awaiting_input');
      return { text: `Question · ${q?.name ?? ''}`.trim(), kind: 'waiting' };
    }
    if (dag.held_reason) return { text: dag.held_reason, kind: 'held' };
    const running = dag.nodes.filter((n) => n.status === 'running').map((n) => n.name);
    if (running.length) return { text: `Running · ${running.join(', ')}`, kind: 'plain' };
    return { text: '—', kind: 'plain' };
  }

  // ── Stat grid ─────────────────────────────────────────────────────────────
  let stats = $derived(() => {
    const s = $store.data?.stats;
    if (!s) return [];
    const successPct = s.success_rate != null ? Math.round(s.success_rate * 100) + '%' : '--';
    return [
      { label: 'Active DAGs', value: String(s.active_count) },
      { label: 'Waiting on you', value: String(s.waiting_count ?? 0), tone: (s.waiting_count ? 'waiting' : undefined) as 'waiting' | undefined },
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
        status: dag.waiting > 0 ? 'waiting on you' : dag.status,
        statusCol: dag.waiting > 0 ? WAITING : statusColor(dag.status),
        now: nowOf(dag),
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
    { key: 'summary', label: 'Summary' },
    { key: 'nodes', label: 'Nodes' },
    { key: 'tokens', label: 'Tokens' },
    { key: 'completed', label: 'Completed' },
  ];

  let recentRows = $derived(
    ($store.data?.recent_dags ?? []).map((dag) => ({
      _dag: dag,
      name: dag.name,
      statusBadge: dag.stopped_by ? 'stopped at approval' : dag.status,
      summary: dag.result_summary ?? '',
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

  {#if data.waiting_on_you.length > 0}
    <section class="waiting" aria-labelledby="waiting-title">
      <div class="waiting-head">
        <h2 id="waiting-title" class="section-title">
          <span class="dot" aria-hidden="true"></span>Waiting on you
          <span class="count" aria-label="{data.waiting_on_you.length} questions">{data.waiting_on_you.length}</span>
        </h2>
        <p class="small muted">A DAG resumes when you answer its card — or takes its default at the deadline.</p>
      </div>
      {#each data.waiting_on_you as w (w.node_id)}
        <div class="q-row">
          <div class="q-main">
            <div class="q-question">{w.question || w.node_name}</div>
            <div class="small muted">
              <span class="text">{w.dag_name}</span> · step <span class="mono">{w.node_name}</span>
              {#if w.reviewing.length} · reviewing <span class="mono">{w.reviewing.join(', ')}</span>{/if}
            </div>
            {#if w.card_error}
              <div class="q-error">{w.card_error}</div>
            {:else if !w.card_url}
              <div class="small muted">Card being delivered…</div>
            {/if}
          </div>
          <div class="q-when">
            <div class="label">Answer by</div>
            <div>{fmtUtc(w.deadline)}</div>
            <div class="small" class:soon={isSoon(w.deadline)} class:muted={!isSoon(w.deadline)}>{relUntil(w.deadline)}</div>
          </div>
          <div class="q-default">
            <div class="label">If no answer</div>
            <div>'{w.default_label}'</div>
            <div class="small muted">the DAG stops here</div>
          </div>
          <div class="q-actions">
            {#if w.card_url}
              <a class="btn-primary" href={w.card_url} target="_blank" rel="noopener">Answer in companion</a>
            {/if}
            <button type="button" class="btn-sm btn-tall" onclick={() => openWaiting(w)}>Details</button>
          </div>
        </div>
      {/each}
    </section>
  {/if}

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
              <th>Now</th>
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
                  <span class="status-badge" style={badgeStyle(row.statusCol)}>{row.status}</span>
                </td>
                <td class="now-cell">
                  <span class="now now-{row.now.kind}">{row.now.text}</span>
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
          <p class="graph-hint">Tap a node for details, or pick one below</p>
          <ul class="node-list" aria-label="Nodes in {activeDag.name}">
            {#each toVizNodes(activeDag) as n (n.id)}
              <li>
                <button type="button" class="node-btn" onclick={() => onNodeClick(n)}>
                  <span class="mono">{n.name}</span>
                  <span class="small muted">{n.node_type}</span>
                  <span class="status-badge" style={badgeStyle(statusColor(n.status))}>{n.status}</span>
                </button>
              </li>
            {/each}
          </ul>
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
        rowLabel={(r) => r.name}
        mode="scroll"
      >
        {#snippet cell(row, c)}
          {#if c.key === 'statusBadge'}
            {@const col = row._dag.stopped_by ? WAITING : statusColor(row._dag.status)}
            <span class="status-badge" style={badgeStyle(col)}>{row.statusBadge}</span>
          {:else if c.key === 'summary'}
            {#if row._dag.stops.length}
              <span class="small muted">{row._dag.stops.map((s: { node_name: string; answer_source: string; answer_label: string }) =>
                s.answer_source === 'companion' ? `${s.node_name}: declined '${s.answer_label}'` : `${s.node_name}: no answer, default '${s.answer_label}' applied`,
              ).join(' · ')}</span>
            {:else}
              <span class="small muted">{row.summary}</span>
            {/if}
          {:else}
            {row[c.key]}
          {/if}
        {/snippet}
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
        <span class="status-badge" style={badgeStyle(statusColor(n.status))}>{n.status}</span>
      </div>
      {#if fullNode(n.id)?.approval}
        {@const a = fullNode(n.id)!.approval!}
        <div class="detail-section">
          <div class="detail-label">Question</div>
          <div class="detail-text q-question">{a.question}</div>
        </div>
        <div class="detail-section">
          <div class="detail-label">What the card shows</div>
          <div class="detail-text card-summary">{a.card_summary}</div>
          {#if a.reviewing.length}<div class="small muted">Reviewing the output of {a.reviewing.join(', ')}.</div>{/if}
        </div>
        <div class="detail-section">
          <div class="detail-label">Options</div>
          <div class="opts">
            {#each a.options as o (o.id)}
              <span class="opt opt-{o.outcome}">{o.label} — {o.outcome === 'proceed' ? 'continues' : 'stops here'}</span>
            {/each}
            <span class="opt opt-default">default: {a.default_label}</span>
          </div>
        </div>
        <dl class="approval-dl">
          <div><dt>Asked</dt><dd>{fmtUtc(a.asked_at)}</dd></div>
          <div><dt>Answer by</dt><dd>{fmtUtc(a.deadline)}{#if !a.answer} ({relUntil(a.deadline)}){/if}</dd></div>
          <div><dt>If no answer</dt><dd>'{a.default_label}' — the DAG stops here</dd></div>
          <div><dt>Answer</dt><dd>{answerLine(a)}</dd></div>
        </dl>
        {#if a.card_url}
          <a class="card-link" href={a.card_url} target="_blank" rel="noopener">Open card in companion</a>
        {:else if a.card_error}
          <p class="q-error">{a.card_error}</p>
        {:else if !a.answer}
          <p class="small muted">Card being delivered…</p>
        {/if}
        {#if a.attempts.length}
          <div class="detail-section">
            <div class="detail-label">Earlier attempts</div>
            <ol class="attempts">
              {#each a.attempts as t, i (i)}<li>{attemptLine(t)}</li>{/each}
            </ol>
          </div>
        {/if}
      {/if}
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

  /* ── Waiting on you (harness dashboard §4) ── */
  .waiting {
    background: var(--surface);
    border: 1px solid rgba(167, 139, 250, 0.35);
    box-shadow: 0 0 0 4px rgba(167, 139, 250, 0.06);
    border-radius: var(--radius-sm, 8px);
    padding: 1.25rem;
    margin-bottom: 1.25rem;
    display: flex;
    flex-direction: column;
    gap: 0.625rem;
  }
  .waiting-head { display: flex; align-items: center; justify-content: space-between; gap: 1rem; flex-wrap: wrap; }
  .waiting .section-title { display: flex; align-items: center; gap: 0.5rem; margin: 0; }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--waiting); }
  .count { min-width: 1.25rem; height: 1.25rem; padding: 0 0.375rem; border-radius: 999px; background: var(--waiting);
    color: var(--bg); font-size: 0.6875rem; font-weight: 700; display: inline-flex; align-items: center; justify-content: center; }
  .q-row { display: grid; grid-template-columns: minmax(0, 1fr) auto auto auto; gap: 1.25rem; align-items: center;
    padding: 0.875rem 1rem; border-radius: 8px; background: var(--bg); border: 1px solid var(--border); }
  .q-question { font-size: 0.9375rem; font-weight: 600; }
  .q-error { font-size: 0.8125rem; color: #f59e0b; margin: 0.25rem 0 0; }
  .q-when, .q-default { font-size: 0.875rem; min-width: 9rem; }
  .label { font-size: 0.6875rem; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; }
  .soon { color: #fbbf24; font-weight: 600; }
  .text { color: var(--text); }
  .mono { font-family: var(--font-mono); font-size: 0.75rem; }
  .q-actions { display: flex; gap: 0.5rem; justify-content: flex-end; flex-wrap: wrap; }
  .btn-primary { display: inline-flex; align-items: center; min-height: 36px; padding: 0 0.875rem; border-radius: 8px;
    background: var(--accent); color: var(--bg); font-size: 0.8125rem; font-weight: 700; text-decoration: none; white-space: nowrap; }
  .btn-primary:focus-visible, .node-btn:focus-visible, .card-link:focus-visible { outline: 2px solid var(--accent-text); outline-offset: 2px; }
  .btn-tall { min-height: 36px; }
  .now { font-size: 0.75rem; }
  .now-waiting { display: inline-block; padding: 3px 10px; border-radius: 999px; font-weight: 600; color: #c4b5fd; background: rgba(167, 139, 250, 0.14); }
  .now-held { display: inline-block; padding: 3px 10px; border-radius: 999px; font-weight: 600; color: #fbbf24; background: rgba(251, 191, 36, 0.10); }
  .now-plain { color: var(--muted); }
  .node-list { list-style: none; margin: 0; padding: 0.5rem; display: flex; flex-wrap: wrap; gap: 0.5rem; border-top: 1px solid var(--border); }
  .node-btn { display: inline-flex; align-items: center; gap: 0.5rem; padding: 0.375rem 0.625rem; border-radius: 8px;
    border: 1px solid var(--border); background: var(--surface); color: var(--text); font-family: inherit; cursor: pointer; }
  .card-summary { font-size: 0.8125rem; white-space: pre-wrap; padding: 0.625rem 0.75rem; border-radius: 8px;
    background: var(--bg); border: 1px solid var(--border); }
  .opts { display: flex; flex-wrap: wrap; gap: 0.375rem; margin-top: 0.25rem; }
  .opt { padding: 3px 10px; border-radius: 999px; font-size: 0.75rem; font-weight: 600; border: 1px solid; }
  .opt-proceed { color: #4ade80; border-color: rgba(74, 222, 128, 0.4); }
  .opt-stop { color: var(--red); border-color: rgba(248, 113, 113, 0.4); }
  .opt-default { color: var(--muted); border-color: var(--border); font-weight: 500; }
  .approval-dl { display: grid; gap: 0.5rem; margin: 0.75rem 0 0; }
  .approval-dl div { display: grid; grid-template-columns: 7rem minmax(0, 1fr); gap: 0.75rem; }
  .approval-dl dt { font-size: 0.6875rem; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; }
  .approval-dl dd { margin: 0; font-size: 0.875rem; }
  .card-link { display: inline-block; margin-top: 0.75rem; font-size: 0.8125rem; font-weight: 600; color: var(--accent-text); }
  .attempts { margin: 0.25rem 0 0; padding-left: 1.25rem; font-size: 0.8125rem; display: grid; gap: 0.25rem; }

  @media (max-width: 900px) {
    .q-row { grid-template-columns: 1fr; gap: 0.625rem; }
    .q-actions { justify-content: stretch; }
    .q-actions > * { flex: 1; justify-content: center; min-height: 44px; }
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
