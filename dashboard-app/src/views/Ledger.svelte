<script lang="ts">
  import { apiGet } from '../lib/api';
  import { makePollStore } from '../lib/stores/registry';
  import { usePoll } from '../lib/poll';
  import { pushCounts } from '../lib/stores/attention';
  import type { ExecutionData, ExecutionRow } from '../lib/types/api';
  import StatGrid from '../lib/ui/StatGrid.svelte';
  import DataTable from '../lib/ui/DataTable.svelte';
  import FilterBar from '../lib/ui/FilterBar.svelte';
  import StaleBadge from '../lib/ui/StaleBadge.svelte';
  import { ledgerStatusColor, badgeStyle } from '../lib/status';
  import { fmtWhen, fmtUtc, ledgerTarget, recipients } from '../lib/harness';

  // Harness dashboard §3.2 / §4: the durable execution ledger. Filters are
  // component-local $state and survive polls by construction.
  let windowSel = $state('24h');
  let context = $state('');
  let status = $state('');
  let effectSel = $state('');
  let q = $state('');

  const CONTEXTS = ['interactive', 'mcp', 'subtask', 'dag_node', 'scheduled', 'agent_action',
    'heartbeat_triage', 'heartbeat_check', 'heartbeat_callback', 'dag_summary', 'background'];
  const STATUSES = ['pending', 'success', 'error', 'blocked', 'unknown'];
  const EFFECTS = ['write', 'external', 'irreversible'];

  function query(before: string | null = null): string {
    const p = new URLSearchParams({ window: windowSel, limit: '50' });
    if (context) p.set('context', context);
    if (status) p.set('status', status);
    if (effectSel) p.set('effect', effectSel);
    if (q.trim()) p.set('q', q.trim().slice(0, 100));
    if (before) p.set('before', before);
    return `/dashboard/execution?${p}`;
  }

  const store = usePoll(
    makePollStore<ExecutionData>((signal) => apiGet<ExecutionData>(query(), { signal }), 15_000),
  );

  // Refetch on a filter change (the poll itself keeps the head fresh).
  let first = true;
  $effect(() => {
    void [windowSel, context, status, effectSel, q];
    if (first) { first = false; return; }
    older = [];
    olderCursor = null;
    paused = false;
    store.start();
    void store.refresh();
  });

  // Keep the nav badge in step with what this tab shows.
  $effect(() => {
    const d = $store.data;
    if (d) pushCounts({ sends: d.attention.length });
  });

  // "Load older" pauses polling, so rows never shift under the reader.
  let older = $state<ExecutionRow[]>([]);
  let olderCursor = $state<string | null>(null);
  let paused = $state(false);
  let loadingOlder = $state(false);

  async function loadOlder() {
    const cursor = olderCursor ?? $store.data?.next_before ?? null;
    if (!cursor) return;
    loadingOlder = true;
    paused = true;
    store.stop();
    try {
      const page = await apiGet<ExecutionData>(query(cursor));
      older = [...older, ...page.rows];
      olderCursor = page.next_before;
    } finally {
      loadingOlder = false;
    }
  }

  function backToLatest() {
    older = [];
    olderCursor = null;
    paused = false;
    store.start();
    void store.refresh();
  }

  let rows = $derived([...($store.data?.rows ?? []), ...older]);
  let canLoadOlder = $derived(paused ? olderCursor !== null : ($store.data?.next_before ?? null) !== null);

  // ── Copy an operator statement (Clipboard API needs a secure context; the
  //    LAN host serves plain http, so fall back to selecting the text).
  let copied = $state('');
  async function copySql(id: string, el: HTMLElement | null) {
    const text = el?.textContent ?? '';
    try {
      await navigator.clipboard.writeText(text);
      copied = 'Copied to the clipboard.';
    } catch {
      if (el) {
        const range = document.createRange();
        range.selectNodeContents(el);
        const sel = window.getSelection();
        sel?.removeAllRanges();
        sel?.addRange(range);
      }
      copied = 'Selected — press Ctrl+C (or ⌘C) to copy.';
    }
    lastCopied = id;
  }
  let lastCopied = $state('');

  const cols = [
    { key: 'when', label: 'When' },
    { key: 'tool', label: 'Tool' },
    { key: 'context', label: 'Context' },
    { key: 'effect', label: 'Effect' },
    { key: 'status', label: 'Status' },
    { key: 'target', label: 'Target' },
  ];

  function stats(d: ExecutionData) {
    const s = d.stats;
    return [
      { label: 'Side-effecting calls', value: s.calls, note: `in ${windowSel.replace('h', ' h').replace('d', ' d')}` },
      { label: 'Sends', value: s.sends, note: 'send_email + send_file' },
      { label: 'Repeat sends refused', value: s.repeat_sends_refused, note: 'the key was already held' },
      { label: 'Blocked by a rule', value: s.blocked, note: 'offered-tool · policy · gate' },
      { label: 'Unknown outcome', value: s.unknown, note: `${s.unknown_keyed} hold a send`, tone: s.unknown ? 'unknown' as const : undefined },
      { label: 'Errors', value: s.errors, note: 'failed calls', tone: s.errors ? 'error' as const : undefined },
    ];
  }

  function modePillClass(mode: string): string {
    if (mode === 'enforce') return 'mode-enforce';
    if (mode === 'warn' || mode === 'shadow') return 'mode-warn';
    return 'mode-off';
  }

  const sqlEls: Record<string, HTMLElement | null> = {};
