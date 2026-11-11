import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, cleanup, waitFor, within, fireEvent } from '@testing-library/svelte';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import Browser from './Browser.svelte';
import { styleBlocks } from '../lib/testing/css';

// Browse on the paper look: pill tabs and card tables, with every column, row field and page control kept (dense
// stays dense). Every value below is made up.
const HOSTILE = '<img src=x onerror="alert(1)">';
const UNBROKEN = 'x'.repeat(1000);
const T = '2026-11-08T10:00:00+00:00';

const FACTS = {
  facts: [
    { id: 'f-1', content: UNBROKEN, category: 'technical', subject: 'Tim', confidence: 0.9, active: true, tags: ['a', 'b'],
      superseded_by: null, actionable: null, event_date: '2026-11-01', source: null, source_episode_id: null, learned_at: T, created_at: T },
    { id: 'f-2', content: HOSTILE, category: null, subject: null, confidence: 0.4, active: false, tags: [],
      superseded_by: null, actionable: null, event_date: null, source: null, source_episode_id: null, learned_at: T, created_at: T },
  ],
  total: 2,
};
const EPISODES = {
  episodes: [{ id: 'e-1', title: 'An episode', summary: 'what happened', outcome: 'success', started_at: T, tags: ['t'],
    structured_summary: { key_points: ['one', 'two'], outcome_rationale: 'because', lessons: ['learn'] } }],
  total: 1, limit: 20, offset: 0,
};
const DECISIONS = {
  decisions: [{ id: 'd-1', description: 'Use a token', category: 'architecture', stakes: 'medium', confidence: 0.8,
    outcome: 'success', pattern: 'tokens first', tags: [], created_at: T }],
  total: 1, limit: 20, offset: 0,
};
const DECISION = { ...DECISIONS.decisions[0], context: 'the context', reasons: [{ type: 'analysis', text: 'it is simpler' }] };
const PROCEDURES = {
  procedures: [{ id: 'p-1', name: 'Deploy', domain: 'ops', description: 'how to deploy', core_patterns: ['build', 'ship'],
    implementation_notes: ['note'], activation_count: 3, effectiveness: 0.75, score: null }],
  total: 1, limit: 20, offset: 0,
};
const CENSORS = {
  censors: [{ id: 'c-1', trigger_pattern: 'rm -rf', action: 'steer', reason: 'dangerous', domain: null, provenance: 'learned',
    activation_count: 2, false_positive_count: 1, active: true, trigger_action: null, action_instruction: null, unblock_pattern: null, created_at: T }],
  total: 1, limit: 20, offset: 0,
};
const CHUNKS = {
  chunks: [{ id: 'k-1', episode_id: 'e-1234567890', chunk_index: 0, content: 'a chunk', created_at: T }],
  total: 1, limit: 20, offset: 0,
};

let putStatus = 200;
function install() {
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === 'PUT') {
      return { ok: putStatus < 400, status: putStatus, json: async () => CENSORS.censors[0] } as Response;
    }
    const path = url.split('?')[0];
    const body = path === '/facts' ? FACTS : path === '/episodes' ? EPISODES : path === '/decisions' ? DECISIONS
      : path === '/decisions/d-1' ? DECISION : path === '/procedures' ? PROCEDURES : path === '/censors' ? CENSORS
      : path === '/chunks' ? CHUNKS : null;
    if (!body) return { ok: false, status: 404, json: async () => ({}) } as Response;
    return { ok: true, status: 200, json: async () => body } as Response;
  }));
}

afterEach(() => { cleanup(); vi.unstubAllGlobals(); putStatus = 200; });

const TABS: [string, string[], number][] = [
  ['Facts', ['Content', 'Category', 'Subject', 'Confidence', 'Active'], 2],
  ['Episodes', ['Title', 'Summary', 'Outcome', 'Started'], 1],
  ['Decisions', ['Description', 'Category', 'Stakes', 'Confidence', 'Outcome', 'Created'], 1],
  ['Procedures', ['Name', 'Domain', 'Activations', 'Effectiveness'], 1],
  ['Censors', ['Trigger', 'Action', 'Reason', 'Domain', 'Activations', 'Active'], 1],
  ['Chunks', ['Content', 'Idx', 'Episode', 'Created'], 1],
];

async function openTab(name: string) {
  await fireEvent.click(screen.getByRole('tab', { name }));
  await waitFor(() => expect(screen.getByRole('tab', { name }).getAttribute('aria-selected')).toBe('true'));
  return waitFor(() => {
    const table = document.querySelector('.tab-body table');
    expect(table).not.toBeNull();
    return table as HTMLTableElement;
  });
}
/** The column headers, without the disclosure column's hidden "Details". */
const headers = (table: HTMLElement) =>
  within(table).getAllByRole('columnheader').filter((h) => !h.classList.contains('dt-disclose-col')).map((h) => h.textContent?.trim());
