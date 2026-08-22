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
  let appliedAutomated = true;

  // The automated predicate runs CLIENT-side (the server has no notion of
  // agent-initiated), but the API limits to the newest N rows BEFORE we see
  // them. On a heartbeat-heavy agent a run of automated turns can fill that
  // whole window, so the filter would empty the table while the user's own
  // retrieval sat just past the cutoff — the exact pagination trap that moved
  // `path` filtering server-side. Widen the request to the endpoint maximum
  // while the toggle is on. The window size is disclosed in the funnel header,
  // so the rollup denominator changing with it is visible, not silent.
  const DEFAULT_LIMIT = 50;
  const WIDE_LIMIT = 200; // rest.py caps `limit` at 200

  const store = usePoll(
    makePollStore<RetrievalData>(
      (signal) => {
        appliedPath = pathFilter;
        appliedAutomated = hideAutomated;
        const params = new URLSearchParams();
        if (pathFilter) params.set('path', pathFilter);
        params.set('limit', String(hideAutomated ? WIDE_LIMIT : DEFAULT_LIMIT));
        return apiGet<RetrievalData>(
          `/dashboard/retrieval?${params}`,
          { signal },
        );
      },
      0, // fetch-once + manual refresh, matching sibling views
    ),
  );

  function toggleAutomated() {
    hideAutomated = !hideAutomated;
    void store.refresh();
  }

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
    if (!$store.loading && (appliedPath !== pathFilter || appliedAutomated !== hideAutomated)) {
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

  // A ticking clock, because this store is fetch-once: without it `fmtAge` is
  // only evaluated on a parent render, so a row that said "just now" keeps
  // saying it for hours — stale recency is worse than no recency.
  let nowMs = $state(Date.now());
  $effect(() => {
    const id = setInterval(() => { nowMs = Date.now(); }, 30_000);
    return () => clearInterval(id);
  });

  /** Relative age. The absolute stamp repeated 50x is noise; recency is signal. */
  function fmtAge(iso: string | null, now: number): string {
    if (!iso) return '';
    const t = new Date(iso).getTime();
    if (Number.isNaN(t)) return '';
    const s = Math.max(0, (now - t) / 1000);
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
  // Dispositions are not 10 nominal categories — they are one delivered
  // outcome and nine ways to lose, with real family structure. Mapping them
  // onto three semantic badge classes (good/warn/bad) painted every drop the
  // same amber, so a five-entry legend rendered in two colours and a swatch
  // could not identify the segment it belonged to — the legend's only job.
  //
  // Four hue families, distinguished WITHIN a family by lightness, keeps the
  // hue count inside colour-vision-deficiency limits where nine hues would
  // not. `unaccounted` keeps its own alarm hue and is never blended: it is
  // the drift sentinel, and it should read as wrong on sight.
  //
  // `order` is load-bearing. A 0.1% segment is too small to identify by
  // colour at any palette quality, so POSITION is authoritative: segments and
  // legend are rendered in this order always, and colour only confirms.
  const DISPOSITION_META: Record<string, { color: string; family: string; order: number }> = {
    rendered:           { color: '#34d399', family: 'delivered',  order: 0 },
    // capacity — a cut or budget removed it; the remedy is a bigger budget
    sliced_off:         { color: '#f59e0b', family: 'capacity',   order: 1 },
    budget_truncated:   { color: '#fcd34d', family: 'capacity',   order: 2 },
    // quality — a score or filter judged it; a bigger budget changes nothing
    below_floor:        { color: '#a78bfa', family: 'quality',    order: 3 },
    filter_dropped:     { color: '#c4b5fd', family: 'quality',    order: 4 },
    // redundancy/scope — deliberately not included, nothing is wrong
    deduped:            { color: '#94a3b8', family: 'redundancy', order: 5 },
    superseded:         { color: '#cbd5e1', family: 'redundancy', order: 6 },
    replaced_at_merge:  { color: '#64748b', family: 'redundancy', order: 7 },
    f071_excluded:      { color: '#7c8da3', family: 'redundancy', order: 8 },
    type_excluded:      { color: '#475569', family: 'redundancy', order: 9 },
    // anomaly — reserved, never reused
    unaccounted:        { color: '#f472b6', family: 'anomaly',    order: 10 },
  };

  function dispositionColor(d: string): string {
    return DISPOSITION_META[d]?.color ?? '#64748b';
  }

  function dispositionFamily(d: string): string {
    return DISPOSITION_META[d]?.family ?? 'redundancy';
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
    // Canonical order, so a segment's POSITION identifies it even when its
    // width is 0.1%. Alphabetical-within-the-middle used to reshuffle the
    // bar between windows as dispositions appeared and vanished.
    const rank = (k: string) => DISPOSITION_META[k]?.order ?? 50;
    return [...keys].sort((a, b) => rank(a) - rank(b) || a.localeCompare(b));
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

  // Chronological or slowest-first. The latency strip above surfaces a 54s
  // outlier; without a sort there is no way to reach it except scrolling.
  let sortBy = $state<'recent' | 'slowest'>('recent');
  let visibleEntries = $derived(
    sortBy === 'slowest'
      // Same absence-is-not-a-value rule as `timing`: an untimed row is not a
      // fast row. Coercing null to 0 sorted every failed retrieval to the
      // bottom of a slowest-first list, which is where an operator is least
      // likely to look for a failure.
      ? [...filteredEntries].sort((a, b) => {
          const av = a.duration_ms, bv = b.duration_ms;
          if (typeof av !== 'number') return typeof bv !== 'number' ? 0 : 1;
          if (typeof bv !== 'number') return -1;
          return bv - av;
        })
      : filteredEntries,
  );

  /**
   * Session cluster markers. Retrievals cluster by session, but most clusters
   * are a single row — 26 sessions across 66 rows on the instance this was
   * built against — so marking every boundary put a rule every ~2.5 rows and
   * read as noise rather than structure. Only runs of 2+ get a marker, and it
   * carries the run length (a raw session hex means nothing on its own).
   * Chronological order only: sorting by duration interleaves sessions, so a
   * "cluster" would be a lie.
   */
  let sessionRuns = $derived.by(() => {
    const out = new Map<number, number>(); // start index -> run length
    if (sortBy !== 'recent') return out;
    let i = 0;
    while (i < visibleEntries.length) {
      // A null session_id is "no session was available" (retrievals outside a
      // tool loop), not a session that two rows share. Matching null to null
      // asserted a relationship the data does not contain — the same
      // absence-is-not-a-value error as the timing stats above.
      const sid = visibleEntries[i].session_id;
      if (sid == null) { i++; continue; }
      let j = i;
      while (
        j + 1 < visibleEntries.length &&
        visibleEntries[j + 1].session_id === sid
      ) j++;
      const len = j - i + 1;
      if (len >= 2) out.set(i, len);
      i = j + 1;
    }
    return out;
  });

  // Rows whose candidate array stopped at retrieval_telemetry_max_candidates.
  // Counted over the WHOLE window, not the automated-filtered view, because the
  // funnel above it is a window-level rollup.
  let cappedRows = $derived(
    ($store.data?.entries ?? []).filter((e) => e.truncated).length,
  );

  // Latency was the one window-level dimension the page never showed. On the
  // instance this was built against: p50 3.8s, p95 11.7s, slowest 54.7s, with
  // 21 of 66 over 5s — a user-facing stall that existed only as one orange
  // number inside a single row. Computed over the WHOLE window, like the
  // funnel, not the automated-filtered view.
  let timing = $derived.by(() => {
    const all = $store.data?.entries ?? [];
    // MEASURED rows only. A persisted partial trace from a failed recall_deep
    // carries duration_ms: null; coercing that to 0 reports a failure as
    // instantaneous, drags the median and p95 down, and pads the "over 5s"
    // denominator with rows that were never timed. If nothing was timed we
    // return null so the strip renders nothing at all rather than "0ms".
    const ds = all
      .map((e) => e.duration_ms)
      .filter((d): d is number => typeof d === 'number')
      .sort((a, b) => a - b);
    if (!ds.length) return null;
    // Linear-interpolated quantile. Nearest-rank on an even-sized window
    // returns the UPPER middle, not the median — a 1ms/100ms pair reads as
    // 100ms instead of 50.5ms, which matters most on the skewed windows the
    // strip exists to surface.
    const q = (p: number) => {
      const pos = (ds.length - 1) * p;
      const lo = Math.floor(pos);
      const hi = Math.ceil(pos);
      return lo === hi ? ds[lo] : ds[lo] + (ds[hi] - ds[lo]) * (pos - lo);
    };
    return {
      p50: q(0.5),
      p95: q(0.95),
      max: ds[ds.length - 1],
      slow: ds.filter((x) => x >= 5000).length,
      n: ds.length,
      untimed: all.length - ds.length,
    };
  });

  let totals = $derived.by(() => {
    const t = $store.data?.disposition_totals ?? {};
    const sum = Object.values(t).reduce((a, b) => a + b, 0);
    return { entries: Object.entries(t), sum };
  });

  /**
   * Why a sampled retrieval produced no candidates. "Every leg silent" is only
   * one of three causes — legs can also have errored or never run — and the
   * entry already carries `error`/`attempted` per leg, so stating the actual
   * cause costs nothing and naming the wrong one hides a failure.
   */
  function zeroReason(e: RetrievalEntry): string {
    const legs = e.legs ?? [];
    const errored = legs.filter((l) => l.error).length;
    if (errored > 0) return `0 entered · ${errored} leg${errored === 1 ? '' : 's'} errored`;
    const ran = legs.filter((l) => l.attempted).length;
    const skipped = legs.length - ran;
    if (legs.length > 0 && ran === 0) return '0 entered · no leg ran';
    // MIXED is its own case. Falling through to "every leg ran" whenever the
    // all-skipped test failed asserted something false and hid the skips.
    if (skipped > 0) {
      return `0 entered · ${ran} ran empty, ${skipped} skipped`;
    }
    return '0 entered · every leg ran and returned nothing';
  }

  /** Per-row funnel segments, so the list is scannable without drilling in. */
  function funnelSegments(e: RetrievalEntry) {
    const counts = e.disposition_counts ?? {};
    return orderDispositions(Object.keys(counts)).map((k) => ({
      key: k,
      val: counts[k] ?? 0,
      color: dispositionColor(k),
    }));
  }

  /** Group expansion edges by seed so the traversal reads as a tree. */
  /** id -> {snippet, type} for every recorded candidate on this retrieval.
   *  Graph rows carry only UUIDs, and a 6-hex prefix names nothing a human can
   *  recognise — the single least useful token on the page. Candidates already
   *  carry a snippet, so the tree can say what a node IS. Empty when the row
   *  was not sampled, in which case we fall back to the short id. */
  let candidateIndex = $derived.by(() => {
    const m = new Map<
      string,
      { snippet: string | null; disposition: string; entryLeg: string | null }
    >();
    const cbd = detail?.candidates_by_disposition;
    if (!cbd) return m;
    for (const [disp, arr] of Object.entries(cbd)) {
      for (const c of arr) {
        m.set(String(c.id), {
          snippet: c.snippet ?? null,
          disposition: disp,
          entryLeg: c.entry_leg ?? null,
        });
      }
    }
    return m;
  });

  // TWO allowlists, not one allowlist and an else-branch. Saying a neighbour
  // "was already present from a direct leg" is a positive claim and needs
  // positive evidence; deriving it from "not in the graph list" made every
  // unrecognised leg into a direct-leg assertion. That produced two wrong
  // answers at once: `context_procedures_graph` (the K-line traversal, which
  // genuinely IS graph entry) was reported as corroboration, and the cap
  // sentinel written by RetrievalTrace.finalize() — a NON-null string meaning
  // "provenance was the casualty" — sailed past the null check into `direct`.
  //
  // With both sides enumerated, anything unrecognised falls to `unknown` by
  // construction: the sentinel, a leg added later, a typo. No string matching
  // against the sentinel, which would have to be kept in sync across two
  // languages and would silently rot the day someone reworded it.
  const GRAPH_ENTRY_LEGS = new Set([
    'heart_graph',
    'heart_graph_neighbors',
    'heart_graph_memory',
    'heart_graph_memory_neighbors',
    'graph_expanded',
    'spreading_activation',
    'context_procedures_graph',
    'context_procedure_kline',
  ]);
  const DIRECT_ENTRY_LEGS = new Set([
    'heart_primary',
    'chunk',
    'brain',
    'keyed',
    'keyed_r2',
    'exemplar',
    'parent_episode',
    'context_facts',
    'context_episodes',
    'context_episodes_temporal',
    'context_decisions',
    'context_procedures',
    'context_procedures_ladder',
    'context_procedures_critic',
    'context_procedures_critic_fallback',
    'context_procedures_cosine_fallback',
  ]);

  function nodeLabel(id: string): string {
    const s = candidateIndex.get(String(id))?.snippet;
    return s && s.trim() ? s.trim() : shortId(id, 8);
  }

  let expansionsBySeed = $derived.by(() => {
    const rows: RetrievalExpansion[] = detail?.expansions ?? [];

    // How many DISTINCT seeds reached each neighbour. Convergence is the one
    // thing a per-seed tree cannot show structurally (it prints the neighbour
    // once under each parent), and it was the sole justification for the
    // bipartite diagram — so the tree has to state it outright.
    // HOP-1 ONLY. A hop-2 row's seed_id is `seeds[0][0]` with
    // seed_type="multi" (retrieval_pipeline.py:1565) — a placeholder for the
    // whole activation, not a seed that reached this neighbour. Counting it
    // meant a neighbour found by one real seed AND present in the spreading
    // result reported "reached from 2 seeds", inventing convergence out of a
    // field explicitly documented as unattributable.
    const seedsPerNeighbour = new Map<string, Set<string>>();
    for (const r of rows) {
      if (r.hop === 2 || r.seed_type === 'multi') continue;
      let s = seedsPerNeighbour.get(r.neighbor_id);
      if (!s) { s = new Set(); seedsPerNeighbour.set(r.neighbor_id, s); }
      s.add(r.seed_id);
    }

    // Hop-2 spreading activation has NO single attributable seed — the CTE is
    // multi-seed by construction. Rooting it under whichever seed happened to
    // be recorded would be a tree that lies. It gets its own pseudo-group.
    const spreading = rows.filter((r) => r.hop === 2);
    const direct = rows.filter((r) => r.hop !== 2);

    const groups = new Map<string, { seed: RetrievalExpansion; items: RetrievalExpansion[] }>();
    for (const r of direct) {
      const g = groups.get(r.seed_id);
      if (g) g.items.push(r);
      else groups.set(r.seed_id, { seed: r, items: [r] });
    }

    const out = [...groups.values()]
      .map((g) => ({
        ...g,
        key: g.seed.seed_id,
        isSpreading: false,
        items: [...g.items].sort(
          (a, b) => (b.path_strength ?? 0) - (a.path_strength ?? 0),
        ),
      }))
      // Widest fan-out first: that is where expansion actually did work, and
      // it is the group an operator opens.
      .sort((a, b) => b.items.length - a.items.length);

    if (spreading.length) {
      out.push({
        seed: spreading[0],
        key: '__spreading__',
        isSpreading: true,
        items: [...spreading].sort((a, b) => (b.path_strength ?? 0) - (a.path_strength ?? 0)),
      });
    }
    return out.map((g) => ({
      ...g,
      seedCount: (e: RetrievalExpansion) => seedsPerNeighbour.get(e.neighbor_id)?.size ?? 1,
    }));
  });

  let expansionTotals = $derived.by(() => {
    const rows: RetrievalExpansion[] = detail?.expansions ?? [];
    const seeds = new Set(rows.filter((r) => r.hop !== 2).map((r) => r.seed_id));
    const nbrs = new Set(rows.map((r) => r.neighbor_id));
    // Same hop-1-only rule as the per-row chip: the header count and the chip
    // must not disagree, and neither may treat the hop-2 placeholder as a seed.
    const conv = new Map<string, Set<string>>();
    for (const r of rows) {
      if (r.hop === 2 || r.seed_type === 'multi') continue;
      let s = conv.get(r.neighbor_id);
      if (!s) { s = new Set(); conv.set(r.neighbor_id, s); }
      s.add(r.seed_id);
    }
    // `seeds` counts DIRECT (hop-1) seeds only. Spreading activation is
    // multi-seed by construction and stores its first id as an unattributable
    // placeholder, so on a spreading-only retrieval this set is empty — and
    // printing "0 seeds" would be observably false, since the producer starts
    // from a non-empty seed list. Report it as absent instead.
    const directRows = rows.filter((r) => r.hop !== 2);
    return {
      seeds: directRows.length ? seeds.size : null,
      edges: rows.length,
      neighbours: nbrs.size,
      // The unrecorded-drop caveat is TRUE only for spreading activation. The
      // one-hop stages (2, 2b, 4 fallback, context K-line) all call
      // tr.expansion() BEFORE their dedup/selection guards, so a neighbour they
      // drop pre-merge still HAS an edge — on a one-hop-only retrieval the
      // displayed reach may be complete, and a blanket "true reach is wider"
      // was simply false. Generalised from one observation; narrowed to the
      // stage that actually has the gap.
      hasSpreading: rows.some((r) => r.hop === 2 || r.seed_type === 'multi'),
      convergent: [...conv.values()].filter((s) => s.size > 1).length,
      // Counted by ENTRY LEG, not disposition. `tr.expansion()` is recorded at
      // retrieval_pipeline.py:1341, BEFORE the duplicate guard at :1371 — so a
      // neighbour that merely corroborated an existing direct candidate still
      // has an edge, and joining on id alone resolves it to that direct
      // candidate's `rendered`. Counting those as expansion yield credited the
      // graph for memories it did not contribute; entry_leg is first-wins, so
      // it names how the candidate ACTUALLY got into the pool.
      // THREE outcomes, not two. A neighbour missing from the index has not
      // been shown to come from a direct leg — it was never recorded, because
      // the capture cap rejected it or the row was not sampled. Folding those
      // into "direct" turned an absent record into a positive claim about
      // provenance, which is the same error the cap already caused once on the
      // write side. `?? ''` made the miss silent; it is now its own bucket.
      ...(() => {
        let viaGraph = 0, viaDirect = 0, unknown = 0;
        for (const n of nbrs) {
          const leg = candidateIndex.get(String(n))?.entryLeg;
          if (leg && GRAPH_ENTRY_LEGS.has(leg)) viaGraph++;
          else if (leg && DIRECT_ENTRY_LEGS.has(leg)) viaDirect++;
          else unknown++; // missing, cap sentinel, or a leg we cannot classify
        }
        return { viaGraph, viaDirect, unknown };
      })(),
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
  function legRank(v: { returned: number; errors: number; attempted: number }): number {
    if (v.errors > 0) return 0;
    // `attempted` here is a COUNT of retrievals in which the leg actually ran
    // (dashboard_queries sums the per-retrieval boolean). Zero means it never
    // fired at all — a planner or budget skip, which is expected and benign, so
    // it must not be promoted as a ran-but-empty diagnostic.
    if (v.attempted === 0) return 3;
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
      {@const unaccounted = $store.data.disposition_totals['unaccounted'] ?? 0}
      <!-- `unaccounted` is BY DEFINITION "no stage claimed this drop" — the
           drift alarm. Summing it into "dropped at a gate" would bury the one
           number that says the instrumentation itself is incomplete inside an
           ordinary total, which is the class of error this whole feature
           exists to prevent. -->
      {@const dropped = totals.sum - rendered - unaccounted}
      <!-- One element, one job: the bar IS the figure. The old version stated
           each number three times — headline, bar, then a legend column. -->
      <div class="funnel">
        <div class="funnel-in">
          <span class="funnel-n">{totals.sum.toLocaleString()}</span>
          <!-- Once any row hit the per-retrieval capture cap, this sum is the
               recorded prefix, not the true intake. Say "recorded" rather than
               presenting an undercount as exact. -->
          <span class="funnel-l">
            {cappedRows > 0 ? 'candidates recorded' : 'candidates entered'}
          </span>
          {#if cappedRows > 0}
            <span class="funnel-cap">
              {cappedRows} retrieval{cappedRows === 1 ? '' : 's'} hit the capture cap
            </span>
          {/if}
        </div>

        <div class="funnel-track" role="img"
             aria-label="{rendered} of {totals.sum} candidates reached the model">
          {#each orderDispositions(totals.entries.map(([k]) => k)) as key (key)}
            {@const val = $store.data.disposition_totals[key] ?? 0}
            <!-- `flex: <n> 1 0` — a ZERO basis. With the default `auto` basis
                 the label's intrinsic width is allotted before the remaining
                 space is divided by count, so a label-bearing drop segment
                 renders wider than its share while the label-free `rendered`
                 segment does not. A bar chart that misstates its proportions
                 is worse than no bar. -->
            <div
              class="funnel-seg"
              style="flex: {val} 1 0; background: {dispositionColor(key)}"
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
          {#if unaccounted > 0}
            <span class="funnel-alarm">
              {unaccounted.toLocaleString()} unaccounted — a filter is not reporting
            </span>
          {/if}
        </div>
      </div>

      <!-- Legend order == segment order, always. The family separator is the
           actual reading aid: it says WHY these are grouped (capacity vs
           quality vs redundancy are three different remedies), which is the
           thing a flat five-item list never conveyed. -->
      <ul class="disp-legend">
        {#each orderDispositions(totals.entries.map(([k]) => k)) as key, i (key)}
          {@const val = $store.data.disposition_totals[key] ?? 0}
          {@const fam = dispositionFamily(key)}
          {@const prevFam =
            i === 0
              ? null
              : dispositionFamily(orderDispositions(totals.entries.map(([k]) => k))[i - 1])}
          {#if prevFam !== null && prevFam !== fam}
            <li class="fam-sep" aria-hidden="true"></li>
          {/if}
          <li title="{DISPOSITION_HELP[key] ?? ''} — {fam}">
            <span class="dot" style="background: {dispositionColor(key)}"></span>
            <span class="k">{key}</span>
            <span class="pct">{((val / totals.sum) * 100).toFixed(1)}%</span>
          </li>
        {/each}
      </ul>
    {/if}

    {#if timing}
      <!-- Separate strip, own label: this card's subject is candidate flow, and
           latency is a different dimension. Folding it into the funnel figures
           would make the heading a category error. -->
      <div class="timing">
        <span class="timing-label">Timing</span>
        <span><strong>{fmtMs(timing.p50)}</strong> median</span>
        <span><strong>{fmtMs(timing.p95)}</strong> p95</span>
        <span class:slow={timing.max >= 20000}>
          <strong>{fmtMs(timing.max)}</strong> slowest
        </span>
        {#if timing.slow > 0}
          <span class="timing-slow">
            {timing.slow} of {timing.n} over 5s
          </span>
        {/if}
        {#if timing.untimed > 0}
          <!-- Say what the figures are NOT computed over. Silently narrowing
               the denominator is how "median 3.8s" starts describing a
               different population than the list below it. -->
          <span class="muted">{timing.untimed} untimed</span>
        {/if}
      </div>
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
            onclick={toggleAutomated}
          >
            Hide automated{hiddenCount > 0 ? ` (${hiddenCount})` : ''}
          </button>
          <button
            class="chip-btn"
            class:on={sortBy === 'slowest'}
            title="Sort by duration — session grouping only applies in time order"
            onclick={() => (sortBy = sortBy === 'slowest' ? 'recent' : 'slowest')}
          >
            Slowest first
          </button>
        </div>
      </div>

      <ul class="rlist">
        {#each visibleEntries as e, ei (e.id)}
          {@const segs = funnelSegments(e)}
          <li>
            {#if sessionRuns.has(ei)}
              <div class="sess-break">
                <span class="sess-id">
                  {sessionRuns.get(ei)} retrievals · one session
                </span>
                <span class="sess-rule"></span>
              </div>
            {/if}
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
              <!-- NOT aria-hidden as a whole: this row is a button, so its
                   accessible name is its content, and hiding the bar removed
                   the retrieval's OUTCOME ("not sampled", the zero reason, the
                   in→out counts) from what a screen reader announces — leaving
                   only the query and metadata. The old table exposed the
                   outcome as text. Only the coloured segments are decorative;
                   they carry aria-hidden individually. -->
              <div class="rrow-bar">
                <!-- Three distinct states, not two. Folding "sampled, nothing
                     entered" into the unsampled branch made the row assert
                     false sampling information — the same null-vs-empty
                     distinction the detail view and the API already keep. -->
                {#if !e.has_candidates}
                  <!-- Not sampled is not "nothing happened": legs and graph
                       expansion are captured on every retrieval regardless. -->
                  <span class="rseg unsampled" aria-hidden="true"></span>
                  <span class="rbar-n muted">candidates not sampled</span>
                {:else if segs.length === 0}
                  <span class="rseg empty" aria-hidden="true"></span>
                  <span class="rbar-n muted">{zeroReason(e)}</span>
                {:else}
                  {@const tot = segs.reduce((a, s) => a + s.val, 0)}
                  {#each segs as s, si (si)}
                    <span
                      class="rseg"
                      aria-hidden="true"
                      style="flex-grow: {s.val}; background: {s.color}"
                      title="{s.key}: {s.val}"
                    ></span>
                  {/each}
                  <span class="rbar-n">{tot} → <strong>{e.n_rendered}</strong></span>
                {/if}
              </div>
              <div class="rrow-meta">
                <span class="chip sm">{e.path === 'context' ? 'pre-turn' : 'recall_deep'}</span>
                <!-- turn_number is populated on every row by the correlation
                     fix and was never surfaced; it is what joins a retrieval to
                     its context_log entry. -->
                {#if e.turn_number !== null && e.turn_number !== undefined}
                  <span class="muted">turn {e.turn_number}</span>
                {/if}
                <span class="muted">{e.legs.length} legs</span>
                {#if e.n_expansions > 0}
                  <span class="expn">{e.n_expansions} graph edges</span>
                {/if}
                {#if e.truncated}<span class="chip sm warnchip">capped</span>{/if}
                <span class="spacer"></span>
                <span class="muted" title={e.timestamp}>{fmtClock(e.timestamp)} · {fmtAge(e.timestamp, nowMs)}</span>
              </div>
            </button>
          </li>
        {:else}
          <li class="empty-cell">
            <!-- Say which of the two situations this is. An all-automated
                 window at the widened limit means older user retrievals exist
                 further back than the endpoint will return in one page. -->
            {#if hiddenCount > 0}
              All {hiddenCount} retrievals in the last {WIDE_LIMIT} were automated.
              Turn off “Hide automated” to see them — any of your own retrievals
              are older than this window.
            {:else}
              No retrievals recorded yet
            {/if}
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
                  {#if agg.attempted === 0}
                    <span class="muted sm">never fired</span>
                  {:else if agg.returned === 0}
                    <span class="muted sm">silent</span>
                  {/if}
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
            <!-- The columns are NOT NULL DEFAULT 0, so an unsampled row stores
                 0/0 — printing those as counts contradicts the "not sampled"
                 notice this same panel shows below. `candidates_by_disposition`
                 is the authoritative null-vs-empty signal the API preserves. -->
            {#if detail.candidates_by_disposition === null}
              <span class="muted">candidates not sampled</span>
            {:else}
              <span><strong>{detail.n_candidates}</strong> candidates</span>
              <span><strong>{detail.n_rendered}</strong> rendered</span>
            {/if}
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
              <tr><th>Leg</th><th class="num">Returned</th><th class="num">Corroborated</th><th class="num">Dropped</th><th>State</th></tr>
            </thead>
            <tbody>
              {#each detail.legs as leg, li (li)}
                <tr>
                  <td><code>{leg.name}</code></td>
                  <td class="num">{leg.n_returned}</td>
                  <td class="num muted">{leg.n_deduped || '—'}</td>
                  <!-- Exact on every retrieval, unlike the sampled candidate
                       array below: how many rows this leg fetched and then
                       discarded internally before returning. -->
                  <td class="num muted">{leg.n_dropped || '—'}</td>
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
                    <!-- A leg that RAN can still carry a skip_reason — it
                         explains why something about it captured less than
                         you'd expect (C1's vector-only chunk path reports no
                         discard set because its cut is pushed into SQL).
                         Rendering this only under !attempted, as before, meant
                         that explanation was persisted and never shown. -->
                    {#if leg.skip_reason && leg.attempted && !leg.error}
                      <span class="muted sm">{leg.skip_reason}</span>
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
            <!-- One summary line replaces the bipartite diagram this section
                 used to open with. That diagram spent ~800px of width and
                 three screens of scroll to encode "seed reached neighbour" —
                 which the tree below already states, alongside relation,
                 weight, strength, hop and provenance, none of which the
                 diagram showed at all. Its one unique contribution was
                 convergence, so that is now called out here in words. -->
            <div class="xsummary">
              {#if expansionTotals.seeds !== null}
                <span><strong>{expansionTotals.seeds}</strong> direct seeds</span>
              {:else}
                <span class="muted">multi-seed traversal only</span>
              {/if}
              <span><strong>{expansionTotals.edges}</strong> edges</span>
              <span><strong>{expansionTotals.neighbours}</strong> neighbours</span>
              {#if expansionTotals.convergent > 0}
                <span class="conv-note">
                  {expansionTotals.convergent} reached from more than one seed
                </span>
              {/if}
            </div>
            <!-- Provenance, deliberately NOT yield. Two separate reasons a
                 yield figure here would be false: an edge is only recorded for
                 a neighbour that reached the merged candidate set (so drops
                 are invisible), AND the edge is recorded before the duplicate
                 guard (so a neighbour that merely corroborated a direct hit
                 also has one). What IS answerable is how many neighbours the
                 graph actually put in the pool versus how many were already
                 there. -->
            {#if candidateIndex.size > 0}
              <p class="status-msg sm">
                {expansionTotals.viaGraph} of {expansionTotals.neighbours} neighbours entered
                the candidate pool through a graph leg{#if expansionTotals.viaDirect > 0}; {expansionTotals.viaDirect}
                  {expansionTotals.viaDirect === 1 ? 'was' : 'were'} already present from a
                  direct leg and {expansionTotals.viaDirect === 1 ? 'was' : 'were'} corroborated,
                  not contributed{/if}{#if expansionTotals.unknown > 0}; {expansionTotals.unknown}
                  could not be attributed — no
                  candidate record (capture stopped at the cap) or an entry leg this view does
                  not recognise{/if}.{#if expansionTotals.hasSpreading}
                  Spreading activation drops nodes inside its own stage without recording an
                  edge, so its reach is wider than shown.{/if}
              </p>
            {/if}

            {#each expansionsBySeed as group, gi (group.key)}
              <details class="seed-group" open={gi === 0}>
                <summary class="seed-head">
                  {#if group.isSpreading}
                    <span class="chip sm prov">spreading activation</span>
                    <span class="seed-label">multi-seed traversal, hop 2</span>
                  {:else}
                    <span class="chip sm t-{group.seed.seed_type}">{group.seed.seed_type}</span>
                    <span class="seed-label" title={nodeLabel(group.seed.seed_id)}>
                      {nodeLabel(group.seed.seed_id)}
                    </span>
                    <span class="muted sm nowrap">seed {fmtScore(group.seed.seed_score)}</span>
                  {/if}
                  <span class="muted sm nowrap">
                    · {group.items.length} neighbour{group.items.length === 1 ? '' : 's'}
                  </span>
                </summary>
                <ul class="edge-list">
                  {#each group.items as edge, ei (ei)}
                    {@const seeds = group.seedCount(edge)}
                    <li class:lost={!edge.won_best_path}>
                      <span class="rel">{edge.edge_relation ?? 'related'}</span>
                      <span class="chip sm t-{edge.neighbor_type}">{edge.neighbor_type}</span>
                      <span class="nbr-label" title={nodeLabel(edge.neighbor_id)}>
                        {nodeLabel(edge.neighbor_id)}
                      </span>
                      <span class="muted sm nowrap">
                        w {fmtScore(edge.edge_weight)} · str {fmtScore(edge.path_strength)}
                      </span>
                      <!-- Provenance is load-bearing, not decoration: inferred
                           edges take graph_inferred_edge_penalty, so without it
                           two otherwise-identical edges score differently with
                           no visible reason. -->
                      {#if edge.extraction_method}
                        <span class="chip sm prov">{edge.extraction_method}</span>
                      {/if}
                      {#if seeds > 1}
                        <span class="chip sm conv" title="Also reached from {seeds - 1} other seed(s)">
                          ×{seeds} seeds
                        </span>
                      {/if}
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
                  <span
                    class="badge disp-badge"
                    style="color: {dispositionColor(disp)}; border-color: {dispositionColor(disp)}55"
                  >{disp}</span>
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

  .funnel-alarm {
    display: block;
    margin-top: 0.15rem;
    font-size: 0.72rem;
    color: var(--red);
    font-variant-numeric: tabular-nums;
  }
  .funnel-cap {
    display: block;
    margin-top: 0.15rem;
    font-size: 0.7rem;
    color: var(--muted);
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
  /* Family separator: a hairline, not a heading. It groups the legend into
     capacity / quality / redundancy without spending a row on labels. */
  .disp-legend li.fam-sep {
    width: 1px;
    align-self: stretch;
    padding: 0;
    background: var(--border);
    margin: 0 0.15rem;
  }
  .disp-badge {
    border: 1px solid;
    background: transparent;
  }
  .disp-legend li {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    font-size: 0.75rem;
    cursor: default;
  }
  .dot { width: 8px; height: 8px; border-radius: 2px; display: inline-block; flex-shrink: 0; }
  .disp-legend .k { font-family: var(--font-mono, monospace); color: var(--text); }
  .disp-legend .pct { color: var(--muted); font-variant-numeric: tabular-nums; }

  .timing {
    display: flex;
    align-items: baseline;
    gap: 1.1rem;
    flex-wrap: wrap;
    margin-top: 0.85rem;
    padding-top: 0.7rem;
    border-top: 1px solid var(--border);
    font-size: 0.75rem;
    color: var(--muted);
  }
  .timing strong { color: var(--text); font-variant-numeric: tabular-nums; }
  .timing-label {
    font-size: 0.7rem;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }
  .timing .slow strong { color: var(--red); }
  .timing-slow { color: var(--yellow); margin-left: auto; }

  /* ── Session break ───────────────────────────────────────── */
  .sess-break {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.5rem 0.55rem 0.15rem;
  }
  .sess-id {
    font-family: var(--font-mono, monospace);
    font-size: 0.65rem;
    color: var(--muted);
    letter-spacing: 0.04em;
  }
  .sess-rule { flex: 1; height: 1px; background: var(--border); }

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


  /* ── Expansion tree ──────────────────────────────────────── */
  .xsummary {
    display: flex;
    flex-wrap: wrap;
    gap: 0.35rem 1.1rem;
    align-items: baseline;
    font-size: 0.78rem;
    color: var(--muted);
    padding: 0.5rem 0.7rem;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 6px;
    margin-bottom: 0.5rem;
  }
  .xsummary strong { color: var(--fg); font-variant-numeric: tabular-nums; }
  .conv-note { color: var(--accent); }
  /* The node's identity is the content, so it takes the flexible space and
     truncates; the numbers next to it are fixed-width and must never wrap. */
  .seed-label, .nbr-label {
    flex: 1 1 12rem;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .seed-label { color: var(--fg); font-weight: 500; }
  .nbr-label { color: var(--fg); }
  .nowrap { white-space: nowrap; flex: 0 0 auto; }
  .chip.conv { color: var(--accent); border-color: color-mix(in srgb, var(--accent) 45%, transparent); }

  .edge-list { list-style: none; margin: 0.2rem 0 0; padding: 0 0 0 1rem; border-left: 2px solid var(--border); }
  .edge-list li {
    display: flex; gap: 0.4rem; align-items: center; flex-wrap: wrap;
    padding: 0.16rem 0; font-size: 0.75rem;
  }
  .edge-list li.lost { opacity: 0.5; }
  .rel { font-family: var(--font-mono, monospace); color: var(--accent); }

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
  .chip.prov { color: var(--muted); border-style: dashed; }
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
