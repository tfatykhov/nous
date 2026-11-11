import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/svelte';
import MemoryWriteHealth from './MemoryWriteHealth.svelte';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { styleBlocks } from '../lib/testing/css';

// Render smoke for the Memory write health view. The fixture has the shape
// nous/api/memory_write_health_dashboard.py returns; every value is made up.

const dayMetrics = (over: Record<string, unknown> = {}) => ({
  facts_by_source: { user_direct: 12, enumerative_extractor: 40, other: 3 },
  max_facts_one_episode: 9,
  tier1_inflow: { person: { all: 3, non_enum: 2 }, preference: { all: 1, non_enum: 1 }, rule: { all: 4, non_enum: 4 } },
  deactivated: { stale: 0, superseded: 2, merged: 5, removed: 0, user: 0, rollback: 0, unknown: 0 },
  deactivated_tier1_stale: 0, admission_rejected: 7, episodes_started: 6, episodes_ended: 5,
  sleep_missing_endpoint: 0, profile_card_rewrites: 1, ...over,
});

const check = (id: string, state: string, over: Record<string, unknown> = {}) => ({
  id, name: id, state, value: 0, threshold: 1, urgency: 'normal', summary: '', detail: {}, acknowledged: null, ...over,
});

const FULL = () => ({
  enabled: true,
  window_days: 30,
  as_of: new Date().toISOString(),
  days: [
    { day: '2026-11-04', metrics: dayMetrics() },
    { day: '2026-11-05', metrics: dayMetrics({ deactivated_tier1_stale: 2 }) },
  ],
  live: {
    day: '2026-11-05', observed_at: new Date(Date.now() - 5 * 60000).toISOString(), facts_total: 1234, episodes_total: 56,
    vanished_fact_ids: 7, vanished_episode_ids: 0, dangling_edges: 0, sleep_missing_endpoint_total: 3,
    open_episodes: 4, open_episodes_over_24h: 2, ledger_oldest: '2026-02-24T04:40:12+00:00',
    profile_card: { entries: 18, tokens: 380, changed_frac: 0.25 },
    counters: { vanished_fact_ids: 1, vanished_episode_ids: 0, rows_dropped: 1, dangling_edges: 0, sleep_missing_endpoint: 0 },
  },
  checks: [
    check('H1', 'tripped', { urgency: 'high', value: 2, summary: 'memory rows vanished since the baseline: 1 fact ids (owner decides)' }),
    check('F1', 'ok', { value: 40, threshold: 100 }),
    check('S1', 'tripped', { urgency: 'high', value: 2, summary: 'stale scan deactivated 2 tier-1 facts today or yesterday (owner decides)' }),
    check('P1', 'acknowledged', { value: 3, threshold: 5.6, acknowledged: { day: '2026-11-01', at: null, actor: 'dashboard:owner', note: '' } }),
    check('R1', 'ok', { value: 30, threshold: 11.2 }),
    check('A1', 'ok', { value: 0.12, threshold: 0.75 }),
  ],
  rules: [],
  acknowledgements: [{ rule: 'H1', day: '2026-11-03', note: 'baseline at the first observation', actor: 'system', at: '2026-11-03T10:00:00+00:00' }],
});

let body: Record<string, unknown> = FULL();
let urls: string[] = [];
let posts: { url: string; body: unknown }[] = [];
let postStatus = 200;

function install() {
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === 'POST') {
      posts.push({ url, body: JSON.parse(String(init.body)) });
      return { ok: postStatus < 400, status: postStatus, json: async () => ({ acknowledged: {} }) } as Response;
    }
    if (!url.startsWith('/dashboard/memory-write-health')) throw new Error(`unexpected ${url}`);
    urls.push(url);
    return { ok: true, status: 200, json: async () => body } as Response;
  }));
}

