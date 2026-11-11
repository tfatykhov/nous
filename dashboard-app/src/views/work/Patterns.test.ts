import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/svelte';
import Work from '../Work.svelte';
import { initRouter } from '../../lib/router';

const HOSTILE = '<img src=x onerror=alert(1)>';

const kinds = (c: number, t: number, b: number) => ({
  conversation: { turns: c, calls: c },
  task: { turns: t, calls: t },
  background: { turns: b, calls: b },
});
const tool = (name: string, lane: string, calls: number, by: [number, number, number], reasons: { text: string; count: number }[] = []) => ({
  tool: name, lane, calls, errors: 0, sessions: 3, by_kind: kinds(...by), reasons,
});
const DATA = {
  enabled: true,
  window_days: 7,
  reason_coverage: { calls: 1203, with_reason: 812 },
  tools: [
    tool('recall_deep', 'memory', 40, [20, 20, 0], [{ text: 'Find what was said before', count: 9 }]),
    tool('bash', 'workspace', 100, [50, 0, 50], [
      { text: 'Check a service', count: 7 }, { text: 'Read a log', count: 4 }, { text: 'Run a script', count: 2 },
    ]),
    tool(HOSTILE, 'acts', 5, [0, 5, 0], [{ text: HOSTILE, count: 1 }]),
  ],
  failing: [
    { tool: 'learn_skill', calls: 40, errors: 18, error_rate: 0.45 },
    { tool: 'dag_create', calls: 20, errors: 5, error_rate: 0.25 },
  ],
  wasted: { repeated_calls: 12, retried_and_failed_again: 3 },
};

let urls: string[] = [];
let respond: (url: string) => unknown = () => DATA;
function install() {
  urls = [];
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    urls.push(url);
    return { ok: true, status: 200, json: async () => respond(url) } as Response;
  }));
}
function go(hash: string) {
  location.hash = hash;
  window.dispatchEvent(new HashChangeEvent('hashchange'));
}
const params = () => new URL(urls[urls.length - 1], 'http://x').searchParams;
const rowOf = (name: string) => screen.getByText(name, { selector: '.tool-name' }).closest('tr') as HTMLElement;

