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

  // ── Expansion diagram ────────────────────────────────────────────────────
  // Bipartite (seeds left, neighbours right), NOT a force layout: the whole
  // structure here IS "seed reached neighbour", and physics would scramble the
  // one distinction worth showing. It also surfaces CONVERGENCE — two seeds
  // arriving at the same neighbour — which the grouped list structurally
  // cannot, because it prints that neighbour once under each parent.
  const DIAGRAM_MAX_EDGES = 120;
  const ROW_H = 21;
  const NODE_R = 4.5;

  let expansionGraph = $derived.by(() => {
    const rows: RetrievalExpansion[] = detail?.expansions ?? [];
    const shown = rows.slice(0, DIAGRAM_MAX_EDGES);

    const seedIds: string[] = [];
    const nbrIds: string[] = [];
    const seedMeta = new Map<string, RetrievalExpansion>();
    const nbrMeta = new Map<string, RetrievalExpansion>();
    const nbrSeeds = new Map<string, Set<string>>();

    for (const r of shown) {
      if (!seedMeta.has(r.seed_id)) { seedMeta.set(r.seed_id, r); seedIds.push(r.seed_id); }
      if (!nbrMeta.has(r.neighbor_id)) { nbrMeta.set(r.neighbor_id, r); nbrIds.push(r.neighbor_id); }
      let s = nbrSeeds.get(r.neighbor_id);
      if (!s) { s = new Set(); nbrSeeds.set(r.neighbor_id, s); }
      s.add(r.seed_id);
    }

    const rowsCount = Math.max(seedIds.length, nbrIds.length, 1);
    const height = rowsCount * ROW_H + 16;
    // viewBox width is derived from the height so the aspect stays landscape.
    // A fixed narrow width against a tall row stack makes the default
    // `xMidYMid meet` scale to fit the HEIGHT, collapsing the whole diagram
    // into a thin band down the middle of a wide card.
    const width = Math.max(420, Math.round(height * 1.55));
    const leftX = Math.round(width * 0.13);
    const rightX = width - leftX;

    const yOf = (i: number, n: number) =>
      8 + (n <= 1 ? (height - 16) / 2 : (i * (height - 16)) / (n - 1));

    const seeds = seedIds.map((id, i) => ({
      id, meta: seedMeta.get(id)!, x: leftX, y: yOf(i, seedIds.length),
    }));
    const neighbours = nbrIds.map((id, i) => ({
      id, meta: nbrMeta.get(id)!, x: rightX, y: yOf(i, nbrIds.length),
      convergent: (nbrSeeds.get(id)?.size ?? 1) > 1,
    }));

    const sy = new Map(seeds.map((s) => [s.id, s.y]));
    const ny = new Map(neighbours.map((n) => [n.id, n.y]));
    const maxStrength = Math.max(
      ...shown.map((r) => r.path_strength ?? 0), 0.0001,
    );

    const edges = shown.map((r, i) => {
      const y1 = sy.get(r.seed_id) ?? 0;
      const y2 = ny.get(r.neighbor_id) ?? 0;
      const mx = (leftX + rightX) / 2;
      return {
        i,
        d: `M ${leftX + NODE_R} ${y1} C ${mx} ${y1}, ${mx} ${y2}, ${rightX - NODE_R} ${y2}`,
        // Stroke weight carries traversal strength; a hairline still renders so
        // a zero-strength edge is visible as "traversed but weak", not absent.
        w: 0.5 + 1.9 * ((r.path_strength ?? 0) / maxStrength),
        lost: !r.won_best_path,
        title: `${r.seed_type} ${shortId(r.seed_id, 6)} —[${r.edge_relation ?? 'related'}]→ `
             + `${r.neighbor_type} ${shortId(r.neighbor_id, 6)}  `
             + `strength ${fmtScore(r.path_strength)} · hop ${r.hop}`
             + (r.won_best_path ? '' : ' · lost best-path'),
      };
    });

    return {
      seeds, neighbours, edges, width, height,
      truncated: rows.length - shown.length,
      convergentCount: neighbours.filter((n) => n.convergent).length,
    };
  });

  let detailDispositions = $derived(
    orderDispositions(Object.keys(detail?.candidates_by_disposition ?? {})),
  );

  /**
   * Diagnostic order: errored legs, then SILENT ones (ran, returned nothing),
   * then productive ones by yield. The previous comparator claimed this in a
   * comment and then sorted by yield descending — which buried the silent legs
   * at the bottom of a height-limited pane, i.e. the exact opposite.
   */
  function legRank(v: { returned: number; errors: number }): number {
    if (v.errors > 0) return 0;
    return v.returned === 0 ? 1 : 2;
  }
  let sortedLegs = $derived.by(() => {
    const legs = [...($store.data ? Object.entries($store.data.leg_totals) : [])];
    return legs.sort((a, b) => {
      const r = legRank(a[1]) - legRank(b[1]);
      return r !== 0 ? r : b[1].returned - a[1].returned;
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
      {@const dropped = totals.sum - rendered}
      <!-- One element, one job: the bar IS the figure. The old version stated
           each number three times — headline, bar, then a legend column. -->
      <div class="funnel">
        <div class="funnel-in">
          <span class="funnel-n">{totals.sum.toLocaleString()}</span>
          <span class="funnel-l">candidates entered</span>
        </div>

        <div class="funnel-track" role="img"
             aria-label="{rendered} of {totals.sum} candidates reached the model">
          {#each orderDispositions(totals.entries.map(([k]) => k)) as key (key)}
            {@const val = $store.data.disposition_totals[key] ?? 0}
            <div
              class="funnel-seg {dispositionClass(key)}"
              style="flex-grow: {val}"
              title="{key}: {val} ({((val / totals.sum) * 100).toFixed(1)}%) — {DISPOSITION_HELP[key] ?? ''}"
            >
              <!-- Only the DROP segments carry a count. The survivors already
                   have one as the headline to the right, and stating it twice
                   is the duplication this bar was meant to remove. -->
              {#if key !== 'rendered'}
                <span class="seg-label">{val.toLocaleString()}</span>
              {/if}
            </div>
          {/each}
        </div>

        <div class="funnel-out">
          <span class="funnel-n good">{rendered.toLocaleString()}</span>
          <span class="funnel-l">reached the model</span>
          <span class="funnel-sub">{dropped.toLocaleString()} dropped at a gate</span>
        </div>
      </div>

      <ul class="disp-legend">
        {#each orderDispositions(totals.entries.map(([k]) => k)) as key (key)}
          {@const val = $store.data.disposition_totals[key] ?? 0}
          <li title={DISPOSITION_HELP[key] ?? ''}>
            <span class="dot {dispositionClass(key)}"></span>
            <span class="k">{key}</span>
            <span class="pct">{((val / totals.sum) * 100).toFixed(1)}%</span>
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
                <!-- Three distinct states, not two. Folding "sampled, nothing
                     entered" into the unsampled branch made the row assert
                     false sampling information — the same null-vs-empty
                     distinction the detail view and the API already keep. -->
                {#if !e.has_candidates}
                  <!-- Not sampled is not "nothing happened": legs and graph
                       expansion are captured on every retrieval regardless. -->
                  <span class="rseg unsampled"></span>
                  <span class="rbar-n muted">candidates not sampled</span>
                {:else if segs.length === 0}
                  <span class="rseg empty"></span>
                  <span class="rbar-n muted">0 entered · every leg silent</span>
                {:else}
                  {@const tot = segs.reduce((a, s) => a + s.val, 0)}
                  {#each segs as s, si (si)}
                    <span
                      class="rseg {s.cls}"
                      style="flex-grow: {s.val}"
                      title="{s.key}: {s.val}"
                    ></span>
                  {/each}
                  <span class="rbar-n">{tot} → <strong>{e.n_rendered}</strong></span>
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
            {@const g = expansionGraph}
            <div class="xdiagram">
              <div class="xd-cols">
                <span>{g.seeds.length} seed{g.seeds.length === 1 ? '' : 's'}</span>
                <span class="muted">
                  {g.edges.length} edges
                  {#if g.convergentCount > 0}
                    · <span class="conv-note">{g.convergentCount} reached from more than one seed</span>
                  {/if}
                </span>
                <span>{g.neighbours.length} neighbour{g.neighbours.length === 1 ? '' : 's'}</span>
              </div>
              <!-- No height attribute: with a viewBox, `width:100%; height:auto`
                   in CSS preserves the aspect exactly and avoids letterboxing. -->
              <svg
                viewBox="0 0 {g.width} {g.height}"
                role="img"
                aria-label="{g.edges.length} graph edges from {g.seeds.length} seeds to {g.neighbours.length} neighbours"
              >
                {#each g.edges as e (e.i)}
                  <path
                    class="xd-edge"
                    class:lost={e.lost}
                    d={e.d}
                    style="stroke-width: {e.w}"
                  ><title>{e.title}</title></path>
                {/each}
                {#each g.seeds as s (s.id)}
                  <circle class="xd-node t-{s.meta.seed_type}" cx={s.x} cy={s.y} r={NODE_R}>
                    <title>{s.meta.seed_type} {shortId(s.id, 8)} · seed {fmtScore(s.meta.seed_score)}</title>
                  </circle>
                  <text class="xd-label right" x={s.x - 9} y={s.y + 3}>{shortId(s.id, 6)}</text>
                {/each}
                {#each g.neighbours as n (n.id)}
                  <circle
                    class="xd-node t-{n.meta.neighbor_type}"
                    class:convergent={n.convergent}
                    cx={n.x} cy={n.y} r={n.convergent ? NODE_R + 1.5 : NODE_R}
                  >
                    <title>{n.meta.neighbor_type} {shortId(n.id, 8)}{n.convergent ? ' · reached from several seeds' : ''}</title>
                  </circle>
                  <text class="xd-label" x={n.x + 9} y={n.y + 3}>{shortId(n.id, 6)}</text>
                {/each}
              </svg>
              {#if g.truncated > 0}
                <p class="status-msg warn sm">
                  {g.truncated} further edge{g.truncated === 1 ? '' : 's'} not drawn — the full
                  set is listed below.
                </p>
              {/if}
            </div>

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
  .funnel-head {
    display: flex;
    align-items: baseline;
    gap: 0.6rem;
    margin-bottom: 1rem;
  }
  /* Section label: small, tracked, quiet — so the figures below carry the
     weight instead of competing with a same-size heading. */
  .funnel-head h2 {
    font-size: 0.7rem;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--muted);
  }

  .funnel {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    align-items: center;
    gap: 1.5rem;
  }
  .funnel-in { text-align: right; }
  .funnel-out { text-align: left; }
  .funnel-n {
    display: block;
    font-size: 2.4rem;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
    letter-spacing: -0.02em;
    color: var(--text);
    line-height: 1;
  }
  .funnel-n.good { color: var(--green); }
  .funnel-l {
    display: block;
    margin-top: 0.25rem;
    font-size: 0.75rem;
    color: var(--muted);
  }
  /* The dropped count belongs BESIDE the survivors it is measured against —
     it was previously flung to the far edge of the card by margin-left:auto. */
  .funnel-sub {
    display: block;
    margin-top: 0.15rem;
    font-size: 0.75rem;
    color: var(--yellow);
    font-variant-numeric: tabular-nums;
  }

  .funnel-track {
    display: flex;
    height: 34px;
    border-radius: 6px;
    overflow: hidden;
    gap: 2px;
  }
  .funnel-seg {
    min-width: 3px;
    display: flex;
    align-items: center;
    justify-content: center;
    transition: filter var(--transition, 0.2s ease);
  }
  .funnel-seg:hover { filter: brightness(1.15); }
  .funnel-seg.badge-good { background: var(--green); }
  .funnel-seg.badge-warn { background: var(--yellow); }
  .funnel-seg.badge-bad  { background: var(--red); }
  /* Counts sit ON the bar, so the bar is the figure rather than a decoration
     the legend then restates. Hidden when a segment is too thin to hold them. */
  .seg-label {
    font-size: 0.72rem;
    font-weight: 700;
    color: #0a0a0f;
    font-variant-numeric: tabular-nums;
    overflow: hidden;
    text-overflow: clip;
    white-space: nowrap;
  }

  .disp-legend {
    list-style: none;
    margin: 0.85rem 0 0;
    padding: 0;
    display: flex;
    flex-wrap: wrap;
    gap: 0.35rem 1.1rem;
  }
  .disp-legend li {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    font-size: 0.75rem;
    cursor: default;
  }
  .dot { width: 8px; height: 8px; border-radius: 2px; display: inline-block; flex-shrink: 0; }
  .dot.badge-good { background: var(--green); }
  .dot.badge-warn { background: var(--yellow); }
  .dot.badge-bad  { background: var(--red); }
  .disp-legend .k { font-family: var(--font-mono, monospace); color: var(--text); }
  .disp-legend .pct { color: var(--muted); font-variant-numeric: tabular-nums; }

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
  /* The query is the row's subject — give it real size and full contrast so
     the eye lands there first instead of on a wall of same-size grey. */
  .rq {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: 0.9rem;
    font-weight: 500;
    color: var(--text);
  }
  .rms {
    font-size: 0.72rem;
    color: var(--muted);
    font-variant-numeric: tabular-nums;
    flex-shrink: 0;
  }
  .rms.slow { color: var(--yellow); }

  /* A 3px rule under the row rather than a floating bar between two text
     lines — the row reads as one block with a progress underline instead of
     a text/bar/text sandwich. */
  .rrow-bar { display: flex; align-items: center; gap: 1.5px; height: 3px; margin: 0.45rem 0 0.3rem; }
  .rseg { height: 3px; border-radius: 1px; }
  .rseg.badge-good { background: var(--green); }
  .rseg.badge-warn { background: var(--yellow); }
  .rseg.badge-bad  { background: var(--red); }
  /* Hatched = we did not look. Solid hairline = we looked and found nothing.
     Two different facts, so two different marks. */
  .rseg.unsampled {
    flex: 1;
    background: repeating-linear-gradient(
      90deg, var(--border), var(--border) 4px, transparent 4px, transparent 8px
    );
  }
  .rseg.empty { flex: 1; background: var(--border); }
  .rbar-n {
    flex-shrink: 0;
    margin-left: 0.55rem;
    font-size: 0.7rem;
    color: var(--muted);
    font-variant-numeric: tabular-nums;
    line-height: 1;
  }
  .rbar-n strong { color: var(--green); font-weight: 600; }

  .rrow-meta { display: flex; align-items: center; gap: 0.5rem; font-size: 0.7rem; color: var(--muted); }
  .spacer { flex: 1; }
  /* Graph edges are the datum unique to this row and captured even unsampled,
     so it keeps colour while time and leg count recede. */
  .expn { color: var(--chunk-color); }
  .rrow-meta .muted { opacity: 0.75; }

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

  /* ── Expansion diagram ───────────────────────────────────── */
  .xdiagram {
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 0.6rem 0.5rem 0.4rem;
    margin-bottom: 0.75rem;
  }
  .xd-cols {
    display: flex;
    justify-content: space-between;
    gap: 0.75rem;
    font-size: 0.7rem;
    color: var(--text);
    padding: 0 0.25rem 0.4rem;
  }
  .xd-cols .muted { text-align: center; }
  .conv-note { color: var(--accent); }
  .xdiagram svg { display: block; width: 100%; height: auto; }

  .xd-edge {
    fill: none;
    stroke: var(--muted);
    opacity: 0.45;
    transition: opacity var(--transition, 0.2s ease), stroke var(--transition, 0.2s ease);
  }
  .xd-edge:hover { stroke: var(--accent); opacity: 1; }
  /* A path that lost best-path arbitration still HAPPENED — dashed rather than
     hidden, so the traversal that was attempted stays visible. */
  .xd-edge.lost { stroke-dasharray: 2 3; opacity: 0.22; }

  .xd-node { fill: var(--muted); stroke: var(--bg); stroke-width: 1; }
  .xd-node.convergent { stroke: var(--accent); stroke-width: 1.5; }
  .xd-node.t-fact      { fill: var(--fact-color); }
  .xd-node.t-episode   { fill: var(--episode-color); }
  .xd-node.t-decision  { fill: var(--decision-color); }
  .xd-node.t-procedure { fill: var(--procedure-color); }
  .xd-node.t-chunk     { fill: var(--chunk-color); }
  .xd-node.t-censor    { fill: var(--censor-color); }
  .xd-node.t-multi     { fill: var(--accent); }

  .xd-label {
    font-family: var(--font-mono, monospace);
    font-size: 7px;
    fill: var(--muted);
  }
  .xd-label.right { text-anchor: end; }

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
