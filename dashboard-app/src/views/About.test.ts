import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/svelte';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import About from './About.svelte';
import type { AboutAnswer } from '../lib/about';

// The About screen over GET /dashboard/about. The answer has the shape nous/api/about_dashboard.py gives; every value
// is made up.

const ANSWER: AboutAnswer = {
  name: 'Nous',
  version: '1.3.0',
  build: { commit: '2b97aa31c0de', time: '2026-11-23T14:02:05Z' },
  started_at: '2026-11-20T09:30:00Z',
  uptime_seconds: 3 * 86400 + 120,
  python: '3.14.0',
  schema: { latest: 122 },
  database: { server: '17.2', pgvector: '0.8.0' },
  agent: { id: 'nous-test', name: '<b>bold</b>' },
  models: {
    main: 'claude-main-test',
    background: 'claude-background-test',
    critic: 'claude-critic-test',
    heartbeat: 'claude-heartbeat-test',
    embeddings: 'text-embedding-test',
  },
};

let answers: [number, unknown][] = [];
let urls: string[] = [];

function install() {
  urls = [];
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    urls.push(url);
    const [status, body] = answers.shift() ?? [200, ANSWER];
    return new Response(JSON.stringify(body), { status });
  }));
}

const rowValue = (label: string) => {
  const dt = screen.getAllByText(label, { selector: 'dt' });
  expect(dt).toHaveLength(1);
  return dt[0].nextElementSibling as HTMLElement;
};

beforeEach(() => {
  answers = [];
  install();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('About', () => {
  it('shows the title, every label and value, and reads once', async () => {
    render(About);
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('About');
    expect(screen.getByText('What this Nous is and where it runs.')).toBeTruthy();
    expect(await screen.findByText('1.3.0')).toBeTruthy();

    expect(rowValue('Version').textContent).toBe('1.3.0');
    expect(rowValue('Build').textContent).toBe('Commit 2b97aa31c0de, built Nov 23 14:02 UTC');
    expect(within(rowValue('Build')).getByText('2b97aa31c0de').classList.contains('mono')).toBe(true);
    expect(rowValue('Running since').textContent).toBe('Nov 20 09:30 UTC, up 3 days');
    expect(rowValue('Name').textContent).toBe('<b>bold</b>');
    expect(rowValue('Name').querySelector('b')).toBeNull();
    expect(rowValue('Id').textContent).toBe('nous-test');
    expect(screen.getByText('Configured models')).toBeTruthy();
    for (const [label, value] of [
      ['Main', 'claude-main-test'], ['Background work', 'claude-background-test'], ['Critic', 'claude-critic-test'],
      ['Heartbeat', 'claude-heartbeat-test'], ['Embeddings', 'text-embedding-test'],
      ['Python', '3.14.0'], ['PostgreSQL', '17.2'], ['pgvector', '0.8.0'], ['Database schema', 'Migration 122'],
    ]) {
      expect(rowValue(label).textContent, label).toBe(value);
    }
    expect(screen.queryByText('The database could not be read')).toBeNull();
    expect(urls).toEqual(['/dashboard/about']);
  });

  it('links to the release notes and the source in a new tab, and says so', async () => {
    render(About);
    await screen.findByText('1.3.0');
    for (const [label, href] of [
      ['Release notes', 'https://github.com/tfatykhov/nous/releases'],
      ['Source code', 'https://github.com/tfatykhov/nous'],
    ]) {
      const link = screen.getByRole('link', { name: label });
      expect(link.getAttribute('href')).toBe(href);
      expect(link.getAttribute('target')).toBe('_blank');
      expect(link.getAttribute('rel')?.split(' ')).toEqual(expect.arrayContaining(['noopener', 'noreferrer']));
      expect(link.closest('li')?.textContent).toContain('Opens in a new tab');
    }
  });

  it('leaves the Build row out when neither build value is known', async () => {
    answers = [[200, { ...ANSWER, build: { commit: null, time: null } }]];
    render(About);
    await screen.findByText('1.3.0');
    expect(screen.queryByText('Build', { selector: 'dt' })).toBeNull();
  });

  it('shows a time alone as the Build row', async () => {
    answers = [[200, { ...ANSWER, build: { commit: null, time: '2026-11-23T14:02:05Z' } }]];
    render(About);
    await screen.findByText('1.3.0');
    expect(rowValue('Build').textContent).toBe('Built Nov 23 14:02 UTC');
  });

  it('says the database could not be read when its part is missing', async () => {
    answers = [[200, { ...ANSWER, schema: { latest: null }, database: { server: null, pgvector: null } }]];
    render(About);
    expect(await screen.findByText('The database could not be read')).toBeTruthy();
    for (const label of ['PostgreSQL', 'pgvector', 'Database schema']) {
      expect(rowValue(label).textContent, label).toBe('Not available');
    }
    expect(rowValue('Version').textContent).toBe('1.3.0');
  });

  it('a database read without pgvector shows only that row as not available', async () => {
    answers = [[200, { ...ANSWER, database: { server: '17.2', pgvector: null } }]];
    render(About);
    await screen.findByText('1.3.0');
    expect(rowValue('pgvector').textContent).toBe('Not available');
    expect(rowValue('PostgreSQL').textContent).toBe('17.2');
    expect(rowValue('Database schema').textContent).toBe('Migration 122');
    expect(screen.queryByText('The database could not be read')).toBeNull();
  });

  it('a failed read offers to try again, and trying again reads once more', async () => {
    answers = [[404, { error: 'not found' }]];
    render(About);
    expect(await screen.findByText('This page could not be loaded.')).toBeTruthy();
    expect(urls).toHaveLength(1);
    await fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    await waitFor(() => expect(screen.getByText('1.3.0')).toBeTruthy());
    expect(urls).toEqual(['/dashboard/about', '/dashboard/about']);
    expect(screen.queryByText('This page could not be loaded.')).toBeNull();
  });

  it('says it is loading until the answer comes', async () => {
    let release: (r: Response) => void = () => {};
    vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>((r) => { release = r; })));
    render(About);
    expect(screen.getByRole('status').textContent).toBe('Loading…');
    release(new Response(JSON.stringify(ANSWER), { status: 200 }));
    expect(await screen.findByText('1.3.0')).toBeTruthy();
    expect(screen.queryByRole('status')).toBeNull();
  });

  it('has no refresh and no staleness badge: it reads once on mount, and again only after a failed read', async () => {
    render(About);
    await screen.findByText('1.3.0');
    expect(screen.queryAllByRole('button')).toEqual([]);
    const src = readFileSync(join(__dirname, 'About.svelte'), 'utf-8');
    expect(src).not.toContain('StaleBadge');
    expect(src).not.toMatch(/usePoll|setInterval/);
  });
});
