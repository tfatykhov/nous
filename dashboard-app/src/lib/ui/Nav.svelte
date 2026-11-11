<script lang="ts">
  import { routeHref, type RouteName } from '$lib/router';
  import { attentionCounts } from '$lib/stores/attention';

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

  // Each item's label and icon; the order on screen is NAV_GROUPS' below.
  const NAV_ITEMS: NavItem[] = [
    {
      id: 'overview',
      label: 'Overview',
      icon: '<path d="M3 4a1 1 0 011-1h12a1 1 0 011 1v2a1 1 0 01-1 1H4a1 1 0 01-1-1V4zm0 6a1 1 0 011-1h6a1 1 0 011 1v6a1 1 0 01-1 1H4a1 1 0 01-1-1v-6zm10 0a1 1 0 011-1h2a1 1 0 011 1v6a1 1 0 01-1 1h-2a1 1 0 01-1-1v-6z"/>',
    },
    {
      id: 'graph',
      label: 'Graph',
      icon: '<path d="M5 3a2 2 0 00-2 2v2a2 2 0 002 2h2a2 2 0 002-2V5a2 2 0 00-2-2H5zm0 8a2 2 0 00-2 2v2a2 2 0 002 2h2a2 2 0 002-2v-2a2 2 0 00-2-2H5zm6-6a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2V5zm0 8a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2v-2z"/>',
    },
    {
      id: 'browser',
      label: 'Browse',
      icon: '<path d="M9 4.804A7.968 7.968 0 005.5 4c-1.255 0-2.443.29-3.5.804v10A7.969 7.969 0 015.5 14c1.669 0 3.218.51 4.5 1.385A7.962 7.962 0 0114.5 14c1.255 0 2.443.29 3.5.804v-10A7.968 7.968 0 0014.5 4c-1.255 0-2.443.29-3.5.804V14a1 1 0 11-2 0V4.804z"/>',
    },
    {
      id: 'decisions',
      label: 'Decisions',
      icon: '<path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z" clip-rule="evenodd"/>',
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
      id: 'retrieval',
      label: 'Retrieval',
      icon: '<path fill-rule="evenodd" d="M8 4a4 4 0 100 8 4 4 0 000-8zM2 8a6 6 0 1110.89 3.476l4.817 4.817a1 1 0 01-1.414 1.414l-4.816-4.816A6 6 0 012 8z" clip-rule="evenodd"/>',
    },
    {
      id: 'health',
      label: 'Health',
      icon: '<path fill-rule="evenodd" d="M3 3a1 1 0 000 2v8a2 2 0 002 2h2.586l-1.293 1.293a1 1 0 101.414 1.414L10 15.414l2.293 2.293a1 1 0 001.414-1.414L12.414 15H15a2 2 0 002-2V5a1 1 0 100-2H3zm11.707 4.707a1 1 0 00-1.414-1.414L10 9.586 8.707 8.293a1 1 0 00-1.414 0l-2 2a1 1 0 101.414 1.414L8 10.414l1.293 1.293a1 1 0 001.414 0l4-4z" clip-rule="evenodd"/>',
    },
    {
      id: 'rubric',
      label: 'Scoring',
      icon: '<path fill-rule="evenodd" d="M6 2a2 2 0 00-2 2v12a2 2 0 002 2h8a2 2 0 002-2V7.414A2 2 0 0015.414 6L12 2.586A2 2 0 0010.586 2H6zm5 6a1 1 0 10-2 0v2H7a1 1 0 100 2h2v2a1 1 0 102 0v-2h2a1 1 0 100-2h-2V8z" clip-rule="evenodd"/>',
    },
    {
      id: 'work',
      label: 'Work',
      icon: '<path fill-rule="evenodd" d="M6 2a1 1 0 00-1 1v1H4a2 2 0 00-2 2v2h16V6a2 2 0 00-2-2h-1V3a1 1 0 10-2 0v1H7V3a1 1 0 00-1-1zm12 8H2v6a2 2 0 002 2h12a2 2 0 002-2v-6z" clip-rule="evenodd"/>',
    },
    {
      id: 'model-calls',
      label: 'Model calls',
      icon: '<path fill-rule="evenodd" d="M18 10c0 3.866-3.582 7-8 7a8.84 8.84 0 01-3.007-.517l-3.78 1.26a.5.5 0 01-.65-.62l1.06-3.18A6.6 6.6 0 012 10c0-3.866 3.582-7 8-7s8 3.134 8 7zM7 9H5v2h2V9zm8 0h-2v2h2V9zM9 9h2v2H9V9z" clip-rule="evenodd"/>',
    },
    {
      id: 'harness',
      label: 'Guards',
      icon: '<path d="M5 4a1 1 0 00-2 0v7.268a2 2 0 000 3.464V16a1 1 0 102 0v-1.268a2 2 0 000-3.464V4zM11 4a1 1 0 10-2 0v1.268a2 2 0 000 3.464V16a1 1 0 102 0V8.732a2 2 0 000-3.464V4zM16 3a1 1 0 011 1v7.268a2 2 0 010 3.464V16a1 1 0 11-2 0v-1.268a2 2 0 010-3.464V4a1 1 0 011-1z"/>',
    },
    {
      id: 'intentions',
      label: 'Intentions',
      icon: '<path fill-rule="evenodd" d="M3 5a2 2 0 012-2h2a2 2 0 012 2v1h4a1 1 0 011 1v3h1a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2v-2a2 2 0 012-2V8H9v1a2 2 0 01-2 2H5a2 2 0 01-2-2V5z" clip-rule="evenodd"/>',
    },
    {
      id: 'autonomy',
      label: 'Autonomy',
      icon: '<path d="M2 10a8 8 0 018-8v8h8a8 8 0 11-16 0z"/><path d="M12 2.252A8.014 8.014 0 0117.748 8H12V2.252z"/>',
    },
    {
      id: 'consolidation',
      label: 'Consolidation',
      icon: '<path fill-rule="evenodd" d="M10 2a1 1 0 011 1v1.323l3.954 1.582 1.599-.8a1 1 0 01.894 1.79l-1.233.616 1.738 5.42a1 1 0 01-.285 1.05A3.989 3.989 0 0115 16a3.989 3.989 0 01-2.667-1.019 1 1 0 01-.285-1.05l1.715-5.349L11 7.677V16h2a1 1 0 110 2H7a1 1 0 110-2h2V7.677l-2.763 1.105 1.715 5.349a1 1 0 01-.285 1.05A3.989 3.989 0 015 16a3.989 3.989 0 01-2.667-1.019 1 1 0 01-.285-1.05l1.738-5.42-1.233-.617a1 1 0 01.894-1.788l1.599.799L9 4.323V3a1 1 0 011-1z" clip-rule="evenodd"/>',
    },
    {
      id: 'dag',
      label: 'Workflows',
      // DAG icon uses stroke not fill — rendered differently below
      icon: '__dag__',
    },
    {
      id: 'subtasks',
      label: 'Subtasks',
      icon: '<path fill-rule="evenodd" d="M3 4a1 1 0 011-1h3a1 1 0 011 1v3a1 1 0 01-1 1H4a1 1 0 01-1-1V4zm9 0a1 1 0 011-1h3a1 1 0 011 1v3a1 1 0 01-1 1h-3a1 1 0 01-1-1V4zM3 12a1 1 0 011-1h3a1 1 0 011 1v3a1 1 0 01-1 1H4a1 1 0 01-1-1v-3zm9 0a1 1 0 011-1h3a1 1 0 011 1v3a1 1 0 01-1 1h-3a1 1 0 01-1-1v-3z" clip-rule="evenodd"/>',
    },
    {
      id: 'identity',
      label: 'Identity',
      icon: '<path d="M10 2a4 4 0 100 8 4 4 0 000-8zM3 18a7 7 0 0114 0H3z"/>',
    },
  ];

  // F108 Phase 2: six groups, in this order (the order of ROUTES). Overview stands alone; every other group has a
  // heading, which is text, not a link.
  const byId = new Map(NAV_ITEMS.map((item) => [item.id, item]));
  const group = (heading: string | null, ids: RouteName[]) => ({ heading, items: ids.map((id) => byId.get(id)!) });
  const NAV_GROUPS = [
    group(null, ['overview']),
    group('Memory', ['graph', 'browser', 'health', 'retrieval', 'consolidation']),
    group('Execution', ['work', 'model-calls', 'dag', 'subtasks', 'heartbeat']),
    group('Decisions', ['decisions', 'rubric']),
    group('Autonomy', ['autonomy', 'intentions', 'harness']),
    group('System', ['observability', 'identity']),
  ];
  // The sidebar and the mobile drawer each render a Nav: heading ids must differ between them.
  const uid = $props.id();

  // Harness dashboard §4.6: counts that need a person — and their number is
  // part of the link's accessible name, not just a coloured pill.
  function badge(id: RouteName): { count: number; text: string; kind: string } | null {
    const c = $attentionCounts;
    if (id === 'dag' && c.questions > 0) {
      return { count: c.questions, text: `${c.questions} question${c.questions === 1 ? '' : 's'} waiting`, kind: 'waiting' };
    }
    // The sends in doubt are on Work's Changes tab.
    if (id === 'work' && c.sends > 0) {
      return { count: c.sends, text: `${c.sends} send${c.sends === 1 ? '' : 's'} in doubt`, kind: 'unknown' };
    }
    return null;
  }
