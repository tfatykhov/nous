import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, cleanup, within, waitFor } from '@testing-library/svelte';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import Overview from './Overview.svelte';
import { attentionOverride, attentionPoll } from '../lib/stores/attention';
import { setLook } from '../lib/look';

// The redesigned Overview: Needs you, Today so far and Health on top, the compact day timeline below them, then the
// memory numbers as before. Each card reads data the dashboard already serves; nothing is invented.

const STATUS = {
  memory: { total_facts: 1, total_episodes: 1, total_chunks: 0, total_decisions: 1, total_procedures: 0,
    active_censors: 0, active_conversations: 0 },
  dashboard: {
    deltas: { facts: { last_7_days: 0 }, episodes: { last_7_days: 0 }, decisions: { last_7_days: 0 },
      procedures: { last_7_days: 0 } },
    distributions: { fact_categories: {}, decision_outcomes: {}, edge_relations: {} },
    timeseries: { facts: [], episodes: [], decisions: [] },
    graph_density: null,
  },
  calibration: { brier_score: null },
};
const MODES = { persist: true, claim_verification: 'warn', context_policy: 'warn', offered_set: 'warn' };
const EXECUTION = { modes: MODES, stats: { calls: 3, sends: 1, repeat_sends_refused: 0 }, attention: [], attention_total: 0, rows: [], next_before: null };
const ATTENTION = {
  questions_waiting: 0, next: null, sends_in_doubt: 0, latest_in_doubt: null, ledger_persisted: true,
  harness: { events_persisted: true, offered_set: { mode: 'warn', warn_7d: 0, refused_7d: 0 },
    context_policy: { mode: 'warn', warn_7d: 0, refused_7d: 0 } },
};
const TOTALS = { owner_turns: 7, background_turns: 135, tool_calls: 611, errors: 3, gated: 1, tool_error_turns: 2, median_seconds: 6.4,
  model_calls: 1284, input_tokens: 4_100_000, cache_read_tokens: 3_300_000 };
const TODAY = {
  enabled: true, day: '2026-11-10', now: '2026-11-10T09:40:00+00:00', truncated: false, totals: TOTALS,
  turns: [{ id: 't1', session_id: 's1', started_at: '2026-11-10T09:12:04+00:00', ended_at: '2026-11-10T09:12:11+00:00',
    work_kind: 'conversation', frame_id: 'question', error: true, gated: false, tool_calls: 3 }],
};
const finding = (fp: string, state: string, over: Record<string, unknown> = {}) => ({
  fingerprint: fp, check_name: 'write_health', source: 'heartbeat', summary: `Finding ${fp}`, urgency: 'normal', state,
  first_seen: '2026-11-10T02:14:00+00:00', last_seen: '2026-11-10T02:14:00+00:00', seen_count: 1, escalated: false,
  outcome: null, reopen_count: 0, ...over,
});

type Reply = { status?: number; body?: unknown };
let replies: Record<string, () => Reply> = {};
let urls: string[] = [];
const base = (): Record<string, () => Reply> => ({
  '/status': () => ({ body: STATUS }),
  '/dashboard/execution': () => ({ body: EXECUTION }),
  '/dashboard/attention': () => ({ body: ATTENTION }),
  '/dashboard/work/today': () => ({ body: TODAY }),
  '/heartbeat/findings': () => ({ body: { findings: [], stats: {} } }),
});
function install() {
  urls = [];
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    urls.push(url);
    const key = Object.keys(replies).find((p) => url.startsWith(p));
    if (!key) throw new Error(`unexpected ${url}`);
    const r = replies[key]();
    const status = r.status ?? 200;
    return { ok: status < 400, status, json: async () => r.body } as Response;
  }));
  vi.stubGlobal('Chart', class { destroy() {} update() {} data = {}; options = {}; });
}
async function show() {
  await attentionPoll.refresh();
  const r = render(Overview);
  await screen.findByRole('heading', { level: 2, name: 'Needs you' });
  return r;
}
const card = (name: string) => screen.getByRole('heading', { level: 2, name }).closest('section') as HTMLElement;

