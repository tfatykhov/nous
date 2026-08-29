<script lang="ts">
  // Basic catalog Tabs — hand-rolled per the plan (no bits-ui anywhere in the
  // companion). Implements the APG tabs pattern: one tab stop for the whole
  // tablist (roving tabindex), arrows/Home/End move selection, and only the
  // ACTIVE panel is rendered — an inactive panel's subtree would otherwise
  // run its bindings and effects while invisible.
  //
  // Selection is renderer-local state, not data-model state: the catalog
  // gives Tabs no `value` property, so which tab is open is a view concern.
  import Renderer from '../Renderer.svelte';
  import { store } from '../store.svelte';
  import { flexGrow, resolveDynamic, toDisplayString } from '../functions';
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

  let requested = $state(0);
  let buttons: HTMLButtonElement[] = $state([]);

  const ctx = $derived({ dataModel: store.surfaces[surfaceId]?.dataModel ?? {}, scope });
  const tabs = $derived.by(() => {
    const raw = Array.isArray(comp.tabs) ? comp.tabs : [];
    return raw.map((tab) => {
      const record = (typeof tab === 'object' && tab !== null ? tab : {}) as Record<string, unknown>;
      return {
        title: toDisplayString(resolveDynamic(record.title, ctx)),
        child: typeof record.child === 'string' ? record.child : null,
      };
    });
  });

  // Clamped rather than assigned: updateComponents can shrink the tab list
  // under us, and a stale index would render an empty panel forever.
  const active = $derived(Math.min(Math.max(requested, 0), Math.max(tabs.length - 1, 0)));

  const tabId = (i: number) => `${surfaceId}__${comp.id}__tab${i}`;
  const panelId = (i: number) => `${surfaceId}__${comp.id}__panel${i}`;

  function select(index: number) {
    requested = (index + tabs.length) % tabs.length;
    buttons[requested]?.focus();
  }

  function onKeydown(event: KeyboardEvent) {
    const moves: Record<string, number> = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 };
    if (event.key in moves) {
      event.preventDefault();
      select(active + moves[event.key]);
    } else if (event.key === 'Home') {
      event.preventDefault();
      select(0);
    } else if (event.key === 'End') {
      event.preventDefault();
      select(tabs.length - 1);
    }
  }
</script>

{#if tabs.length > 0}
  <div class="tabs" style:flex-grow={flexGrow(comp.weight)}>
    <!-- tabindex="-1" satisfies the interactive-role focus rule without
         making the tablist a tab stop: with roving tabindex the tab BUTTONS
         own the focus, which is the APG pattern. -->
    <div class="tablist" role="tablist" tabindex="-1" onkeydown={onKeydown}>
      {#each tabs as tab, i (i)}
        <button
          bind:this={buttons[i]}
          type="button"
          role="tab"
          id={tabId(i)}
          aria-selected={i === active}
          aria-controls={panelId(i)}
          tabindex={i === active ? 0 : -1}
          class:active={i === active}
          onclick={() => (requested = i)}
        >
          {tab.title}
        </button>
      {/each}
    </div>

    <div class="panel" role="tabpanel" id={panelId(active)} aria-labelledby={tabId(active)} tabindex="0">
      {#if tabs[active]?.child}
        <Renderer {surfaceId} componentId={tabs[active].child} {scope} {depth} {ancestors} />
      {/if}
    </div>
  </div>
{/if}

<style>
  .tabs {
    display: flex;
    flex-direction: column;
    min-width: 0;
  }
  .tablist {
    display: flex;
    gap: 0.15rem;
    border-bottom: 1px solid var(--border);
    overflow-x: auto;
  }
  button {
    font: inherit;
    color: var(--muted);
    background: none;
    border: none;
    border-bottom: 2px solid transparent;
    padding: 0.45rem 0.8rem;
    cursor: pointer;
    white-space: nowrap;
    transition: var(--transition);
  }
  button:hover {
    color: var(--text);
  }
  button.active {
    color: var(--accent);
    border-bottom-color: var(--accent);
  }
  button:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: -2px;
  }
  .panel {
    padding-top: 0.75rem;
  }
  .panel:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
  }
</style>