</script>

<nav aria-label={navLabel}>
  {#each NAV_GROUPS as g, gi (gi)}
    <div
      class="nav-group"
      role={g.heading ? 'group' : undefined}
      aria-labelledby={g.heading ? `${uid}-group-${gi}` : undefined}
    >
      {#if g.heading}
        <div class="nav-group-label" id="{uid}-group-{gi}">{g.heading}</div>
      {/if}
      {#each g.items as item (item.id)}
        {@const b = badge(item.id)}
        <a
          href={routeHref(item.id)}
          class="nav-link"
          class:active={currentRoute === item.id}
          aria-label={b ? `${item.label}, ${b.text}` : item.label}
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
          <span class="nav-label">{item.label}</span>
          {#if b}
            <span class="nav-badge nav-badge--{b.kind}" aria-hidden="true">{b.count}</span>
          {/if}
        </a>
      {/each}
    </div>
  {/each}
</nav>

<style>
  nav {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
    padding: 0.5rem 0.75rem;
  }

  .nav-group {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .nav-group-label {
    padding: 0 0.75rem 0.125rem;
    color: var(--muted);
    font-size: 0.6875rem;
    font-weight: 600;
    letter-spacing: var(--nav-label-tracking);
    text-transform: var(--label-transform);
  }

  .nav-link {
    display: flex;
    align-items: center;
    gap: 0.625rem;
    padding: 0.5rem 0.75rem;
    border-radius: var(--nav-radius);
    color: var(--nav-ink);
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
    /* #a99df9 on the glow: 6.69:1 (the raw accent was 3.96:1) */
    color: var(--accent-text);
    font-weight: var(--nav-active-weight);
  }

  /* The ground's ring: today's accent under the dark look, white on the rail (it sits outside the paper pill). */
  .nav-link:focus-visible {
    outline: 2px solid var(--focus-ring);
    outline-offset: 2px;
  }

  .nav-label {
    flex: 1;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .nav-badge {
    min-width: 1.25rem;
    height: 1.25rem;
    padding: 0 0.375rem;
    border-radius: 999px;
    font-size: 0.6875rem;
    font-weight: 700;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    color: var(--bg);
  }

  .nav-badge--waiting { background: var(--waiting); }
  .nav-badge--unknown { background: var(--unknown); }

  /* display: none on the rail (no icons there), so a hidden icon never takes space, focus or speech. */
  .nav-icon {
    display: var(--nav-icon-display);
    width: 1.125rem;
    height: 1.125rem;
    flex-shrink: 0;
  }
</style>
