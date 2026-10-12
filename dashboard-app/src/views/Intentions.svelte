<script lang="ts">
  import { apiGet } from '../lib/api';
  import { makePollStore } from '../lib/stores/registry';
  import { usePoll } from '../lib/poll';
  import type { IntentionsData, IntentionNode } from '../lib/types/api';
  import StaleBadge from '../lib/ui/StaleBadge.svelte';
  import FilterBar from '../lib/ui/FilterBar.svelte';
  import { fmtSeconds, countNodes } from '../lib/intentions';

  // F099 Phase 3c: every intention root as a tree, and the metrics beside it. Read-only. Every string a model
  // wrote (intent, note, rationale, arguments, result, title) is shown as a text node: interpolation only.
  let windowSel = $state('7d');
  let stateSel = $state('all');
  const store = usePoll(
    makePollStore<IntentionsData>(
      (signal) => apiGet<IntentionsData>(`/dashboard/intentions?window=${windowSel}&state=${stateSel}`, { signal }),
      30_000,
    ),
  );
  let first = true;
  $effect(() => {
    void windowSel; void stateSel;
    if (first) { first = false; return; }
    void store.refresh();
  });
  const fmt = (n: number) => n.toLocaleString('en-US');
  const when = (iso: string | null) => (iso ? iso.replace('T', ' ').slice(0, 16) + ' UTC' : '—');
  const pretty = (v: unknown) => JSON.stringify(v);
</script>

