import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, cleanup, waitFor, fireEvent, within } from '@testing-library/svelte';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import Consolidation from './Consolidation.svelte';
import { styleBlocks } from '../lib/testing/css';

// Consolidation on the paper look: status badges by token, the Before and After panes side by side with no arrow
// between them, and the cycle table with every column. Every value below is made up.
const SRC = readFileSync(join(__dirname, 'Consolidation.svelte'), 'utf-8');
const T = '2026-11-08T03:00:00+00:00';
const cycle = (id: string, status: string) => ({
  cycle_id: id, trace_id: `t-${id}`, started_at: T, finished_at: T, status, phases_run: ['merge', 'supersede'],
  totals: { merged: 1, superseded: 2 }, action_count: 3,
});
const LIST = { cycles: [cycle('c-done', 'completed'), cycle('c-run', 'running'), cycle('c-fail', 'failed')] };
const LONG = 'w'.repeat(600);
const DETAIL = {
  cycle: LIST.cycles[0],
  actions: [
    { action_id: 'a-1', phase: 'merge', op: 'merge', target_ids: ['f-11111111', 'f-22222222'], rationale: 'same fact twice',
      before: { content: LONG }, after: { content: 'one fact' }, created_at: T },
    { action_id: 'a-2', phase: 'supersede', op: 'supersede', target_ids: ['f-3'], rationale: null, before: null, after: { content: 'new' }, created_at: T },
    // A side whose recorded value is the string "none": it must not read like a missing side.
    { action_id: 'a-3', phase: 'supersede', op: 'supersede', target_ids: ['f-4'], rationale: null, before: 'none', after: { content: 'x' }, created_at: T },
  ],
};
const ACTIVITY = {
  events: [], censor_stats: {}, schedule_stats: {},
  sleep_stats: { last_sleep: T, total_sleeps: 4, facts_created: 9, procedures_created: 1, censors_retired: 0 },
};

function install() {
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    const body = url.startsWith('/dashboard/consolidation/') ? DETAIL : url.startsWith('/dashboard/consolidation') ? LIST
      : url.startsWith('/dashboard/activity') ? ACTIVITY : null;
    if (!body) return { ok: false, status: 404, json: async () => ({}) } as Response;
    return { ok: true, status: 200, json: async () => body } as Response;
  }));
}

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe('Consolidation on the paper look', () => {
  it('the cycle table keeps every column, and each status is a badge of its class', async () => {
    install();
    const { container } = render(Consolidation);
    const table = await waitFor(() => {
      const t = container.querySelector('table.data-table');
      expect(t?.querySelectorAll('tbody tr.cycle-row')).toHaveLength(3);
      return t as HTMLTableElement;
    });
    expect(within(table).getAllByRole('columnheader').map((h) => h.textContent?.trim())).toEqual(['Started', 'Status', 'Phases', 'Totals', 'Actions']);
    const badges = Array.from(table.querySelectorAll('.badge'));
    expect(badges.map((b) => [b.textContent, [...b.classList].find((c) => c.startsWith('badge-'))])).toEqual([
      ['completed', 'badge-good'], ['running', 'badge-warn'], ['failed', 'badge-bad'],
    ]);
    const css = styleBlocks(SRC);
    expect(css).toContain('.badge-good { background: var(--badge-good-bg); color: var(--ok-text); }');
    expect(css).toContain('.badge-warn { background: var(--badge-warn-bg); color: var(--warn-text); }');
    expect(css).toContain('.badge-bad  { background: var(--badge-bad-bg); color: var(--err-text); }');
  });

  it('a cycle\'s actions show Before and After side by side, with no arrow; a missing side is not the string "none"', async () => {
    install();
    const { container } = render(Consolidation);
    await waitFor(() => expect(container.querySelector('tr.cycle-row')).not.toBeNull());
    await fireEvent.click(container.querySelector('tr.cycle-row')!);
    await screen.findByText('same fact twice');
    const diffs = Array.from(container.querySelectorAll('.diff'));
    expect(diffs).toHaveLength(3);
    for (const d of diffs) {
      expect(Array.from(d.querySelectorAll('.diff-label')).map((l) => l.textContent)).toEqual(['Before', 'After']);
      expect(d.querySelector('.diff-arrow')).toBeNull();
    }
    // Review M9: a missing side prints as before (the empty-set sign), so it differs from a recorded "none".
    expect(diffs[1].querySelector('.diff-before pre')?.textContent).toBe('∅'); // nothing before a supersede's new row
    expect(diffs[2].querySelector('.diff-before pre')?.textContent).toBe('none');
    expect(document.body.textContent).not.toMatch(/→/);
    expect(diffs[0].textContent).toContain(LONG);
    const css = styleBlocks(SRC);
    expect(css).toContain('.diff-before pre { border-left: 2px solid var(--red); }');
    expect(css).toContain('.diff-after pre  { border-left: 2px solid var(--green); }');
    expect(css).not.toContain('diff-arrow');
  });

  it('sleep activity keeps all five figures; no colour literal anywhere in the file', async () => {
    install();
    const { container } = render(Consolidation);
    await waitFor(() => expect(container.querySelectorAll('.detail-grid dt')).toHaveLength(5));
    expect(Array.from(container.querySelectorAll('.detail-grid dt')).map((d) => d.textContent)).toEqual(
      ['Last Sleep', 'Total Sleeps', 'Facts Created', 'Procedures', 'Censors Retired'],
    );
    expect(SRC).not.toMatch(/#[0-9a-fA-F]{3,8}\b|rgba?\(/);
    expect(SRC).not.toContain('{@html');
  });

  it('source contract (jsdom cannot lay out; the 390 px overflow check in the screenshot round is the proof): the header may wrap, and the cycle table sits in an overflow-x box', async () => {
    const css = styleBlocks(SRC);
    expect(css).toMatch(/\.view-head \{[^}]*flex-wrap: wrap;/);
    expect(css).toMatch(/\.table-wrap \{[^}]*overflow-x: auto;/);
    install();
    const { container } = render(Consolidation);
    await waitFor(() => expect(container.querySelector('.table-wrap > table.data-table')).not.toBeNull());
  });
});
