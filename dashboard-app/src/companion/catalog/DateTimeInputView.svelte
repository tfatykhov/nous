<script lang="ts">
  // Basic catalog DateTimeInput — native date / time / datetime-local by
  // enableDate + enableTime. Both false is meaningless as a control, so it
  // falls back to a date picker rather than rendering nothing.
  //
  // TIMEZONE (deliberate v1 choice): the native inputs produce ZONE-LESS
  // local wall-clock strings ("2026-08-29T14:30"), and we store exactly that,
  // unconverted. The catalog says "ISO 8601" without settling zone handling,
  // and a single-user companion app is always read back in the same zone it
  // was entered in. Converting to UTC here would silently shift every value
  // the agent later echoes back to the user. If a future surface needs true
  // instants, the fix is an explicit offset on the wire, not a conversion
  // buried in this adapter.
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
  const min = $derived(toDisplayString(resolveDynamic(comp.min, ctx)));
  const max = $derived(toDisplayString(resolveDynamic(comp.max, ctx)));
  const type = $derived(
    comp.enableDate === true && comp.enableTime === true
      ? 'datetime-local'
      : comp.enableTime === true
        ? 'time'
        : 'date',
  );
  const boundPath = $derived(isDataBinding(comp.value) ? absolute(comp.value.path, scope) : null);

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
  {#if label}
    <span class="label">{label}</span>
  {/if}
  <input
    {type}
    min={min || undefined}
    max={max || undefined}
    style:flex-grow={flexGrow(comp.weight)}
    bind:value={read, write}
  />
  {#each failures as failure (failure.message)}
    <span class="err">{failure.message}</span>
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
  input {
    font: inherit;
    color: var(--text);
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    padding: 0.45rem 0.6rem;
  }
  input:focus {
    outline: none;
    border-color: var(--accent);
  }
  .err {
    color: var(--red);
    font-size: 0.8rem;
  }
</style>
