<script lang="ts">
  // F092: companion shell — surface feed + focused deep-link view.
  //
  // Routing is ~25 own lines, NOT a reuse of src/lib/router.ts: that router
  // validates against a closed ROUTES union and would send arbitrary
  // surface ids to 'overview'. The `initialized` guard is copied from it —
  // it stops hashchange listeners stacking across tests and HMR.
  import { onMount } from 'svelte';
  import { resolveDynamic, toDisplayString } from './functions';
  import InstallPrompt from './InstallPrompt.svelte';
  import Renderer from './Renderer.svelte';
  import { store } from './store.svelte';
  import { transport } from './transport';

  type View = { view: 'feed' } | { view: 'surface'; id: string };

  function parseHash(): View {
    const h = location.hash;
    // #/s/<id> (Phase 1) and #/a/<id> (F092.1 §7 per-app deep links) are
    // aliases — an app is a surface; the /companion/a/<id> server route
    // redirects into the latter.
    const m = /^#\/[sa]\/(.+)$/.exec(h);
    if (m) return { view: 'surface', id: decodeURIComponent(m[1]) };
    return { view: 'feed' };
  }

  const KIND_LABELS: Record<string, string> = {
    micro_app: 'app',
    approval_gate: 'approval',
    action_review: 'review',
    heartbeat_findings: 'findings',
    decision_sweep: 'decisions',
    dag_monitor: 'DAG',
    memory_graph: 'graph',
  };

  function kindOf(surfaceId: string): string {
    // Surface ids are minted as nous:<origin>:<kind>:<hex> — the kind
    // segment is stable for the surface's lifetime.
    return surfaceId.split(':')[2] ?? '';
  }

  /** Chip labels are ~a dozen characters before they crowd the switcher.
   * Cut on a word boundary when one is close to the limit, else hard-cut. */
  const CHIP_MAX = 22;
  function shorten(title: string): string {
    const t = title.replace(/\s+/g, ' ').trim();
    // Cut on GRAPHEME CLUSTERS. `slice` counts UTF-16 units and splits a
    // surrogate pair; `[...t]` counts code points and still splits a cluster —
    // a flag is two regional indicators, and a letter plus a combining mark is
    // two code points that render as one character (codex P2, twice). Only
    // segmentation gets this right; code points remain the fallback where
    // Intl.Segmenter is unavailable, since it is still better than units.
    const units =
      typeof Intl !== 'undefined' && 'Segmenter' in Intl
        ? [...new Intl.Segmenter(undefined, { granularity: 'grapheme' }).segment(t)].map(
            (s) => s.segment,
          )
        : [...t];
    if (units.length <= CHIP_MAX) return t;
    // Find the word boundary in GRAPHEME units too. `lastIndexOf` returns a
    // UTF-16 offset, which was then compared against the grapheme-based
    // CHIP_MAX \u2014 mixing units, so astral graphemes before a space made the
    // space look far earlier than it is. Two distinct names could collapse to
    // the same truncated chip (codex P2).
    const kept = units.slice(0, CHIP_MAX);
    const sp = kept.lastIndexOf(' ');
    const body = (sp >= CHIP_MAX - 8 ? kept.slice(0, sp) : kept).join('');
    return body.replace(/[\s:,\u2014-]+$/, '') + '\u2026';
  }

  /** An app's AppHeader is structurally mandatory (lint_micro_app: it must
   * be the first top-level child) and its title is authored SHORT for
   * display — "Crypto Note", not the record title "Crypto Note: Six
   * Months, Forward View". That makes it the better chip label, and it
   * needs no server round-trip: it is already in the components we render. */
  function headerTitle(surface: {
    components?: Record<string, { component?: string; title?: unknown; children?: unknown }>;
    dataModel?: unknown;
  }): string {
    const comps = surface.components ?? {};
    // Reach the header THROUGH the current root, the way Renderer does.
    // `updateComponents` merges by id and never deletes, so a refine that
    // replaces the header under a new id leaves the old one as an invisible
    // orphan — and scanning by TYPE returns that stale orphan first, since
    // object insertion order is preserved. The chip would then disagree with
    // the header actually on screen (codex P2).
    const rootChildren = comps['root']?.children;
    const ids = Array.isArray(rootChildren)
      ? rootChildren.filter((c): c is string => typeof c === 'string')
      : [];
    const headerId = ids.find((id) => comps[id]?.component === 'AppHeader');
    const header = headerId ? comps[headerId] : undefined;
    if (!header) return '';
    // Resolve exactly as AppHeaderView does: a bound title ({path} or a
    // function call) renders fine there, so a `typeof === 'string'` check
    // would have rejected a title the user can plainly see. Trim HERE, since
    // a whitespace-only title is truthy and would short-circuit the
    // record-title fallback, landing back on "app".
    const ctx = { dataModel: (surface.dataModel ?? {}) as Record<string, unknown>, scope: null };
    return toDisplayString(resolveDynamic(header.title, ctx)).trim();
  }

  type ChipSurface = {
    surfaceId: string;
    title?: string;
    components?: Record<string, { component?: string; title?: unknown; children?: unknown }>;
    dataModel?: unknown;
  };

  /** The FULL (untruncated) name a micro-app chip stands for. Shared by the
   * label AND the tooltip: the tooltip exists to reveal what truncation hid,
   * so deriving it from a different string defeats it — a header title that
   * differs from the record title showed one thing on the chip and another on
   * hover (codex P2). */
  function chipName(surface: ChipSurface): string {
    if (kindOf(surface.surfaceId) !== 'micro_app') return '';
    return (headerTitle(surface) || surface.title || '').trim();
  }

  function chipTooltip(surface: ChipSurface): string {
    const name = chipName(surface) || surface.title || '';
    return name ? name + ' — ' + surface.surfaceId : surface.surfaceId;
  }

  function chipLabel(surface: ChipSurface): string {
    const kind = kindOf(surface.surfaceId);
    // Every composed app's id carries the SAME kind segment ("micro_app"),
    // so KIND_LABELS can only ever say "app" — N live apps become N chips
    // reading "app". Its own title is the only thing that distinguishes
    // them. Template kinds keep the curated label: it is shorter and more
    // scannable than their titles, which carry timestamps.
    if (kind === 'micro_app') {
      // Header title first (short, authored); the record title from
      // createSurface metadata is the fallback for a headerless surface.
      const name = chipName(surface);
      if (name) return shorten(name);
    }
    return KIND_LABELS[kind] ?? kind ?? surface.surfaceId.slice(-4);
  }

  let route = $state<View>(parseHash());
  let initialized = false;

  onMount(() => {
    route = parseHash();
    if (!initialized) {
      initialized = true;
      window.addEventListener('hashchange', () => {
        route = parseHash();
      });
    }
    void transport.connect();
    return () => transport.stop();
  });

  const feed = $derived(store.ordered());
  const focused = $derived(route.view === 'surface' ? store.surfaces[route.id] : null);

  // F092.1 §7: several live apps at once is now the normal case — the
  // switcher gives one-tap focus per surface, and close-all sweeps the
  // disposable micro-apps (they are rebuilt, not restored, by design).
  const microApps = $derived(feed.filter((s) => kindOf(s.surfaceId) === 'micro_app'));
  let closingAll = $state(false);

  async function closeAllApps() {
    if (closingAll) return;
    closingAll = true;
    try {
      // Sequential on purpose: app.close posts share the server rate limit
      // with everything else, and each close's footer is its own component.
      for (const surface of microApps) {
        await transport.postAction(surface.surfaceId, 'app.close', 'footer', {});
      }
    } finally {
      closingAll = false;
    }
  }
