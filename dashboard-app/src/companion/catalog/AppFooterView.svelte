<script lang="ts">
  // F092.1 AppFooter — the micro-app's whole control surface: enumerated
  // refine options, refresh, close. It owns its DOM and calls the transport
  // DIRECTLY (rev-ui #2): a basic Button cannot do either verb — a
  // functionCall action dispatches to the CLIENT-LOCAL function table and
  // silently no-ops, and an event action for app.refine fails the build-time
  // allowlist check (micro-apps allow only app.close).
  //
  // The lock is load-bearing: the server rate limit is SHARED between
  // actions and function calls, so rapid refine clicks would 429.
  //
  // F092.4 activity indicator: every call is bracketed in the store
  // (beginActivity / endActivityIf) so the header and the sections show
  // the same in-flight state; the pressed control itself carries a spinner
  // and a present-tense label. An agent action's activity outlives the POST
  // — the server's /meta/pendingAction stamp carries it from there.
  //
  // The STORE record is the lock, not a component flag: Companion.svelte's
  // feed and focused views are exclusive branches, so following a permalink
  // or switching views destroys this footer and mounts another for the
  // same unchanged surface. Locking on the record keeps the controls
  // disabled and the pressed control spinning across that remount, and the
  // original response still finishes the record it began (by token).
  import { store } from '../store.svelte';
  import { transport } from '../transport';
  import { resolveDynamic } from '../functions';
  import { ACT_STAMP_WAIT_MS, pendingActionOf, pendingIsFresh } from '../activity';
  import type { ActivityKind } from '../activity';
  import type { Scope } from '../pointer';
  import type { A2uiComponent } from '../store.svelte';

  interface RefineOption {
    id: string;
    label: string;
  }

  let {
    surfaceId,
    comp,
  }: {
    surfaceId: string;
    comp: A2uiComponent;
    scope?: Scope | null;
    depth?: number;
    ancestors?: readonly string[];
  } = $props();

  let closing = $state(false);
  let error = $state('');

  const dataModel = $derived(store.surfaces[surfaceId]?.dataModel ?? {});
  const ctx = $derived({ dataModel, scope: null });
  const refineOptions = $derived.by(() => {
    const raw = resolveDynamic(comp.refineOptions, ctx);
    if (!Array.isArray(raw)) return [];
    return (raw as RefineOption[]).filter(
      (o) => o && typeof o.id === 'string' && typeof o.label === 'string',
    );
  });
  const showRefresh = $derived(comp.showRefresh !== false);

  // F092.2 agent actions: server-stamped {id, label} list. Pending state is
  // a server-owned /meta stamp with a timestamp — a successful recompose
  // replaces the whole model and clears it; the timestamp is what makes a
  // dead watcher degrade to an honest "no update arrived" instead of an
  // infinite spinner. The parsing and the freshness rule live in
  // activity.ts, shared with the header (codex on F092.2: one definition).
  const agentActions = $derived.by(() => {
    const raw = comp.agentActions;
    if (!Array.isArray(raw)) return [];
    return (raw as RefineOption[]).filter(
      (o) => o && typeof o.id === 'string' && typeof o.label === 'string',
    );
  });
  const meta = $derived(
    (dataModel as Record<string, unknown>).meta as Record<string, unknown> | undefined,
  );
  const pendingAction = $derived(pendingActionOf(meta));
  let nowTick = $state(Date.now());
  $effect(() => {
    if (!pendingAction) return;
    const t = setInterval(() => (nowTick = Date.now()), 10_000);
    return () => clearInterval(t);
  });
  const pendingFresh = $derived(pendingIsFresh(pendingAction, nowTick));
  const actionError = $derived(typeof meta?.actionError === 'string' ? meta.actionError : '');

  // What this SURFACE has in flight right now — refresh / refine / an
  // agent action's POST (and, after it succeeds, the hold for the server
  // stamp). Controls are matched by ID — two agent actions may share a
  // label.
  const activity = $derived(store.activity[surfaceId] ?? null);
  const inFlight = $derived(activity !== null);
  const locked = $derived(inFlight || closing || pendingFresh);
  const actWorking = (id: string) =>
    (pendingFresh && pendingAction?.id === id) || (activity?.kind === 'act' && activity.id === id);

  // A successful app.act POST only means the turn STARTED; from there the
  // server's pendingAction stamp carries the activity. The stamp arrives
  // over SSE, independently of the HTTP response, and can land AFTER it —
  // releasing on the response would drop the rail and re-enable every
  // control for that gap, and a second tap would be refused as already
  // running. So the record is held (holdSince) until the stamp is
  // observed, for at most ACT_STAMP_WAIT_MS from the hand-off; past that
  // the controls release and the server's own guard covers a duplicate.
  // Store-driven, so whichever footer is mounted runs it. Never a flash:
  // completion is the header's call, and only a recompose counts.
  $effect(() => {
    if (!activity || activity.holdSince === undefined) return;
    const token = activity.token;
    if (pendingFresh) {
      store.endActivityIf(surfaceId, token, false);
      return;
    }
    const remaining = activity.holdSince + ACT_STAMP_WAIT_MS - Date.now();
    const t = setTimeout(() => store.endActivityIf(surfaceId, token, false), Math.max(0, remaining));
    return () => clearTimeout(t);
  });

  // Teardown releases the surface's record ONLY when the surface itself is
  // gone — closed, pruned on reconnect, or mid-replacement (deleteSurface
  // precedes the createSurface, and a fast action can finish that way
  // before its POST response lands; a record left behind would show
  // "agent working" on the replacement forever). A route remount keeps
  // the surface, so it keeps the record.
  $effect(() => () => {
    if (!store.surfaces[surfaceId]) store.endActivity(surfaceId, false);
  });

  async function act(actionId: string) {
    if (locked) return;
    error = '';
    // Whether a stamp has been observed since THIS tap decides what a
    // success response means: none yet → hold for it; one already seen →
    // it came, and may already have gone (the failure watcher can clear it
    // and write actionError before a slow response lands) → nothing to
    // wait for. Compared as server-stamped values, so client/server clock
    // skew cannot mislead.
    const seenBefore = store.stampSeenAt[surfaceId];
    const token = store.beginActivity(surfaceId, 'act', actionId);
    let handedOff = false;
    try {
      const res = await transport.postAction(surfaceId, 'app.act', comp.id, { actionId });
      if (res.ok) {
        if (store.stampSeenAt[surfaceId] === seenBefore) handedOff = store.holdForStamp(surfaceId, token);
      } else {
        error = res.message;
      }
    } finally {
      if (!handedOff) store.endActivityIf(surfaceId, token, false);
    }
  }

  async function call(name: string, args: Record<string, unknown>, kind: ActivityKind, id: string) {
    if (locked) return;
    error = '';
    const token = store.beginActivity(surfaceId, kind, id);
    let ok = false;
    try {
      const res = await transport.callAgentFunction(surfaceId, name, args);
      ok = res.ok;
      if (!res.ok) error = res.message;
    } finally {
      store.endActivityIf(surfaceId, token, ok);
    }
  }

  // Two-tap confirmation. close() resolves the surface irreversibly — a
  // micro-app is REBUILT, not restored, so one stray tap costs a compose the
  // user may have refined. The first tap ARMS (the button turns into an
  // explicit "sure? close" and auto-disarms after 4s); only the second tap
  // executes. Deliberately not window.confirm: a native modal blocks the
  // whole page and cannot be styled or tested.
  let armed = $state(false);
  let disarmTimer: ReturnType<typeof setTimeout> | undefined;

  function requestClose() {
    if (inFlight || closing) return;
    if (!armed) {
      armed = true;
      clearTimeout(disarmTimer);
      disarmTimer = setTimeout(() => (armed = false), 4000);
      return;
    }
    clearTimeout(disarmTimer);
    armed = false;
    void close();
  }

  async function close() {
    if (inFlight || closing) return;
    closing = true;
    error = '';
    try {
      const res = await transport.postAction(surfaceId, 'app.close', comp.id, {});
      if (!res.ok) error = res.message;
      // On success the server resolves the surface and the deleteSurface
      // envelope removes it from the feed — nothing to paint here.
    } finally {
      closing = false;
    }
  }
