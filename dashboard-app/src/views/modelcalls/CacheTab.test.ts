import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, cleanup, within } from '@testing-library/svelte';
import ModelCalls from '../ModelCalls.svelte';
import Cache from '../Cache.svelte';
import { initRouter } from '../../lib/router';

// F108 Phase 2 T10: the Cache screen is the Model calls screen's second tab. Its rows link to the call's detail when
// the row carries the model-call id (the endpoint sends it when the model-call log is on).
const CALL = '55555555-5555-4555-8555-555555555555';

const timelineRow = (over: Record<string, unknown> = {}) => ({
  timestamp: '2026-11-07T00:07:00+00:00', session_id: 'sess-aaaaaaaa-1', turn: 3, model: 'claude-opus-5-5',
  input_tokens: 46100, cache_read: 44900, cache_created: 0, hit_rate: 97.4, cache_break: false, break_components: [],
  ...over,
});
const CACHE = {
  summary: {
    total_calls: 3, total_input_tokens: 138300, total_cache_read: 134700, total_cache_created: 0, overall_hit_rate: 97.4,
    total_breaks: 1, break_rate: 33.3, tokens_lost_to_breaks: 1200,
  },
  break_components: { system_prompt: 1 },
  sessions: [
    { session_id: 'sess-aaaaaaaa-1', calls: 2, input_tokens: 92200, cache_read: 89800, cache_created: 0, hit_rate: 97.4, breaks: 1 },
    { session_id: null, calls: 1, input_tokens: 46100, cache_read: 44900, cache_created: 0, hit_rate: 97.4, breaks: 0 },
  ],
  timeline: [
    timelineRow({ id: CALL }),
    timelineRow({ session_id: null, turn: null }),
  ],
};

let urls: string[] = [];
function install() {
  urls = [];
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    urls.push(url);
    const body = url.startsWith('/dashboard/cache') ? CACHE : null;
    if (!body) return { ok: false, status: 404, json: async () => ({}) } as Response;
    return { ok: true, status: 200, json: async () => body } as Response;
  }));
  vi.stubGlobal('Chart', class { destroy() {} update() {} data = {}; options = {}; });
}
function go(hash: string) {
  location.hash = hash;
  window.dispatchEvent(new HashChangeEvent('hashchange'));
}
const recentCalls = () => screen.getByRole('heading', { level: 2, name: 'Recent Calls' }).closest('section') as HTMLElement;
const sessions = () => screen.getByRole('heading', { level: 2, name: 'Sessions' }).closest('section') as HTMLElement;

describe('Model calls Cache tab', () => {
  beforeEach(() => { install(); location.hash = ''; initRouter(); go('#/model-calls/cache'); });
  afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

  it('renders the Cache content under the Model calls header, the Cache sub view current', async () => {
    render(ModelCalls);
    for (const name of ['Token Breakdown', 'Break Components', 'Efficiency Timeline', 'Sessions', 'Recent Calls']) {
      expect(await screen.findByRole('heading', { level: 2, name })).toBeTruthy();
    }
    expect(screen.getByRole('heading', { level: 1, name: 'Model calls' })).toBeTruthy();
    expect(screen.queryByRole('heading', { level: 1, name: 'Cache' })).toBeNull();
    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1);
    const nav = screen.getByRole('navigation', { name: 'Model calls views' });
    expect(within(nav).getByRole('link', { name: 'Cache' }).getAttribute('aria-current')).toBe('page');
    expect(within(nav).getByRole('link', { name: 'Calls' }).getAttribute('aria-current')).toBeNull();
    expect(screen.queryByText(/this view is being migrated/)).toBeNull();
  });

  it('reads the cache endpoint only: the call list is not mounted', async () => {
    render(ModelCalls);
    await screen.findByRole('heading', { level: 2, name: 'Recent Calls' });
    expect(urls.some((u) => u.startsWith('/dashboard/cache'))).toBe(true);
    expect(urls.some((u) => u.startsWith('/dashboard/model-calls'))).toBe(false);
  });

  it('a Recent Calls row links to its call when it carries an id, and not otherwise', async () => {
    render(ModelCalls);
    await screen.findByRole('heading', { level: 2, name: 'Recent Calls' });
    const links = within(recentCalls()).getAllByRole('link');
    expect(links).toHaveLength(1);
    expect(links[0].getAttribute('href')).toBe(`#/model-calls/c/${CALL}`);
    // The link's name carries its visible text (the time).
    expect(links[0].getAttribute('aria-label')).toContain(links[0].textContent!.trim());
  });

  it('a null session shows — in Recent Calls and in Sessions', async () => {
    render(ModelCalls);
    await screen.findByRole('heading', { level: 2, name: 'Recent Calls' });
    const recentRows = within(recentCalls()).getAllByRole('row').slice(1);
    expect(recentRows).toHaveLength(2);
    expect(within(recentRows[1]).getByText('—')).toBeTruthy();
    const sessionRows = within(sessions()).getAllByRole('row').slice(1);
    expect(sessionRows).toHaveLength(2);
    expect(within(sessionRows[1]).getByText('—')).toBeTruthy();
  });
});

describe('Cache standalone (embedded false)', () => {
  beforeEach(() => { install(); location.hash = ''; initRouter(); go('#/cache'); });
  afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

  it('keeps its own Cache header', async () => {
    render(Cache);
    expect(screen.getByRole('heading', { level: 1, name: 'Cache' })).toBeTruthy();
    expect(await screen.findByRole('heading', { level: 2, name: 'Recent Calls' })).toBeTruthy();
  });

  it('embedded drops the h1 and keeps the subtitle and the staleness badge row', async () => {
    const { container } = render(Cache, { props: { embedded: true } });
    await screen.findByRole('heading', { level: 2, name: 'Recent Calls' });
    expect(screen.queryByRole('heading', { level: 1 })).toBeNull();
    expect(container.querySelector('.view-head .subtitle')?.textContent).toContain('API cache performance');
    expect(container.querySelector('.view-head')?.textContent).toMatch(/updated .* ago/);
  });
});
