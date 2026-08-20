<script lang="ts">
  import { apiGet } from '../lib/api';
  import { makePollStore } from '../lib/stores/registry';
  import { usePoll } from '../lib/poll';
  import type {
    RetrievalData,
    RetrievalDetail,
    RetrievalEntry,
    RetrievalExpansion,
  } from '../lib/types/api';
  import StaleBadge from '../lib/ui/StaleBadge.svelte';

  // Server-side, not a client filter over the fetched page: the `context` path
  // fires every turn while recall_deep is occasional, so the recent-50 window
  // is routinely 100% context — filtering locally showed an empty table while
  // pipeline rows sat pages back. Refetch on change so the rollups match too.
  let pathFilter = $state<'' | 'pipeline' | 'context'>('');
  // Which path the data currently in the store was actually fetched for.
  // Stamped when a request STARTS, so it always describes the in-flight or
  // most-recent fetch rather than the selection.
  let appliedPath: '' | 'pipeline' | 'context' = '';
  // Heartbeat turns are machine chatter, not questions anyone asked. They
  // dominate the window (17 of 21 on the instance this was designed against),
  // so an operator debugging their own question wades through noise. Filtered
  // CLIENT-side on purpose: the server has no notion of "agent-initiated", and
  // the rollups above deliberately keep covering the whole window.
  let hideAutomated = $state(true);

  const store = usePoll(
    makePollStore<RetrievalData>(
      (signal) => {
        appliedPath = pathFilter;
        return apiGet<RetrievalData>(
          `/dashboard/retrieval${pathFilter ? `?path=${pathFilter}` : ''}`,
          { signal },
        );
      },
      0, // fetch-once + manual refresh, matching sibling views
    ),
  );

  function setPath(p: '' | 'pipeline' | 'context') {
    if (p === pathFilter) return;
    pathFilter = p;
    void store.refresh();
  }

  // makePollStore.refresh() returns immediately when a request is already in
  // flight — it does not queue. In fetch-once mode (intervalMs=0) nothing
  // re-arms, so a filter change during the initial load, or rapid switching,
  // would leave the selected chip paired with the previous path's rows
  // indefinitely. Reconcile once the in-flight request settles.
  $effect(() => {
    if (!$store.loading && appliedPath !== pathFilter) {
      void store.refresh();
    }
  });

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
  function fmtClock(iso: string | null): string {
    if (!iso) return '—';
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    return d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
  }

  /** Relative age. The absolute stamp repeated 50x is noise; recency is signal. */
  function fmtAge(iso: string | null): string {
    if (!iso) return '';
    const t = new Date(iso).getTime();
    if (Number.isNaN(t)) return '';
    const s = Math.max(0, (Date.now() - t) / 1000);
    if (s < 60) return 'just now';
    if (s < 3600) return `${Math.floor(s / 60)}m ago`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
    return `${Math.floor(s / 86400)}d ago`;
  }

  function fmtScore(n: number | null | undefined): string {
    return n === null || n === undefined ? '—' : n.toFixed(3);
  }

  function fmtMs(n: number | null): string {
    if (n === null || n === undefined) return '—';
    return n >= 1000 ? `${(n / 1000).toFixed(1)}s` : `${Math.round(n)}ms`;
  }

  function shortId(id: string, n = 8): string {
    return id.length > n ? id.slice(0, n) : id;
  }

  /**
   * Agent-initiated turns announce themselves in the query text. Callbacks
   * ("Dynamic Check Callback") are the same class of machine chatter as the
   * checks themselves and were missed by an earlier heartbeat-only pattern.
   */
  const AUTOMATED_RE = /^\s*\[(dynamic (heartbeat check|check callback)|heartbeat)\b/i;
  function isAutomated(q: string | null): boolean {
    return AUTOMATED_RE.test(q ?? '');
  }

  /** Strip the machine prefix so the actual subject is what you read. */
  function queryLabel(q: string | null): string {
    if (!q) return '(no query)';
    const m = q.match(
      /^\s*\[(?:Dynamic Heartbeat Check|Dynamic Check Callback|Heartbeat):?\s*([^\]]*)\]\s*(.*)$/i,
    );
    return m ? (m[1].trim() || m[2]?.trim() || '(no query)') : q;
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

  // Server already applied `path`; the only client filter is the automated
  // toggle, which the server cannot express.
  let filteredEntries = $derived(
    ($store.data?.entries ?? []).filter(
      (e) => !hideAutomated || !isAutomated(e.query),
    ),
  );
  let hiddenCount = $derived(
    ($store.data?.entries ?? []).length - filteredEntries.length,
  );

  let totals = $derived.by(() => {
    const t = $store.data?.disposition_totals ?? {};
    const sum = Object.values(t).reduce((a, b) => a + b, 0);
    return { entries: Object.entries(t), sum };
  });

  /** Per-row funnel segments, so the list is scannable without drilling in. */
  function funnelSegments(e: RetrievalEntry) {
    const counts = e.disposition_counts ?? {};
    return orderDispositions(Object.keys(counts)).map((k) => ({
      key: k,
      val: counts[k] ?? 0,
      cls: dispositionClass(k),
    }));
  }

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

  /** Legs that produced nothing are the interesting ones; sort them up. */
  let sortedLegs = $derived.by(() => {
    const legs = [...($store.data ? Object.entries($store.data.leg_totals) : [])];
    return legs.sort((a, b) => {
      if ((b[1].errors > 0 ? 1 : 0) !== (a[1].errors > 0 ? 1 : 0)) {
        return (b[1].errors > 0 ? 1 : 0) - (a[1].errors > 0 ? 1 : 0);
      }
      return b[1].returned - a[1].returned;
    });
  });
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
  <!-- ── Window funnel: the whole window as one in → out ──────────────────── -->
  <section class="chart-card funnel-card">
    <div class="funnel-head">
      <h2>Candidate flow</h2>
      <!-- Denominator is the SAMPLED count, not the window count: dispositions
           only exist on sampled rows, so claiming the full window here would
           understate every rate by 1/sample_rate. -->
      <span class="muted sm">
        {$store.data.sampled_count ?? 0} sampled of {$store.data.count} retrievals
      </span>
    </div>

    {#if totals.sum === 0}
      <p class="status-msg">
        No candidates captured yet in this window — candidate detail is sampled,
        so a quiet bar here does not mean retrieval found nothing.
      </p>
    {:else}
      {@const rendered = $store.data.disposition_totals['rendered'] ?? 0}
      <div class="funnel-figures">
        <div class="fig">
          <span class="fig-n">{totals.sum.toLocaleString()}</span>
          <span class="fig-l">entered</span>
        </div>
        <span class="fig-arrow">→</span>
        <div class="fig">
          <span class="fig-n good">{rendered.toLocaleString()}</span>
          <span class="fig-l">reached the model</span>
        </div>
        <div class="fig drop">
          <span class="fig-n warn">{(totals.sum - rendered).toLocaleString()}</span>
          <span class="fig-l">dropped at a gate</span>
        </div>
      </div>

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

  <!-- ── Master / detail ─────────────────────────────────────────────────── -->
  <div class="split mt">
    <section class="chart-card list-card">
      <div class="list-head">
        <h2>Retrievals</h2>
        <div class="filters">
          <button class="chip-btn" class:on={pathFilter === ''} onclick={() => setPath('')}>All</button>
          <button class="chip-btn" class:on={pathFilter === 'pipeline'} onclick={() => setPath('pipeline')}>
            recall_deep
          </button>
          <button class="chip-btn" class:on={pathFilter === 'context'} onclick={() => setPath('context')}>
            pre-turn
          </button>
          <button
            class="chip-btn"
            class:on={hideAutomated}
            title="Heartbeat and other agent-initiated turns"
            onclick={() => (hideAutomated = !hideAutomated)}
          >
            Hide automated{hiddenCount > 0 ? ` (${hiddenCount})` : ''}
          </button>
        </div>
      </div>

      <ul class="rlist">
        {#each filteredEntries as e (e.id)}
          {@const segs = funnelSegments(e)}
          <li>
            <button
              class="rrow"
              class:selected={selectedId === e.id}
              class:automated={isAutomated(e.query)}
              onclick={() => void loadDetail(e.id)}
            >
              <div class="rrow-top">
                <span class="rq" title={e.query ?? ''}>{queryLabel(e.query)}</span>
                <span class="rms" class:slow={(e.duration_ms ?? 0) >= 3000}>
                  {fmtMs(e.duration_ms)}
                </span>
              </div>
              <div class="rrow-bar" aria-hidden="true">
                {#if e.has_candidates && segs.length}
                  {@const tot = segs.reduce((a, s) => a + s.val, 0)}
                  {#each segs as s, si (si)}
                    <span
                      class="rseg {s.cls}"
                      style="flex-grow: {s.val}"
                      title="{s.key}: {s.val}"
                    ></span>
                  {/each}
                  <span class="rbar-n">{tot} → <strong>{e.n_rendered}</strong></span>
                {:else}
                  <!-- Not sampled is not "nothing happened": legs and graph
                       expansion are captured on every retrieval regardless. -->
                  <span class="rseg unsampled"></span>
                  <span class="rbar-n muted">candidates not sampled</span>
                {/if}
              </div>
              <div class="rrow-meta">
                <span class="chip sm">{e.path === 'context' ? 'pre-turn' : 'recall_deep'}</span>
                <span class="muted">{e.legs.length} legs</span>
                {#if e.n_expansions > 0}
                  <span class="expn">{e.n_expansions} graph edges</span>
                {/if}
                {#if e.truncated}<span class="chip sm warnchip">capped</span>{/if}
                <span class="spacer"></span>
                <span class="muted" title={e.timestamp}>{fmtClock(e.timestamp)} · {fmtAge(e.timestamp)}</span>
              </div>
            </button>
          </li>
        {:else}
          <li class="empty-cell">
            {hiddenCount > 0
              ? `All ${hiddenCount} retrievals in this window were automated — turn off “Hide automated” to see them.`
              : 'No retrievals recorded yet'}
          </li>
        {/each}
      </ul>
    </section>

    <!-- ── Detail ───────────────────────────────────────────────────────── -->
    <section class="chart-card detail-card">
      {#if !selectedId}
        <!-- Idle state carries the window-level leg rollup rather than an empty
             panel. Legs are captured on EVERY retrieval regardless of sampling,
             so this is the one view that covers the whole window — and a leg
             that ran and returned nothing across the window is the signal an
             operator is usually hunting. -->
        <div class="detail-head">
          <h2>Legs across this window</h2>
          <span class="muted sm">all {$store.data.count} retrievals</span>
        </div>
        <p class="status-msg">
          Legs are recorded on every retrieval, sampled or not. Select one on the
          left to see its candidates and graph expansion.
        </p>
        <table class="data-table compact">
          <thead>
            <tr>
              <th>Leg</th>
              <th class="num">Fired</th>
              <th class="num">Returned</th>
              <th class="num">Corroborated</th>
              <th class="num">Errors</th>
            </tr>
          </thead>
          <tbody>
            {#each sortedLegs as [name, agg] (name)}
              <tr class:silent={agg.returned === 0 && agg.errors === 0}>
                <td><code>{name}</code></td>
                <td class="num">{agg.attempted.toLocaleString()}</td>
                <td class="num">
                  {agg.returned.toLocaleString()}
                  {#if agg.returned === 0}<span class="muted sm">silent</span>{/if}
                </td>
                <td class="num muted">{agg.deduped ? agg.deduped.toLocaleString() : '—'}</td>
                <td class="num" class:err={agg.errors > 0}>{agg.errors || '—'}</td>
              </tr>
            {:else}
              <tr><td colspan="5" class="empty-cell">No legs recorded yet</td></tr>
            {/each}
          </tbody>
        </table>
      {:else}
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
          <p class="detail-query">{detail.query || '(no query)'}</p>
          <div class="detail-stats">
            <span><strong>{detail.n_candidates}</strong> candidates</span>
            <span><strong>{detail.n_rendered}</strong> rendered</span>
            <span><strong>{detail.n_expansions}</strong> graph edges</span>
            <span><strong>{fmtMs(detail.duration_ms)}</strong></span>
          </div>

          {#if detail.excluded_types.length > 0}
            <p class="status-msg warn">
              Excluded before search:
              {#each detail.excluded_types as x (x.type)}
                <span class="chip sm">{x.type} <span class="muted">({x.stage})</span></span>
              {/each}
            </p>
          {/if}

          <!-- Legs -->
          <h3 class="phase-head">Legs</h3>
          <table class="data-table compact">
            <thead>
              <tr><th>Leg</th><th class="num">Returned</th><th class="num">Corroborated</th><th>State</th></tr>
            </thead>
            <tbody>
              {#each detail.legs as leg, li (li)}
                <tr>
                  <td><code>{leg.name}</code></td>
                  <td class="num">{leg.n_returned}</td>
                  <td class="num muted">{leg.n_deduped || '—'}</td>
                  <td>
                    {#if leg.error}
                      <span class="badge badge-bad">error</span>
                      <span class="muted sm">{leg.error}</span>
                    {:else if !leg.attempted}
                      <span class="badge badge-warn">skipped</span>
                      <span class="muted sm">{leg.skip_reason ?? ''}</span>
                    {:else if leg.n_returned === 0}
                      <span class="muted sm">ran, found nothing</span>
                    {:else}
                      <span class="muted sm">ok</span>
                    {/if}
                  </td>
                </tr>
              {/each}
            </tbody>
          </table>

          <!-- Graph expansion -->
          <h3 class="phase-head">Graph expansion</h3>
          {#if expansionsBySeed.length === 0}
            <p class="status-msg">No graph expansion ran for this retrieval.</p>
          {:else}
            {#each expansionsBySeed as group (group.seed.seed_id)}
              <details class="seed-group">
                <summary class="seed-head">
                  <span class="chip sm">{group.seed.seed_type}</span>
                  <code>{shortId(group.seed.seed_id)}</code>
                  <span class="muted sm">seed {fmtScore(group.seed.seed_score)}</span>
                  <span class="muted sm">· {group.items.length} neighbour{group.items.length === 1 ? '' : 's'}</span>
                </summary>
                <ul class="edge-list">
                  {#each group.items as edge, ei (ei)}
                    <li class:lost={!edge.won_best_path}>
                      <span class="rel">{edge.edge_relation ?? 'related'}</span>
                      <span class="arrow">→</span>
                      <span class="chip sm t-{edge.neighbor_type}">{edge.neighbor_type}</span>
                      <code>{shortId(edge.neighbor_id)}</code>
                      <span class="muted sm">w {fmtScore(edge.edge_weight)} · str {fmtScore(edge.path_strength)} · hop {edge.hop}</span>
                      {#if !edge.won_best_path}<span class="chip sm">lost best-path</span>{/if}
                    </li>
                  {/each}
                </ul>
              </details>
            {/each}
          {/if}

          <!-- Candidates -->
          <h3 class="phase-head">Candidates</h3>
          {#if detail.truncated}
            <p class="status-msg error">
              Candidate recording hit its cap for this retrieval — candidates beyond
              the limit have no row and no disposition here. If an item you expect is
              missing below, it may have been retrieved and dropped without being
              recorded, rather than never retrieved.
            </p>
          {/if}
          {#if detail.candidates_by_disposition === null}
            <p class="status-msg">
              This retrieval was not sampled for candidate capture, so per-item detail was
              never recorded. Legs and graph expansion above are complete.
            </p>
          {:else if detailDispositions.length === 0}
            <p class="status-msg">
              This retrieval was sampled, and no candidates entered — every leg
              returned nothing. (Distinct from “not sampled” above.)
            </p>
          {:else}
            {#each detailDispositions as disp (disp)}
              {@const items = detail.candidates_by_disposition[disp] ?? []}
              <details class="phase-group" open={disp !== 'rendered'}>
                <summary class="disp-head">
                  <span class="badge {dispositionClass(disp)}">{disp}</span>
                  <span class="count">{items.length}</span>
                  <span class="help">{DISPOSITION_HELP[disp] ?? ''}</span>
                </summary>
                <table class="data-table compact">
                  <thead>
                    <tr>
                      <th>Type</th><th>Entry leg</th><th class="num">Score</th>
                      <th class="num">Rank</th><th>Dropped at</th><th>Content</th>
                    </tr>
                  </thead>
                  <tbody>
                    {#each items as c (c.id + c.type)}
                      <tr>
                        <td><span class="chip sm t-{c.type}">{c.type}</span></td>
                        <td><code class="sm">{c.entry_leg}</code></td>
                        <td class="num">
                          {fmtScore(c.entry_score)}
                          {#each c.mutations as m, mi (mi)}
                            <span class="mut" title="{m.stage}: {fmtScore(m.score_before)} → {fmtScore(m.score_after)}">
                              →{fmtScore(m.score_after)}
                            </span>
                          {/each}
                        </td>
                        <td class="num">{c.final_rank ?? '—'}</td>
                        <td>
                          <span class="sm">{c.disposition_stage ?? '—'}</span>
                          {#if c.restored_from}
                            <span class="chip sm restored" title="Dropped by {c.restored_from}, then rescued">
                              rescued
                            </span>
                          {/if}
                        </td>
                        <td class="snippet-cell" title={c.snippet}>{c.snippet || '—'}</td>
                      </tr>
                    {/each}
                  </tbody>
                </table>
              </details>
            {/each}
          {/if}
        {/if}
      {/if}
    </section>
  </div>
{:else if $store.error}
  <p class="status-msg error">Failed to load retrieval telemetry.</p>
{:else}
  <p class="status-msg">Loading…</p>
{/if}

<style>
  /* ── Header ──────────────────────────────────────────────── */
  .view-head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 1rem;
    margin-bottom: 1.25rem;
  }
  h1 { font-size: 1.4rem; font-weight: 700; color: var(--text); margin: 0 0 0.125rem; }
  .subtitle { font-size: 0.8125rem; color: var(--muted); margin: 0; }
  h2 { font-size: 0.875rem; font-weight: 600; color: var(--text); margin: 0; }
  .head-right { display: flex; align-items: center; gap: 0.5rem; flex-shrink: 0; }

  .refresh-btn {
    font-size: 0.8125rem;
    padding: 0.25rem 0.75rem;
    border: 1px solid var(--border);
    border-radius: var(--radius-sm, 8px);
    background: var(--surface);
    color: var(--text);
    cursor: pointer;
  }
  .refresh-btn:hover:not(:disabled) { background: var(--surface-hover); }
  .refresh-btn:disabled { opacity: 0.5; cursor: default; }

  /* ── Card ────────────────────────────────────────────────── */
  .chart-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 1rem;
  }
  .mt { margin-top: 1rem; }

  /* ── Window funnel ───────────────────────────────────────── */
  .funnel-head { display: flex; align-items: baseline; gap: 0.75rem; margin-bottom: 0.85rem; }

  .funnel-figures {
    display: flex;
    align-items: baseline;
    gap: 1.25rem;
    margin-bottom: 0.7rem;
    flex-wrap: wrap;
  }
  .fig { display: flex; align-items: baseline; gap: 0.4rem; }
  .fig.drop { margin-left: auto; }
  .fig-n {
    font-size: 1.55rem;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
    color: var(--text);
    line-height: 1;
  }
  .fig-n.good { color: var(--green); }
  .fig-n.warn { color: var(--yellow); }
  .fig-l { font-size: 0.78rem; color: var(--muted); }
  .fig-arrow { color: var(--muted); font-size: 1.1rem; }

  .disp-bar { display: flex; height: 16px; border-radius: 4px; overflow: hidden; gap: 1px; }
  .disp-seg { min-width: 2px; }
  .disp-seg.badge-good { background: var(--green); }
  .disp-seg.badge-warn { background: var(--yellow); }
  .disp-seg.badge-bad  { background: var(--red); }

  .disp-legend { list-style: none; margin: 0.75rem 0 0; padding: 0; }
  .disp-legend li {
    display: grid;
    grid-template-columns: 10px 10rem 4.5rem 4rem 1fr;
    gap: 0.6rem;
    align-items: center;
    padding: 0.16rem 0;
    font-size: 0.8rem;
  }
  .dot { width: 9px; height: 9px; border-radius: 50%; display: inline-block; }
  .dot.badge-good { background: var(--green); }
  .dot.badge-warn { background: var(--yellow); }
  .dot.badge-bad  { background: var(--red); }
  .disp-legend .k { font-family: var(--font-mono, monospace); color: var(--text); }
  .disp-legend .v,
  .disp-legend .pct { text-align: right; font-variant-numeric: tabular-nums; }
  .disp-legend .pct { color: var(--muted); }
  .disp-legend .help { color: var(--muted); }

  /* ── Split layout ────────────────────────────────────────── */
  .split {
    display: grid;
    grid-template-columns: minmax(360px, 5fr) minmax(420px, 7fr);
    gap: 1rem;
    align-items: start;
  }
  @media (max-width: 1180px) {
    .split { grid-template-columns: 1fr; }
    .detail-card { position: static !important; }
  }

  .list-card { padding: 0.75rem 0.75rem 0.5rem; }
  .list-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.75rem;
    flex-wrap: wrap;
    margin-bottom: 0.6rem;
    padding: 0 0.25rem;
  }
  .filters { display: flex; gap: 0.3rem; flex-wrap: wrap; }
  .chip-btn {
    border: 1px solid var(--border);
    background: transparent;
    color: var(--muted);
    border-radius: 999px;
    padding: 0.14rem 0.65rem;
    font-size: 0.75rem;
    cursor: pointer;
  }
  .chip-btn:hover { background: var(--surface-hover); color: var(--text); }
  .chip-btn.on { background: var(--accent-glow); border-color: var(--accent-dim); color: var(--text); }

  /* ── Retrieval rows ──────────────────────────────────────── */
  .rlist { list-style: none; margin: 0; padding: 0; max-height: 68vh; overflow-y: auto; }
  .rrow {
    display: block;
    width: 100%;
    text-align: left;
    background: transparent;
    border: 1px solid transparent;
    border-radius: 8px;
    padding: 0.5rem 0.55rem;
    cursor: pointer;
    color: inherit;
    font: inherit;
  }
  .rrow:hover { background: var(--surface-hover); }
  .rrow.selected { background: var(--accent-glow); border-color: var(--accent-dim); }
  .rrow:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
  .rrow.automated .rq { color: var(--muted); font-style: italic; }

  .rrow-top { display: flex; align-items: baseline; gap: 0.6rem; }
  .rq {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: 0.84rem;
    color: var(--text);
  }
  .rms { font-size: 0.72rem; color: var(--muted); font-variant-numeric: tabular-nums; }
  .rms.slow { color: var(--yellow); }

  .rrow-bar { display: flex; align-items: center; gap: 1px; height: 7px; margin: 0.4rem 0 0.35rem; }
  .rseg { height: 7px; border-radius: 2px; }
  .rseg.badge-good { background: var(--green); }
  .rseg.badge-warn { background: var(--yellow); }
  .rseg.badge-bad  { background: var(--red); }
  .rseg.unsampled {
    flex: 1;
    background: repeating-linear-gradient(
      90deg, var(--border), var(--border) 3px, transparent 3px, transparent 6px
    );
  }
  .rbar-n {
    flex-shrink: 0;
    margin-left: 0.5rem;
    font-size: 0.72rem;
    color: var(--muted);
    font-variant-numeric: tabular-nums;
  }
  .rbar-n strong { color: var(--green); }

  .rrow-meta { display: flex; align-items: center; gap: 0.45rem; font-size: 0.72rem; }
  .spacer { flex: 1; }
  .expn { color: var(--chunk-color); }

  /* ── Detail ──────────────────────────────────────────────── */
  .detail-card { position: sticky; top: 1rem; max-height: 88vh; overflow-y: auto; }
  .data-table tr.silent td { color: var(--muted); }
  .data-table .err { color: var(--red); font-weight: 600; }
  .detail-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.75rem;
    margin-bottom: 0.6rem;
  }
  .close-btn {
    font-size: 0.75rem;
    padding: 0.15rem 0.6rem;
    border: 1px solid var(--border);
    border-radius: var(--radius-sm, 8px);
    background: var(--surface);
    color: var(--muted);
    cursor: pointer;
  }
  .close-btn:hover { color: var(--text); background: var(--surface-hover); }
  .retry-link {
    background: none;
    border: none;
    color: var(--accent);
    cursor: pointer;
    padding: 0;
    font: inherit;
    text-decoration: underline;
  }

  .detail-query {
    margin: 0 0 0.55rem;
    font-size: 0.86rem;
    color: var(--text);
    line-height: 1.45;
  }
  .detail-stats {
    display: flex;
    gap: 1rem;
    flex-wrap: wrap;
    font-size: 0.76rem;
    color: var(--muted);
    padding-bottom: 0.7rem;
    border-bottom: 1px solid var(--border);
  }
  .detail-stats strong { color: var(--text); font-variant-numeric: tabular-nums; }

  .phase-head {
    font-size: 0.78rem;
    font-weight: 600;
    color: var(--text);
    margin: 1rem 0 0.4rem;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }

  /* ── Tables ──────────────────────────────────────────────── */
  .data-table { width: 100%; border-collapse: collapse; font-size: 0.78rem; }
  .data-table th {
    text-align: left;
    padding: 0.3rem 0.5rem;
    font-size: 0.7rem;
    font-weight: 600;
    color: var(--muted);
    border-bottom: 1px solid var(--border);
    white-space: nowrap;
  }
  .data-table td {
    padding: 0.35rem 0.5rem;
    color: var(--text);
    border-bottom: 1px solid var(--border);
    vertical-align: top;
  }
  .data-table tbody tr:last-child td { border-bottom: none; }
  .data-table .num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
  .data-table code { font-family: var(--font-mono, monospace); font-size: 0.72rem; }

  .empty-cell { text-align: center; color: var(--muted); font-style: italic; padding: 1.5rem 0.5rem; font-size: 0.8rem; }

  /* ── Groups ──────────────────────────────────────────────── */
  .phase-group, .seed-group { margin-bottom: 0.5rem; }
  .disp-head, .seed-head {
    display: flex;
    gap: 0.55rem;
    align-items: center;
    flex-wrap: wrap;
    cursor: pointer;
    padding: 0.3rem 0;
    font-size: 0.8rem;
    list-style: none;
  }
  .disp-head::-webkit-details-marker, .seed-head::-webkit-details-marker { display: none; }
  .disp-head::before, .seed-head::before { content: '▸'; color: var(--muted); font-size: 0.7rem; }
  details[open] > .disp-head::before, details[open] > .seed-head::before { content: '▾'; }
  .disp-head .count { font-variant-numeric: tabular-nums; color: var(--text); font-weight: 600; }
  .disp-head .help { color: var(--muted); font-size: 0.74rem; }

  .edge-list { list-style: none; margin: 0.2rem 0 0; padding: 0 0 0 1rem; border-left: 2px solid var(--border); }
  .edge-list li {
    display: flex; gap: 0.4rem; align-items: center; flex-wrap: wrap;
    padding: 0.16rem 0; font-size: 0.75rem;
  }
  .edge-list li.lost { opacity: 0.5; }
  .rel { font-family: var(--font-mono, monospace); color: var(--accent); }
  .arrow { opacity: 0.5; }

  /* ── Chips / badges ──────────────────────────────────────── */
  .chip {
    display: inline-block;
    padding: 0.08em 0.45em;
    border-radius: 4px;
    background: var(--surface-hover);
    border: 1px solid var(--border);
    color: var(--text);
    font-size: 0.72rem;
    white-space: nowrap;
  }
  .chip.sm { font-size: 0.68rem; }
  .chip.restored { opacity: 0.85; }
  .chip.warnchip { color: var(--yellow); border-color: var(--yellow); }
  /* Memory-type identity, reusing the palette the rest of the dashboard uses. */
  .chip.t-fact      { color: var(--fact-color);      border-color: var(--fact-color); }
  .chip.t-episode   { color: var(--episode-color);   border-color: var(--episode-color); }
  .chip.t-decision  { color: var(--decision-color);  border-color: var(--decision-color); }
  .chip.t-procedure { color: var(--procedure-color); border-color: var(--procedure-color); }
  .chip.t-chunk     { color: var(--chunk-color);     border-color: var(--chunk-color); }
  .chip.t-censor    { color: var(--censor-color);    border-color: var(--censor-color); }

  .badge {
    display: inline-block;
    padding: 0.12em 0.5em;
    border-radius: 4px;
    font-size: 0.7rem;
    font-weight: 600;
    white-space: nowrap;
  }
  .badge-good { background: rgba(52, 211, 153, 0.15); color: var(--green); }
  .badge-warn { background: rgba(251, 191, 36, 0.15); color: var(--yellow); }
  .badge-bad  { background: rgba(248, 113, 113, 0.15); color: var(--red); }

  /* ── Misc ────────────────────────────────────────────────── */
  .status-msg { font-size: 0.8rem; color: var(--muted); margin: 0.4rem 0; }
  .status-msg.error { color: var(--red); }
  .status-msg.warn { color: var(--yellow); }
  .muted { color: var(--muted); }
  .sm { font-size: 0.72rem; }
  .mut { font-size: 0.68rem; opacity: 0.75; margin-left: 0.15rem; }
  .snippet-cell {
    max-width: 22rem;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: var(--muted);
  }
</style>
