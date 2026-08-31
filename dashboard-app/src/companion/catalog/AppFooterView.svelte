<script lang="ts">
  // F092.1 AppFooter — the micro-app's whole control surface: enumerated
  // refine options, refresh, close. It owns its DOM and calls the transport
  // DIRECTLY (rev-ui #2): a basic Button cannot do either verb — a
  // functionCall action dispatches to the CLIENT-LOCAL function table and
  // silently no-ops, and an event action for app.refine fails the build-time
  // allowlist check (micro-apps allow only app.close).
  //
  // The busy guard is load-bearing: the server rate limit is SHARED between
  // actions and function calls, so rapid refine clicks would 429.
  import { store } from '../store.svelte';
  import { transport } from '../transport';
  import { resolveDynamic } from '../functions';
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

  let busy = $state(false);
  let error = $state('');

  const ctx = $derived({ dataModel: store.surfaces[surfaceId]?.dataModel ?? {}, scope: null });
  const refineOptions = $derived.by(() => {
    const raw = resolveDynamic(comp.refineOptions, ctx);
    if (!Array.isArray(raw)) return [];
    return (raw as RefineOption[]).filter(
      (o) => o && typeof o.id === 'string' && typeof o.label === 'string',
    );
  });
  const showRefresh = $derived(comp.showRefresh !== false);

  async function call(name: string, args: Record<string, unknown>) {
    if (busy) return;
    busy = true;
    error = '';
    try {
      const res = await transport.callAgentFunction(surfaceId, name, args);
      if (!res.ok) error = res.message;
    } finally {
      busy = false;
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
    if (busy) return;
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
    if (busy) return;
    busy = true;
    error = '';
    try {
      const res = await transport.postAction(surfaceId, 'app.close', comp.id, {});
      if (!res.ok) error = res.message;
      // On success the server resolves the surface and the deleteSurface
      // envelope removes it from the feed — nothing to paint here.
    } finally {
      busy = false;
    }
  }
</script>

<footer class="app-footer">
  <div class="controls">
    {#each refineOptions as option (option.id)}
      <button class="ctl" disabled={busy} onclick={() => void call('app.refine', { id: option.id })}>
        {option.label}
      </button>
    {/each}
    {#if showRefresh}
      <button class="ctl" disabled={busy} onclick={() => void call('app.refresh', {})}>
        {busy ? 'working…' : 'refresh'}
      </button>
    {/if}
    <button
      class="ctl quiet"
      class:armed
      disabled={busy}
      aria-label={armed ? 'confirm close' : 'close app'}
      onclick={requestClose}
    >
      {armed ? 'sure? close' : 'close'}
    </button>
  </div>
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
  }
  .ctl:hover:not(:disabled) {
    border-color: var(--accent);
  }
  .ctl:disabled {
    opacity: 0.5;
    cursor: not-allowed;
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
  .ctl.quiet {
    margin-left: auto;
    background: none;
    border-color: transparent;
    color: var(--muted);
  }
  .ctl.quiet:hover:not(:disabled) {
    color: var(--text);
  }
  .err {
    color: var(--crit);
    font-size: 0.8rem;
  }
</style>
