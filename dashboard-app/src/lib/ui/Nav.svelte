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

  type NavItem = { id: RouteName; label: string };

  // Each item's label; the order on screen is NAV_GROUPS' below.
  const NAV_ITEMS: NavItem[] = [
    {
      id: 'overview',
      label: 'Overview',
    },
    {
      id: 'graph',
      label: 'Graph',
    },
    {
      id: 'browser',
      label: 'Browse',
    },
    {
      id: 'decisions',
      label: 'Decisions',
    },
    {
      id: 'heartbeat',
      label: 'Heartbeat',
    },
    {
      id: 'observability',
      label: 'Observability',
    },
    {
      id: 'retrieval',
      label: 'Retrieval',
    },
    {
      id: 'health',
      label: 'Health',
    },
    {
      id: 'rubric',
      label: 'Scoring',
    },
    {
      id: 'work',
      label: 'Work',
    },
    {
      id: 'model-calls',
      label: 'Model calls',
    },
    {
      id: 'harness',
      label: 'Guards',
    },
    {
      id: 'intentions',
      label: 'Intentions',
    },
    {
      id: 'autonomy',
      label: 'Autonomy',
    },
    {
      id: 'consolidation',
      label: 'Consolidation',
    },
    {
      id: 'dag',
      label: 'Workflows',
    },
    {
      id: 'subtasks',
      label: 'Subtasks',
    },
    {
      id: 'identity',
      label: 'Identity',
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
    letter-spacing: 0;
    text-transform: none;
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
    color: var(--accent-text);
    font-weight: var(--nav-active-weight);
  }

  /* The rail's focus ring, white (it sits outside the paper pill). */
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
</style>
