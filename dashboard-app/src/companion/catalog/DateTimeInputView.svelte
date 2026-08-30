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
  // Bounds go through the same control-type normalization as the value
  // (codex P2): a schema-valid ISO instant like "…T17:00:00Z" is silently
  // IGNORED by datetime-local min/max, letting users pick out-of-range values.
  const min = $derived(normalizeForControl(toDisplayString(resolveDynamic(comp.min, ctx))));
  const max = $derived(normalizeForControl(toDisplayString(resolveDynamic(comp.max, ctx))));
  const type = $derived(
    comp.enableDate === true && comp.enableTime === true
      ? 'datetime-local'
      : comp.enableTime === true
        ? 'time'
        : 'date',
  );
  const boundPath = $derived(isDataBinding(comp.value) ? absolute(comp.value.path, scope) : null);

  /**
   * Normalize an incoming ISO value to what the native control accepts
   * (codex P2): agents legitimately send true instants like
   * "2025-12-15T17:00:00Z" (the vendored fixtures do), but datetime-local
   * rejects any timezone designator and renders an EMPTY field. Values with
   * an explicit zone are converted to the local wall clock for display;
   * zone-less values pass through untouched. The write side keeps the
   * documented zone-less local policy above.
   */
  function normalizeForControl(raw: string): string {
    if (!raw) return '';
    const hasZone = /[zZ]$|[+-]\d{2}:\d{2}$/.test(raw);
    if (type === 'time') {
      return raw.includes('T') ? normalizeDateTime(raw, hasZone).slice(11, 16) : raw.slice(0, 5);
    }
    if (type === 'date') {
      return raw.includes('T') ? normalizeDateTime(raw, hasZone).slice(0, 10) : raw.slice(0, 10);
    }
    return normalizeDateTime(raw, hasZone);
  }

  function normalizeDateTime(raw: string, hasZone: boolean): string {
    if (!hasZone) return raw.slice(0, 16);
    const parsed = new Date(raw);
    if (Number.isNaN(parsed.getTime())) return '';
    const pad = (n: number) => String(n).padStart(2, '0');
    return (
      `${parsed.getFullYear()}-${pad(parsed.getMonth() + 1)}-${pad(parsed.getDate())}` +
      `T${pad(parsed.getHours())}:${pad(parsed.getMinutes())}`
    );
  }

  function read(): string {
    const surface = store.surfaces[surfaceId];
    if (!surface) return '';
    const raw = boundPath
      ? toDisplayString(getPointer(surface.dataModel, boundPath))
      : toDisplayString(resolveDynamic(comp.value, ctx));
    return normalizeForControl(raw);
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
    color: var(--crit);
    font-size: 0.8rem;
  }
</style>
