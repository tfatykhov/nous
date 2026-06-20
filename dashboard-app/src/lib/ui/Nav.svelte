<script lang="ts">
  import type { RouteName } from '$lib/router';

  let {
    currentRoute,
    navLabel = 'Main navigation',
    onnavigate,
  }: {
    currentRoute: RouteName;
    navLabel?: string;
    onnavigate?: () => void;
  } = $props();

  type NavItem = { id: RouteName; label: string; icon: string };

  const NAV_ITEMS: NavItem[] = [
    {
      id: 'overview',
      label: 'Overview',
      icon: '<path d="M3 4a1 1 0 011-1h12a1 1 0 011 1v2a1 1 0 01-1 1H4a1 1 0 01-1-1V4zm0 6a1 1 0 011-1h6a1 1 0 011 1v6a1 1 0 01-1 1H4a1 1 0 01-1-1v-6zm10 0a1 1 0 011-1h2a1 1 0 011 1v6a1 1 0 01-1 1h-2a1 1 0 01-1-1v-6z"/>',
    },
    {
      id: 'graph',
      label: 'Knowledge Graph',
      icon: '<path d="M5 3a2 2 0 00-2 2v2a2 2 0 002 2h2a2 2 0 002-2V5a2 2 0 00-2-2H5zm0 8a2 2 0 00-2 2v2a2 2 0 002 2h2a2 2 0 002-2v-2a2 2 0 00-2-2H5zm6-6a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2V5zm0 8a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2v-2z"/>',
    },
    {
      id: 'browser',
      label: 'Memory Browser',
      icon: '<path d="M9 4.804A7.968 7.968 0 005.5 4c-1.255 0-2.443.29-3.5.804v10A7.969 7.969 0 015.5 14c1.669 0 3.218.51 4.5 1.385A7.962 7.962 0 0114.5 14c1.255 0 2.443.29 3.5.804v-10A7.968 7.968 0 0014.5 4c-1.255 0-2.443.29-3.5.804V14a1 1 0 11-2 0V4.804z"/>',
    },
    {
      id: 'decisions',
      label: 'Decisions',
      icon: '<path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z" clip-rule="evenodd"/>',
    },
    {
      id: 'activity',
      label: 'Activity',
      icon: '<path fill-rule="evenodd" d="M11.3 1.046A1 1 0 0112 2v5h4a1 1 0 01.82 1.573l-7 10A1 1 0 018 18v-5H4a1 1 0 01-.82-1.573l7-10a1 1 0 011.12-.38z" clip-rule="evenodd"/>',
    },
    {
      id: 'heartbeat',
      label: 'Heartbeat',
      icon: '<path d="M3.172 5.172a4 4 0 015.656 0L10 6.343l1.172-1.171a4 4 0 115.656 5.656L10 17.657l-6.828-6.829a4 4 0 010-5.656z"/>',
    },
    {
      id: 'observability',
      label: 'Observability',
      icon: '<path d="M10 12a2 2 0 100-4 2 2 0 000 4z"/><path fill-rule="evenodd" d="M.458 10C1.732 5.943 5.522 3 10 3s8.268 2.943 9.542 7c-1.274 4.057-5.064 7-9.542 7S1.732 14.057.458 10zM14 10a4 4 0 11-8 0 4 4 0 018 0z" clip-rule="evenodd"/>',
    },
    {
      id: 'health',
      label: 'Graph Health',
      icon: '<path fill-rule="evenodd" d="M3 3a1 1 0 000 2v8a2 2 0 002 2h2.586l-1.293 1.293a1 1 0 101.414 1.414L10 15.414l2.293 2.293a1 1 0 001.414-1.414L12.414 15H15a2 2 0 002-2V5a1 1 0 100-2H3zm11.707 4.707a1 1 0 00-1.414-1.414L10 9.586 8.707 8.293a1 1 0 00-1.414 0l-2 2a1 1 0 101.414 1.414L8 10.414l1.293 1.293a1 1 0 001.414 0l4-4z" clip-rule="evenodd"/>',
    },
    {
      id: 'admission',
      label: 'Admission',
      icon: '<path fill-rule="evenodd" d="M2.166 4.999A11.954 11.954 0 0010 1.944 11.954 11.954 0 0017.834 5c.11.65.166 1.32.166 2.001 0 5.225-3.34 9.67-8 11.317C5.34 16.67 2 12.225 2 7c0-.682.057-1.35.166-2.001zm11.541 3.708a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z" clip-rule="evenodd"/>',
    },
    {
      id: 'rubric',
      label: 'Rubric',
      icon: '<path fill-rule="evenodd" d="M6 2a2 2 0 00-2 2v12a2 2 0 002 2h8a2 2 0 002-2V7.414A2 2 0 0015.414 6L12 2.586A2 2 0 0010.586 2H6zm5 6a1 1 0 10-2 0v2H7a1 1 0 100 2h2v2a1 1 0 102 0v-2h2a1 1 0 100-2h-2V8z" clip-rule="evenodd"/>',
    },
    {
      id: 'execution',
      label: 'Execution',
      icon: '<path fill-rule="evenodd" d="M4 2a2 2 0 00-2 2v12a2 2 0 002 2h12a2 2 0 002-2V4a2 2 0 00-2-2H4zm3 3a1 1 0 011-1h4a1 1 0 110 2H8a1 1 0 01-1-1zm0 4a1 1 0 011-1h8a1 1 0 110 2H8a1 1 0 01-1-1zm0 4a1 1 0 011-1h5a1 1 0 110 2H8a1 1 0 01-1-1zM5 6a1 1 0 100-2 1 1 0 000 2zm0 4a1 1 0 100-2 1 1 0 000 2zm0 4a1 1 0 100-2 1 1 0 000 2z" clip-rule="evenodd"/>',
    },
    {
      id: 'cache',
      label: 'Cache',
      icon: '<path d="M3 12v3c0 1.657 3.134 3 7 3s7-1.343 7-3v-3c0 1.657-3.134 3-7 3s-7-1.343-7-3z"/><path d="M3 7v3c0 1.657 3.134 3 7 3s7-1.343 7-3V7c0 1.657-3.134 3-7 3S3 8.657 3 7z"/><path d="M17 5c0 1.657-3.134 3-7 3S3 6.657 3 5s3.134-3 7-3 7 1.343 7 3z"/>',
    },
    {
      id: 'density',
      label: 'Density',
      icon: '<path fill-rule="evenodd" d="M17.778 8.222c-4.296-4.296-11.26-4.296-15.556 0A1 1 0 01.808 6.808c5.076-5.076 13.308-5.076 18.384 0a1 1 0 01-1.414 1.414zM14.95 11.05a7 7 0 00-9.9 0 1 1 0 01-1.414-1.414 9 9 0 0112.728 0 1 1 0 01-1.414 1.414zM12.12 13.88a3 3 0 00-4.242 0 1 1 0 01-1.414-1.414 5 5 0 017.07 0 1 1 0 01-1.414 1.414zM10 16a1 1 0 100-2 1 1 0 000 2z" clip-rule="evenodd"/>',
    },
    {
      id: 'dag',
      label: 'DAG Orchestrator',
      // DAG icon uses stroke not fill — rendered differently below
      icon: '__dag__',
    },
    {
      id: 'subtasks',
      label: 'Subtasks',
      icon: '<path fill-rule="evenodd" d="M3 4a1 1 0 011-1h3a1 1 0 011 1v3a1 1 0 01-1 1H4a1 1 0 01-1-1V4zm9 0a1 1 0 011-1h3a1 1 0 011 1v3a1 1 0 01-1 1h-3a1 1 0 01-1-1V4zM3 12a1 1 0 011-1h3a1 1 0 011 1v3a1 1 0 01-1 1H4a1 1 0 01-1-1v-3zm9 0a1 1 0 011-1h3a1 1 0 011 1v3a1 1 0 01-1 1h-3a1 1 0 01-1-1v-3z" clip-rule="evenodd"/>',
    },
  ];