const bodyRows = (table: HTMLElement) => Array.from(table.querySelectorAll('tbody > tr:not(.detail)'));
const fields = (panel: Element) => Array.from(panel.querySelectorAll('.dl')).map((d) => d.textContent?.trim());

describe('Browse', () => {
  it('six tabs, each with every column header and every fixture row, inside a card', async () => {
    install();
    render(Browser);
    expect(screen.getAllByRole('tab').map((t) => t.textContent?.trim())).toEqual(TABS.map(([n]) => n));
    for (const [name, cols, rows] of TABS) {
      const table = await openTab(name);
      expect(headers(table), name).toEqual(cols);
      expect(bodyRows(table), name).toHaveLength(rows);
      expect(table.closest('.table-card'), name).not.toBeNull();
      expect(screen.getByText(/^Page 1 of 1/)).toBeTruthy();
    }
  });

  it('an expanded row shows every field of its detail', async () => {
    install();
    render(Browser);
    const cases: [string, string[]][] = [
      ['Facts', ['Content', 'Category', 'Subject', 'Confidence', 'ID', 'Tags', 'Event date']],
      ['Episodes', ['Title', 'Summary', 'Outcome', 'Started', 'Key Points', 'Rationale', 'Lessons', 'Tags']],
      ['Decisions', ['Description', 'Category', 'Stakes', 'Confidence', 'Outcome', 'Pattern', 'Context', 'Reasons', 'ID']],
      ['Procedures', ['Name', 'Domain', 'Description', 'Patterns', 'Notes', 'Activations']],
      ['Censors', ['Trigger', 'Action', 'Provenance', 'Reason', 'Domain', 'Activations', 'False Positives', 'ID']],
      ['Chunks', ['Content', 'Chunk Index', 'Episode', 'Created', 'ID']],
    ];
    for (const [name, want] of cases) {
      const table = await openTab(name);
      await fireEvent.click(within(table).getAllByRole('button', { name: /^Show details for / })[0]);
      await waitFor(() => expect(fields(table.querySelector('tr.detail')!), name).toEqual(want));
    }
  });

  it('a hostile value renders as text, and a 1,000-character unbroken value is allowed to wrap', async () => {
    install();
    const { container } = render(Browser);
    const table = await openTab('Facts');
    for (const b of within(table).getAllByRole('button', { name: /^Show details for / })) await fireEvent.click(b);
    await waitFor(() => expect(table.querySelectorAll('tr.detail')).toHaveLength(2));
    expect(table.textContent).toContain(UNBROKEN);
    expect(table.textContent).toContain(HOSTILE);
    expect(container.querySelector('img')).toBeNull();
    const css = styleBlocks(readFileSync(join(__dirname, 'Browser.svelte'), 'utf-8'));
    expect(css).toMatch(/\.detail-grid > span\)\s*\{[^}]*overflow-wrap:\s*anywhere/);
  });

  it('the censor save message: --save-err when the save fails, --save-ok when saved', async () => {
    install();
    putStatus = 500;
    render(Browser);
    const table = await openTab('Censors');
    await fireEvent.click(within(table).getByRole('button', { name: /^Show details for / }));
    await fireEvent.click(await within(table).findByRole('button', { name: 'Save' }));
    const err = await screen.findByText(/^HTTP 500/);
    expect(err.classList.contains('save-err')).toBe(true);
    // A good save reloads the list at once (the row closes), so "Saved" is pinned in the source.
    const src = readFileSync(join(__dirname, 'Browser.svelte'), 'utf-8');
    expect(src).toContain("class:save-ok={censorSaveStatus[row.id] === 'saved'}");
    const css = styleBlocks(src);
    expect(css).toMatch(/\.save-status\.save-ok\s*\{\s*color:\s*var\(--save-ok\)/);
    expect(css).toMatch(/\.save-status\.save-err\s*\{\s*color:\s*var\(--save-err\)/);
  });

  it('source contract: the tab rules read the SubNav tokens; no colour literal anywhere in the file', () => {
    const src = readFileSync(join(__dirname, 'Browser.svelte'), 'utf-8');
    const css = styleBlocks(src);
    expect(css).toMatch(/\.tab-btn\s*\{[^}]*border-radius:\s*var\(--ctl-radius\)/);
    expect(css).toMatch(/\.tab-btn\.active\s*\{[^}]*background:\s*var\(--tab-active-bg\);[^}]*box-shadow:\s*var\(--tab-active-shadow\)/);
    expect(src).not.toMatch(/#[0-9a-fA-F]{3,8}\b|rgba?\(/);
    expect(src).not.toContain('{@html');
  });
});