describe('Overview', () => {
  beforeEach(() => { replies = base(); attentionOverride.set({}); install(); });
  afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

  it('a page left open past 00:00 UTC refetches the new day by itself (review M13)', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'Date'] });
    vi.setSystemTime(new Date('2026-11-10T23:59:30Z'));
    try {
      await show();
      await vi.advanceTimersByTimeAsync(0);
      const todays = () => urls.filter((u) => u.startsWith('/dashboard/work/today')).length;
      const before = todays();
      expect(before).toBe(1);
      replies['/dashboard/work/today'] = () => ({ body: { ...TODAY, day: '2026-11-11', now: '2026-11-11T00:00:31+00:00', turns: [] } });
      await vi.advanceTimersByTimeAsync(20_000); // still the 10th: nothing
      expect(todays()).toBe(before);
      await vi.advanceTimersByTimeAsync(45_000); // past midnight
      expect(todays()).toBe(before + 1);
      await vi.advanceTimersByTimeAsync(0);
      await waitFor(() => expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('Today, 11 November'));
    } finally {
      vi.useRealTimers();
    }
  });

  it('the title is the UTC day and the header pills come from the day totals', async () => {
    const { container } = await show();
    // The endpoint's UTC day wins over the browser's clock once it arrives.
    await waitFor(() => expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('Today, 10 November'));
    await waitFor(() => expect(container.querySelector('.meta')).not.toBeNull());
    const pills = Array.from(container.querySelectorAll('.meta > *')).map((p) => p.textContent);
    expect(pills).toEqual(['142 turns', '611 tool calls', '1,284 model calls', '3 errors', 'Now 09:40 UTC']);
    expect(within(container.querySelector('.meta') as HTMLElement).getByText('3 errors').classList.contains('bad')).toBe(true);
  });

  it('Today so far lists the totals; a null figure is left out, never shown as 0', async () => {
    replies['/dashboard/work/today'] = () => ({ body: { ...TODAY, totals: { ...TOTALS, model_calls: null, input_tokens: null, cache_read_tokens: null, median_seconds: null } } });
    const { container } = await show();
    await waitFor(() => expect(card('Today so far').textContent).toContain('Owner turns'));
    const rows = Array.from(card('Today so far').querySelectorAll('tr')).map((r) => [r.querySelector('th')!.textContent, r.querySelector('td')!.textContent]);
    expect(rows).toEqual([['Owner turns', '7'], ['Background turns', '135'], ['Tool calls', '611'], ['Errors', '3']]);
    expect(container.querySelector('.meta')!.textContent).not.toContain('model calls');
  });

  it('Today so far with every figure: median, model calls, input tokens and cache reads', async () => {
    await show();
    await waitFor(() => expect(card('Today so far').textContent).toContain('Owner turns'));
    const rows = Array.from(card('Today so far').querySelectorAll('tr')).map((r) => [r.querySelector('th')!.textContent, r.querySelector('td')!.textContent]);
    expect(rows).toEqual([['Owner turns', '7'], ['Background turns', '135'], ['Tool calls', '611'], ['Errors', '3'],
      ['Median turn', '6.4 s'], ['Model calls', '1,284'], ['Input tokens', '4.1M'], ['Cache reads', '3.3M']]);
  });

  it('Needs you with nothing waiting says so, naming what it checked', async () => {
    await show();
    await waitFor(() => expect(card('Needs you').textContent).toContain('Nothing needs you'));
    expect(card('Needs you').textContent).toContain('no open heartbeat findings');
  });

  it('Needs you lists the open heartbeat findings (new, then acknowledged), at most three, and links the rest', async () => {
    replies['/heartbeat/findings'] = () => ({ body: { stats: {}, findings: [
      finding('a', 'new', { last_seen: '2026-11-10T01:00:00+00:00' }),
      finding('b', 'acknowledged', { urgency: 'high' }),
      finding('c', 'resolved'),
      finding('d', 'suppressed'),
      finding('e', 'auto_closed_known_fp'),
      finding('f', 'new', { urgency: 'low' }),
      finding('g', 'new', { last_seen: '2026-11-10T03:00:00+00:00' }),
    ] } });
    await show();
    await waitFor(() => expect(card('Needs you').querySelectorAll('li.item')).toHaveLength(3));
    const items = Array.from(card('Needs you').querySelectorAll('li.item'));
    // High urgency first, then the most recently seen.
    expect(items.map((i) => i.querySelector('a')!.textContent)).toEqual(['Finding b', 'Finding g', 'Finding a']);
    // The heartbeat acknowledges a finding itself when it routes it to triage: one badge for both states.
    expect(items.map((i) => i.querySelector('[data-slot="badge"]')!.textContent?.trim())).toEqual(['Open', 'Open', 'Open']);
    for (const i of items) expect(i.querySelector('a')!.getAttribute('href')).toBe('#/heartbeat');
    expect(within(card('Needs you')).getByRole('link', { name: '1 more open finding on Heartbeat' }).getAttribute('href')).toBe('#/heartbeat');
    expect(card('Needs you').textContent).not.toContain('Nothing needs you');
  });

  it('a question and a send in doubt are listed with their links', async () => {
    replies['/dashboard/attention'] = () => ({ body: { ...ATTENTION, questions_waiting: 2, sends_in_doubt: 1,
      latest_in_doubt: { tool_name: 'send_email', recipients: ['a@example.test'], created_at: '2026-11-10T08:00:00+00:00', dag_name: null, tombstone: false } } });
    await show();
    expect((await within(card('Needs you')).findByRole('link', { name: '2 questions waiting on you' })).getAttribute('href')).toBe('#/dag');
    expect(within(card('Needs you')).getByRole('link', { name: '1 send ended without confirming delivery' }).getAttribute('href')).toBe('#/work/changes');
  });

  it('findings that cannot be loaded are said, and nothing reads as all-clear', async () => {
    replies['/heartbeat/findings'] = () => ({ status: 404, body: { error: 'Heartbeat not available' } });
    await show();
    await waitFor(() => expect(card('Needs you').textContent).toContain('Heartbeat findings could not be loaded.'));
    expect(card('Needs you').textContent).not.toContain('Nothing needs you');
    expect(card('Needs you').textContent).toContain('No questions waiting and no sends in doubt.');
  });

  it('the attention counts failing say so in Needs you and Health, never a lasting Loading', async () => {
    // A fresh module graph (the app-wide attention poll keeps its last data after an error), the testing library
    // included, so both share one Svelte runtime.
    vi.resetModules();
    const tl = await import('@testing-library/svelte');
    const { default: Fresh } = await import('./Overview.svelte');
    const { attentionPoll: freshPoll } = await import('../lib/stores/attention');
    replies['/dashboard/attention'] = () => ({ status: 404, body: { error: 'no' } });
    replies['/heartbeat/findings'] = () => ({ body: { stats: {}, findings: [
      { fingerprint: 'fp-x', check_name: 'disk_usage', summary: 'Disk at 81% on the database volume', urgency: 'normal', state: 'new', first_seen: '2026-11-10T15:00:00Z', last_seen: '2026-11-10T15:00:00Z' },
    ] } });
    await freshPoll.refresh();
    tl.render(Fresh);
    try {
      await waitFor(() => expect(card('Needs you').textContent).toContain('Questions and sends in doubt could not be loaded.'));
      expect(card('Health').textContent).toContain('Guards and the ledger could not be loaded.');
      expect(card('Needs you').textContent).not.toContain('Loading');
      // Codex on #898: the findings come from another source, so they still show.
      await waitFor(() => expect(card('Needs you').textContent).toContain('Disk at 81% on the database volume'));
    } finally {
      tl.cleanup();
    }
  });

  it('Errors today links the Work list only when its "a tool failed" lens can match, and says it is the last 24 h (review M6)', async () => {
    await show();
    await waitFor(() => expect(card('Health').textContent).toContain('Errors today'));
    const link = within(card('Health')).getByRole('link', { name: 'Sessions where a tool failed, last 24 h' });
    expect(link.getAttribute('href')).toBe('#/work?lens=error&kind=any&days=1');
    cleanup();
    // Three turns had an error, none of them from a failed tool call (had_error or an error text): no link.
    replies['/dashboard/work/today'] = () => ({ body: { ...TODAY, totals: { ...TOTALS, tool_error_turns: 0 } } });
    await show();
    await waitFor(() => expect(card('Health').textContent).toContain('3 turns had an error'));
    expect(within(card('Health')).queryByRole('link', { name: /tool failed/ })).toBeNull();
  });

  it('Health: guards, the ledger, and errors today on watch when a turn failed', async () => {
    await show();
    await waitFor(() => expect(card('Health').textContent).toContain('Errors today'));
    const rows = Array.from(card('Health').querySelectorAll('.item')).map((r) => [
      r.querySelector('.what')!.textContent, r.querySelector('[data-slot="badge"]')!.textContent?.trim(),
    ]);
    expect(rows).toEqual([['Guards', 'Clear'], ['Ledger', 'Clear'], ['Errors today', 'Watch']]);
    expect(within(card('Health')).getByRole('link', { name: 'Open Guards' }).getAttribute('href')).toBe('#/harness');
  });

  it('a failed timeline says it is unavailable while the rest of the page renders', async () => {
    replies['/dashboard/work/today'] = () => ({ status: 404, body: { error: 'no' } });
    const { container } = await show();
    await waitFor(() => expect(card("Today's turns").textContent).toContain("Today's turns are unavailable"));
    expect(card('Today so far').textContent).toContain("Today's turns are unavailable");
    expect(card('Needs you')).not.toBeNull();
    await waitFor(() => expect(container.querySelector('.stat-grid')).not.toBeNull());
    expect(container.querySelector('svg[role="img"]')).toBeNull();
  });

  it('recording off: the timeline and the totals say so', async () => {
    replies['/dashboard/work/today'] = () => ({ body: { ...TODAY, enabled: false, turns: [],
      totals: { ...TOTALS, owner_turns: 0, background_turns: 0, tool_calls: 0, errors: 0, gated: 0, median_seconds: null, model_calls: null, input_tokens: null, cache_read_tokens: null } } });
    await show();
    await waitFor(() => expect(card("Today's turns").textContent).toContain('Tool-call recording is off'));
  });

  it('the cards come first, the compact timeline below them, then the memory numbers', async () => {
    const { container } = await show();
    await waitFor(() => expect(container.querySelector('svg[role="img"]')).not.toBeNull());
    await waitFor(() => expect(container.querySelector('.stat-grid')).not.toBeNull());
    const h2s = Array.from(container.querySelectorAll('h2')).map((h) => h.textContent);
    expect(h2s.slice(0, 5)).toEqual(['Needs you', 'Today so far', 'Health', "Today's turns", 'Memory and decisions']);
  });

  it('the charts read their colours from the ground, and again when the look changes (review I4)', async () => {
    const configs: { data: { datasets: { backgroundColor: unknown; borderColor?: unknown }[] } }[] = [];
    vi.stubGlobal('Chart', class {
      data: unknown; options = {};
      constructor(_el: unknown, cfg: { data: unknown }) { this.data = cfg.data; configs.push(cfg as (typeof configs)[number]); }
      destroy() {}
      update() { configs.push({ data: this.data } as (typeof configs)[number]); }
    });
    const root = document.documentElement.style;
    const set = (vars: Record<string, string>) => Object.entries(vars).forEach(([k, v]) => root.setProperty(k, v));
    set({ '--green': '#34d399', '--yellow': '#fbbf24', '--red': '#f87171', '--series-7': '#6b6b8a', '--series-0': '#7c6af7' });
    replies['/status'] = () => ({ body: { ...STATUS, dashboard: { ...STATUS.dashboard,
      distributions: { fact_categories: {}, decision_outcomes: { success: 3, pending: 1 }, edge_relations: { supports: 2 } } } } });
    try {
      await show();
      await waitFor(() => expect(configs.length).toBeGreaterThanOrEqual(2));
      const outcomes = () => configs.filter((c) => Array.isArray(c.data.datasets[0].backgroundColor)).at(-1)!.data.datasets[0];
      const bars = () => configs.filter((c) => !Array.isArray(c.data.datasets[0].backgroundColor)).at(-1)!.data.datasets[0];
      expect(outcomes().backgroundColor).toEqual(['#34d399', '#6b6b8a']);
      expect(bars().backgroundColor).toBe('rgba(124, 106, 247, 0.5)');
      expect(bars().borderColor).toBe('#7c6af7');
      set({ '--green': '#2f8a4e', '--series-7': '#8a5a2b', '--series-0': '#274455' });
      setLook('paper');
      await waitFor(() => expect(outcomes().backgroundColor).toEqual(['#2f8a4e', '#8a5a2b']));
      expect(bars().borderColor).toBe('#274455');
    } finally {
      setLook('dark');
      for (const k of ['--green', '--yellow', '--red', '--series-7', '--series-0']) root.removeProperty(k);
    }
  });

  it('text only, no colour literal in the style, and the hex guard covers the new files', () => {
    const src = readFileSync(resolve(__dirname, 'Overview.svelte'), 'utf-8');
    expect(src).not.toContain('{@html');
    expect(src).not.toMatch(/#[0-9a-fA-F]{6}\b|rgba?\(/); // markup and script too, not only the style
    const guard = readFileSync(resolve(__dirname, '..', 'hexGuard.test.ts'), 'utf-8');
    for (const f of ['views/Overview.svelte', 'lib/viz/DayTimeline.svelte']) expect(guard).toContain(`'${f}'`);
  });
});
