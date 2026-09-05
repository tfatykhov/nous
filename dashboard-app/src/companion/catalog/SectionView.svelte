<script lang="ts">
  // F092.1 Section — titled content block. Light chrome (title + top rule):
  // the companion shell already wraps every surface in a card, and five
  // nested cards read as boxes-in-boxes. The visible border is reserved for
  // provenance === 'model': data no registered source produced, rendered
  // amber with a chip so the gap is visible, never silent.
  import Renderer from '../Renderer.svelte';
  import { store } from '../store.svelte';
  import { resolveDynamic, toDisplayString } from '../functions';
  import { pendingActionOf, pendingActivity } from '../activity';
  import type { Scope } from '../pointer';
  import type { A2uiComponent } from '../store.svelte';

  let {
    surfaceId,
    comp,
    scope = null,
    depth = 0,
    ancestors = [],
  }: {
    surfaceId: string;
    comp: A2uiComponent;
    scope?: Scope | null;
    depth?: number;
    ancestors?: readonly string[];
  } = $props();

  // Static enum prop, narrowed against an allowlist — never a binding
  // (app_spec's provenance map is server-side audit and never ships).
  const modelSupplied = $derived(comp.provenance === 'model');
  // F093 §6.1 layout enum. hero enlarges the heading; grid/rail turn the
  // child's DIRECT container into a grid/scroll rail via :global below (the
  // child is a Column/Row whose items become the cells). Unknown → stack.
  // F096 §4.2 adds `cards`: an auto-fit grid of ≥220px MetricCards/ScoreCards.
  const LAYOUTS = ['stack', 'hero', 'grid-2', 'grid-3', 'rail', 'accordion', 'cards'];
  const layout = $derived(
    typeof comp.layout === 'string' && LAYOUTS.includes(comp.layout) ? comp.layout : 'stack',
  );
  // F096 §4.1 — a DynamicString qualifier in the head: source attribution,
  // the comparison window, a count. Bound or literal; absent ⇒ nothing.
  const ctx = $derived({ dataModel: store.surfaces[surfaceId]?.dataModel ?? {}, scope });
  // F092.4: while the app is working (footer call in flight, or a fresh
  // agent-action stamp) the data dims to 72% — visible, never hidden, and
  // clearly about to change. Same definition of "working" as the header.
  const meta = $derived((store.surfaces[surfaceId]?.dataModel as Record<string, unknown> | undefined)?.meta);
  let nowTick = $state(Date.now());
  $effect(() => {
    if (!pendingActionOf(meta)) return;
    const t = setInterval(() => (nowTick = Date.now()), 10_000);
    return () => clearInterval(t);
  });
  const dimmed = $derived(store.activity[surfaceId] !== undefined || pendingActivity(meta, nowTick) !== null);
  const caption = $derived(toDisplayString(resolveDynamic(comp.caption, ctx)));
  // Accordion open state is renderer-local, exactly like Tabs selection: the
  // catalog gives Section no `open` property, so whether a section is
  // expanded is a view concern that never round-trips the server. Collapsed
  // by default — the whole point is that long secondary detail stays out of
  // the way until asked for.
  let open = $state(false);
  const collapsible = $derived(layout === 'accordion');
</script>

