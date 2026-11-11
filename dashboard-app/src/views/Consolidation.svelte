<script lang="ts">
  import { fmtUtc } from '../lib/harness';
  import { apiGet } from '../lib/api';
  import { makePollStore } from '../lib/stores/registry';
  import { usePoll } from '../lib/poll';
  import type {
    ActivityData,
    ConsolidationData,
    ConsolidationCycle,
    ConsolidationCycleDetail,
    ConsolidationAction,
  } from '../lib/types/api';
  import StaleBadge from '../lib/ui/StaleBadge.svelte';
  import { Button } from '../lib/ui/button';

  // ── List store (fetch-once + manual refresh; refresh-state preserved by the
  //    shared poll registry, matching the sibling views). ────────────────────
  const store = usePoll(
    makePollStore<ConsolidationData>(
      (signal) => apiGet<ConsolidationData>('/dashboard/consolidation', { signal }),
      0, // fetch-once (manual refresh only)
    ),
  );

  // ── Sleep Activity, moved here from the Activity screen (F108): fetch-once like the list; Refresh reloads both.
  const activity = usePoll(
    makePollStore<ActivityData>(
      (signal) => apiGet<ActivityData>('/dashboard/activity?hours=168', { signal }),
      0, // fetch-once
    ),
  );

  function refreshAll() {
    void store.refresh();
    void activity.refresh();
  }

  function formatDateTime(dateStr: string | null): string {
    return fmtUtc(dateStr);
  }

  // ── Detail drill-through (per-cycle, on-demand fetch with its own state) ───
  let selectedCycleId = $state<string | null>(null);
  let detail = $state<ConsolidationCycleDetail | null>(null);
  let detailLoading = $state(false);
  let detailError = $state(false);

  let detailReq = 0; // monotonic guard against out-of-order responses
  let detailAbort: AbortController | null = null;

  async function loadDetail(cycleId: string) {
    selectedCycleId = cycleId;
    detail = null;
    detailError = false;
    detailLoading = true;

    detailAbort?.abort();
    const ac = new AbortController();
    detailAbort = ac;
    const req = ++detailReq;
    try {
      const data = await apiGet<ConsolidationCycleDetail>(
        `/dashboard/consolidation/${encodeURIComponent(cycleId)}`,
        { signal: ac.signal, retries: 1 },
      );
      if (req !== detailReq) return; // a newer request superseded this one
      detail = data;
    } catch (err) {
      if (req !== detailReq) return;
      detail = { cycle: null, actions: [] };
      detailError = (err as { status?: number }).status !== 404;
    } finally {
      if (req === detailReq) detailLoading = false;
    }
  }

  function closeDetail() {
    detailAbort?.abort();
    detailReq++;
    selectedCycleId = null;
    detail = null;
    detailError = false;
    detailLoading = false;
  }

  // ── Helpers ────────────────────────────────────────────────────────────────
  function statusClass(status: string): string {
    if (status === 'completed') return 'badge-good';
    if (status === 'failed') return 'badge-bad';
    return 'badge-warn'; // running / unknown
  }

  function fmtTs(iso: string | null): string {
    return fmtUtc(iso);
  }

  function shortId(id: string): string {
    return id.length > 8 ? id.slice(0, 8) : id;
  }

  /** Render a totals dict as "merged 1 · superseded 2 · deactivated 4". */
  function totalsSummary(totals: Record<string, number>): string {
    const entries = Object.entries(totals ?? {}).filter(([, v]) => typeof v === 'number');
    if (entries.length === 0) return '—';
    return entries.map(([k, v]) => `${k} ${v}`).join(' · ');
  }

  /** Pretty-print a before/after JSON blob for diff display (a missing side is drawn by the markup, not here). */
  function fmtJson(val: unknown): string {
    if (typeof val === 'string') return val;
    try {
      return JSON.stringify(val, null, 2);
    } catch {
      return String(val);
    }
  }

  // Group a cycle's actions by phase, preserving the backend's created_at-asc
  // order within each phase.
  let groupedActions = $derived.by(() => {
    const actions: ConsolidationAction[] = detail?.actions ?? [];
    const groups = new Map<string, ConsolidationAction[]>();
    for (const a of actions) {
      const phase = a.phase || 'unknown';
      const arr = groups.get(phase);
      if (arr) arr.push(a);
      else groups.set(phase, [a]);
    }
    return [...groups.entries()].map(([phase, items]) => ({ phase, items }));
  });
