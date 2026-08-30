<script lang="ts">
  // Basic catalog TextField — two-way bound LOCALLY via function bindings
  // (Svelte 5.9+): keystrokes update the surface data model and never touch
  // the network; state ships only on an action. The `number` variant still
  // round-trips as a string (value is DynamicString in the catalog).
  import { store } from '../store.svelte';
  import {
    isDataBinding,
    resolveDynamic,
    runChecks,
    toDisplayString,
    type CheckRule,
  } from '../functions';
  import { absolute, getPointer, setPointer, type Scope } from '../pointer';
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

  const ctx = $derived({ dataModel: store.surfaces[surfaceId]?.dataModel ?? {}, scope });
  const label = $derived(toDisplayString(resolveDynamic(comp.label, ctx)));
  const placeholder = $derived(toDisplayString(resolveDynamic(comp.placeholder, ctx)));
  const variant = $derived(typeof comp.variant === 'string' ? comp.variant : 'shortText');
  const failures = $derived(runChecks(comp.checks as CheckRule[] | undefined, ctx));
  const boundPath = $derived(
    isDataBinding(comp.value) ? absolute(comp.value.path, scope) : null,
  );

  function read(): string {
    const surface = store.surfaces[surfaceId];
    if (!surface) return '';
    if (boundPath) return toDisplayString(getPointer(surface.dataModel, boundPath));
    return toDisplayString(resolveDynamic(comp.value, ctx));
  }

  function write(v: string) {
    const surface = store.surfaces[surfaceId];
    if (surface && boundPath) setPointer(surface.dataModel, boundPath, v);
  }
</script>

<label class="field">
  <span class="label">{label}</span>
  {#if variant === 'longText'}
    <textarea rows="3" {placeholder} bind:value={read, write}></textarea>
  {:else}
    <input
      type={variant === 'obscured' ? 'password' : variant === 'number' ? 'number' : 'text'}
      {placeholder}
      bind:value={read, write}
    />
  {/if}
  {#each failures as f (f.message)}
    <span class="err">{f.message}</span>
  {/each}
</label>

<style>
  .field {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
  }
  .label {
    color: var(--muted);
    font-size: 0.82rem;
  }
  input,
  textarea {
    font: inherit;
    color: var(--text);
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    padding: 0.45rem 0.6rem;
    resize: vertical;
  }
  input:focus,
  textarea:focus {
    outline: none;
    border-color: var(--accent);
  }
  .err {
    color: var(--crit);
    font-size: 0.8rem;
  }
</style>