<section class="app-section {layout}" class:model={modelSupplied} class:open class:dim={dimmed}>
  {#if collapsible}
    <!-- APG disclosure: the BUTTON goes inside the H3, not the reverse — an
         h3 inside a button is invalid button content, and screen readers
         navigating by headings would skip every collapsed section (codex P2). -->
    <h3 class="head toggle-head">
      <button class="toggle" aria-expanded={open} onclick={() => (open = !open)}>
        <span class="title-text">{comp.title}</span>
        {#if modelSupplied}
          <span class="chip">model-supplied</span>
        {/if}
        {#if caption}
          <span class="caption">{caption}</span>
        {/if}
        <span class="caret" aria-hidden="true">{open ? '▾' : '▸'}</span>
      </button>
    </h3>
  {:else}
    <div class="head">
      <h3>{comp.title}</h3>
      {#if modelSupplied}
        <span class="chip">model-supplied</span>
      {/if}
      {#if caption}
        <span class="caption">{caption}</span>
      {/if}
    </div>
  {/if}
  <!-- A collapsed panel is NOT rendered at all, mirroring Tabs: an invisible
       subtree would still run its bindings and effects. -->
  {#if typeof comp.child === 'string' && (!collapsible || open)}
    <Renderer {surfaceId} componentId={comp.child} {scope} {depth} {ancestors} />
  {/if}
</section>

<style>
  .app-section {
    border-top: 1px solid var(--border);
    padding: 0.6rem 0 0.2rem;
  }
  .app-section.dim {
    opacity: 0.72;
    transition: opacity var(--transition);
  }
  .app-section.model {
    border-left: 3px solid var(--warn);
    padding-left: 0.7rem;
  }
  .toggle-head {
    margin: 0 0 0.45rem;
  }
  .toggle {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    width: 100%;
    background: none;
    border: none;
    padding: 0;
    cursor: pointer;
    text-align: left;
    /* inherit the h3's type styles so the title looks identical to a plain
       section heading */
    font: inherit;
    color: inherit;
    text-transform: inherit;
    letter-spacing: inherit;
  }
  .toggle:hover .title-text {
    color: var(--text);
  }
  .caret {
    color: var(--muted);
    margin-left: auto;
    font-size: 0.8rem;
  }
  .head {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    margin-bottom: 0.45rem;
  }
  h3 {
    margin: 0;
    font-size: 0.82rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--muted);
  }
  .chip {
    font-size: 0.7rem;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--warn);
    border: 1px solid var(--warn);
    border-radius: 999px;
    padding: 0.1rem 0.5rem;
  }

  /* hero: the primary section — larger heading, more air. */
  .app-section.hero {
    padding-top: 1rem;
  }
  .app-section.hero h3 {
    font-size: 1rem;
    text-transform: none;
    letter-spacing: -0.01em;
    color: var(--text);
    font-family: var(--font-display);
  }

  /* grid/rail reshape the child's direct container (a Column/Row → .col/.row,
     a StatRow → .stat-row) so its items become cells. :global reaches the
     rendered child, which Svelte places as a direct descendant. */
  .app-section.grid-2 > :global(.col),
  .app-section.grid-2 > :global(.row) {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 0.6rem;
  }
  /* Two fixed columns on a 390px phone are ~155px each — a DeltaList row
     (label · delta · from → to) collapsed its label to one letter per line
     in the F096 browser check. Collapse to one column when the SECTION is
     narrower than ~520px (a container query, not a viewport one: the
     surface is also narrow in the switcher's split views), the way the
     reference report does at ≤760px. The renderer owns breakpoints. */
  .app-section.grid-2 {
    container-type: inline-size;
    /* A size container's inline size cannot depend on its contents, so as a
       NON-stretched flex item (root Column with align start/center/end) the
       section would shrink to its padding and its grid would overflow (codex
       P2 on #631). A definite width resolves against the root instead. */
    width: 100%;
    min-width: 0;
  }
  @container (max-width: 520px) {
    .app-section.grid-2 > :global(.col),
    .app-section.grid-2 > :global(.row) {
      grid-template-columns: minmax(0, 1fr);
    }
  }
  .app-section.grid-3 > :global(.col),
  .app-section.grid-3 > :global(.row) {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
    gap: 0.6rem;
  }
  /* F096 §4.2 cards: grid-3's 140px is sized for tiles; a MetricCard with a
     56px sparkline needs ~220px or its caption wraps twice and the chart
     becomes a smear. One column on a phone, two at the shell's 720px max. */
  .app-section.cards > :global(.col),
  .app-section.cards > :global(.row) {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    gap: 0.6rem;
  }
  /* F096 §4.1 caption: the right-aligned qualifier (source, window, count). */
  .caption {
    margin-left: auto;
    color: var(--muted);
    font-size: 0.72rem;
    letter-spacing: 0.04em;
    text-align: right;
    text-transform: none;
    /* A long caption wraps on a phone instead of forcing the TITLE to wrap
       (browser check: "a stale lane silently freezes its trend" broke the
       "Data freshness" heading in two). */
    max-width: 58%;
    overflow-wrap: anywhere;
  }
  .toggle .caption + .caret {
    margin-left: 0;
  }
  .app-section.rail > :global(.col),
  .app-section.rail > :global(.row) {
    display: flex;
    flex-direction: row;
    /* A Row child keeps its own flex-wrap: wrap, so without this reset rail
       items wrap onto new lines instead of scrolling horizontally (codex P2). */
    flex-wrap: nowrap;
    gap: 0.6rem;
    overflow-x: auto;
    padding-bottom: 0.3rem;
  }
  .app-section.rail > :global(.col) > :global(*),
  .app-section.rail > :global(.row) > :global(*) {
    flex: 0 0 auto;
    min-width: 160px;
  }
</style>
