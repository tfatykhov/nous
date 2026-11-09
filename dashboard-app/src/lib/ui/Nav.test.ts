import { describe, it, expect, beforeEach } from 'vitest';
import { render, within, cleanup } from '@testing-library/svelte';
import { get } from 'svelte/store';
import Nav from './Nav.svelte';
import { ROUTES, currentRoute, initRouter } from '../router';

// Regression guard: the Nav link hrefs MUST be in the exact hash format the
// router accepts (`#/<route>`). They previously used `#<route>` (no slash),
// which the router's tightened parse() ignored — silently breaking all nav
// (stuck on Overview). Keep Nav and the router in lock-step.
describe('Nav', () => {
  beforeEach(() => {
    location.hash = '';
  });

  it('renders one #/<route> link per known route (18), in route order, all router-parseable', () => {
    const { container } = render(Nav, { props: { currentRoute: 'overview' } });
    const hrefs = Array.from(container.querySelectorAll('a.nav-link')).map((a) =>
      a.getAttribute('href'),
    );
    expect(hrefs.length).toBe(18);
    expect(hrefs.length).toBe(ROUTES.length);
    for (const href of hrefs) {
      expect(href).toMatch(/^#\//); // must be a router-recognised fragment, not a bare anchor
      const route = href!.slice(2);
      expect(ROUTES).toContain(route as (typeof ROUTES)[number]);
    }
    expect(hrefs).toEqual(ROUTES.map((r) => `#/${r}`));
  });

  it('every link drives the router to its own route', () => {
    initRouter();
    const { container } = render(Nav, { props: { currentRoute: 'overview' } });
    for (const a of Array.from(container.querySelectorAll('a.nav-link'))) {
      const href = a.getAttribute('href')!;
      location.hash = href;
      window.dispatchEvent(new HashChangeEvent('hashchange'));
      expect(get(currentRoute)).toBe(href.slice(2));
      expect(location.hash).toBe(href); // no link is a redirect
    }
  });

  it('puts a badge count in the link name, not only in a coloured pill', async () => {
    const { pushCounts } = await import('../stores/attention');
    pushCounts({ questions: 2, sends: 1 });
    const { container } = render(Nav, { props: { currentRoute: 'overview' } });
    const dag = container.querySelector('a[href="#/dag"]');
    const work = container.querySelector('a[href="#/work"]');
    expect(dag?.getAttribute('aria-label')).toBe('Workflows, 2 questions waiting');
    expect(work?.getAttribute('aria-label')).toBe('Work, 1 send in doubt');
    expect(work?.querySelector('.nav-badge')?.textContent).toBe('1');
    expect(container.querySelector('a[href="#/harness"]')?.getAttribute('aria-label')).toBe('Guards');
    expect(container.querySelectorAll('.nav-badge')).toHaveLength(2);
  });

  it('clicking a nav href format actually drives the router', () => {
    initRouter();
    // Simulate what a nav click does: set the hash to the Nav's href format.
    location.hash = '#/model-calls';
    window.dispatchEvent(new HashChangeEvent('hashchange'));
    expect(get(currentRoute)).toBe('model-calls');
  });
});

// F108 Phase 2 T14: six groups. Overview stands alone; the other five have a heading that is text, not a link.
describe('Nav groups', () => {
  beforeEach(() => { cleanup(); });
  const GROUPS: [string | null, [string, string][]][] = [
    [null, [['overview', 'Overview']]],
    ['Memory', [['graph', 'Graph'], ['browser', 'Browse'], ['health', 'Health'], ['retrieval', 'Retrieval'], ['consolidation', 'Consolidation']]],
    ['Execution', [['work', 'Work'], ['model-calls', 'Model calls'], ['dag', 'Workflows'], ['subtasks', 'Subtasks'], ['heartbeat', 'Heartbeat']]],
    ['Decisions', [['decisions', 'Decisions'], ['rubric', 'Scoring']]],
    ['Autonomy', [['autonomy', 'Autonomy'], ['intentions', 'Intentions'], ['harness', 'Guards']]],
    ['System', [['observability', 'Observability'], ['identity', 'Identity']]],
  ];

  it('renders six groups in order, each with its links and labels in order', () => {
    const { container } = render(Nav, { props: { currentRoute: 'overview' } });
    const groups = Array.from(container.querySelectorAll('nav > .nav-group'));
    expect(groups).toHaveLength(6);
    groups.forEach((g, i) => {
      const [heading, items] = GROUPS[i];
      expect(g.querySelector('.nav-group-label')?.textContent ?? null).toBe(heading);
      const links = Array.from(g.querySelectorAll('a.nav-link'));
      expect(links.map((a) => a.getAttribute('href'))).toEqual(items.map(([id]) => `#/${id}`));
      expect(links.map((a) => a.querySelector('.nav-label')?.textContent)).toEqual(items.map(([, label]) => label));
    });
  });

  it('names each headed group by its heading, and the heading is not a link', () => {
    const { container } = render(Nav, { props: { currentRoute: 'overview' } });
    const named = within(container).getAllByRole('group');
    expect(named.map((g) => g.getAttribute('aria-labelledby') && document.getElementById(g.getAttribute('aria-labelledby')!)?.textContent))
      .toEqual(['Memory', 'Execution', 'Decisions', 'Autonomy', 'System']);
    for (const label of Array.from(container.querySelectorAll('.nav-group-label'))) {
      expect(label.closest('a')).toBeNull();
      expect(label.querySelector('a')).toBeNull();
    }
  });

  it('two navs on one page (sidebar and drawer) never share a heading id', () => {
    const { container } = render(Nav, { props: { currentRoute: 'overview', navLabel: 'Site navigation' } });
    render(Nav, { props: { currentRoute: 'overview', navLabel: 'Mobile navigation' } });
    const ids = Array.from(document.querySelectorAll('.nav-group-label')).map((l) => l.id);
    expect(ids).toHaveLength(10);
    expect(new Set(ids).size).toBe(10);
    expect(container.querySelectorAll('.nav-group-label')).toHaveLength(5);
  });

  it('marks the current route', () => {
    const { container } = render(Nav, { props: { currentRoute: 'model-calls' } });
    const item = container.querySelector('a[href="#/model-calls"]');
    expect(item?.getAttribute('aria-label')).toBe('Model calls');
    expect(item?.getAttribute('aria-current')).toBe('page');
    expect(container.querySelectorAll('[aria-current="page"]')).toHaveLength(1);
  });
});