</script>

<!-- ── Header ──────────────────────────────────────────────────────────── -->
<header class="view-head">
  <div>
    <h1>Consolidation</h1>
    <p class="subtitle">What each sleep cycle merged, superseded and deactivated in memory.</p>
  </div>
  <div class="head-right">
    <StaleBadge state={$store} />
    <Button variant="outline" size="lg" class="px-4 max-md:min-h-11" onclick={refreshAll} disabled={$store.loading}>
      {$store.loading ? 'Loading…' : 'Refresh'}
    </Button>
  </div>
</header>

{#if $store.data}
  {@const cycles = $store.data.cycles}

  <!-- ── Cycle list ──────────────────────────────────────────────────────── -->
  <section class="chart-card">
    <h2>Recent cycles</h2>
    <div class="table-wrap table-scroll">
    <table class="data-table">
      <thead>
        <tr>
          <th>Started</th>
          <th>Status</th>
          <th>Phases</th>
          <th>Totals</th>
          <th>Actions</th>
        </tr>
      </thead>
      <tbody>
        {#each cycles as cycle (cycle.cycle_id)}
          <tr
            class="cycle-row"
            class:selected={selectedCycleId === cycle.cycle_id}
            onclick={() => void loadDetail(cycle.cycle_id)}
          >
            <td>
              <span class="ts">{fmtTs(cycle.started_at)}</span>
              {#if cycle.trace_id}<span class="trace">#{cycle.trace_id}</span>{/if}
            </td>
            <td><span class="badge {statusClass(cycle.status)}">{cycle.status}</span></td>
            <td>
              <span class="chips">
                {#each cycle.phases_run as phase}
                  <span class="chip">{phase}</span>
                {:else}
                  <span class="muted">—</span>
                {/each}
              </span>
            </td>
            <td class="totals-cell">{totalsSummary(cycle.totals)}</td>
            <td>{cycle.action_count.toLocaleString()}</td>
          </tr>
        {:else}
          <tr><td colspan="5" class="empty-cell">No consolidation cycles recorded yet</td></tr>
        {/each}
      </tbody>
    </table>
    </div>
  </section>

  <!-- ── Cycle detail ────────────────────────────────────────────────────── -->
  {#if selectedCycleId}
    <section class="chart-card mt detail-card">
      <div class="detail-head">
        <h2>Cycle {shortId(selectedCycleId)} — Actions</h2>
        <button class="close-btn" onclick={closeDetail} aria-label="Close detail">Close</button>
      </div>

      {#if detailLoading}
        <p class="status-msg">Loading actions…</p>
      {:else if detailError}
        <p class="status-msg error">
          Failed to load cycle —
          <button class="retry-link" onclick={() => selectedCycleId && void loadDetail(selectedCycleId)}>retry</button>
        </p>
      {:else if detail?.cycle === null}
        <p class="status-msg">Cycle not found (it may have been pruned).</p>
      {:else if (detail?.actions ?? []).length === 0}
        <p class="status-msg">This cycle recorded no individual actions.</p>
      {:else}
        {#each groupedActions as group (group.phase)}
          <div class="phase-group">
            <h3 class="phase-head">
              {group.phase}
              <span class="phase-count">{group.items.length}</span>
            </h3>
            {#each group.items as action (action.action_id)}
              <div class="action-row">
                <div class="action-meta">
                  <span class="op">{action.op}</span>
                  {#if action.target_ids.length}
                    <span class="targets">
                      {#each action.target_ids as tid}
                        <span class="chip mono">{shortId(tid)}</span>
                      {/each}
                    </span>
                  {/if}
                </div>
                {#if action.rationale}
                  <p class="rationale">{action.rationale}</p>
                {/if}
                {#if action.before !== null || action.after !== null}
                  <div class="diff">
                    <!-- A missing side is a muted italic word outside the code block (no symbol, review M9), so it
                         never reads like a recorded value "none", which stays in its <pre>. -->
                    <div class="diff-side diff-before">
                      <span class="diff-label">Before</span>
                      {#if action.before == null}<em class="diff-none">none</em>{:else}<pre>{fmtJson(action.before)}</pre>{/if}
                    </div>
                    <div class="diff-side diff-after">
                      <span class="diff-label">After</span>
                      {#if action.after == null}<em class="diff-none">none</em>{:else}<pre>{fmtJson(action.after)}</pre>{/if}
                    </div>
                  </div>
                {/if}
              </div>
            {/each}
          </div>
        {/each}
      {/if}
    </section>
  {/if}

{:else if $store.error}
  <p class="status-msg error">
    Failed to load consolidation data —
    <button class="retry-link" onclick={() => void store.refresh()}>retry</button>
  </p>
{:else}
  <p class="status-msg">Loading…</p>
{/if}

<!-- ── Sleep Activity (moved from the Activity screen, F108) ─────────────── -->
<section class="chart-card mt">
  <h2>Sleep activity</h2>
  {#if $activity.data}
    {@const sl = $activity.data.sleep_stats}
    <dl class="detail-grid">
      <dt>Last sleep</dt>
      <dd>{sl.last_sleep ? formatDateTime(sl.last_sleep) : 'Never'}</dd>
      <dt>Total sleeps</dt>
      <dd>{sl.total_sleeps}</dd>
      <dt>Facts created</dt>
      <dd>{sl.facts_created}</dd>
      <dt>Procedures</dt>
      <dd>{sl.procedures_created}</dd>
      <dt>Censors retired</dt>
      <dd>{sl.censors_retired}</dd>
    </dl>
  {:else if $activity.error}
    <p class="empty error">
      Failed to load activity data —
      <button class="retry-link" onclick={() => void activity.refresh()}>retry</button>
    </p>
  {:else}
    <p class="empty">Loading…</p>
  {/if}
</section>

<style>
  /* ── Header ──────────────────────────────────────────────── */
  .view-head {
    display: flex;
    flex-wrap: wrap; /* at phone width the actions drop below the title rather than widen the page */
    align-items: flex-start;
    justify-content: space-between;
    gap: 1rem;
    margin-bottom: 1.25rem;
  }

  h1 {
    font-size: 30px;
    font-weight: 650;
    color: var(--text);
    margin: 0 0 0.125rem;
    letter-spacing: -0.022em;
    line-height: 1.15;
  }

  .subtitle {
    font-size: 0.8125rem;
    color: var(--muted);
    margin: 0;
  }

  h2 {
    font-size: 15px;
    font-weight: 650;
    color: var(--text);
    margin: 0 0 0.75rem;
  }

  .head-right {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex-shrink: 0;
  }

  /* ── Card ────────────────────────────────────────────────── */
  .chart-card {
    background: var(--surface);
    border: var(--card-border);
    border-radius: var(--card-radius);
    box-shadow: var(--card-shadow);
    padding: 1.125rem 1.25rem;
  }

  .mt { margin-top: 1rem; }

  /* ── Data table ──────────────────────────────────────────── */
  /* A wide table scrolls in its own box instead of widening the page. */
  .table-wrap { overflow-x: auto; }

  .data-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.8125rem;
  }

  .data-table th {
    text-align: left;
    padding: 0.375rem 0.5rem;
    font-size: 0.75rem;
    font-weight: 600;
    color: var(--muted);
    border-bottom: 1px solid var(--border);
  }

  .data-table td {
    padding: 0.5rem;
    color: var(--text);
    border-bottom: 1px solid var(--border);
    vertical-align: top;
  }

  .data-table tbody tr:last-child td { border-bottom: none; }

  .cycle-row { cursor: pointer; }
  .cycle-row:hover td { background: var(--surface-hover); }
  .cycle-row.selected td { background: var(--accent-glow); }

  .ts { display: block; }
  .trace {
    font-size: 0.7rem;
    color: var(--muted);
    font-family: var(--font-mono, monospace);
  }

  .totals-cell { color: var(--muted); }

  .empty-cell {
    text-align: center;
    color: var(--muted);
    font-style: italic;
    padding: 1rem 0.5rem;
  }

  .muted { color: var(--muted); }

  /* ── Chips ───────────────────────────────────────────────── */
  .chips, .targets {
    display: inline-flex;
    flex-wrap: wrap;
    gap: 0.25rem;
  }

  .chip {
    display: inline-block;
    padding: 0.1em 0.45em;
    border-radius: 999px;
    background: var(--surface-hover);
    border: 1px solid var(--border);
    color: var(--text);
    font-size: 0.7rem;
    font-weight: 500;
    white-space: nowrap;
  }

  .chip.mono { font-family: var(--font-mono, monospace); }

  /* ── Badge ───────────────────────────────────────────────── */
  .badge {
    display: inline-block;
    padding: 0.15em 0.55em;
    border-radius: 999px;
    font-size: 0.75rem;
    font-weight: 600;
    white-space: nowrap;
  }

  .badge-good { background: var(--badge-good-bg); color: var(--ok-text); }
  .badge-warn { background: var(--badge-warn-bg); color: var(--warn-text); }
  .badge-bad  { background: var(--badge-bad-bg); color: var(--err-text); }

  /* ── Detail card ─────────────────────────────────────────── */
  .detail-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    margin-bottom: 0.75rem;
  }
  .detail-head h2 { margin: 0; }

  .close-btn {
    font-size: 0.75rem;
    padding: 0.2rem 0.6rem;
    border: 1px solid var(--border);
    border-radius: var(--ctl-radius);
    background: var(--surface);
    color: var(--muted);
    cursor: pointer;
  }
  .close-btn:hover { background: var(--surface-hover); color: var(--text); }

  /* ── Phase group ─────────────────────────────────────────── */
  .phase-group { margin-bottom: 1rem; }
  .phase-group:last-child { margin-bottom: 0; }

  .phase-head {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    font-size: 0.8125rem;
    font-weight: 600;
    color: var(--text);
    text-transform: capitalize;
    margin: 0 0 0.5rem;
    padding-bottom: 0.25rem;
    border-bottom: 1px solid var(--border);
  }

  .phase-count {
    font-size: 0.7rem;
    font-weight: 600;
    color: var(--muted);
    background: var(--surface-hover);
    border-radius: 999px;
    padding: 0.05em 0.5em;
  }

  /* ── Action row ──────────────────────────────────────────── */
  .action-row {
    padding: 0.625rem;
    border: 1px solid var(--border);
    border-radius: var(--card-radius-sm);
    margin-bottom: 0.5rem;
    background: var(--bg, var(--surface));
  }
  .action-row:last-child { margin-bottom: 0; }

  .action-meta {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 0.5rem;
    margin-bottom: 0.375rem;
  }

  .op {
    font-size: 0.75rem;
    font-weight: 700;
    color: var(--accent);
    text-transform: none;
    letter-spacing: 0.02em;
  }

  .rationale {
    font-size: 0.8125rem;
    color: var(--text);
    margin: 0 0 0.5rem;
  }

  /* ── Diff ────────────────────────────────────────────────── */
  /* Before and After side by side; their labels say which is which. */
  .diff {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
    gap: 0.5rem;
    align-items: stretch;
  }

  @media (max-width: 700px) {
    .diff { grid-template-columns: minmax(0, 1fr); }
  }

  .diff-side {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
    min-width: 0;
  }

  .diff-label {
    font-size: 0.7rem;
    font-weight: 600;
    color: var(--muted);
    text-transform: none;
  }

  .diff-none {
    font-size: 0.75rem;
    color: var(--muted);
    font-style: italic;
  }

  .diff pre {
    margin: 0;
    padding: 0.5rem;
    border-radius: var(--card-radius-sm);
    border: 1px solid var(--border);
    background: var(--surface);
    font-family: var(--font-mono, monospace);
    font-size: 0.7rem;
    color: var(--text);
    white-space: pre-wrap;
    word-break: break-word;
    overflow-wrap: anywhere;
    overflow-x: auto;
  }

  /* Before and after are tinted blocks (the err and ok tints), not side stripes; the labels name them. */
  .diff-before pre { background: var(--err-bg); }
  .diff-after pre  { background: var(--ok-bg); }

  /* ── Status messages ─────────────────────────────────────── */
  .status-msg {
    color: var(--muted);
    font-size: 0.9375rem;
    padding: 2rem 1rem;
    text-align: center;
    margin: 0;
  }

  .status-msg.error { color: var(--err-text); }

  /* ── Sleep Activity (from Activity) ──────────────────────── */
  .detail-grid {
    display: grid;
    grid-template-columns: auto 1fr;
    gap: 0.375rem 1rem;
    font-size: 0.8125rem;
    margin: 0;
  }

  .detail-grid dt {
    color: var(--muted);
    font-weight: 500;
  }

  .detail-grid dd {
    color: var(--text);
    margin: 0;
  }

  .empty {
    color: var(--muted);
    font-size: 0.875rem;
    padding: 1rem 0;
    text-align: center;
    margin: 0;
  }

  .empty.error { color: var(--err-text); }

  .retry-link {
    background: none;
    border: none;
    color: var(--accent);
    cursor: pointer;
    font-size: inherit;
    padding: 0;
    text-decoration: underline;
  }
</style>
