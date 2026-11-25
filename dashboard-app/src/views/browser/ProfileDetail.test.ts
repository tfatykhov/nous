import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/svelte';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import Browser from '../Browser.svelte';
import { FORBIDDEN_TEXT } from '../../lib/autonomy_settings';
import { EMPTY_TEXT, WRITER_OFF_TEXT, type ProfileDetail, type ProfileList, type ProfileRow } from '../../lib/entity_profiles';
import { fmtUtc } from '../../lib/harness';

// F119 PR 2: Browse's Profiles tab over the /entity-profiles routes. The answers have the shapes list_row and
// detail_body (nous/heart/entity_profile.py) give; every name, line and fact below is made up.

type Answer = [number, unknown];
type Call = { url: string; method: string; body: unknown; type: string | null };
let calls: Call[] = [];
let handlers: Record<string, (body: unknown) => Answer | Promise<Answer>> = {};

function install() {
  vi.stubGlobal('fetch', vi.fn(async (url: string, init: RequestInit = {}) => {
    const method = init.method ?? 'GET';
    const body = typeof init.body === 'string' ? JSON.parse(init.body) : undefined;
    const headers = (init.headers ?? {}) as Record<string, string>;
    calls.push({ url, method, body, type: headers['Content-Type'] ?? null });
    const handler = handlers[`${method} ${url}`];
    const [status, payload] = handler ? await handler(body) : [500, { error: `unexpected ${method} ${url}` }];
    return new Response(JSON.stringify(payload), { status });
  }));
}

const HOSTILE = '<img src=x onerror=alert(1)>';
const T = '2026-11-24T10:00:00+00:00';
// Long before any day the tests run: "Write it again now" is open.
const OLD = '2026-11-02T10:00:00+00:00';
const F1 = '11111111-1111-4111-8111-111111111111';
const F2 = '22222222-2222-4222-8222-222222222222';

const row = (over: Partial<ProfileRow>): ProfileRow => ({
  key: 'quillon', display_name: 'Quillon Marsh', kind: 'person', status: 'profile', fact_count: 12, source_count: 12,
  tokens: 40, written_at: T, active: true, owner_verdict: null, lines: 2, ...over,
});
const COUNTS = { profile: 1, not_entity: 2, owner: 0, asked: 0, failed: 0, removed: 1 };
const list = (profiles: ProfileRow[], over: Partial<ProfileList> = {}): ProfileList => ({
  writer_enabled: true, profiles, counts: COUNTS, total: profiles.length, limit: 20, offset: 0, ...over,
});
const LIST = list([
  row({}),
  row({ key: 'spreadsheet', display_name: null, kind: null, status: 'not_entity', lines: 0 }),
  row({ key: 'ledgerwick', display_name: 'Ledgerwick', status: 'profile', owner_verdict: { verdict: 'not_entity', by: 'proxy', at: T } }),
]);
const REMOVED = list([row({ key: 'harborline', display_name: 'Harborline', active: false })], { total: 1 });
const detail = (over: Partial<ProfileDetail> = {}): ProfileDetail => ({
  key: 'quillon', display_name: 'Quillon Marsh', kind: 'person', status: 'profile',
  lines: [
    { id: 'l1', text: 'Quillon Marsh keeps bees.', fact_ids: [F1], said_on: '2026-11-02', date_from: 'mentioned_at' },
    { id: 'l2', text: HOSTILE, fact_ids: [F1, F2], said_on: '2026-10-30', date_from: 'learned_at' },
  ],
  rejected: ['Quillon Marsh sails.'],
  fact_count: 12, source_count: 12, tokens: 40, written_at: OLD, owner_verdict: null,
  removed_lines: [{ id: 'r1', text: 'Quillon Marsh sails.', fact_ids: [F2], said_on: '2026-10-01', date_from: 'learned_at',
    removed_by: 'proxy', removed_at: T }],
  unkeyed_mentions: 0,
  ...over,
});

const FIRST = 'GET /entity-profiles?limit=20&offset=0';
const ok = (): Answer => [200, { status: 'done', key: 'quillon' }];

