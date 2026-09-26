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
  .filter-bar {
    display: flex;
    flex-wrap: wrap;
    gap: 0.375rem;
  }

  .filter-btn {
    display: inline-flex;
    align-items: center;
    padding: 0.375rem 0.75rem;
    min-height: 32px;
    border-radius: 999px;
    border: 1px solid var(--border);
    background: var(--surface);
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
    border-color: var(--muted);
  }

  .filter-btn.active {
    background: var(--accent);
    border-color: var(--accent);
    /* #0a0a0f on the accent: 4.95:1 (white was 3.99:1) */
    color: var(--bg);
    font-weight: 600;
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