</script>

{#snippet spinner()}
  <svg class="spin" viewBox="0 0 16 16" aria-hidden="true">
    <circle cx="8" cy="8" r="6" fill="none" stroke="currentColor" stroke-opacity="0.25" stroke-width="2" />
    <path d="M14 8a6 6 0 0 0-6-6" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" />
  </svg>
{/snippet}

<footer class="app-footer">
  <div class="controls">
    {#each agentActions as action (action.id)}
      <button
        class="ctl act"
        class:pressed={actWorking(action.id)}
        disabled={locked}
        onclick={() => void act(action.id)}
      >
        {#if actWorking(action.id)}{@render spinner()}{/if}
        {action.label}
      </button>
    {/each}
    <!-- refine/refresh disabled while an action runs: the server refuses
         them anyway (they'd erase the pending stamp without stopping the
         turn) — the disable just keeps the UI honest about it. -->
    {#each refineOptions as option (option.id)}
      <button
        class="ctl"
        class:pressed={activity?.kind === 'refine' && activity.id === option.id}
        disabled={locked}
        onclick={() => void call('app.refine', { id: option.id }, 'refine', option.id)}
      >
        {#if activity?.kind === 'refine' && activity.id === option.id}{@render spinner()}{/if}
        {option.label}
      </button>
    {/each}
    {#if showRefresh}
      <button
        class="ctl"
        class:pressed={activity?.kind === 'refresh'}
        disabled={locked}
        onclick={() => void call('app.refresh', {}, 'refresh', 'refresh')}
      >
        {#if activity?.kind === 'refresh'}{@render spinner()}Refreshing{:else}refresh{/if}
      </button>
    {/if}
    <button
      class="ctl quiet"
      class:armed
      disabled={inFlight || closing}
      aria-label={armed ? 'confirm close' : 'close app'}
      onclick={requestClose}
    >
      {armed ? 'sure? close' : 'close'}
    </button>
  </div>
  {#if pendingAction && !pendingFresh}
    <span class="stale">
      "{pendingAction.label}" got no update — the agent may have failed; tap again to retry.
    </span>
  {/if}
  {#if actionError}
    <span class="err" role="alert">{actionError}</span>
  {/if}
  {#if error}
    <span class="err" role="alert">{error}</span>
  {/if}
</footer>

<style>
  .app-footer {
    display: flex;
    flex-direction: column;
    gap: 0.3rem;
    border-top: 1px solid var(--border);
    padding-top: 0.6rem;
  }
  .controls {
    display: flex;
    gap: 0.5rem;
    flex-wrap: wrap;
    align-items: center;
  }
  .ctl {
    font: inherit;
    font-size: 0.85rem;
    color: var(--text);
    background: var(--surface-hover);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    padding: 0.3rem 0.7rem;
    cursor: pointer;
    transition: var(--transition);
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
  }
  .ctl:hover:not(:disabled) {
    border-color: var(--accent);
  }
  .ctl:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
  /* The pressed control stays at full strength while its call runs — it is
     the one thing on the card that should NOT look inert. */
  .ctl.pressed:disabled {
    opacity: 1;
    border-color: var(--accent);
  }
  /* .quiet's rule appears later at equal specificity and was overriding both
     declarations, so the armed state showed only a text change (codex P2).
     Match the compound class and pin hover too. */
  .ctl.armed,
  .ctl.quiet.armed,
  .ctl.quiet.armed:hover:not(:disabled) {
    color: var(--crit);
    border-color: var(--crit);
  }
  .ctl.act {
    border-color: var(--accent);
  }
  .ctl.quiet {
    margin-left: auto;
    background: none;
    border-color: transparent;
    color: var(--muted);
  }
  .spin {
    width: 12px;
    height: 12px;
    flex: 0 0 auto;
    animation: rot 0.9s linear infinite;
  }
  @keyframes rot {
    to {
      transform: rotate(360deg);
    }
  }
  @media (prefers-reduced-motion: reduce) {
    .spin {
      animation: none;
    }
  }
  .stale {
    color: var(--warn, var(--muted));
    font-size: 0.8rem;
  }
  .ctl.quiet:hover:not(:disabled) {
    color: var(--text);
  }
  .err {
    color: var(--crit);
    font-size: 0.8rem;
  }
</style>
