<script lang="ts">
  // Basic catalog Button. Checks failing => disabled (spec). Agent-event
  // actions resolve their context bindings against the CURRENT data model at
  // click time and POST through the transport; a rejection paints inline and
  // the surface stays interactive (never a silent no-op).
  import Renderer from '../Renderer.svelte';
  import { store } from '../store.svelte';
  import { transport } from '../transport';
  import { flexGrow, resolveDynamic, runChecks, callFunction, type CheckRule } from '../functions';
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

  let busy = $state(false);
  let error = $state('');

  const ctx = $derived({ dataModel: store.surfaces[surfaceId]?.dataModel ?? {}, scope });
  const failures = $derived(runChecks(comp.checks as CheckRule[] | undefined, ctx));
  const variant = $derived(typeof comp.variant === 'string' ? comp.variant : 'default');

  async function onClick() {
    error = '';
    const action = comp.action as
      | { event?: { name: string; context?: Record<string, unknown> }; functionCall?: { call: string; args?: Record<string, unknown> } }
      | undefined;
    if (!action) return;
    if (action.functionCall) {
      try {
        callFunction(action.functionCall.call, action.functionCall.args ?? {}, ctx);
      } catch {
        error = 'action failed';
      }
      return;
    }
    if (!action.event) return;
    const resolved: Record<string, unknown> = {};
    for (const [key, value] of Object.entries(action.event.context ?? {})) {
      resolved[key] = resolveDynamic(value, ctx);
    }
    busy = true;
    try {
      const result = await transport.postAction(surfaceId, action.event.name, comp.id, resolved);
      if (!result.ok) error = result.message;
    } finally {
      busy = false;
    }
  }
</script>

<span class="wrap" style:flex-grow={flexGrow(comp.weight)}>
  <button
    class="btn {variant}"
    disabled={busy || failures.length > 0}
    title={failures[0]?.message ?? null}
    onclick={onClick}
  >
    {#if typeof comp.child === 'string'}
      <Renderer {surfaceId} componentId={comp.child} {scope} {depth} {ancestors} />
    {/if}
  </button>
  {#if error}
    <span class="err" role="alert">{error}</span>
  {/if}
</span>

<style>
  .wrap {
    display: inline-flex;
    flex-direction: column;
    gap: 0.2rem;
  }
  .btn {
    font: inherit;
    color: var(--text);
    background: var(--surface-hover);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    padding: 0.45rem 0.9rem;
    cursor: pointer;
    transition: var(--transition);
  }
  .btn:hover:not(:disabled) {
    border-color: var(--accent);
  }
  .btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
  .btn.primary {
    background: var(--accent);
    border-color: var(--accent);
    color: var(--on-accent);
  }
  .btn.primary:hover:not(:disabled) {
    background: var(--accent-dim);
  }
  .btn.borderless {
    background: none;
    border-color: transparent;
    color: var(--muted);
  }
  .btn.borderless:hover:not(:disabled) {
    color: var(--text);
  }
  .err {
    color: var(--crit);
    font-size: 0.8rem;
  }
</style>