</script>

<div class="shell">
  <header>
    <a class="brand" href="#/">Nous <span>Companion</span></a>
    <span class="conn {store.connection}">{store.connection}</span>
  </header>

  <InstallPrompt />

  {#if feed.length > 1}
    <nav class="switcher" aria-label="live surfaces">
      <a class="chip" class:active={route.view === 'feed'} href="#/">all ({feed.length})</a>
      {#each feed as surface (surface.surfaceId)}
        <a
          class="chip"
          class:active={route.view === 'surface' && route.id === surface.surfaceId}
          href={'#/s/' + encodeURIComponent(surface.surfaceId)}
          title={chipTooltip(surface)}
        >
          {chipLabel(surface)}
        </a>
      {/each}
      {#if microApps.length >= 2}
        <button class="chip close-all" disabled={closingAll} onclick={() => void closeAllApps()}>
          {closingAll ? 'closing…' : `close all apps (${microApps.length})`}
        </button>
      {/if}
    </nav>
  {/if}

  <main>
    {#if route.view === 'surface'}
      {#if focused}
        <section class="surface" data-theme={focused.theme || null} aria-label={focused.surfaceId}>
          <Renderer surfaceId={focused.surfaceId} componentId="root" />
        </section>
      {:else}
        <p class="empty">
          Surface not found — it may have resolved or expired.
          <a href="#/">Back to feed</a>
        </p>
      {/if}
    {:else if feed.length === 0}
      <p class="empty">
        Nothing needs you right now. Surfaces Nous pushes — escalations,
        action reviews, triage — appear here.
      </p>
    {:else}
      {#each feed as surface (surface.surfaceId)}
        <section class="surface" data-theme={surface.theme || null} aria-label={surface.surfaceId}>
          <a class="permalink" href={'#/s/' + encodeURIComponent(surface.surfaceId)}>⧉</a>
          <Renderer surfaceId={surface.surfaceId} componentId="root" />
        </section>
      {/each}
    {/if}
  </main>
</div>

<style>
  .shell {
    max-width: 720px;
    margin: 0 auto;
    padding: 1rem 1rem calc(2rem + env(safe-area-inset-bottom));
    display: flex;
    flex-direction: column;
    gap: 1rem;
  }
  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0.25rem 0;
  }
  .brand {
    color: var(--text);
    text-decoration: none;
    font-weight: 600;
    font-size: 1.05rem;
  }
  .brand span {
    color: var(--accent);
  }
  .conn {
    font-size: 0.75rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    padding: 0.15rem 0.6rem;
    border-radius: 999px;
    border: 1px solid var(--border);
    color: var(--muted);
  }
  .conn.live {
    color: var(--green);
    border-color: var(--green);
  }
  .conn.error,
  .conn.resyncing {
    color: var(--yellow);
    border-color: var(--yellow);
  }
  .switcher {
    display: flex;
    gap: 0.4rem;
    flex-wrap: wrap;
    align-items: center;
  }
  .chip {
    font: inherit;
    font-size: 0.78rem;
    color: var(--muted);
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 999px;
    padding: 0.2rem 0.7rem;
    text-decoration: none;
    cursor: pointer;
    transition: var(--transition);
  }
  .chip:hover {
    border-color: var(--accent);
    color: var(--text);
  }
  .chip.active {
    color: var(--accent);
    border-color: var(--accent);
  }
  .chip.close-all {
    margin-left: auto;
    color: var(--muted);
    background: none;
  }
  .chip.close-all:hover:not(:disabled) {
    color: var(--red);
    border-color: var(--red);
  }
  .chip.close-all:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
  main {
    display: flex;
    flex-direction: column;
    gap: 1rem;
  }
  .surface {
    position: relative;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 1.1rem;
  }
  .permalink {
    position: absolute;
    top: 0.6rem;
    right: 0.8rem;
    color: var(--muted);
    text-decoration: none;
    font-size: 0.85rem;
  }
  .permalink:hover {
    color: var(--accent);
  }
  .empty {
    color: var(--muted);
    text-align: center;
    padding: 3rem 1rem;
  }
  .empty a {
    color: var(--accent);
  }
</style>
