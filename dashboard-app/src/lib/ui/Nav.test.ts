import { describe, it, expect, beforeEach } from 'vitest';
import { render } from '@testing-library/svelte';
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

  it('renders one #/<route> link per known route, all router-parseable', () => {
    const { container } = render(Nav, { props: { currentRoute: 'overview' } });
    const hrefs = Array.from(container.querySelectorAll('a.nav-link')).map((a) =>
      a.getAttribute('href'),
    );
    expect(hrefs.length).toBe(ROUTES.length);
    for (const href of hrefs) {
      expect(href).toMatch(/^#\//); // must be a router-recognised fragment, not a bare anchor
      const route = href!.slice(2);
      expect(ROUTES).toContain(route as (typeof ROUTES)[number]);
    }
  });

  it('puts a badge count in the link name, not only in a coloured pill', async () => {
    const { pushCounts } = await import('../stores/attention');
    pushCounts({ questions: 2, sends: 1 });
    const { container } = render(Nav, { props: { currentRoute: 'overview' } });
    const dag = container.querySelector('a[href="#/dag"]');
    const exec = container.querySelector('a[href="#/execution"]');
    expect(dag?.getAttribute('aria-label')).toBe('DAG Orchestrator, 2 questions waiting');
    expect(exec?.getAttribute('aria-label')).toBe('Execution, 1 send in doubt');
    expect(container.querySelector('a[href="#/harness"]')?.getAttribute('aria-label')).toBe('Harness');
  });

  it('clicking a nav href format actually drives the router', () => {
    initRouter();
    // Simulate what a nav click does: set the hash to the Nav's href format.
    location.hash = '#/cache';
    window.dispatchEvent(new HashChangeEvent('hashchange'));
    expect(get(currentRoute)).toBe('cache');
  });
});
