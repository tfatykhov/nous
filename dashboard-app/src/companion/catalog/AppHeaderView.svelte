<script lang="ts">
  // F092.1 AppHeader — title, subtitle, and the mandatory freshness stamp.
  // composedAt is a BINDING (app.refresh patches /meta/composedAt in the
  // data model; a literal would keep saying "2h ago" over fresh data).
  // The stamp ticks: nowMs updates every 30s via $effect with teardown —
  // a stamp that said "just now" forever would be worse than none.
  //
  // F092.4 activity indicator: while the app's footer has a call in flight
  // (refresh / refine) or the server holds a fresh /meta/pendingAction (an
  // agent action), the stamp becomes the live status — pulsing dot, verb,
  // ticking elapsed time — and a 2px rail runs along the top edge of the
  // card. The stamp is the app's clock already, so it is the natural place
  // for "what is the agent doing to this data right now". Elapsed time is
  // shown for every kind; no budget is promised for agent actions. When a
  // call succeeds the stamp flashes "updated just now" for DONE_FLASH_MS,
  // then the normal freshness stamp returns. A pending action that goes
  // stale reads "no update after …" in amber, the same degradation the
  // footer's note already performs.
  //
  // Two review findings shaped the details. An agent action counts as
  // done only when the app was RECOMPOSED after the tap (/meta/composedAt
  // moves past the stamp's `at`) — the failure watcher also clears the
  // stamp, so "stamp gone" alone would flash "updated just now" over an
  // action that explicitly failed (codex P1); that detection lives in the
  // STORE at envelope arrival (observe), because the recompose replaces
  // the surface (this header is destroyed and remounted) and may land
  // while the user is on another surface — this header only reads doneAt.
  // And the screen-reader announcement is a persistent polite live region
  // that carries only the transitions; the ticking elapsed value lives
  // outside it, or a refresh would re-announce every second for its whole
  // duration (codex P2).
  import { store } from '../store.svelte';
  import { resolveDynamic, toDisplayString } from '../functions';
  import { formatFreshness } from '../freshness';
  import {
    ACTIVITY_VERBS,
    DONE_FLASH_MS,
    formatElapsed,
    pendingActionOf,
    pendingActivity,
    pendingIsFresh,
  } from '../activity';
  import type { Scope } from '../pointer';
  import type { A2uiComponent } from '../store.svelte';

  let {
    surfaceId,
    comp,
    scope = null,
  }: {
    surfaceId: string;
    comp: A2uiComponent;
    scope?: Scope | null;
    depth?: number;
    ancestors?: readonly string[];
  } = $props();

  let nowMs = $state(Date.now());

  const dataModel = $derived(store.surfaces[surfaceId]?.dataModel ?? {});
  const ctx = $derived({ dataModel, scope });
  const meta = $derived(
    (dataModel as Record<string, unknown>).meta as Record<string, unknown> | undefined,
  );
  const title = $derived(toDisplayString(resolveDynamic(comp.title, ctx)));
  const subtitle = $derived(toDisplayString(resolveDynamic(comp.subtitle, ctx)));
  const composedAt = $derived(toDisplayString(resolveDynamic(comp.composedAt, ctx)));
  const staleAfterS = $derived(typeof comp.staleAfterS === 'number' ? comp.staleAfterS : 3600);
  const freshness = $derived(formatFreshness(composedAt, nowMs, staleAfterS));
  // F096 §4.4 — the data-reach line ("data through 2026-09-01"): the stamp
  // says when the app was composed, the note says how far the data reaches.
  const note = $derived(toDisplayString(resolveDynamic(comp.note, ctx)));

  // One definition of "working" (activity.ts): the footer's in-flight call
  // wins, else a fresh server stamp.
  const pending = $derived(pendingActionOf(meta));
  const pendingFresh = $derived(pendingIsFresh(pending, nowMs));
  const activity = $derived(store.activity[surfaceId] ?? pendingActivity(meta, nowMs));
  const elapsed = $derived(activity ? formatElapsed(nowMs - activity.startedAt) : '');
  const flashing = $derived(!activity && nowMs - (store.doneAt[surfaceId] ?? 0) < DONE_FLASH_MS);
  const pendingStale = $derived(pending !== null && !pendingFresh);

  // Tick fast only while there is something to count; the idle stamp only
  // needs to notice minutes.
  $effect(() => {
    const fast = activity !== null || flashing;
    const id = setInterval(() => {
      nowMs = Date.now();
    }, fast ? 1000 : 30_000);
    return () => clearInterval(id);
  });

  // What assistive tech hears: ONE persistent polite region whose text
  // changes only on a transition. The visible stamps below are aria-hidden
  // so the same words are not read twice, and the elapsed counter is never
  // inside a live region.
  const announcement = $derived(
    activity
      ? ACTIVITY_VERBS[activity.kind]
      : flashing
        ? 'updated just now'
        : pendingStale
          ? `no update after ${formatElapsed(pending?.staleMs ?? 0)}`
          : '',
  );