{#snippet node(n: IntentionNode)}
  <li>
    <div class="row">
      <span class="pill">{n.state}</span>
      <span class="muted small">{n.source_kind}</span>
      <span class="intent">{n.intent}</span>
      {#if n.attempts > 0}<span class="small muted">attempts {n.attempts}</span>{/if}
      {#if n.failed_tokens > 0}<span class="small muted">failed tokens {fmt(n.failed_tokens)}</span>{/if}
    </div>
    {#if n.children.length > 0}
      <ul>{#each n.children as c (c.id)}{@render node(c)}{/each}</ul>
    {/if}
  </li>
{/snippet}

<header class="view-head">
  <div>
    <h1>Intentions</h1>
    <p class="subtitle">Every chain of background work, and how it is going. The window sets the metrics' period; the tree lists the newest roots whatever their age.</p>
  </div>
  <div class="head-right">
    <FilterBar label="Window" required bind:value={windowSel}
      options={[{ value: '24h', label: '24 h' }, { value: '7d', label: '7 d' }, { value: '30d', label: '30 d' }]} />
    <FilterBar label="Roots" required bind:value={stateSel}
      options={[{ value: 'all', label: 'All' }, { value: 'open', label: 'Open' }]} />
    <StaleBadge state={$store} />
  </div>
</header>

{#if $store.data}
  {@const d = $store.data}
  {#if !d.continuation || !d.metrics}
    <div class="notice" role="status">
      Continuation is off (<code>NOUS_CONTINUATION_ENABLED=false</code>), so nothing is read or shown here.
    </div>
  {:else}
    {@const m = d.metrics}
    <section class="panel" aria-labelledby="int-metrics">
      <h2 id="int-metrics">Metrics ({d.window})</h2>
      <dl class="tiles">
        <div><dt>Result to decision, median</dt><dd>{fmtSeconds(m.result_to_decision_latency_s.p50)}</dd></div>
        <div><dt>90th percentile</dt><dd>{fmtSeconds(m.result_to_decision_latency_s.p90)}</dd></div>
        <div><dt>Slowest</dt><dd>{fmtSeconds(m.result_to_decision_latency_s.max)}</dd></div>
        <div><dt>Results lost (target 0)</dt><dd>{m.results_lost}</dd></div>
        <div><dt>Results waiting past {fmt(m.results_lost_grace_s)} s</dt><dd>{m.results_waiting}</dd></div>
        <div><dt>Owner rows never pushed (target 0)</dt><dd>{m.owner_rows_unpushed}</dd></div>
        <div><dt>Proposals approved</dt><dd>{m.proposals_by_state.approved_total}</dd></div>
        <div><dt>Roots expired</dt><dd>{m.escalations_by_cause.expired_roots}</dd></div>
      </dl>
      <p>
        Progress: {m.stalls.progress_claimed} claimed, {m.stalls.progress_credited} credited,
        {m.stalls.claimed_not_credited} claimed but not credited; {m.stalls.verified_no_progress} verified no progress;
        {m.stalls.stall_escalations} ended by the stall budget.
      </p>
      <p class="small muted">{m.arrivals_ungradable} arrivals have no decision record, so calibration cannot grade them.</p>
      <div class="scroll">
        <table>
          <caption>Arrivals by decision and outcome</caption>
          <thead><tr><th>Decision</th><th>Outcome</th><th>Gate</th><th>Count</th></tr></thead>
          <tbody>
            {#each m.arrivals_by_decision_outcome as r}
              <tr><td>{r.decision ?? '—'}</td><td>{r.outcome}</td><td>{r.gated ? 'gate arrival' : ''}</td><td>{r.count}</td></tr>
            {/each}
          </tbody>
        </table>
      </div>
      <div class="scroll">
        <table>
          <caption>Why chains were escalated</caption>
          <thead><tr><th>Cause</th><th>Count</th></tr></thead>
          <tbody>
            {#each Object.entries(m.escalations_by_cause.by_reason) as [reason, n]}
              <tr><td>{reason}</td><td>{n}</td></tr>
            {/each}
          </tbody>
        </table>
      </div>
    </section>

    <section class="panel" aria-labelledby="int-tokens">
      <h2 id="int-tokens">Token spend</h2>
      <p class="small muted">
        Lifetime totals of the {m.token_spend.roots_counted} roots active in the window. The context-log column covers
        continuation turns only (subtask and DAG sessions have their own ids), so it is not the whole of the budget
        column. The budget does not count cached tokens (issue 744), which is what this page is evidence for.
      </p>
      <div class="scroll">
        <table>
          <caption>Budget count against context log</caption>
          <thead><tr><th>Root</th><th>Budget total</th><th>Arrivals out</th><th>Failed (in + out)</th><th>Log output</th><th>Log cache read</th><th>Output not in an arrival</th></tr></thead>
          <tbody>
            <tr>
              <th scope="row">All {m.token_spend.roots_counted}</th>
              <td>{fmt(m.token_spend.budget_counted.total)}</td><td>{fmt(m.token_spend.budget_counted.arrivals_out)}</td>
              <td>{fmt(m.token_spend.failed_tokens)}</td><td>{fmt(m.token_spend.context_log.output)}</td>
              <td>{fmt(m.token_spend.context_log.cache_read)}</td><td>{fmt(m.token_spend.unattributed_output)}</td>
            </tr>
            {#each m.token_spend.top_roots as r (r.root_id)}
              <tr>
                <th scope="row">{r.short_id} {r.intent ?? ''}</th>
                <td>{fmt(r.budget_counted.total)}</td><td>{fmt(r.budget_counted.arrivals_out)}</td>
                <td>{fmt(r.budget_counted.failed_tokens)}</td><td>{fmt(r.context_log.output)}</td>
                <td>{fmt(r.context_log.cache_read)}</td><td>{fmt(r.unattributed_output)}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    </section>

    <section class="panel" aria-labelledby="int-roots">
      <h2 id="int-roots">Roots ({d.roots.length})</h2>
      {#if d.roots.length === 0}<p class="empty">No intention roots yet.</p>{/if}
      {#each d.roots as root (root.id)}
        <details class="root">
          <summary>
            <span class="pill">{root.state}</span>
            <span class="muted small">{root.short_id}</span>
            <span class="intent">{root.intent}</span>
            <span class="small muted">{countNodes(root)} below · {root.arrivals.length} arrivals · {when(root.created_at)}</span>
          </summary>
          <p class="small muted">
            {root.wake_policy} · {root.authority} · from {root.origin_kind}{root.origin_channel ? ` (${root.origin_channel})` : ''}
            {#if root.root_cancelled_at} · cancelled {when(root.root_cancelled_at)}{/if}
            {#if root.root_expired_at} · expired {when(root.root_expired_at)}{/if}
          </p>
          {#if root.limits}
            <p class="small">
              depth {root.limits.depth}, spawns {root.limits.spawns}, turns {root.limits.turns},
              tokens {fmt(root.limits.tokens)}, stalls {root.limits.stalls}
              {#if root.limits.escalate} · limit reached: {root.limits.escalate}{/if}
            </p>
          {/if}
          {#if root.children.length > 0}<ul class="tree">{#each root.children as c (c.id)}{@render node(c)}{/each}</ul>{/if}
          {#if root.arrivals.length > 0}
            <div class="scroll">
              <table>
                <caption>Arrivals</caption>
                <thead><tr><th>n</th><th>Decision</th><th>Outcome</th><th>Gate</th><th>Progress</th><th>Graded</th><th>Latency</th><th>Note</th></tr></thead>
                <tbody>
                  {#each root.arrivals as a (a.id)}
                    <tr>
                      <td>{a.n}</td><td>{a.decision ?? '—'}</td><td>{a.outcome}</td><td>{a.gate_reason ?? ''}</td>
                      <td>{a.progress_claimed === null ? '—' : a.progress_claimed ? 'claimed' : 'not claimed'}{a.progress === null ? '' : a.progress ? ', credited' : ', not credited'}</td>
                      <td>{a.decision_record_id === null ? 'no record' : `${a.decision_outcome ?? 'pending'} (${a.decision_reviewer ?? 'unreviewed'})`}</td>
                      <td>{fmtSeconds(a.result_to_decision_s)}</td><td>{a.note ?? ''}</td>
                    </tr>
                  {/each}
                </tbody>
              </table>
            </div>
          {/if}
          {#if root.proposals.length > 0}
            <div class="scroll">
              <table>
                <caption>Proposals</caption>
                <thead><tr><th>Id</th><th>Tool</th><th>State</th><th>Why</th><th>Arguments</th><th>Result</th></tr></thead>
                <tbody>
                  {#each root.proposals as p (p.id)}
                    <tr><td>{p.short_id}</td><td>{p.tool}</td><td>{p.state}</td><td>{p.rationale}</td><td>{pretty(p.arguments)}</td><td>{p.result ?? ''}</td></tr>
                  {/each}
                </tbody>
              </table>
            </div>
          {/if}
          {#if root.owner_rows.length > 0}
            <div class="scroll">
              <table>
                <caption>Messages to the owner</caption>
                <thead><tr><th>Kind</th><th>Title</th><th>Created</th><th>Pushed</th><th>Delivered</th></tr></thead>
                <tbody>
                  {#each root.owner_rows as r (r.id)}
                    <tr><td>{r.msg_type}</td><td>{r.title}</td><td>{when(r.created_at)}</td><td>{when(r.pushed_at)}</td><td>{when(r.delivered_at)}</td></tr>
                  {/each}
                </tbody>
              </table>
            </div>
          {/if}
        </details>
      {/each}
    </section>
  {/if}
{:else if $store.error}
  <div class="notice" role="alert">Could not load the intentions dashboard: {$store.error.message}</div>
{:else}
  <p class="empty">Loading…</p>
{/if}

<style>
  .view-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 1rem; flex-wrap: wrap; margin-bottom: 1.25rem; }
  .head-right { display: flex; align-items: center; gap: 0.75rem; flex-wrap: wrap; }
  h1 { font-size: 1.375rem; font-weight: 700; margin: 0 0 0.25rem; }
  h2 { font-size: 0.9375rem; font-weight: 600; margin: 0 0 0.75rem; }
  .subtitle { font-size: 0.8125rem; color: var(--muted); margin: 0; }
  .notice { padding: 0.75rem 1rem; border-radius: 8px; background: rgba(245, 158, 11, 0.08);
    border: 1px solid rgba(245, 158, 11, 0.4); font-size: 0.8125rem; margin-bottom: 1rem; }
  .notice code { font-family: var(--font-mono); font-size: 0.75rem; }
  .panel { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 1.25rem; margin-top: 1.25rem; }
  .empty { color: var(--muted); font-size: 0.875rem; text-align: center; padding: 1.5rem 0; margin: 0; }
  .small { font-size: 0.75rem; }
  .muted { color: var(--muted); }
  .tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(11rem, 1fr)); gap: 0.75rem; margin: 0 0 1rem; }
  .tiles div { border: 1px solid var(--border); border-radius: 8px; padding: 0.625rem 0.75rem; }
  .tiles dt { font-size: 0.6875rem; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; }
  .tiles dd { margin: 0.25rem 0 0; font-size: 1.25rem; font-weight: 700; }
  .scroll { overflow-x: auto; margin-top: 0.75rem; }
  table { border-collapse: collapse; width: 100%; font-size: 0.8125rem; }
  caption { text-align: left; font-weight: 600; padding-bottom: 0.375rem; }
  th, td { text-align: left; padding: 0.25rem 0.625rem 0.25rem 0; border-bottom: 1px solid var(--border); vertical-align: top; }
  .tree, .tree ul { list-style: none; padding-left: 1.25rem; }
  .row { display: flex; gap: 0.5rem; align-items: baseline; flex-wrap: wrap; }
  .intent { overflow-wrap: anywhere; }
  .pill { border: 1px solid var(--border); border-radius: 999px; padding: 0 0.5rem; font-size: 0.6875rem; }
  details.root { border-top: 1px solid var(--border); padding: 0.5rem 0; }
  details.root > summary { cursor: pointer; display: flex; gap: 0.5rem; align-items: baseline; flex-wrap: wrap; }
</style>