</script>

<nav aria-label={navLabel}>
  {#each NAV_ITEMS as item (item.id)}
    <a
      href="#/{item.id}"
      class="nav-link"
      class:active={currentRoute === item.id}
      aria-label={item.label}
      aria-current={currentRoute === item.id ? 'page' : undefined}
      onclick={onnavigate}
    >
      {#if item.icon === '__dag__'}
        <svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
          <circle cx="5" cy="6" r="2"/><circle cx="12" cy="6" r="2"/><circle cx="19" cy="18" r="2"/>
          <circle cx="5" cy="18" r="2"/><circle cx="12" cy="18" r="2"/>
          <line x1="5" y1="8" x2="5" y2="16"/><line x1="12" y1="8" x2="12" y2="16"/>
          <line x1="7" y1="7" x2="17" y2="17"/><line x1="14" y1="7" x2="17" y2="16"/>
        </svg>
      {:else}
        <svg class="nav-icon" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
          <!-- icon strings are static SVG path data defined in NAV_ITEMS above; no user input reaches here -->
          {@html item.icon}
        </svg>
      {/if}
      <span>{item.label}</span>
    </a>
  {/each}
</nav>

<style>
  nav {
    display: flex;
    flex-direction: column;
    gap: 2px;
    padding: 0.5rem 0.75rem;
  }

  .nav-link {
    display: flex;
    align-items: center;
    gap: 0.625rem;
    padding: 0.5rem 0.75rem;
    border-radius: var(--radius-sm);
    color: var(--muted);
    text-decoration: none;
    font-size: 0.875rem;
    font-weight: 500;
    transition: background var(--transition), color var(--transition);
    white-space: nowrap;
    overflow: hidden;
  }

  .nav-link:hover {
    background: var(--surface-hover);
    color: var(--text);
  }

  .nav-link.active {
    background: var(--accent-glow);
    color: var(--accent);
  }

  .nav-icon {
    width: 1.125rem;
    height: 1.125rem;
    flex-shrink: 0;
  }
</style>
