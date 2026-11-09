<script lang="ts">
  // A screen's sub views as links (each one a route, so the address bar and Back work).
  let {
    items,
    current,
    label,
  }: {
    items: { id: string; label: string; href: string }[];
    current: string;
    label: string;
  } = $props();
</script>

<nav class="subnav" aria-label={label}>
  {#each items as item (item.id)}
    <a
      href={item.href}
      class="subnav-link"
      class:active={item.id === current}
      aria-current={item.id === current ? 'page' : undefined}
    >{item.label}</a>
  {/each}
</nav>

<style>
  .subnav {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
  }

  @media (max-width: 768px) {
    .subnav-link { display: inline-flex; align-items: center; min-height: 44px; }
  }

  .subnav-link {
    padding: 8px 14px;
    border-radius: var(--radius-sm);
    color: var(--muted);
    font-size: 0.875rem;
    text-decoration: none;
    transition: background var(--transition), color var(--transition);
  }

  .subnav-link:hover {
    background: var(--surface-hover);
    color: var(--text);
  }

  .subnav-link.active {
    background: var(--surface-hover);
    color: var(--text);
    font-weight: 600;
    box-shadow: inset 0 0 0 1px var(--accent);
  }
</style>