</script>

{#if activity}
  <div class="rail" aria-hidden="true"><div class="fill"></div></div>
{/if}
<header class="app-header">
  <div class="titles">
    <h2>{title}</h2>
    {#if subtitle}
      <p class="subtitle">{subtitle}</p>
    {/if}
  </div>
  <span class="meta">
    <span class="sr-only" role="status" aria-live="polite">{announcement}</span>
    {#if activity}
      <span class="stamp working" aria-hidden="true">
        <i class="dot"></i>{ACTIVITY_VERBS[activity.kind]} · <span class="elapsed">{elapsed}</span>
      </span>
    {:else if flashing}
      <span class="stamp done" aria-hidden="true">
        <svg class="check" viewBox="0 0 16 16" aria-hidden="true"><path d="M3 8.5l3 3 7-7" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" /></svg>
        updated just now
      </span>
    {:else if pendingStale}
      <span class="stamp stale" aria-hidden="true"><i class="dot warn"></i>no update after {formatElapsed(pending?.staleMs ?? 0)}</span>
    {:else}
      <span class="stamp" class:stale={freshness.stale}>{freshness.label}</span>
    {/if}
    {#if note}<span class="note">{note}</span>{/if}
  </span>
</header>

<style>
  .app-header {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 0.8rem;
    flex-wrap: wrap;
  }
  h2 {
    margin: 0;
    font-size: 1.15rem;
    line-height: 1.3;
  }
  .subtitle {
    margin: 0.15rem 0 0;
    color: var(--muted);
    font-size: 0.88rem;
  }
  .meta {
    display: flex;
    flex-direction: column;
    align-items: flex-end;
    text-align: right;
  }
  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    margin: -1px;
    padding: 0;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
    border: 0;
  }
  .stamp {
    color: var(--muted);
    font-size: 0.75rem;
    white-space: nowrap;
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
  }
  .stamp.stale {
    color: var(--warn);
  }
  .stamp.working {
    color: var(--text);
  }
  .stamp.done {
    color: var(--ok);
  }
  .elapsed {
    font-family: var(--font-numeric);
    font-variant-numeric: tabular-nums;
  }
  .note {
    color: var(--muted);
    font-size: 0.72rem;
    white-space: nowrap;
  }
  .dot {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: var(--accent);
    animation: pulse 1.2s ease-in-out infinite;
  }
  .dot.warn {
    background: var(--warn);
    animation: none;
  }
  .check {
    width: 12px;
    height: 12px;
  }
  @keyframes pulse {
    0%,
    100% {
      opacity: 1;
      transform: scale(1);
    }
    50% {
      opacity: 0.35;
      transform: scale(0.7);
    }
  }
  /* The rail sits on the card's top edge: absolute against the surface
     (Companion.svelte gives .surface position: relative), inset by the
     card radius so it never pokes past the rounded corners. */
  .rail {
    position: absolute;
    top: 0;
    left: var(--radius);
    right: var(--radius);
    height: 2px;
    overflow: hidden;
    background: var(--accent-glow);
  }
  .rail .fill {
    position: absolute;
    top: 0;
    bottom: 0;
    left: -35%;
    width: 35%;
    background: var(--accent);
    animation: slide 1.4s cubic-bezier(0.4, 0, 0.2, 1) infinite;
  }
  @keyframes slide {
    to {
      left: 100%;
    }
  }
  @media (prefers-reduced-motion: reduce) {
    .dot,
    .rail .fill {
      animation: none;
    }
    .rail .fill {
      left: 0;
      width: 100%;
      opacity: 0.6;
    }
  }
</style>
