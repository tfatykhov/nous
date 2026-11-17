import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, cleanup, waitFor } from '@testing-library/svelte';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import App from '../../App.svelte';
import CommandsBanner from './CommandsBanner.svelte';
import { COMMANDS_NOW } from '../testing/goalFixtures';
import { styleBlocks } from '../testing/css';

// The banner of the goals that may run any command: on every desktop screen, from GET /goals/commands in the server's
// words, never dismissed, the last answer kept when a read fails, nothing with the mode off.

type Answer = [number, unknown];
let answers: Answer[] = [];
let reads = 0;

beforeEach(() => {
  reads = 0;
  answers = [[200, COMMANDS_NOW()]];
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    if (url === '/goals/commands') {
      const answer = answers[Math.min(reads, answers.length - 1)];
      reads += 1;
      if (answer[0] === 0) throw new TypeError('Failed to fetch');
      return new Response(JSON.stringify(answer[1]), { status: answer[0] });
    }
    return new Response(JSON.stringify({ error: 'not found' }), { status: 404 });
  }));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.useRealTimers(); });

describe('the banner', () => {
  for (const hash of ['#/overview', '#/goals', '#/limits']) {
    it(`says which goal may run any command, on ${hash}`, async () => {
      location.hash = hash;
      render(App);
      const banner = await screen.findByRole('status', { name: 'Goals that may run any command' });
      expect(banner.textContent).toContain('G-0003 may run any command until 2026-11-15 20:00 UTC.');
      expect(banner.querySelector('button')).toBeNull(); // nothing closes it
    });
  }

  it('says nothing with the mode off or no goal in it', async () => {
    answers = [[200, COMMANDS_NOW({ allowed: false, active: [] })]];
    render(CommandsBanner);
    await waitFor(() => expect(reads).toBe(1));
    expect(screen.queryByRole('status')).toBeNull();
    cleanup();
    answers = [[200, { enabled: false, allowed: false, active: [] }]];
    render(CommandsBanner);
    await waitFor(() => expect(reads).toBe(2));
    expect(screen.queryByRole('status')).toBeNull();
  });

  it('keeps the last answer when a read fails, and shows nothing before the first', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    answers = [[0, null]];
    render(CommandsBanner);
    await waitFor(() => expect(reads).toBe(1));
    expect(screen.queryByRole('status')).toBeNull();
    answers = [[0, null], [200, COMMANDS_NOW()], [500, { error: 'down' }], [0, null]];
    await vi.advanceTimersByTimeAsync(30_000);
    await screen.findByRole('status');
    await vi.advanceTimersByTimeAsync(30_000);
    await vi.advanceTimersByTimeAsync(30_000);
    expect(reads).toBe(4);
    expect(screen.getByRole('status').textContent).toContain('G-0003 may run any command');
  });

  it('is not shown on a phone', () => {
    const css = styleBlocks(readFileSync(join(__dirname, 'CommandsBanner.svelte'), 'utf-8'));
    expect(css).toMatch(/@media \(max-width: 768px\) \{\s*\.commands-banner \{ display: none; \}/);
  });
});
