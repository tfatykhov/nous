<script lang="ts">
  type Option = { value: string; label: string };

  let {
    options,
    value = $bindable(''),
    multiple = false,
    label = 'Filter options',
    required = false,
  }: {
    options: Option[];
    value?: string;
    multiple?: boolean;
    /** The group's accessible name — each group on a page needs its own. */
    label?: string;
    /** A value is always selected: clicking the active option keeps it. */
    required?: boolean;
  } = $props();

  function handleClick(opt: Option) {
    if (multiple) {
      // Multi-select: toggle comma-separated values
      const vals = value ? value.split(',').filter(Boolean) : [];
      const idx = vals.indexOf(opt.value);
      if (idx >= 0) {
        vals.splice(idx, 1);
      } else {
        vals.push(opt.value);
      }
      value = vals.join(',');
    } else if (required) {
      value = opt.value;
    } else {
      value = value === opt.value ? '' : opt.value;
    }
  }

  function isActive(opt: Option): boolean {
    if (multiple) {
      return value.split(',').includes(opt.value);
    }
    return value === opt.value;
  }
</script>

<div class="filter-bar" role="group" aria-label={label}>
  {#each options as opt}
    <button
      type="button"
      class="filter-btn"
      class:active={isActive(opt)}
      onclick={() => handleClick(opt)}
      aria-pressed={isActive(opt)}
    >
      {opt.label}
    </button>
  {/each}
</div>

<style>
  /* A segmented control: the selected option is the white raised pill the Work filters and the sub-navigation use
     (finish review fix 2); the others are plain words on the track. */
  .filter-bar {
    display: inline-flex;
    flex-wrap: wrap;
    gap: 4px;
    padding: 4px;
    border: var(--card-border);
    border-radius: var(--ctl-radius);
  }

  .filter-btn {
    display: inline-flex;
    align-items: center;
    padding: 0.375rem 0.75rem;
    min-height: 32px;
    border-radius: 999px;
    border: 0;
    background: transparent;
    color: var(--muted);
    font-size: 0.8125rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.15s ease;
    white-space: nowrap;
    font-family: inherit;
  }

  .filter-btn:hover {
    background: var(--surface-hover);
    color: var(--text);
  }

  .filter-btn.active {
    background: var(--tab-active-bg);
    color: var(--text);
    font-weight: 600;
    box-shadow: var(--tab-active-shadow);
  }

  .filter-btn:focus-visible {
    outline: 2px solid var(--accent-text);
    outline-offset: 2px;
  }

  @media (max-width: 768px) {
    .filter-btn {
      min-height: 44px;
    }
  }
</style>
