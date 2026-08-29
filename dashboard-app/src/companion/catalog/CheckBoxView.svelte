<script lang="ts">
  // Basic catalog CheckBox. Two-way bound locally through function bindings,
  // exactly like TextField: the click updates the surface data model and
  // never touches the network — state ships only when an action fires.
  // `value` is a DynamicBoolean, so writes are real booleans (setPointer only
  // treats null as a delete, so writing `false` stores false).
  import { store } from '../store.svelte';
  import {
    flexGrow,
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
  const failures = $derived(runChecks(comp.checks as CheckRule[] | undefined, ctx));
  const boundPath = $derived(isDataBinding(comp.value) ? absolute(comp.value.path, scope) : null);

  function read(): boolean {
    const surface = store.surfaces[surfaceId];
    if (!surface) return false;
    if (boundPath) return Boolean(getPointer(surface.dataModel, boundPath));
    return Boolean(resolveDynamic(comp.value, ctx));
  }

  function write(v: boolean) {
    const surface = store.surfaces[surfaceId];
    if (surface && boundPath) setPointer(surface.dataModel, boundPath, Boolean(v));
  }
</script>

<div class="wrap" style:flex-grow={flexGrow(comp.weight)}>
  <label class="box">
    <input type="checkbox" bind:checked={read, write} />
    <span>{label}</span>
  </label>
  {#each failures as failure (failure.message)}
    <span class="err">{failure.message}</span>
  {/each}
</div>

<style>
  .wrap {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
  }
  .box {
    display: flex;
    align-items: flex-start;
    gap: 0.5rem;
    cursor: pointer;
  }
  input {
    margin: 0;
    width: 1rem;
    height: 1rem;
    accent-color: var(--accent);
    flex-shrink: 0;
    margin-top: 0.2rem;
  }
  .err {
    color: var(--red);
    font-size: 0.8rem;
  }
</style>