beforeEach(() => {
  calls = [];
  handlers = {
    [FIRST]: () => [200, LIST],
    'GET /entity-profiles?limit=20&offset=0&status=removed': () => [200, REMOVED],
    'GET /entity-profiles/quillon': () => [200, detail()],
    'GET /entity-profiles/ledgerwick': () => [200, detail({ key: 'ledgerwick', display_name: 'Ledgerwick',
      owner_verdict: { verdict: 'not_entity', by: 'proxy', at: T }, removed_lines: [] })],
    [`GET /dashboard/graph/node/${F1}?type=fact`]: () => [200, { found: true, node: { id: F1, type: 'fact',
      content: `Quillon keeps three hives ${HOSTILE}`, category: 'person', created_at: T }, connections: [] }],
    [`GET /dashboard/graph/node/${F2}?type=fact`]: () => [404, { found: false }],
  };
  install();
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

async function openProfiles() {
  render(Browser);
  await fireEvent.click(screen.getByRole('tab', { name: 'Profiles' }));
  return waitFor(() => {
    const table = document.querySelector('.tab-body table');
    expect(table).not.toBeNull();
    return table as HTMLTableElement;
  });
}
async function openRow(table: HTMLElement, name: string) {
  await fireEvent.click(within(table).getByRole('button', { name: `Show details for ${name}` }));
  return waitFor(() => {
    const panel = table.querySelector('tr.detail .profile');
    expect(panel).not.toBeNull();
    return panel as HTMLElement;
  });
}
const sent = (method: string) => calls.filter((c) => c.method === method);
/** The last correction's sentence, held above the table. */
const noteText = () => document.getElementById('profiles-note')?.textContent ?? null;
const gets = () => calls.filter((c) => c.method === 'GET').map((c) => c.url);

describe('Browse, Profiles', () => {
  it("each row's status in words, and the status filter with its counts", async () => {
    const table = await openProfiles();
    const rows = Array.from(table.querySelectorAll('tbody > tr:not(.detail)'));
    expect(rows.map((r) => r.querySelector('td[data-label="Status"]')?.textContent?.trim())).toEqual([
      'Profile', 'Not an entity', 'Not an entity, you said so',
    ]);
    expect(rows[1].querySelector('td[data-label="Name"]')?.textContent?.trim()).toBe('spreadsheet');
    const filter = screen.getByRole('group', { name: 'Status' });
    expect(within(filter).getAllByRole('button').map((b) => b.textContent?.trim())).toEqual([
      'All (3)', 'Profile (1)', 'Not an entity (2)', 'You (0)', 'Waiting for a reply (0)', 'Could not be written (0)',
      'Removed (1)',
    ]);
  });

  it('a status reads the list again from its first page, and the search sends what was typed', async () => {
    await openProfiles();
    await fireEvent.click(screen.getByRole('button', { name: 'Removed (1)' }));
    await waitFor(() => expect(screen.getByText('Harborline')).toBeTruthy());
    expect(gets()).toContain('/entity-profiles?limit=20&offset=0&status=removed');

    handlers['GET /entity-profiles?limit=20&offset=0&q=quil&status=removed'] = () => [200, list([])];
    await fireEvent.input(screen.getByRole('textbox', { name: 'Search by name…' }), { target: { value: ' quil ' } });
    await fireEvent.click(screen.getByRole('button', { name: 'Search' }));
    await waitFor(() => expect(screen.getByText('No profile matches.')).toBeTruthy());
  });

  it('the writer off and an empty table each get their sentence', async () => {
    handlers[FIRST] = () => [200, list([], { writer_enabled: false, counts: { profile: 0, not_entity: 0, owner: 0, asked: 0, failed: 0, removed: 0 } })];
    render(Browser);
    await fireEvent.click(screen.getByRole('tab', { name: 'Profiles' }));
    await waitFor(() => expect(screen.getByText(EMPTY_TEXT)).toBeTruthy());
    expect(screen.getByText(WRITER_OFF_TEXT)).toBeTruthy();
  });

  it('a profile opens to its dated lines, the lines removed with who and when, and its facts on demand', async () => {
    const table = await openProfiles();
    const panel = await openRow(table, 'Quillon Marsh');
    await waitFor(() => expect(within(panel).getByText('Quillon Marsh keeps bees.')).toBeTruthy());
    expect(within(panel).getByText('2026-11-02')).toBeTruthy();
    expect(panel.textContent).toContain(HOSTILE);
    expect(panel.querySelector('img')).toBeNull();
    expect(within(panel).getByText('Written Nov 2 10:00 UTC from its 12 facts.')).toBeTruthy();
    const removed = within(panel).getByText(/^Removed from the dashboard, Nov 24 10:00 UTC\./);
    expect(removed.closest('li')?.textContent).toContain('Quillon Marsh sails.');
    // No fact is read until asked for.
    expect(gets().some((u) => u.startsWith('/dashboard/graph/node/'))).toBe(false);

    const second = within(panel).getAllByRole('listitem')[1];
    await fireEvent.click(within(second).getByRole('button', { name: 'Show its facts' }));
    await waitFor(() => expect(second.textContent).toContain(`Quillon keeps three hives ${HOSTILE}`));
    await waitFor(() => expect(second.textContent).toContain('This fact could not be read.'));
    expect(second.querySelector('img')).toBeNull();
    expect(gets()).toEqual(expect.arrayContaining([
      `/dashboard/graph/node/${F1}?type=fact`, `/dashboard/graph/node/${F2}?type=fact`,
    ]));
  });

  it('each correction is one JSON request to its route, then the profile and the list are read again', async () => {
    handlers['DELETE /entity-profiles/quillon/lines/l1'] = ok;
    handlers['POST /entity-profiles/quillon/lines/r1/restore'] = ok;
    handlers['POST /entity-profiles/quillon/not-entity'] = ok;
    handlers['POST /entity-profiles/rebuild'] = () => [200, { outcome: 'written', key: 'quillon' }];
    const table = await openProfiles();
    const panel = await openRow(table, 'Quillon Marsh');
    await waitFor(() => expect(within(panel).getByText('Quillon Marsh keeps bees.')).toBeTruthy());

    const cases: [string, string, string, string][] = [
      ['Remove this line', 'DELETE', '/entity-profiles/quillon/lines/l1', 'Line removed.'],
      ['Put it back', 'POST', '/entity-profiles/quillon/lines/r1/restore', 'Line put back.'],
      ['This is not an entity', 'POST', '/entity-profiles/quillon/not-entity', 'Marked as not an entity.'],
      ['Write it again now', 'POST', '/entity-profiles/rebuild', 'Written again now.'],
    ];
    for (const [label, method, url, said] of cases) {
      const before = calls.length;
      await fireEvent.click(within(panel).getAllByRole('button', { name: label })[0]);
      await waitFor(() => expect(noteText()).toBe(`Quillon Marsh: ${said}`));
      const made = calls.slice(before);
      const change = made.filter((c) => c.method !== 'GET');
      expect(change.map((c) => [c.method, c.url, c.type]), label).toEqual([[method, url, 'application/json']]);
      expect(made.map((c) => c.url), label).toEqual(expect.arrayContaining(['/entity-profiles/quillon', '/entity-profiles?limit=20&offset=0']));
    }
    expect(sent('POST').find((c) => c.url === '/entity-profiles/rebuild')?.body).toEqual({ key: 'quillon' });
  });

  it("the owner's verdict offers its undo and no rewrite; a removed profile offers its restore", async () => {
    handlers['DELETE /entity-profiles/ledgerwick/not-entity'] = ok;
    handlers['POST /entity-profiles/harborline/restore'] = ok;
    const table = await openProfiles();
    const panel = await openRow(table, 'Ledgerwick');
    await waitFor(() => expect(within(panel).getByText(/^You said this is not an entity from the dashboard/)).toBeTruthy());
    expect(within(panel).queryByRole('button', { name: 'Write it again now' })).toBeNull();
    expect(within(panel).queryByRole('button', { name: 'This is not an entity' })).toBeNull();
    await fireEvent.click(within(panel).getByRole('button', { name: 'It is an entity after all' }));
    await waitFor(() => expect(sent('DELETE').map((c) => c.url)).toEqual(['/entity-profiles/ledgerwick/not-entity']));

    await fireEvent.click(screen.getByRole('button', { name: 'Removed (1)' }));
    const removedTable = await waitFor(() => {
      const t = document.querySelector('.tab-body table') as HTMLTableElement;
      expect(within(t).getByText('Harborline')).toBeTruthy();
      return t;
    });
    const removedPanel = await openRow(removedTable, 'Harborline');
    expect(gets()).not.toContain('/entity-profiles/harborline');
    await fireEvent.click(within(removedPanel).getByRole('button', { name: 'Restore this profile' }));
    await waitFor(() => expect(sent('POST').map((c) => c.url)).toEqual(['/entity-profiles/harborline/restore']));
  });

  it('a refused correction says why in words', async () => {
    handlers['DELETE /entity-profiles/quillon/lines/l1'] = () => [403, { error: 'forbidden' }];
    handlers['POST /entity-profiles/quillon/lines/r1/restore'] = () => [409, { error: 'a fact this line cites is no longer live' }];
    const table = await openProfiles();
    const panel = await openRow(table, 'Quillon Marsh');
    await waitFor(() => expect(within(panel).getByText('Quillon Marsh keeps bees.')).toBeTruthy());
    await fireEvent.click(within(panel).getAllByRole('button', { name: 'Remove this line' })[0]);
    await waitFor(() => expect(screen.getByRole('alert').textContent).toBe(`Quillon Marsh: ${FORBIDDEN_TEXT}`));
    await fireEvent.click(within(panel).getByRole('button', { name: 'Put it back' }));
    await waitFor(() => expect(screen.getByRole('alert').textContent).toBe('Quillon Marsh: a fact this line cites is no longer live.'));
  });

  // Review F4: the four corrections that take the row out of the list it is in. The list's answer after the change no
  // longer has the row; the sentence stays above the table.
  const LEAVES: [string, string, string, string, string, string, string][] = [
    // [filter button, list url, row, button, method, change url, sentence]
    ['', FIRST, 'Quillon Marsh', 'Remove this profile', 'DELETE', '/entity-profiles/quillon',
      'Quillon Marsh: Profile removed. Find it under Removed to restore it.'],
    ['Removed (1)', 'GET /entity-profiles?limit=20&offset=0&status=removed', 'Harborline', 'Restore this profile', 'POST',
      '/entity-profiles/harborline/restore', 'Harborline: Profile restored.'],
    ['Profile (1)', 'GET /entity-profiles?limit=20&offset=0&status=profile', 'Quillon Marsh', 'This is not an entity', 'POST',
      '/entity-profiles/quillon/not-entity', 'Quillon Marsh: Marked as not an entity.'],
    ['Not an entity (2)', 'GET /entity-profiles?limit=20&offset=0&status=not_entity', 'Ledgerwick',
      'It is an entity after all', 'DELETE', '/entity-profiles/ledgerwick/not-entity',
      'Ledgerwick: Taken back. Nous writes this profile again when its facts change, or now with Write it again now.'],
  ];
  it.each(LEAVES)('%s: "%s" keeps its sentence when the row leaves the list', async (filter, listUrl, name, label, method, url, said) => {
    let changed = false;
    const shown = filter === '' ? LIST : filter.startsWith('Removed') ? REMOVED
      : filter.startsWith('Profile') ? list([row({})]) : list([LIST.profiles[2]]);
    handlers[listUrl] = () => [200, changed ? list(shown.profiles.filter((p) => (p.display_name ?? p.key) !== name)) : shown];
    handlers[`${method} ${url}`] = () => { changed = true; return ok(); };
    await openProfiles();
    if (filter) await fireEvent.click(screen.getByRole('button', { name: filter }));
    const table = await waitFor(() => {
      const t = document.querySelector('.tab-body table') as HTMLTableElement;
      expect(within(t).getByText(name)).toBeTruthy();
      return t;
    });
    const panel = await openRow(table, name);
    const button = await waitFor(() => within(panel).getByRole('button', { name: label }));
    await fireEvent.click(button);
    await waitFor(() => expect(noteText()).toBe(said));
    await waitFor(() => expect(screen.queryByText(name, { selector: 'td *, td' })).toBeNull());
    expect(document.getElementById('profiles-note')?.getAttribute('role')).toBe('status');
    // A new filter or search: the sentence no longer speaks of what is shown.
    await fireEvent.click(screen.getByRole('button', { name: 'Search' }));
    await waitFor(() => expect(noteText()).toBeNull());
  });

  it('a profile written with a call in the last 24 hours says when it can be written again, and makes no request', async () => {
    const hourAgo = new Date(Date.now() - 3_600_000).toISOString();
    handlers['GET /entity-profiles/quillon'] = () => [200, detail({ written_at: hourAgo })];
    const table = await openProfiles();
    const panel = await openRow(table, 'Quillon Marsh');
    const next = fmtUtc(new Date(new Date(hourAgo).getTime() + 86_400_000).toISOString());
    const button = await waitFor(() => within(panel).getByRole('button', { name: `Write it again after ${next}` }));
    expect((button as HTMLButtonElement).disabled).toBe(true);
    await fireEvent.click(button);
    expect(sent('POST')).toEqual([]);
  });

  it('a rebuild held by the server says when it can be written again', async () => {
    const at = '2026-11-25T09:00:00+00:00';
    handlers['POST /entity-profiles/rebuild'] = () => [200, { outcome: 'held', key: 'quillon', next_at: at }];
    const table = await openProfiles();
    const panel = await openRow(table, 'Quillon Marsh');
    await fireEvent.click(await waitFor(() => within(panel).getByRole('button', { name: 'Write it again now' })));
    await waitFor(() => expect(noteText()).toBe(
      "Quillon Marsh: Not written: it was written in the last 24 hours, and the day's limit has no exception. It can be written again after Nov 25 09:00 UTC.",
    ));
  });

  it('a putting back past the bound says so in words', async () => {
    const full = 'putting it back would make the profile longer than its bound of 8 lines and 300 tokens; remove another line first';
    handlers['POST /entity-profiles/quillon/lines/r1/restore'] = () => [409, { error: full }];
    const table = await openProfiles();
    const panel = await openRow(table, 'Quillon Marsh');
    await fireEvent.click(await waitFor(() => within(panel).getByRole('button', { name: 'Put it back' })));
    await waitFor(() => expect(screen.getByRole('alert').textContent).toBe(`Quillon Marsh: ${full}.`));
  });

  it('a second click while a correction is on its way sends nothing', async () => {
    let release!: () => void;
    handlers['DELETE /entity-profiles/quillon/lines/l1'] = () => new Promise<Answer>((resolve) => { release = () => resolve(ok()); });
    const table = await openProfiles();
    const panel = await openRow(table, 'Quillon Marsh');
    const button = await waitFor(() => within(panel).getAllByRole('button', { name: 'Remove this line' })[0]);
    fireEvent.click(button);
    fireEvent.click(button);
    await waitFor(() => expect(sent('DELETE')).toHaveLength(1));
    release();
    await waitFor(() => expect(noteText()).toBe('Quillon Marsh: Line removed.'));
    expect(sent('DELETE')).toHaveLength(1);
  });

  it('an older answer to the list never replaces a newer one', async () => {
    await openProfiles();
    let release!: () => void;
    handlers['GET /entity-profiles?limit=20&offset=0&status=removed'] = () =>
      new Promise<Answer>((resolve) => { release = () => resolve([200, REMOVED]); });
    await fireEvent.click(screen.getByRole('button', { name: 'Removed (1)' }));
    await waitFor(() => expect(gets()).toContain('/entity-profiles?limit=20&offset=0&status=removed'));
    await fireEvent.click(screen.getByRole('button', { name: /^Removed/ }));  // back to All
    await waitFor(() => expect(screen.getByText('Quillon Marsh')).toBeTruthy());
    release();
    await new Promise((r) => setTimeout(r, 20));
    expect(screen.queryByText('Harborline')).toBeNull();
    expect(screen.getByText('Quillon Marsh')).toBeTruthy();
  });

  it('source contract: no raw HTML, no colour literal', () => {
    for (const file of [join(__dirname, 'ProfileDetail.svelte'), join(__dirname, '..', 'Browser.svelte')]) {
      const src = readFileSync(file, 'utf-8');
      expect(src, file).not.toContain('{@html');
      expect(src, file).not.toMatch(/#[0-9a-fA-F]{3,8}\b|rgba?\(/);
    }
  });
});
