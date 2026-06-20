<script lang="ts">
  import { apiGet } from '../lib/api';
  import { makePollStore } from '../lib/stores/registry';
  import { usePoll } from '../lib/poll';
  import type { LedgerData, LedgerAction, LedgerSession } from '../lib/types/api';
  import StatGrid from '../lib/ui/StatGrid.svelte';
  import DataTable from '../lib/ui/DataTable.svelte';
  import StaleBadge from '../lib/ui/StaleBadge.svelte';

  // Poll every 15 s — matches legacy ledger.js setInterval(…, 15000)
  const store = usePoll(
    makePollStore<LedgerData>(
      (signal) => apiGet<LedgerData>('/dashboard/ledger', { signal }),
      15_000,
    ),
  );

  // ── Filters — component-local $state, survive polls automatically ──────────
  // The whole point: no save/restore code. Changing a filter or expanding a
  // session row, then waiting 15 s, preserves both — by construction.
  let statusFilter = $state<string>('all');
  let effectFilter = $state<string>('all');

  const STATUS_OPTIONS = ['all', 'success', 'blocked', 'error', 'timeout'] as const;
  const EFFECT_OPTIONS = ['all', 'none', 'write', 'external', 'irreversible'] as const;

  // ── Derived: aggregate stats ───────────────────────────────────────────────
  const totals = $derived.by(() => {
    const sessions = $store.data?.sessions ?? [];
    let totalActions = 0, totalBlocked = 0, totalErrors = 0, totalTimeouts = 0;
    for (const s of sessions) {
      totalActions += s.total_actions;
      totalBlocked += s.blocked_actions;
      totalErrors += s.error_actions;
      totalTimeouts += s.timeout_actions;
    }
    const successCount = totalActions - totalBlocked - totalErrors - totalTimeouts;
    const successRate = totalActions > 0
      ? Math.round((successCount / totalActions) * 100) + '%'
      : '—';
    return { sessions: sessions.length, totalActions, totalBlocked, totalErrors, totalTimeouts, successRate };
  });

  // ── Helpers ────────────────────────────────────────────────────────────────
  function filterActions(actions: LedgerAction[]): LedgerAction[] {
    return actions.filter((a) => {
      const matchStatus = statusFilter === 'all' || a.status === statusFilter;
      const matchEffect = effectFilter === 'all' || (a.side_effect_type ?? 'none') === effectFilter;
      return matchStatus && matchEffect;
    });
  }

  function formatTime(ts: string): string {
    return new Date(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  }

  function modeClass(mode: string): string {
    if (mode === 'enforce') return 'mode-enforce';
    if (mode === 'warn') return 'mode-warn';
    return 'mode-shadow';
  }

  // ── Table columns for sessions ─────────────────────────────────────────────
  const sessionCols = [
    { key: 'session_id',    label: 'Session ID' },
    { key: 'current_turn',  label: 'Turn' },
    { key: 'total_actions', label: 'Actions' },
    { key: 'blocked_fmt',   label: 'Blocked' },
    { key: 'error_fmt',     label: 'Errors' },
    { key: 'summary',       label: 'Summary' },
  ];
</script>

<header class="view-head">
  <div>
    <h1>Execution Ledger</h1>
    <p class="subtitle">Real-time tool execution tracking and action gating</p>
  </div>
  <StaleBadge state={$store} />
</header>

{#if $store.data}
  {@const d = $store.data}

  <!-- ── Mode banner ────────────────────────────────────────────────────── -->
  <div class="banner" class:banner-disabled={!d.enabled.ledger} class:banner-blocked={totals.totalBlocked > 0}>
    <div class="banner-left">
      <span class="banner-dot"></span>
      <span class="banner-label">{d.enabled.ledger ? 'Ledger Active' : 'Ledger Disabled'}</span>
    </div>
    <div class="banner-modes">
      {#if !d.enabled.claim_verification}
        <span class="mode-pill mode-off">Claim Verification: off</span>
      {:else}
        <span class="mode-pill {modeClass(d.modes.claim_verification)}">
          Claim Verification: {d.modes.claim_verification}
        </span>
      {/if}
      {#if !d.enabled.action_gating}
        <span class="mode-pill mode-off">Action Gating: off</span>
      {:else}
        <span class="mode-pill {modeClass(d.modes.action_gating)}">
          Action Gating: {d.modes.action_gating}
        </span>
      {/if}
    </div>
  </div>

  <!-- ── Stat cards ────────────────────────────────────────────────────── -->
  <StatGrid stats={[
    { label: 'Active sessions', value: totals.sessions },
    { label: 'Total actions',   value: totals.totalActions.toLocaleString() },
    { label: 'Blocked',         value: totals.totalBlocked.toLocaleString() },
    { label: 'Errors',          value: totals.totalErrors.toLocaleString() },
    { label: 'Timeouts',        value: totals.totalTimeouts.toLocaleString() },
    { label: 'Success rate',    value: totals.successRate },
  ]} />

  <!-- ── Global filter bar ─────────────────────────────────────────────── -->
  <div class="filter-bar">
    <div class="filter-group">
      <span class="filter-label">Status</span>
      {#each STATUS_OPTIONS as opt}
        <button
          class="filter-btn"
          class:active={statusFilter === opt}
          onclick={() => { statusFilter = opt; }}
        >{opt}</button>
      {/each}
    </div>
    <div class="filter-group">
      <span class="filter-label">Effect</span>
      {#each EFFECT_OPTIONS as opt}
        <button
          class="filter-btn"
          class:active={effectFilter === opt}
          onclick={() => { effectFilter = opt; }}
        >{opt}</button>
      {/each}
    </div>
  </div>

  <!-- ── Sessions table ────────────────────────────────────────────────── -->
  {#if d.sessions.length === 0}
    <div class="empty-state">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" width="48" height="48" aria-hidden="true">
        <path d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2"/>
      </svg>
      <h3>No Active Sessions</h3>
      <p>Execution data appears here when sessions are active. Auto-refreshes every 15 seconds.</p>
    </div>
  {:else}
    <section class="table-card">
      <h2>Sessions ({d.sessions.length})</h2>
      <DataTable
        columns={sessionCols}
        rows={d.sessions.map((s) => ({
          ...s,
          blocked_fmt: s.blocked_actions > 0 ? s.blocked_actions.toString() : '—',
          error_fmt:   s.error_actions   > 0 ? s.error_actions.toString()   : '—',
        }))}
        mode="cards"
        rowKey={(r: LedgerSession) => r.session_id}
      >
        {#snippet detail(row: LedgerSession)}
          {@const visible = filterActions(row.actions)}
          <div class="session-detail">
            {#if row.actions_truncated}
              <p class="truncated-note">Showing last 50 actions (truncated)</p>
            {/if}
            {#if visible.length === 0}
              <p class="no-actions">No actions match the current filters.</p>
            {:else}
              <!-- Group by turn -->
              {#each [...new Set(visible.map((a) => a.turn))].sort((a, b) => a - b) as turn}
                {@const turnActions = visible.filter((a) => a.turn === turn)}
                <div class="turn-group">
                  <div class="turn-header">
                    <span class="turn-dot"></span>
                    <span class="turn-label">Turn {turn}</span>
                    <span class="turn-count">{turnActions.length} action{turnActions.length !== 1 ? 's' : ''}</span>
                  </div>
                  {#each turnActions as action}
                    <div class="action-row status-{action.status}">
                      <div class="action-left">
                        <span class="action-dot dot-{action.status}"></span>
                        <span class="action-tool">{action.tool_name}</span>
                      </div>
                      <div class="action-args">
                        {#each Object.entries(action.key_args) as [k, v]}
                          <span class="arg"><span class="arg-key">{k}</span>=<span class="arg-val">{v}</span></span>
                        {/each}
                      </div>
                      <div class="action-right">
                        {#if action.side_effect_type && action.side_effect_type !== 'none'}
                          <span class="effect-pill effect-{action.side_effect_type}">{action.side_effect_type}</span>
                        {/if}
                        <span class="status-pill status-pill-{action.status}">{action.status}</span>
                        <span class="action-time">{formatTime(action.timestamp)}</span>
                      </div>
                      {#if action.status !== 'success' && action.result_summary}
                        <div class="action-detail-text">{action.result_summary}</div>
                      {/if}
                    </div>
                  {/each}
                </div>
              {/each}
            {/if}
          </div>
        {/snippet}
      </DataTable>
    </section>
  {/if}

{:else if $store.error}
  <p class="status-msg error">Failed to load execution ledger — retrying…</p>
{:else}
  <p class="status-msg">Loading…</p>
{/if}

<style>
  /* ── Header ────────────────────────────────────────────────────────── */
  .view-head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 1rem;
    margin-bottom: 1.25rem;
  }

  h1 {
    font-size: 1.4rem;
    font-weight: 700;
    color: var(--text);
    margin: 0 0 0.125rem;
  }

  .subtitle {
    font-size: 0.8125rem;
    color: var(--muted);
    margin: 0;
  }

  h2 {
    font-size: 0.875rem;
    font-weight: 600;
    color: var(--text);
    margin: 0 0 0.75rem;
  }

  h3 {
    font-size: 1rem;
    font-weight: 600;
    color: var(--text);
    margin: 0.75rem 0 0.25rem;
  }

  /* ── Mode banner ───────────────────────────────────────────────────── */
  .banner {
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 0.5rem;
    padding: 0.75rem 1rem;
    border-radius: 8px;
    background: var(--surface);
    border: 1px solid var(--border);
    margin-bottom: 1rem;
  }

  .banner-disabled {
    opacity: 0.6;
  }

  .banner-blocked {
    border-color: var(--red, #ef4444);
  }

  .banner-left {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }

  .banner-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--green, #10b981);
    flex-shrink: 0;
  }

  .banner-disabled .banner-dot {
    background: var(--muted);
  }

  .banner-label {
    font-size: 0.875rem;
    font-weight: 600;
    color: var(--text);
  }

  .banner-modes {
    display: flex;
    gap: 0.5rem;
    flex-wrap: wrap;
  }

  .mode-pill {
    font-size: 0.6875rem;
    font-weight: 600;
    padding: 0.125rem 0.5rem;
    border-radius: 999px;
    border: 1px solid currentColor;
  }

  .mode-off      { color: var(--muted); border-color: var(--border); }
  .mode-enforce  { color: var(--green, #10b981); }
  .mode-warn     { color: #f59e0b; }
  .mode-shadow   { color: #60a5fa; }

  /* ── Filter bar ────────────────────────────────────────────────────── */
  .filter-bar {
    display: flex;
    flex-wrap: wrap;
    gap: 1rem;
    padding: 0.75rem 0;
    margin-bottom: 0.5rem;
  }

  .filter-group {
    display: flex;
    align-items: center;
    gap: 0.375rem;
    flex-wrap: wrap;
  }

  .filter-label {
    font-size: 0.75rem;
    font-weight: 600;
    color: var(--muted);
    margin-right: 0.125rem;
  }

  .filter-btn {
    font-size: 0.75rem;
    padding: 0.25rem 0.625rem;
    border-radius: 999px;
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--muted);
    cursor: pointer;
    transition: background 0.1s, color 0.1s, border-color 0.1s;
    white-space: nowrap;
  }

  .filter-btn:hover {
    background: var(--surface-hover);
    color: var(--text);
  }

  .filter-btn.active {
    background: var(--accent);
    border-color: var(--accent);
    color: #fff;
  }

  /* ── Table card ────────────────────────────────────────────────────── */
  .table-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 1rem;
    margin-top: 1rem;
  }

  /* ── Session detail (rendered by DataTable's detail snippet) ───────── */
  .session-detail {
    padding: 0.5rem 0;
  }

  .truncated-note {
    font-size: 0.75rem;
    color: var(--muted);
    font-style: italic;
    margin: 0 0 0.5rem;
  }

  .no-actions {
    font-size: 0.8125rem;
    color: var(--muted);
    padding: 0.5rem 0;
    margin: 0;
  }

  /* ── Turn group ────────────────────────────────────────────────────── */
  .turn-group {
    margin-bottom: 0.75rem;
  }

  .turn-header {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.25rem 0;
    margin-bottom: 0.25rem;
  }

  .turn-dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: var(--accent);
    flex-shrink: 0;
  }

  .turn-label {
    font-size: 0.75rem;
    font-weight: 600;
    color: var(--text);
  }

  .turn-count {
    font-size: 0.6875rem;
    color: var(--muted);
  }

  /* ── Action row ────────────────────────────────────────────────────── */
  .action-row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.5rem;
    padding: 0.4rem 0.5rem;
    border-radius: 6px;
    margin-bottom: 0.25rem;
    background: var(--bg, #0f172a);
    font-size: 0.8125rem;
  }

  .action-left {
    display: flex;
    align-items: center;
    gap: 0.375rem;
    flex-shrink: 0;
    min-width: 0;
  }

  .action-dot {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    flex-shrink: 0;
  }

  .dot-success     { background: var(--green, #10b981); }
  .dot-blocked     { background: #f59e0b; }
  .dot-error       { background: var(--red, #ef4444); }
  .dot-timeout     { background: #dc2626; }

  .action-tool {
    font-family: monospace;
    font-size: 0.8125rem;
    color: var(--text);
    font-weight: 600;
  }

  .action-args {
    display: flex;
    flex-wrap: wrap;
    gap: 0.375rem;
    flex: 1;
    min-width: 0;
  }

  .arg {
    font-size: 0.75rem;
    color: var(--muted);
    font-family: monospace;
  }

  .arg-key {
    color: #60a5fa;
  }

  .arg-val {
    color: var(--text);
  }

  .action-right {
    display: flex;
    align-items: center;
    gap: 0.375rem;
    flex-shrink: 0;
    margin-left: auto;
  }

  .effect-pill {
    font-size: 0.625rem;
    font-weight: 600;
    padding: 0.125rem 0.375rem;
    border-radius: 999px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }

  .effect-write        { background: rgba(96,165,250,0.15); color: #60a5fa; }
  .effect-external     { background: rgba(251,191,36,0.15); color: #f59e0b; }
  .effect-irreversible { background: rgba(239,68,68,0.15);  color: #ef4444; }

  .status-pill {
    font-size: 0.625rem;
    font-weight: 700;
    padding: 0.125rem 0.5rem;
    border-radius: 999px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }

  .status-pill-success { background: rgba(16,185,129,0.15); color: #10b981; }
  .status-pill-blocked { background: rgba(245,158,11,0.15); color: #f59e0b; }
  .status-pill-error   { background: rgba(239,68,68,0.15);  color: #ef4444; }
  .status-pill-timeout { background: rgba(220,38,38,0.15);  color: #dc2626; }

  .action-time {
    font-size: 0.6875rem;
    color: var(--muted);
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }

  .action-detail-text {
    width: 100%;
    font-size: 0.75rem;
    color: var(--muted);
    padding: 0.125rem 0 0 1.25rem;
  }

  /* ── Empty state ───────────────────────────────────────────────────── */
  .empty-state {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: 4rem 2rem;
    color: var(--muted);
    text-align: center;
  }

  .empty-state svg {
    color: var(--muted);
    opacity: 0.4;
    margin-bottom: 0.5rem;
  }

  .empty-state p {
    margin: 0;
    font-size: 0.875rem;
    max-width: 28rem;
  }

  /* ── Generic status messages ───────────────────────────────────────── */
  .status-msg {
    color: var(--muted);
    font-size: 0.9375rem;
    padding: 3rem 2rem;
    text-align: center;
  }

  .status-msg.error {
    color: var(--red, #ef4444);
  }
</style>
