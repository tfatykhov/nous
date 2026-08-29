<script lang="ts">
  // Basic catalog ChoicePicker — fieldset + native radio/checkbox inputs.
  //
  // The value is a DynamicStringList and is ALWAYS written back as a string
  // ARRAY, including in the mutuallyExclusive (radio) case, where selecting
  // an option writes [value]. That is the catalog's contract, and a server
  // reading `context.choice[0]` must not have to care which variant rendered.
  //
  // No bind:group: it round-trips through a scalar for radios, which fights
  // the always-array contract. Explicit checked + onchange instead.
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

  let filter = $state('');

  const ctx = $derived({ dataModel: store.surfaces[surfaceId]?.dataModel ?? {}, scope });
  const label = $derived(toDisplayString(resolveDynamic(comp.label, ctx)));
  const failures = $derived(runChecks(comp.checks as CheckRule[] | undefined, ctx));
  const multiple = $derived(comp.variant === 'multipleSelection');
  const chips = $derived(comp.displayStyle === 'chips');
  const filterable = $derived(comp.filterable === true);
  const boundPath = $derived(isDataBinding(comp.value) ? absolute(comp.value.path, scope) : null);

  // Radio groups are keyed by surface AND component id: two surfaces in the
  // feed can each carry a picker with the same component id, and a bare id
  // would silently merge them into one group.
  const groupName = $derived(`${surfaceId}__${comp.id}`);

  const options = $derived.by(() => {
    const raw = Array.isArray(comp.options) ? comp.options : [];
    return raw.map((option) => {
      const record = (typeof option === 'object' && option !== null ? option : {}) as Record<
        string,
        unknown
      >;
      return {
        value: String(record.value ?? ''),
        label: toDisplayString(resolveDynamic(record.label, ctx)),
      };
    });
  });

  const visible = $derived.by(() => {
    if (!filterable || filter.trim() === '') return options;
    const needle = filter.trim().toLowerCase();
    return options.filter((option) => option.label.toLowerCase().includes(needle));
  });

  const selected = $derived.by(() => {
    const surface = store.surfaces[surfaceId];
    const raw = boundPath
      ? getPointer(surface?.dataModel ?? {}, boundPath)
      : resolveDynamic(comp.value, ctx);
    return Array.isArray(raw) ? raw.map(String) : [];
  });

  function commit(next: string[]) {
    const surface = store.surfaces[surfaceId];
    if (surface && boundPath) setPointer(surface.dataModel, boundPath, next);
  }

  function choose(value: string, checked: boolean) {
    if (!multiple) {
      commit([value]);
      return;
    }
    commit(checked ? [...selected, value] : selected.filter((v) => v !== value));
  }
</script>

<fieldset class="picker" style:flex-grow={flexGrow(comp.weight)}>
  {#if label}
    <legend>{label}</legend>
  {/if}

  {#if filterable}
    <input class="filter" type="text" placeholder="Filter options" bind:value={filter} />
  {/if}

  <div class="options" class:chips>
    {#each visible as option (option.value)}
      <label class="option" class:selected={selected.includes(option.value)}>
        <input
          type={multiple ? 'checkbox' : 'radio'}
          name={groupName}
          value={option.value}
          checked={selected.includes(option.value)}
          onchange={(e) => choose(option.value, e.currentTarget.checked)}
        />
        <span>{option.label}</span>
      </label>
    {/each}
    {#if visible.length === 0}
      <span class="none">No matching options.</span>
    {/if}
  </div>

  {#each failures as failure (failure.message)}
    <span class="err">{failure.message}</span>
  {/each}
</fieldset>

<style>
  .picker {
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    padding: 0.6rem 0.75rem 0.75rem;
    margin: 0;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
  }
  legend {
    color: var(--muted);
    font-size: 0.82rem;
    padding: 0 0.3rem;
  }
  .filter {
    font: inherit;
    color: var(--text);
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    padding: 0.35rem 0.5rem;
  }
  .filter:focus {
    outline: none;
    border-color: var(--accent);
  }
  .options {
    display: flex;
    flex-direction: column;
    gap: 0.35rem;
  }
  .options.chips {
    flex-direction: row;
    flex-wrap: wrap;
    gap: 0.4rem;
  }
  .option {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    cursor: pointer;
  }
  .option input {
    margin: 0;
    accent-color: var(--accent);
    flex-shrink: 0;
  }
  /* Chips hide the native control and style the label as a pill; the input
     stays in the DOM and focusable so keyboard and screen readers are
     unaffected. */
  .options.chips .option {
    border: 1px solid var(--border);
    border-radius: 999px;
    padding: 0.25rem 0.7rem;
    background: var(--surface-hover);
    transition: var(--transition);
  }
  .options.chips .option input {
    position: absolute;
    opacity: 0;
    width: 1px;
    height: 1px;
  }
  .options.chips .option.selected {
    border-color: var(--accent);
    background: var(--accent-glow);
    color: var(--text);
  }
  .options.chips .option:focus-within {
    border-color: var(--accent);
  }
  .none {
    color: var(--muted);
    font-size: 0.85rem;
  }
  .err {
    color: var(--red);
    font-size: 0.8rem;
  }
</style>
