import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/svelte';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import Heartbeat from './Heartbeat.svelte';
import { setLook } from '../lib/look';
import { PAPER_SEL, parseBlocks, styleBlocks } from '../lib/testing/css';

// Redesign PR 3: Heartbeat on paper, a dense restyle (no frame treatment: HeartbeatData carries no frame). Urgency,
// state and budget colours are tokens; a colour used as text takes a text token, since paper's amber and red fills
// are marks, not text.

const PAPER = parseBlocks(readFileSync(join(__dirname, '..', 'app.css'), 'utf-8')).get(PAPER_SEL) ?? {};
const NOW = Date.now();
const ago = (min: number) => new Date(NOW - min * 60_000).toISOString();

const finding = (fingerprint: string, urgency: string, state: string) => ({
  fingerprint, check_name: 'disk', source: 'disk', summary: `${urgency} finding ${fingerprint}`, urgency, state,
  first_seen: ago(90), last_seen: ago(5), seen_count: 3, escalated: false, outcome: null, reopen_count: 0,
});

let budget = { used: 5000, limit: 10000, percentage: 50 };
const data = () => ({
  recent_ticks: [{ created_at: ago(1), data: {} }],
  cognitive_sessions: [{ timestamp: ago(30), session_id: 'hb-session-123456', findings_count: 2, tokens_used: 4200, response_summary: 'ok' }],
  totals: { total: 3, by_source: {}, by_urgency: {} },
  findings_by_day: [{ date: '2026-11-10', findings_count: 3, by_urgency: { high: 1, normal: 1, low: 1 } }],
  findings_timeline: [
    { source: 'disk', summary: 'Disk at 91%', urgency: 'high', check_name: 'disk', timestamp: ago(10) },
    { source: 'mail', summary: 'Inbox backlog', urgency: 'normal', check_name: 'mail', timestamp: ago(40) },
    { source: 'feeds', summary: 'A feed is slow', urgency: 'low', check_name: 'feeds', timestamp: ago(70) },
  ],
  status: { enabled: true, is_running: true, last_tick: ago(1), tick_interval: 300 },
  checks: [{ name: 'disk', active: true, interval: 300, last_run: ago(1), consecutive_failures: 0, max_failures: 3,
    circuit_breaker_open: false, permanent: false, urgent_override: false }],
  budget,
  quiet_hours: { start: 22, end: 7, active: false },
  finding_lifecycle: {
    stats: { total: 9, by_state: { new: 2, acknowledged: 1, resolved: 3, suppressed: 1, auto_closed_known_fp: 2 }, by_check: {} },
    findings: [finding('f-high', 'high', 'new'), finding('f-normal', 'normal', 'acknowledged'), finding('f-low', 'low', 'new')],
    escalation_policy: { low_to_normal_hours: 24, normal_to_high_hours: 12, high_realert_hours: 4, accumulation_threshold: 5 },
  },
  tuning: { enabled: true, last_report: { adjustments: 1, skipped_checks: [], timestamp: ago(60), summary: 'raised disk interval' } },
});

type Cfg = { type: string; data: { labels: string[]; datasets: Record<string, any>[] }; options: Record<string, any> };
let configs: Cfg[] = [];
function install() {
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    if (!url.startsWith('/dashboard/heartbeat')) throw new Error(`unexpected ${url}`);
    return { ok: true, status: 200, json: async () => data() } as Response;
  }));
  configs = [];
  vi.stubGlobal('Chart', class {
    data = {}; options = {};
    constructor(_el: unknown, cfg: Cfg) { configs.push(cfg); }
    destroy() {}
    update() {}
  });
}
function paperMain(): HTMLElement {
  const main = document.createElement('main');
  main.setAttribute('data-ground', 'paper');
  for (const [k, v] of Object.entries(PAPER)) if (k.startsWith('--')) main.style.setProperty(k, v);
  document.body.appendChild(main);
  return main;
}
const chart = (t: string) => configs.find((c) => c.type === t)!;
const section = (name: string) => screen.getByRole('heading', { level: 2, name }).closest('section') as HTMLElement;

