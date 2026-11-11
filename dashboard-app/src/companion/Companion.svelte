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
    intention_proposal: 'proposal',
    intention_question: 'question',
    intention_active: 'intentions',
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

  // Functions that only READ and format. An allowlist, not a denylist, so a new
  // effectful function is unsafe by DEFAULT here — the direction that fails
  // safe. `openUrl` is excluded (it calls window.open) and `@index` too (it
  // throws at the null scope a chip resolves in).
  //
  // `formatString` is ALSO excluded, despite being pure itself: its template
  // can encode a call as TEXT — `"${openUrl(url:'…')}"` — which the recursive
  // object check below cannot see, because the payload is a primitive string.
  // It is the sole entry point to the `${…}` scanner (functions.ts:242), so
  // excluding it closes that hole outright. The alternative — a second parser
  // for interpolations — would have to track the real scanner forever, and a
  // drift there means silent unsolicited navigation. A formatString title
  // simply falls back to the record title (codex P2).
  const PURE_TITLE_FNS = new Set([
    'formatNumber',
    'formatCurrency',
    'formatDate',
    'pluralize',
    'length',
    'and',
    'or',
    'not',
    'regex',
    'numeric',
    'email',
  ]);

  /** True when a title can be resolved WITHOUT side effects. Recurses through
   * arguments, because `{call:"formatString", args:{value:{call:"openUrl"}}}`
   * is a pure call wrapping an effectful one — checking only the outer name
   * would wave it straight through. */
  function isPureTitle(v: unknown, depth = 0): boolean {
    if (depth > 6) return false;
    if (v === null || typeof v !== 'object') return true;
    if (Array.isArray(v)) return v.every((x) => isPureTitle(x, depth + 1));
    const call = (v as { call?: unknown }).call;
    if (typeof call === 'string' && !PURE_TITLE_FNS.has(call)) return false;
    return Object.values(v as Record<string, unknown>).every((x) =>
      isPureTitle(x, depth + 1),
    );
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
    // Trim whatever we return: a whitespace-only title is truthy and would
    // short-circuit the record-title fallback, landing back on "app".
    const raw = header.title;
    // A LABEL MUST NOT CAUSE EFFECTS. `title` is a DynamicString, so it may
    // hold a function call — and `openUrl` calls `window.open`. Chips resolve
    // EVERY live surface, twice (label + tooltip), so an effectful call fires
    // merely because a surface exists in the feed (codex P2). Only provably
    // pure titles are evaluated — see isPureTitle, which recurses so a pure
    // wrapper cannot smuggle an effectful argument. Everything else falls back
    // to the record title.
    if (typeof raw === 'string') return raw.trim();
    if (!isPureTitle(raw)) return '';
    const ctx = { dataModel: (surface.dataModel ?? {}) as Record<string, unknown>, scope: null };
    try {
      return toDisplayString(resolveDynamic(raw, ctx)).trim();
    } catch {
      // Even a pure read can throw on a hostile model; a label is never worth
      // the shell, which resolves every background app's title.
      return '';
    }
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
  // Two-tap confirmation, same pattern as the per-app close: this sweeps EVERY
  // live micro-app in one press, and apps are rebuilt, not restored. First tap
  // arms ("sure? close N apps", auto-disarms after 4s); second executes.
  let closeAllArmed = $state(false);
  let closeAllTimer: ReturnType<typeof setTimeout> | undefined;
  // SNAPSHOT taken at arm time (codex P1): the confirmation is for the set
  // the user SAW. An app arriving during the 4s window must not be swept by
  // a confirmation that never showed it, and an app closing on its own must
  // not shift the count under the user's finger.
  let armedIds = $state<string[]>([]);

  function requestCloseAll() {
    if (closingAll) return;
    if (!closeAllArmed) {
      closeAllArmed = true;
      armedIds = microApps.map((s) => s.surfaceId);
      clearTimeout(closeAllTimer);
      closeAllTimer = setTimeout(() => {
        closeAllArmed = false;
        armedIds = [];
      }, 4000);
      return;
    }
    clearTimeout(closeAllTimer);
    closeAllArmed = false;
    void closeAllApps(armedIds);
    armedIds = [];
  }

  async function closeAllApps(ids: string[]) {
    if (closingAll) return;
    closingAll = true;
    try {
      // Only surfaces still live from the CONFIRMED snapshot — sequential on
      // purpose: app.close posts share the server rate limit with everything
      // else, and each close's footer is its own component.
      const live = new Set(microApps.map((s) => s.surfaceId));
      for (const id of ids) {
        if (!live.has(id)) continue; // closed itself during the window
        await transport.postAction(id, 'app.close', 'footer', {});
      }
    } finally {
      closingAll = false;
    }
  }
</script>

<!-- The Atlantic-blue bar runs edge to edge and up under the status bar of an
     installed app; the shell below keeps the reading width. The fixed strip
     keeps navy under the (white) status-bar text once the page scrolls the bar
     away; it is the top safe-area inset tall, so 0 in a browser tab. -->
<div class="status-fill" aria-hidden="true"></div>
<header class="rail">
  <div class="rail-inner">
    <a class="brand" href="#/">Nous <span>Companion</span></a>
    <span class="conn {store.connection}">{store.connection}</span>
  </div>
</header>

<div class="shell">
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
        <button
          class="chip close-all"
          class:armed={closeAllArmed}
          disabled={closingAll}
          onclick={requestCloseAll}
        >
          {closingAll
            ? 'closing…'
            : closeAllArmed
              ? `sure? close ${armedIds.length} apps`
              : `close all apps (${microApps.length})`}
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
        <section class="surface linked" data-theme={surface.theme || null} aria-label={surface.surfaceId}>
          <a class="permalink" href={'#/s/' + encodeURIComponent(surface.surfaceId)}>Link</a>
          <Renderer surfaceId={surface.surfaceId} componentId="root" />
        </section>
      {/each}
    {/if}
  </main>
</div>

<style>
  /* The bar: chrome outside every surface, so it reads the paper :root's
     chrome tokens (--rail*) and never a theme. black-translucent +
     viewport-fit=cover extends an installed app's viewport UNDER the status
     bar / notch / Dynamic Island, so the bar takes the top inset (env() is 0
     in a browser tab) and its navy fills the space behind the status text.
     Left/right insets cover landscape notches. */
  .rail {
    background: var(--rail);
    color: var(--rail-ink);
    padding: calc(0.9rem + env(safe-area-inset-top)) calc(1rem + env(safe-area-inset-right)) 0.9rem
      calc(1rem + env(safe-area-inset-left));
  }
  .status-fill {
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
    height: env(safe-area-inset-top);
    background: var(--rail);
    z-index: 10;
    pointer-events: none;
  }
  .rail-inner {
    max-width: 688px; /* the shell's 720px less its padding: the brand lines up with the cards */
    margin: 0 auto;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.75rem;
  }
  .shell {
    max-width: 720px;
    margin: 0 auto;
    padding: 1rem calc(1rem + env(safe-area-inset-right)) calc(2rem + env(safe-area-inset-bottom))
      calc(1rem + env(safe-area-inset-left));
    display: flex;
    flex-direction: column;
    gap: 1rem;
  }
  .brand {
    color: var(--rail-ink);
    text-decoration: none;
    font-weight: 700;
    font-size: 1.2rem;
    letter-spacing: -0.01em;
  }
  .brand span {
    color: var(--rail-mute);
    font-weight: 500;
  }
  .brand:focus-visible,
  .conn:focus-visible {
    outline: 2px solid var(--rail-focus);
    outline-offset: 2px;
    border-radius: 6px;
  }
  /* The connection state: a word on a light pill, in a status ink when it
     matters (never colour alone: the word is the state). */
  .conn {
    font-size: 0.75rem;
    font-weight: 600;
    padding: 0.15rem 0.65rem;
    border-radius: 999px;
    background: var(--rail-ink);
    color: var(--rail);
  }
  .conn.live {
    color: var(--ok);
  }
  .conn.error,
  .conn.resyncing {
    color: var(--warn);
  }
  /* The live surfaces: pill segments on the paper ground. */
  .switcher {
    display: flex;
    gap: 0.4rem;
    flex-wrap: wrap;
    align-items: center;
  }
  .chip {
    font: inherit;
    font-size: 0.8rem;
    color: var(--muted);
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--ctl-radius);
    padding: 0.25rem 0.8rem;
    text-decoration: none;
    cursor: pointer;
    transition: var(--transition);
  }
  .chip:hover {
    border-color: var(--accent);
    color: var(--text);
  }
  .chip.active {
    color: var(--on-accent);
    background: var(--accent);
    border-color: var(--accent);
    font-weight: 600;
  }
  .chip:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
  }
  .chip.close-all.armed {
    color: var(--crit);
    border-color: var(--crit);
  }
  .chip.close-all {
    margin-left: auto;
    color: var(--muted);
    background: none;
  }
  .chip.close-all:hover:not(:disabled) {
    color: var(--crit);
    border-color: var(--crit);
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
  /* A surface is a card in its own theme: these tokens resolve at the
     section, which carries data-theme. The named themes keep the old card
     (radius 12, a hairline border, no shadow) through the legacy group. */
  .surface {
    position: relative;
    background: var(--surface);
    border: var(--card-border);
    border-radius: var(--radius);
    box-shadow: var(--card-shadow);
    padding: 1.1rem;
  }
  /* In the feed a card carries its "Link" (to the focused view) in a strip of
     its own above the content, so the word never sits on the surface's first
     component (an app's header stamp, an ApprovalPanel's border). */
  .surface.linked {
    padding-top: 2rem;
  }
  .permalink {
    position: absolute;
    top: 0.55rem;
    right: 1.1rem;
    color: var(--muted);
    text-decoration: none;
    font-size: 0.75rem;
    font-weight: 500;
    line-height: 1.2;
  }
  .permalink:hover {
    color: var(--accent);
    text-decoration: underline;
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
