<script lang="ts">
  import { apiGet } from '../lib/api';
  import { makePollStore } from '../lib/stores/registry';
  import { usePoll } from '../lib/poll';
  import type {
    RetrievalData,
    RetrievalDetail,
    RetrievalCandidate,
    RetrievalExpansion,
  } from '../lib/types/api';
  import StaleBadge from '../lib/ui/StaleBadge.svelte';

  let pathFilter = $state<'' | 'pipeline' | 'context'>('');

  const store = usePoll(
    makePollStore<RetrievalData>(
      (signal) => apiGet<RetrievalData>('/dashboard/retrieval', { signal }),
      0, // fetch-once + manual refresh, matching sibling views
    ),
  );

  // ── Detail drill-through (own state, out-of-order guarded) ────────────────
  let selectedId = $state<string | null>(null);
  let detail = $state<RetrievalDetail | null>(null);
  let detailLoading = $state(false);
  let detailError = $state(false);

  let detailReq = 0;
  let detailAbort: AbortController | null = null;

  async function loadDetail(entryId: string) {
    selectedId = entryId;
    detail = null;
    detailError = false;
    detailLoading = true;

    detailAbort?.abort();
    const ac = new AbortController();
    detailAbort = ac;
    const req = ++detailReq;
    try {
      const data = await apiGet<RetrievalDetail>(
        `/dashboard/retrieval/${encodeURIComponent(entryId)}`,
        { signal: ac.signal, retries: 1 },
      );
      if (req !== detailReq) return;
      detail = data;
    } catch (err) {
      if (req !== detailReq) return;
      detail = null;
      detailError = (err as { status?: number }).status !== 404;
    } finally {
      if (req === detailReq) detailLoading = false;
    }
  }

  function closeDetail() {
    detailAbort?.abort();
    detailReq++;
    selectedId = null;
    detail = null;
    detailError = false;
    detailLoading = false;
  }

  // ── Helpers ──────────────────────────────────────────────────────────────
  function fmtTs(iso: string | null): string {
    if (!iso) return '—';
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
  }

  function fmtScore(n: number | null | undefined): string {
    return n === null || n === undefined ? '—' : n.toFixed(3);
  }

  function fmtMs(n: number | null): string {
    return n === null || n === undefined ? '—' : `${Math.round(n)} ms`;
  }

  function shortId(id: string, n = 8): string {
    return id.length > n ? id.slice(0, n) : id;
  }

  /**
   * `rendered` is the only good outcome; `unaccounted` means a filter dropped
   * something without reporting why, which is a defect signal rather than a
   * category of drop — both get distinct treatment from ordinary drops.
   */
  function dispositionClass(d: string): string {
    if (d === 'rendered') return 'badge-good';
    if (d === 'unaccounted') return 'badge-bad';
    return 'badge-warn';
  }

  const DISPOSITION_HELP: Record<string, string> = {
    rendered: 'Reached the model',
    sliced_off: 'Fell outside a top-K or max-K cut',
    below_floor: 'Failed a similarity or score floor',
    filter_dropped: 'Removed by a named filter',
    budget_truncated: 'Dropped by token-budget truncation',
    f071_excluded: 'Already in this turn’s system prompt',
    deduped: 'Same id already present from another leg',
    superseded: 'Demoted by the recency resolver',
    replaced_at_merge: 'Stripped by exemplar replace-at-merge',
    type_excluded: 'Its whole type was removed before search',
    unaccounted: 'No stage claimed this — a filter is not reporting',
  };

  /** Ordered so `rendered` leads and the defect signal sorts last. */
  function orderDispositions(keys: string[]): string[] {
    return [...keys].sort((a, b) => {
      if (a === 'rendered') return -1;
      if (b === 'rendered') return 1;
      if (a === 'unaccounted') return 1;
      if (b === 'unaccounted') return -1;
      return a.localeCompare(b);
    });
  }

  let filteredEntries = $derived(
    ($store.data?.entries ?? []).filter((e) => !pathFilter || e.path === pathFilter),
  );

  let totals = $derived.by(() => {
    const t = $store.data?.disposition_totals ?? {};
    const sum = Object.values(t).reduce((a, b) => a + b, 0);
    return { entries: Object.entries(t), sum };
  });

  /** Group expansion edges by seed so the traversal reads as a tree. */
  let expansionsBySeed = $derived.by(() => {
    const rows: RetrievalExpansion[] = detail?.expansions ?? [];
    const groups = new Map<string, { seed: RetrievalExpansion; items: RetrievalExpansion[] }>();
    for (const r of rows) {
      const g = groups.get(r.seed_id);
      if (g) g.items.push(r);
      else groups.set(r.seed_id, { seed: r, items: [r] });
    }
    return [...groups.values()];
  });

  let detailDispositions = $derived(
    orderDispositions(Object.keys(detail?.candidates_by_disposition ?? {})),
  );