describe('Memory write health view', () => {
  beforeEach(() => { body = FULL(); urls = []; posts = []; postStatus = 200; install(); });
  // globals: false (vite.config.ts): testing-library cannot register its own cleanup.
  afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

  it('renders the verdict, a tile per rule, the hard-delete signals, the days and the acknowledgements', async () => {
    const { container } = render(MemoryWriteHealth);
    expect(await screen.findByText('2 rules tripped: you decide')).toBeTruthy();
    expect(urls[0]).toBe('/dashboard/memory-write-health?days=30');
    expect(container.querySelectorAll('.tile').length).toBe(6);
    expect(screen.getByText(/memory rows vanished since the baseline/)).toBeTruthy();
    expect(screen.getByText('Acknowledged up to 2026-11-01 by dashboard:owner')).toBeTruthy();
    expect(screen.getByText('1,234')).toBeTruthy();
    // newest day first, the tier-1 stale count highlighted
    const rows = container.querySelectorAll('tbody tr th[scope="row"]');
    expect(rows[0].textContent).toBe('2026-11-05');
    expect(container.querySelector('td.hot')?.textContent).toBe('2');
    expect(screen.getByText('baseline at the first observation')).toBeTruthy();
  });

  it('offers Acknowledge only on tripped rules, posts the rule and note, then reloads', async () => {
    render(MemoryWriteHealth);
    await screen.findByText('2 rules tripped: you decide');
    expect(screen.queryByRole('button', { name: 'Acknowledge P1' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Acknowledge F1' })).toBeNull();
    const note = screen.getByLabelText('Note (optional)', { selector: '#note-H1' }) as HTMLInputElement;
    await fireEvent.input(note, { target: { value: 'restored from backup' } });
    await fireEvent.click(screen.getByRole('button', { name: 'Acknowledge H1' }));
    await waitFor(() => expect(posts.length).toBe(1));
    expect(posts[0]).toEqual({ url: '/dashboard/memory-write-health/acknowledge', body: { rule: 'H1', note: 'restored from backup' } });
    await waitFor(() => expect(urls.length).toBe(2));
  });

  it('says so when an acknowledgement is refused', async () => {
    postStatus = 400;
    render(MemoryWriteHealth);
    await screen.findByText('2 rules tripped: you decide');
    await fireEvent.click(screen.getByRole('button', { name: 'Acknowledge S1' }));
    expect(await screen.findByRole('alert')).toBeTruthy();
    expect(screen.getByRole('alert').textContent).toContain('Could not acknowledge S1');
  });

  it('says the check is off rather than showing empty tiles', async () => {
    body = { enabled: false, window_days: 30, days: [], live: null, checks: [], acknowledgements: [] };
    render(MemoryWriteHealth);
    expect(await screen.findByText(/Memory write health is off/)).toBeTruthy();
  });

  it('shows an empty state before the first run', async () => {
    body = { ...FULL(), days: [], live: null, checks: [], acknowledgements: [] };
    const { container } = render(MemoryWriteHealth);
    expect(await screen.findByText(/No rollup yet/)).toBeTruthy();
    expect(container.querySelectorAll('.tile').length).toBe(0);
  });

  it('shows no rule tripped when none is', async () => {
    body = { ...FULL(), checks: FULL().checks.map((c) => ({ ...c, state: 'ok', summary: '' })) };
    render(MemoryWriteHealth);
    expect(await screen.findByText('No rule tripped')).toBeTruthy();
    expect(screen.queryByRole('button', { name: /Acknowledge/ })).toBeNull();
  });
});

describe('Memory write health on the paper look (PR 2)', () => {
  beforeEach(() => { body = FULL(); urls = []; posts = []; postStatus = 200; install(); });
  afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

  it('keeps the verdict and tone classes the tokens hang on', async () => {
    const { container } = render(MemoryWriteHealth);
    await screen.findByText('2 rules tripped: you decide');
    expect(container.querySelector('section.strip')?.classList.contains('verdict-bad')).toBe(true);
    const tones = Array.from(container.querySelectorAll('.tile')).map((t) => [...t.classList].find((c) => c.startsWith('tone-')));
    // Each tile's state is the shared tinted status pill (finish review fix 2); muted maps to the neutral pill.
    const pills = Array.from(container.querySelectorAll('.tile .status-pill')).map((p) => [...p.classList].find((c) => c.startsWith('status-pill--')));
    expect(tones).toContain('tone-bad');
    expect(pills.map((p) => p?.replace('status-pill--', 'tone-').replace('tone-neutral', 'tone-muted'))).toEqual(tones);
  });

  it('a quiet day gets the ok verdict', async () => {
    body = { ...FULL(), checks: FULL().checks.map((c) => ({ ...c, state: 'ok', summary: '' })) };
    const { container } = render(MemoryWriteHealth);
    await screen.findByText('No rule tripped');
    expect(container.querySelector('section.strip')?.classList.contains('verdict-ok')).toBe(true);
  });

  it('the style reads status tokens, with the old alphas as color-mix percentages', () => {
    const css = styleBlocks(readFileSync(join(__dirname, 'MemoryWriteHealth.svelte'), 'utf-8'));
    expect(css).toMatch(/\.verdict-ok \{ color: var\(--ok-text\); border-color: color-mix\(in srgb, var\(--ok-text\) 35%, transparent\);/);
    expect(css).toMatch(/\.verdict-bad \{ color: var\(--err-text\); border-color: color-mix\(in srgb, var\(--tone-bad\) 50%, transparent\);/);
    expect(css).toContain('.tile.tone-bad { border-color: color-mix(in srgb, var(--tone-bad) 60%, transparent); }');
    expect(css).not.toMatch(/\.pill\b|\.pill-/); // the states are app.css's .status-pill (finish review fix 2)
    expect(css).toMatch(/background: var\(--action\); color: var\(--on-accent\);/); // Atlantic, the primary action (finish review fix 2)
    expect(css).not.toMatch(/#[0-9a-fA-F]{3,8}\b|rgba?\(/);
  });
});
