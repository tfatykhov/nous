<script lang="ts">
  // Basic catalog Slider — native <input type="range">.
  //
  // `steps` is a COUNT OF DIVISIONS, not a step size, so the DOM step is
  // (max - min) / steps. Absent steps means continuous, which the DOM spells
  // step="any".
  //
  // The write coerces with Number(): the DOM hands back a string, and the
  // catalog types this as DynamicNumber — storing "7" instead of 7 would make
  // every downstream numeric check silently compare strings.
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
  const min = $derived(typeof comp.min === 'number' ? comp.min : 0);
  const max = $derived(typeof comp.max === 'number' ? comp.max : 100);
  const step = $derived(
    typeof comp.steps === 'number' && comp.steps > 0 ? (max - min) / comp.steps : 'any',
  );
  const boundPath = $derived(isDataBinding(comp.value) ? absolute(comp.value.path, scope) : null);

  function read(): number {
    const surface = store.surfaces[surfaceId];
    const raw = boundPath
      ? getPointer(surface?.dataModel ?? {}, boundPath)
      : resolveDynamic(comp.value, ctx);
    const value = Number(raw);
    return Number.isFinite(value) ? value : min;
  }

  function write(v: number | string) {
    const surface = store.surfaces[surfaceId];
    const value = Number(v);
    if (surface && boundPath && Number.isFinite(value)) {
      setPointer(surface.dataModel, boundPath, value);
    }
  }
</script>

<div class="slider" style:flex-grow={flexGrow(comp.weight)}>
  <div class="head">
    <span class="label">{label}</span>
    <span class="value">{read()}</span>
  </div>
  <input type="range" {min} {max} {step} aria-label={label || undefined} bind:value={read, write} />
  {#each failures as failure (failure.message)}
    <span class="err">{failure.message}</span>
  {/each}
</div>

<style>
  .slider {
    display: flex;
    flex-direction: column;
    gap: 0.3rem;
    min-width: 0;
  }
  .head {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: 0.5rem;
  }
  .label {
    color: var(--muted);
    font-size: 0.82rem;
  }
  .value {
    font-family: var(--font-mono);
    font-size: 0.85rem;
  }
  input {
    width: 100%;
    accent-color: var(--accent);
  }
  .err {
    color: var(--red);
    font-size: 0.8rem;
  }
</style>