</script>

<header class="view-head">
  <div>
    <h1>Retrieval</h1>
    <p class="subtitle">
      What memory recall retrieved — and which gate dropped everything it didn’t
    </p>
  </div>
  <div class="head-right">
    <button class="refresh-btn" onclick={() => void store.refresh()} disabled={$store.loading}>
      {$store.loading ? 'Loading…' : 'Refresh'}
    </button>
    <StaleBadge state={$store} />
  </div>
</header>

{#if $store.data}
  <!-- ── Window rollup: systemic drops visible without opening a detail ──── -->
  <section class="chart-card">
    <h2>Dispositions across {$store.data.count} retrievals</h2>
    {#if totals.sum === 0}
      <p class="status-msg">No candidates captured yet in this window.</p>
    {:else}
      <div class="disp-bar" role="img" aria-label="Candidate dispositions">
        {#each orderDispositions(totals.entries.map(([k]) => k)) as key (key)}
          {@const val = $store.data.disposition_totals[key] ?? 0}
          <div
            class="disp-seg {dispositionClass(key)}"
            style="flex-grow: {val}"
            title="{key}: {val} ({((val / totals.sum) * 100).toFixed(1)}%)"
          ></div>
        {/each}
      </div>
      <ul class="disp-legend">
        {#each orderDispositions(totals.entries.map(([k]) => k)) as key (key)}
          {@const val = $store.data.disposition_totals[key] ?? 0}
          <li>
            <span class="dot {dispositionClass(key)}"></span>
            <span class="k">{key}</span>
            <span class="v">{val.toLocaleString()}</span>
            <span class="pct">{((val / totals.sum) * 100).toFixed(1)}%</span>
            <span class="help">{DISPOSITION_HELP[key] ?? ''}</span>
          </li>
        {/each}
      </ul>
    {/if}
  </section>

  <!-- ── Leg rollup ──────────────────────────────────────────────────────── -->
  <section class="chart-card mt">
    <h2>Legs</h2>
    <table class="data-table">
      <thead>
        <tr>
          <th>Leg</th>
          <th>Retrievals attempted</th>
          <th>Items returned</th>
          <th>Deduped (corroboration)</th>
          <th>Errors</th>
        </tr>
      </thead>
      <tbody>
        {#each Object.entries($store.data.leg_totals) as [name, agg] (name)}
          <tr>
            <td><code>{name}</code></td>
            <td>{agg.attempted.toLocaleString()}</td>
            <td>{agg.returned.toLocaleString()}</td>
            <td>{agg.deduped.toLocaleString()}</td>
            <td class:err={agg.errors > 0}>{agg.errors.toLocaleString()}</td>
          </tr>
        {:else}
          <tr><td colspan="5" class="empty-cell">No legs recorded yet</td></tr>
        {/each}
      </tbody>
    </table>
  </section>

  <!-- ── Retrieval list ──────────────────────────────────────────────────── -->
  <section class="chart-card mt">
    <div class="detail-head">
      <h2>Recent Retrievals</h2>
      <div class="filters">
        <button class="chip-btn" class:on={pathFilter === ''} onclick={() => (pathFilter = '')}>All</button>
        <button class="chip-btn" class:on={pathFilter === 'pipeline'} onclick={() => (pathFilter = 'pipeline')}>
          recall_deep
        </button>
        <button class="chip-btn" class:on={pathFilter === 'context'} onclick={() => (pathFilter = 'context')}>
          pre-turn context
        </button>
      </div>
    </div>
    <table class="data-table">
      <thead>
        <tr>
          <th>When</th>
          <th>Path</th>
          <th>Query</th>
          <th>In → Out</th>
          <th>Expansions</th>
          <th>Duration</th>
        </tr>
      </thead>
      <tbody>
        {#each filteredEntries as e (e.id)}
          <tr class="cycle-row" class:selected={selectedId === e.id} onclick={() => void loadDetail(e.id)}>
            <td><span class="ts">{fmtTs(e.timestamp)}</span></td>
            <td><span class="chip">{e.path === 'context' ? 'pre-turn' : 'recall_deep'}</span></td>
            <td class="query-cell" title={e.query ?? ''}>{e.query || '—'}</td>
            <td>
              {#if e.has_candidates}
                <span class="io">{e.n_candidates} → <strong>{e.n_rendered}</strong></span>
              {:else}
                <span class="muted" title="Not sampled for candidate capture">not sampled</span>
              {/if}
            </td>
            <td>{e.n_expansions.toLocaleString()}</td>
            <td>{fmtMs(e.duration_ms)}</td>
          </tr>
        {:else}
          <tr><td colspan="6" class="empty-cell">No retrievals recorded yet</td></tr>
        {/each}
      </tbody>
    </table>
  </section>

  <!-- ── Detail ──────────────────────────────────────────────────────────── -->
  {#if selectedId}
    <section class="chart-card mt detail-card">
      <div class="detail-head">
        <h2>Retrieval {shortId(selectedId)}</h2>
        <button class="close-btn" onclick={closeDetail} aria-label="Close detail">Close</button>
      </div>

      {#if detailLoading}
        <p class="status-msg">Loading retrieval…</p>
      {:else if detailError}
        <p class="status-msg error">
          Failed to load —
          <button class="retry-link" onclick={() => selectedId && void loadDetail(selectedId)}>retry</button>
        </p>
      {:else if detail === null}
        <p class="status-msg">Retrieval not found (it may have been pruned).</p>
      {:else}
        <p class="detail-query"><strong>Query:</strong> {detail.query || '—'}</p>

        {#if detail.excluded_types.length > 0}
          <p class="status-msg warn">
            Excluded before search:
            {#each detail.excluded_types as x (x.type)}
              <span class="chip">{x.type} <span class="muted">({x.stage})</span></span>
            {/each}
          </p>
        {/if}

        <!-- Graph expansion -->
        <h3 class="phase-head">Graph expansion</h3>
        {#if expansionsBySeed.length === 0}
          <p class="status-msg">No graph expansion ran for this retrieval.</p>
        {:else}
          {#each expansionsBySeed as group (group.seed.seed_id)}
            <div class="seed-group">
              <div class="seed-head">
                <span class="chip">{group.seed.seed_type}</span>
                <code>{shortId(group.seed.seed_id)}</code>
                <span class="muted">seed score {fmtScore(group.seed.seed_score)}</span>
                <span class="muted">· {group.items.length} neighbour{group.items.length === 1 ? '' : 's'}</span>
              </div>
              <ul class="edge-list">
                {#each group.items as edge (edge.neighbor_id + edge.stage)}
                  <li class:lost={!edge.won_best_path}>
                    <span class="rel">{edge.edge_relation ?? 'related'}</span>
                    <span class="arrow">→</span>
                    <span class="chip">{edge.neighbor_type}</span>
                    <code>{shortId(edge.neighbor_id)}</code>
                    <span class="muted">w {fmtScore(edge.edge_weight)}</span>
                    <span class="muted">· composed {fmtScore(edge.composed_score)}</span>
                    <span class="muted">· hop {edge.hop}</span>
                    {#if edge.extraction_method}<span class="chip sm">{edge.extraction_method}</span>{/if}
                    {#if !edge.won_best_path}<span class="chip sm">lost best-path</span>{/if}
                  </li>
                {/each}
              </ul>
            </div>
          {/each}
        {/if}

        <!-- Candidates -->
        <h3 class="phase-head">Candidates</h3>
        {#if detail.candidates_by_disposition === null}
          <p class="status-msg">
            This retrieval was not sampled for candidate capture, so per-item detail was
            never recorded. Legs and graph expansion above are complete.
          </p>
        {:else}
          {#each detailDispositions as disp (disp)}
            {@const items = detail.candidates_by_disposition[disp] ?? []}
            <div class="phase-group">
              <h4 class="disp-head">
                <span class="badge {dispositionClass(disp)}">{disp}</span>
                <span class="count">{items.length}</span>
                <span class="help">{DISPOSITION_HELP[disp] ?? ''}</span>
              </h4>
              <table class="data-table compact">
                <thead>
                  <tr>
                    <th>Type</th>
                    <th>Entry leg</th>
                    <th>Entry score</th>
                    <th>Final rank</th>
                    <th>Dropped at</th>
                    <th>Content</th>
                  </tr>
                </thead>
                <tbody>
                  {#each items as c (c.id + c.type)}
                    <tr>
                      <td><span class="chip">{c.type}</span></td>
                      <td><code>{c.entry_leg}</code></td>
                      <td>
                        {fmtScore(c.entry_score)}
                        {#each c.mutations as m (m.stage)}
                          <span class="mut" title="{m.stage}: {fmtScore(m.score_before)} → {fmtScore(m.score_after)}">
                            → {fmtScore(m.score_after)}
                          </span>
                        {/each}
                      </td>
                      <td>{c.final_rank ?? '—'}</td>
                      <td>{c.disposition_stage ? `${c.disposition_stage}` : '—'}</td>
                      <td class="snippet-cell" title={c.snippet}>{c.snippet || '—'}</td>
                    </tr>
                  {/each}
                </tbody>
              </table>
            </div>
          {/each}
        {/if}
      {/if}
    </section>
  {/if}
{:else if $store.error}
  <p class="status-msg error">Failed to load retrieval telemetry.</p>
{:else}
  <p class="status-msg">Loading…</p>
{/if}

<style>
  .disp-bar {
    display: flex;
    height: 22px;
    border-radius: 4px;
    overflow: hidden;
    margin-bottom: 0.9rem;
  }
  .disp-seg { min-width: 2px; }
  .disp-seg.badge-good { background: var(--good, #3f9d55); }
  .disp-seg.badge-warn { background: var(--warn, #c08a2e); }
  .disp-seg.badge-bad  { background: var(--bad, #b04a45); }

  .disp-legend { list-style: none; margin: 0; padding: 0; }
  .disp-legend li {
    display: grid;
    grid-template-columns: 12px 11rem 5rem 4rem 1fr;
    gap: 0.6rem;
    align-items: center;
    padding: 0.18rem 0;
    font-size: 0.86rem;
  }
  .dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; }
  .dot.badge-good { background: var(--good, #3f9d55); }
  .dot.badge-warn { background: var(--warn, #c08a2e); }
  .dot.badge-bad  { background: var(--bad, #b04a45); }
  .disp-legend .k { font-family: var(--mono, monospace); }
  .disp-legend .v { text-align: right; font-variant-numeric: tabular-nums; }
  .disp-legend .pct { text-align: right; opacity: 0.7; font-variant-numeric: tabular-nums; }
  .disp-legend .help { opacity: 0.6; }

  .filters { display: flex; gap: 0.4rem; }
  .chip-btn {
    border: 1px solid var(--border, #444);
    background: transparent;
    color: inherit;
    border-radius: 999px;
    padding: 0.15rem 0.7rem;
    font-size: 0.8rem;
    cursor: pointer;
  }
  .chip-btn.on { background: var(--accent-bg, rgba(255, 255, 255, 0.12)); }

  .query-cell,
  .snippet-cell {
    max-width: 26rem;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .io { font-variant-numeric: tabular-nums; }
  .err { color: var(--bad, #b04a45); }

  .detail-query { margin: 0.2rem 0 0.8rem; }

  .seed-group { margin: 0.5rem 0 0.9rem; }
  .seed-head { display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap; font-size: 0.88rem; }
  .edge-list { list-style: none; margin: 0.35rem 0 0; padding: 0 0 0 1.1rem; border-left: 2px solid var(--border, #444); }
  .edge-list li {
    display: flex;
    gap: 0.45rem;
    align-items: center;
    flex-wrap: wrap;
    padding: 0.18rem 0;
    font-size: 0.84rem;
  }
  .edge-list li.lost { opacity: 0.55; }
  .rel { font-family: var(--mono, monospace); color: var(--accent, #7aa2f7); }
  .arrow { opacity: 0.5; }

  .disp-head { display: flex; gap: 0.6rem; align-items: center; margin: 0.9rem 0 0.35rem; font-size: 0.95rem; }
  .disp-head .count { font-variant-numeric: tabular-nums; opacity: 0.8; }
  .disp-head .help { font-weight: 400; opacity: 0.6; font-size: 0.82rem; }

  .mut { font-size: 0.78rem; opacity: 0.7; margin-left: 0.2rem; }
  .chip.sm { font-size: 0.72rem; }
  .muted { opacity: 0.6; }
  .mt { margin-top: 1rem; }
</style>