</script>

<header class="view-head">
  <div>
    <h1>Execution Ledger</h1>
    <p class="subtitle">Every side-effecting tool call, kept durably — it survives restarts and ended sessions</p>
  </div>
  <StaleBadge state={$store} />
</header>

{#if $store.data}
  {@const d = $store.data}

  <div class="banner" class:banner-off={!d.modes.persist}>
    <div class="banner-left">
      <span class="banner-dot" aria-hidden="true"></span>
      <span class="banner-label">{d.modes.persist ? 'Ledger persisting' : 'Ledger persistence is off'}</span>
      {#if d.modes.persist}<span class="banner-note">· {d.modes.retention_days}-day retention · send de-duplication on</span>{/if}
    </div>
    <div class="banner-modes">
      <span class="mode-pill {modePillClass(d.modes.offered_set)}">Offered-tool rule: {d.modes.offered_set}</span>
      <span class="mode-pill {modePillClass(d.modes.context_policy)}">Context policy: {d.modes.context_policy}</span>
      <span class="mode-pill {modePillClass(d.modes.claim_verification)}">Claim checks: {d.modes.claim_verification}</span>
      <span class="mode-pill {modePillClass(d.modes.action_gating)}">Action gating: {d.modes.action_gating}</span>
    </div>
  </div>

  {#if !d.modes.persist}
    <p class="state-msg">Nothing below is new: with NOUS_EXECUTION_LEDGER_PERSIST_ENABLED off, no calls are recorded and sends are not de-duplicated.</p>
  {/if}

  {#if d.attention.length > 0}
    <section class="attention" aria-labelledby="attn-title">
      <h2 id="attn-title">
        {d.attention.length} send{d.attention.length === 1 ? '' : 's'} ended without confirming delivery
      </h2>
      <p class="attn-lede">
        The outcome is <strong class="unknown-text">unknown</strong>, so each keeps its duplicate-send hold: a retry of
        that send is refused until you record what happened. Check whether the recipients got it.
      </p>
      {#each d.attention as r (r.id)}
        {@const to = recipients(r.key_args)}
        <div class="attn-row">
          <dl class="attn-facts">
            <dt>Tool</dt><dd class="mono">{r.tool_name}</dd>
            <dt>To</dt><dd>{r.tombstone ? 'recipients no longer stored (retention)' : to.length ? to.join(', ') : ledgerTarget(r.key_args)}</dd>
            {#if r.dag_name}<dt>From</dt><dd>DAG {r.dag_name}{r.node_name ? ` · ${r.node_name}` : ''}</dd>{/if}
            <dt>When</dt><dd>{fmtWhen(r.created_at)}</dd>
            {#if r.external_ref}<dt>Provider ref</dt><dd class="mono">{r.external_ref}</dd>{/if}
            {#if r.result_summary}<dt>Stored note</dt><dd class="mono">{r.result_summary}</dd>{/if}
          </dl>
          <div class="attn-actions">
            <div>
              <div class="sql-title">They got it — keep the hold</div>
              <code class="sql" bind:this={sqlEls[`ok-${r.id}`]}>UPDATE nous_system.execution_ledger SET status = 'success', result_summary = 'confirmed delivered by operator'
WHERE id = '{r.id}' AND status = 'unknown';</code>
            </div>
            <div>
              <div class="sql-title">Nobody got it — release the hold</div>
              <code class="sql" bind:this={sqlEls[`rel-${r.id}`]}>UPDATE nous_system.execution_ledger SET status = 'error', result_summary = 'released by operator'
WHERE id = '{r.id}' AND status = 'unknown';</code>
            </div>
            <p class="note">Releasing re-sends nothing — retry the step to send again. If only some recipients got it, keep the hold.</p>
            <div class="btn-row">
              <button type="button" class="btn" onclick={() => copySql(`ok-${r.id}`, sqlEls[`ok-${r.id}`])}>Copy “got it”</button>
              <button type="button" class="btn" onclick={() => copySql(`rel-${r.id}`, sqlEls[`rel-${r.id}`])}>Copy “nobody got it”</button>
            </div>
          </div>
        </div>
      {/each}
      <p class="sr-only" aria-live="polite">{lastCopied ? copied : ''}</p>
    </section>
  {/if}

  <StatGrid stats={stats(d)} />

  <div class="filters">
    <div class="filter-group">
      <span class="filter-label" id="win-label">Window</span>
      <FilterBar label="Window" required bind:value={windowSel}
        options={[{ value: '24h', label: '24 h' }, { value: '7d', label: '7 d' }, { value: '30d', label: '30 d' }]} />
    </div>
    <label class="filter-group">
      <span class="filter-label">Context</span>
      <select bind:value={context}>
        <option value="">All contexts</option>
        {#each CONTEXTS as c}<option value={c}>{c}</option>{/each}
      </select>
    </label>
    <label class="filter-group">
      <span class="filter-label">Status</span>
      <select bind:value={status}>
        <option value="">All statuses</option>
        {#each STATUSES as s}<option value={s}>{s}</option>{/each}
      </select>
    </label>
    <label class="filter-group">
      <span class="filter-label">Effect</span>
      <select bind:value={effectSel}>
        <option value="">All effects</option>
        {#each EFFECTS as e}<option value={e}>{e}</option>{/each}
      </select>
    </label>
    <label class="search">
      <span class="sr-only">Search the ledger</span>
      <input type="search" maxlength="100" placeholder="tool, recipient, key, message id…" bind:value={q} />
    </label>
  </div>

  <section class="table-card">
    <div class="table-head">
      <h2>Calls</h2>
      <span class="small muted">Free-text arguments are stored as sha256 + length; tool output is never stored.</span>
    </div>
    {#if rows.length === 0}
      <p class="empty">{d.modes.persist ? 'No side-effecting calls match these filters.' : 'Nothing is being recorded.'}</p>
    {:else}
      <DataTable
        columns={cols}
        rows={rows}
        mode="cards"
        rowKey={(r: ExecutionRow) => r.id}
        rowLabel={(r: ExecutionRow) => `${r.tool_name} at ${fmtUtc(r.created_at)}`}
      >
        {#snippet cell(r: ExecutionRow, c: { key: string })}
          {#if c.key === 'when'}
            <span class="mono small">{fmtUtc(r.created_at)}</span>
          {:else if c.key === 'tool'}
            <span class="mono">{r.tool_name}</span>
          {:else if c.key === 'context'}
            <span class="small muted">{r.context_kind}</span>
          {:else if c.key === 'effect'}
            <span class="pill effect-{r.side_effect_type}">{r.side_effect_type}</span>
          {:else if c.key === 'status'}
            <span class="pill" style={badgeStyle(ledgerStatusColor(r.status))}>{r.status}</span>
            {#if r.refusal_code}<span class="small muted"> {r.refusal_code}</span>{/if}
            {#if r.status === 'unknown'}<span class="small muted"> {r.idempotency_key ? 'holds a send' : 'nothing held'}</span>{/if}
          {:else if c.key === 'target'}
            <span class="target">{ledgerTarget(r.key_args, r.tombstone)}</span>
          {/if}
        {/snippet}
        {#snippet detail(r: ExecutionRow)}
          <dl class="detail-grid">
            {#if r.held_by}
              <div><dt>{r.status === 'blocked' ? 'Refused as a repeat of' : 'Key currently held by'}</dt>
                <dd>row {r.held_by.id.slice(0, 8)} · {r.held_by.status} (since {fmtUtc(r.held_by.created_at)})</dd></div>
            {/if}
            {#if r.idempotency_key}<div><dt>Idempotency key</dt><dd class="mono wrap">{r.idempotency_key}</dd></div>{/if}
            <div><dt>Context</dt><dd>{r.context_kind}{r.dag_name ? ` · ${r.dag_name}${r.node_name ? ` / ${r.node_name}` : ''}` : ''}{r.turn != null ? ` · turn ${r.turn}` : ''}</dd></div>
            {#if r.session_id}<div><dt>Session</dt><dd class="mono wrap">{r.session_id}{r.parent_session_id ? ` (parent ${r.parent_session_id})` : ''}</dd></div>{/if}
            <div><dt>Key args</dt><dd class="mono wrap">{r.tombstone ? 'removed by retention' : JSON.stringify(r.key_args)}</dd></div>
            {#if r.result_summary}<div><dt>Stored note</dt><dd class="mono wrap">{r.result_summary}</dd></div>{/if}
            {#if r.external_ref}<div><dt>Provider ref</dt><dd class="mono wrap">{r.external_ref}</dd></div>{/if}
            <div><dt>Recorded</dt><dd>{fmtWhen(r.created_at)}{r.completed_at ? ` · closed ${fmtUtc(r.completed_at)}` : ''}</dd></div>
          </dl>
        {/snippet}
      </DataTable>
    {/if}
    <div class="table-foot">
      {#if paused}
        <span class="small muted">Paused while you view older rows.</span>
        <button type="button" class="btn" onclick={backToLatest}>Back to latest</button>
      {:else}
        <span class="small muted">Newest first · refreshes every 15 s</span>
      {/if}
      {#if canLoadOlder}
        <button type="button" class="btn" onclick={loadOlder} disabled={loadingOlder}>{loadingOlder ? 'Loading…' : 'Load older'}</button>
      {/if}
    </div>
  </section>
{:else if $store.error}
  <p class="state-msg error">Failed to load the execution ledger — retrying…</p>
{:else}
  <p class="state-msg">Loading…</p>
{/if}

<style>
  .view-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 1rem; margin-bottom: 1.25rem; }
  h1 { font-size: 1.375rem; font-weight: 700; color: var(--text); margin: 0 0 0.125rem; }
  .subtitle { font-size: 0.8125rem; color: var(--muted); margin: 0; }
  h2 { font-size: 0.9375rem; font-weight: 600; color: var(--text); margin: 0; }

  .banner { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 0.5rem;
    padding: 0.75rem 1rem; border-radius: 8px; background: var(--surface); border: 1px solid var(--border); margin-bottom: 1rem; }
  .banner-left { display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; }
  .banner-dot { width: 8px; height: 8px; border-radius: 50%; background: #10b981; flex-shrink: 0; }
  .banner-off .banner-dot { background: #f59e0b; }
  .banner-label { font-size: 0.875rem; font-weight: 600; }
  .banner-note { font-size: 0.75rem; color: var(--muted); }
  .banner-modes { display: flex; gap: 0.5rem; flex-wrap: wrap; }
  .mode-pill { font-size: 0.6875rem; font-weight: 600; padding: 0.125rem 0.5rem; border-radius: 999px; border: 1px solid currentColor; }
  .mode-off { color: var(--muted); border-color: var(--border); }
  .mode-enforce { color: #10b981; }
  .mode-warn { color: #f59e0b; }

  .attention { border-radius: 8px; background: rgba(244, 114, 182, 0.06); border: 1px solid rgba(244, 114, 182, 0.35);
    padding: 1rem 1.125rem; margin-bottom: 1rem; display: flex; flex-direction: column; gap: 0.75rem; }
  .attn-lede { font-size: 0.8125rem; margin: 0; }
  .unknown-text { color: var(--unknown); }
  .attn-row { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1.1fr); gap: 1.5rem; align-items: start;
    padding-top: 0.75rem; border-top: 1px solid rgba(244, 114, 182, 0.2); }
  .attn-facts { display: grid; grid-template-columns: 6rem minmax(0, 1fr); gap: 0.25rem 0.75rem; font-size: 0.8125rem; margin: 0; }
  .attn-facts dt { color: var(--muted); }
  .attn-facts dd { margin: 0; overflow-wrap: anywhere; }
  .attn-actions { display: flex; flex-direction: column; gap: 0.625rem; }
  .sql-title { font-size: 0.8125rem; font-weight: 600; }
  .sql { display: block; margin-top: 0.25rem; padding: 0.5rem 0.625rem; border-radius: 8px; background: var(--bg);
    border: 1px solid var(--border); font-family: var(--font-mono); font-size: 0.6875rem; line-height: 1.6;
    white-space: pre-wrap; overflow-wrap: anywhere; }
  .note { font-size: 0.75rem; color: var(--muted); margin: 0; }
  .btn-row { display: flex; gap: 0.5rem; flex-wrap: wrap; }
  .btn { min-height: 32px; padding: 0 0.75rem; border-radius: 8px; border: 1px solid var(--border); background: var(--surface);
    color: var(--text); font-family: inherit; font-size: 0.75rem; font-weight: 500; cursor: pointer; }
  .btn:hover { background: var(--surface-hover); }
  .btn:focus-visible { outline: 2px solid var(--accent-text); outline-offset: 2px; }
  .btn:disabled { opacity: 0.6; cursor: default; }

  .filters { display: flex; flex-wrap: wrap; align-items: center; gap: 0.75rem 1.25rem; margin: 1rem 0 0.75rem; }
  .filter-group { display: flex; align-items: center; gap: 0.5rem; }
  .filter-label { font-size: 0.75rem; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; }
  select, input[type='search'] { min-height: 32px; padding: 0 0.625rem; border-radius: 8px; border: 1px solid var(--border);
    background: var(--surface); color: var(--text); font-family: inherit; font-size: 0.8125rem; }
  .search { margin-left: auto; flex: 1 1 14rem; max-width: 18rem; }
  .search input { width: 100%; border-radius: 999px; }

  .table-card { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 1.25rem; }
  .table-head { display: flex; align-items: baseline; justify-content: space-between; gap: 1rem; flex-wrap: wrap; margin-bottom: 0.75rem; }
  .table-foot { display: flex; align-items: center; justify-content: space-between; gap: 0.75rem; flex-wrap: wrap; margin-top: 0.75rem; }
  .empty { color: var(--muted); font-size: 0.875rem; text-align: center; padding: 1.5rem 0; margin: 0; }
  .pill { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 0.6875rem; font-weight: 600;
    white-space: nowrap; border: 1px solid transparent; }
  .effect-write { background: rgba(96, 165, 250, 0.15); color: #60a5fa; }
  .effect-external { background: rgba(251, 191, 36, 0.15); color: #f59e0b; }
  .effect-irreversible { background: rgba(248, 113, 113, 0.15); color: var(--red); }
  .target { overflow-wrap: anywhere; font-size: 0.8125rem; }
  .detail-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 0.625rem 1.5rem; margin: 0; padding: 0.5rem 0; }
  .detail-grid dt { font-size: 0.6875rem; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; }
  .detail-grid dd { margin: 0.125rem 0 0; font-size: 0.8125rem; }
  .mono { font-family: var(--font-mono); font-size: 0.75rem; }
  .wrap { overflow-wrap: anywhere; }
  .small { font-size: 0.75rem; }
  .muted { color: var(--muted); }
  .state-msg { margin: 0.5rem 0 1rem; color: var(--muted); font-size: 0.875rem; }
  .state-msg.error { color: var(--red); }
  .sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }

  @media (max-width: 768px) {
    .attn-row { grid-template-columns: 1fr; }
    .search { margin-left: 0; max-width: none; }
    .btn { min-height: 44px; }
    select, input[type='search'] { min-height: 44px; }
  }
</style>