beforeEach(() => { budget = { used: 5000, limit: 10000, percentage: 50 }; install(); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); setLook('dark'); document.body.innerHTML = ''; });

describe('Heartbeat: urgency, state and budget colours by token', () => {
  it.each([
    [5000, '#22d3ee', '--pending'],
    [8500, '#fbbf24', '--yellow'],
    [10000, '#f87171', '--red'],
  ])('a budget of %i of 10,000 used is %s under dark (%s), the remainder the track', async (used, hex) => {
    budget = { used, limit: 10000, percentage: used / 100 };
    render(Heartbeat);
    await waitFor(() => expect(configs.find((c) => c.type === 'doughnut')).toBeTruthy());
    expect(chart('doughnut').data.datasets[0].backgroundColor).toEqual([hex, 'rgba(255, 255, 255, 0.06)']);
  });

  it('paper: the budget, and the findings bars by urgency, take the paper values', async () => {
    budget = { used: 8500, limit: 10000, percentage: 85 };
    setLook('paper');
    render(Heartbeat, { target: paperMain() });
    await waitFor(() => expect(configs).toHaveLength(2));
    expect(chart('doughnut').data.datasets[0].backgroundColor).toEqual([PAPER['--yellow'], PAPER['--track']]);
    // High is drawn in the error text value: the status red fill and the amber merge under deuteranopia (review I3).
    expect(chart('bar').data.datasets.map((d) => d.backgroundColor)).toEqual([PAPER['--err-text'], PAPER['--yellow'], PAPER['--st-neutral']]);
  });

  it('dark: the findings bars by urgency are today\'s red, amber and grey', async () => {
    render(Heartbeat);
    await waitFor(() => expect(configs).toHaveLength(2));
    expect(chart('bar').data.datasets.map((d) => [d.label, d.backgroundColor])).toEqual([['High', '#f87171'], ['Normal', '#fbbf24'], ['Low', '#6b7280']]);
  });

  it('the state pills show all five states with a word and a count, each edged in its state token', async () => {
    render(Heartbeat);
    await screen.findByRole('heading', { level: 2, name: 'Finding Lifecycle' });
    const pills = Array.from(section('Finding Lifecycle').querySelectorAll<HTMLElement>('.state-pills .hb-pill'));
    expect(pills.map((p) => p.textContent?.trim())).toEqual(
      ['new: 2', 'acknowledged: 1', 'resolved: 3', 'suppressed: 1', 'auto_closed_known_fp: 2', 'total: 9']);
    expect(pills.slice(0, 5).map((p) => p.style.borderColor)).toEqual(
      ['var(--pending)', 'var(--yellow)', 'var(--st-completed)', 'var(--st-neutral)', 'var(--st-neutral)']);
    // The total is dimmed by a class, not an inline opacity, so the paper ground can undo it: muted text at 60% is
    // 2.5:1 on the white card. The dark look keeps its 0.6.
    const total = pills[5];
    expect(total.classList.contains('hb-pill--total')).toBe(true);
    expect(total.style.opacity).toBe('');
    const css = styleBlocks(readFileSync(join(__dirname, 'Heartbeat.svelte'), 'utf-8'));
    expect(css).toMatch(/\.hb-pill--total \{ opacity: 0\.6; \}/);
    expect(css).toMatch(/:global\(html\[data-look="paper"\] \[data-ground="paper"\]\) \.hb-pill--total \{ opacity: 1; \}/);
  });

  it('the escalation note is in words', async () => {
    render(Heartbeat);
    const note = (await screen.findByText(/^Escalation:/)).textContent ?? '';
    expect(note).toContain('low to normal 24h');
    expect(note).toContain('normal to high 12h');
    expect(note).not.toContain('→');
  });

  it('the findings timeline lists each finding with its source, time and summary, its dot by urgency', async () => {
    render(Heartbeat);
    await screen.findByRole('heading', { level: 2, name: 'Findings (Last 24h)' });
    const items = Array.from(section('Findings (Last 24h)').querySelectorAll<HTMLElement>('.tl-item'));
    expect(items.map((i) => i.querySelector('.tl-source')?.textContent)).toEqual(['disk', 'mail', 'feeds']);
    expect(items.map((i) => i.querySelector('.tl-summary')?.textContent)).toEqual(['Disk at 91%', 'Inbox backlog', 'A feed is slow']);
    for (const i of items) expect(i.querySelector('.tl-time')?.textContent).toMatch(/ago$/);
    expect(items.map((i) => i.querySelector<HTMLElement>('.tl-dot')!.style.background)).toEqual(['var(--err-text)', 'var(--yellow)', 'var(--st-neutral)']);
  });

  it('an urgency written as text takes a text token: the error and warn text, the neutral grey', async () => {
    render(Heartbeat);
    await screen.findByRole('heading', { level: 2, name: 'Finding Lifecycle' });
    const life = section('Finding Lifecycle');
    for (const b of within(life).getAllByRole('button', { name: /Show details for/ })) await fireEvent.click(b);
    const inks = Array.from(life.querySelectorAll<HTMLElement>('.finding-detail .urgency')).map((u) => [u.textContent, u.style.color]);
    expect(inks).toEqual([['high', 'var(--err-text)'], ['normal', 'var(--hb-warn-ink)'], ['low', 'var(--st-neutral)']]);
  });

  it('every table keeps its columns', async () => {
    render(Heartbeat);
    await screen.findByRole('heading', { level: 2, name: 'Check Status' });
    // (A table with row details has a first, visually hidden "Details" column.)
    const heads = (name: string) => within(section(name)).getAllByRole('columnheader').map((th) => th.textContent).filter((t) => t && t !== 'Details');
    expect(heads('Check Status')).toEqual(['Check', 'Interval', 'Last run', 'Failures', 'Status']);
    expect(heads('Finding Lifecycle')).toEqual(['State', 'Check', 'Summary', 'Seen', 'Age']);
    expect(heads('Cognitive Sessions')).toEqual(['Time', 'Session', 'Findings', 'Tokens']);
  });

  it('a long unbroken finding wraps, and the two-column rows never widen the page (screenshot round: 2,744 px at 390)', () => {
    const css = styleBlocks(readFileSync(join(__dirname, 'Heartbeat.svelte'), 'utf-8'));
    expect(css).toMatch(/\.chart-row > \* \{ min-width: 0; \}/);
    expect(css).toMatch(/\.tl-source \{[^}]*overflow-wrap: anywhere;/);
    expect(css).toMatch(/\.tl-summary \{[^}]*overflow-wrap: anywhere;/);
  });

  it('the banner and pills read status tokens: text tokens for words, the glow by --glow-blur', () => {
    const css = styleBlocks(readFileSync(join(__dirname, 'Heartbeat.svelte'), 'utf-8'));
    expect(css).toMatch(/\.hb-pill--warn \{ border-color: var\(--yellow\); color: var\(--hb-warn-ink\); \}/);
    expect(css).toMatch(/\.hb-pill--bad \{ border-color: var\(--red\); color: var\(--err-text\); \}/);
    expect(css).toMatch(/\.hb-pill--ok \{ border-color: var\(--st-completed\); color: var\(--st-completed\); \}/);
    expect(css).toMatch(/\.hb-dot--ok \{ background: var\(--st-completed\); box-shadow: 0 0 var\(--glow-blur\) color-mix\(in srgb, var\(--st-completed\) 60%, transparent\); \}/);
    expect(css).toMatch(/\.tuning-pre \{[^}]*background: var\(--wash\);/);
  });
});