describe('Work patterns', () => {
  beforeEach(() => {
    respond = () => DATA;
    install();
    location.hash = '';
    initRouter();
    go('#/work/patterns');
  });
  afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

  it('lists tools by calls with a lane dot, per-kind counts, dashes for zero and proportional bars', async () => {
    render(Work);
    await screen.findByText('bash', { selector: '.tool-name' });
    const names = Array.from(document.querySelectorAll('tbody .tool-name')).map((e) => e.textContent);
    expect(names).toEqual(['bash', 'recall_deep', HOSTILE]);
    const bash = rowOf('bash');
    const cells = bash.querySelectorAll('td');
    expect(cells[0].textContent).toContain('50');
    expect(cells[1].textContent).toContain('—');
    expect(cells[2].textContent).toContain('50');
    const dot = bash.querySelector('.dot') as HTMLElement;
    expect(dot.getAttribute('aria-hidden')).toBe('true');
    expect(dot.style.getPropertyValue('--dot')).toBe('var(--lane-workspace)');
    // The widest value in a column fills it; the rest are in proportion.
    const bar = (tr: HTMLElement, i: number) => (tr.querySelectorAll('td')[i].querySelector('.fill') as HTMLElement | null)?.style.width;
    expect(bar(bash, 0)).toBe('100%');
    expect(bar(rowOf('recall_deep'), 0)).toBe('40%');
    expect(bar(bash, 1)).toBeUndefined(); // zero: no bar at all
  });

  it('the kind bars and the failing bars read tokens', async () => {
    render(Work);
    await screen.findByText('bash', { selector: '.tool-name' });
    const fill = (tr: HTMLElement, i: number) => tr.querySelectorAll('td')[i].querySelector('.fill') as HTMLElement;
    expect(fill(rowOf('recall_deep'), 0).style.getPropertyValue('--fill')).toBe('var(--accent-text)');
    expect(fill(rowOf('recall_deep'), 1).style.getPropertyValue('--fill')).toBe('var(--kind-task)');
    expect(fill(rowOf('bash'), 2).style.getPropertyValue('--fill')).toBe('var(--muted)');
    const fail = (await screen.findByRole('link', { name: /learn_skill/ })).querySelector('.fill') as HTMLElement;
    expect(fail.style.getPropertyValue('--fill')).toBe('var(--red)');
  });

  it('shows the common reasons under each tool', async () => {
    render(Work);
    await screen.findByText('bash', { selector: '.tool-name' });
    const bash = rowOf('bash');
    for (const t of ['Check a service', 'Read a log', 'Run a script']) expect(bash.textContent).toContain(t);
    expect(rowOf('recall_deep').textContent).toContain('Find what was said before');
  });

  it('draws the failing tools as bars with a percentage and a link to the failing sessions', async () => {
    render(Work);
    const link = await screen.findByRole('link', { name: /learn_skill/ });
    expect(link.getAttribute('href')).toBe('#/work?tool=learn_skill&lens=error&kind=any&days=7');
    expect(link.textContent).toContain('45%');
    expect(link.textContent).toContain('18 of 40 calls');
    const fill = link.querySelector('.fill') as HTMLElement;
    expect(fill.style.width).toBe('45%');
    expect(screen.getByRole('link', { name: /dag_create/ }).textContent).toContain('25%');
  });

  it('a failing tool links to the sessions list of the same window', async () => {
    respond = () => ({ ...DATA, window_days: 30 });
    go('#/work/patterns?days=30');
    render(Work);
    const link = await screen.findByRole('link', { name: /learn_skill/ });
    expect(link.getAttribute('href')).toBe('#/work?tool=learn_skill&lens=error&kind=any&days=30');
    expect(screen.getByRole('link', { name: /dag_create/ }).getAttribute('href')).toBe('#/work?tool=dag_create&lens=error&kind=any&days=30');
  });

  it('a one-day window reads "1 day" in the subtitle and over the failing tools', async () => {
    respond = () => ({ ...DATA, window_days: 1 });
    go('#/work/patterns?days=1');
    render(Work);
    await screen.findByRole('link', { name: /learn_skill/ });
    expect(screen.getByText(/Every tool across all sessions in the last 1 day\./)).toBeTruthy();
    expect(screen.getByText('Error rate per call, last 1 day')).toBeTruthy();
    expect(document.body.textContent).not.toMatch(/1 days/);
  });

  it('says so when no tool has enough calls with errors', async () => {
    respond = () => ({ ...DATA, failing: [] });
    render(Work);
    expect(await screen.findByText('No tool has 20 or more calls with errors in this window.')).toBeTruthy();
  });

  it('shows the reason coverage and never divides by zero', async () => {
    render(Work);
    expect(await screen.findByText('Reasons recorded on 812 of 1,203 calls (67%)')).toBeTruthy();
    cleanup();
    respond = () => ({ ...DATA, reason_coverage: { calls: 0, with_reason: 0 }, tools: [], failing: [] });
    install();
    render(Work);
    const line = await screen.findByText(/Reasons recorded on 0 of 0 calls/);
    expect(line.textContent).not.toMatch(/%|NaN/);
  });

  it('explains the two wasted-step counts', async () => {
    render(Work);
    expect(await screen.findByText(/12 extra calls repeated an identical call in the same turn/)).toBeTruthy();
    expect(screen.getByText(/3 calls failed again after the previous identical call in the same turn had failed/)).toBeTruthy();
  });

  it('says the recording is off and shows nothing else', async () => {
    respond = () => ({ ...DATA, enabled: false, tools: [], failing: [] });
    render(Work);
    expect(await screen.findByText(/Tool-call recording is off/)).toBeTruthy();
    expect(screen.queryByText('Tools that fail most')).toBeNull();
  });

  it('renders hostile tool and reason text as text', async () => {
    const { container } = render(Work);
    await screen.findByText(HOSTILE, { selector: '.tool-name' });
    expect(container.querySelector('img')).toBeNull();
    expect(container.textContent).toContain(HOSTILE);
  });

  it('fetches once with days=7, changes the window through the URL, and has a manual Refresh without polling', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      render(Work);
      await screen.findByText('bash', { selector: '.tool-name' });
      expect(urls).toHaveLength(1);
      expect(params().get('days')).toBe('7');
      await vi.advanceTimersByTimeAsync(120_000);
      expect(urls).toHaveLength(1);
      const select = screen.getByLabelText('Window') as HTMLSelectElement;
      expect(Array.from(select.options).map((o) => o.value)).toEqual(['7', '14', '30']);
      await fireEvent.change(select, { target: { value: '30' } });
      expect(location.hash).toBe('#/work/patterns?days=30');
      await waitFor(() => expect(urls).toHaveLength(2));
      expect(params().get('days')).toBe('30');
      await fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
      await waitFor(() => expect(urls).toHaveLength(3));
      expect(params().get('days')).toBe('30');
    } finally {
      vi.useRealTimers();
    }
  });

  it('reads days from the URL and falls back to 7 for anything else', async () => {
    go('#/work/patterns?days=14');
    render(Work);
    await screen.findByText('bash', { selector: '.tool-name' });
    expect(params().get('days')).toBe('14');
    cleanup();
    install();
    go('#/work/patterns?days=999');
    render(Work);
    await waitFor(() => expect(urls).toHaveLength(1));
    expect(params().get('days')).toBe('7');
  });
});
