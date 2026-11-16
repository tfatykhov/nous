import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, cleanup, fireEvent, waitFor } from '@testing-library/svelte';
import { get } from 'svelte/store';
import type { BackgroundState } from '../types/api';

// Goals PR 0c: the Halt and Resume control is always in view. The desktop rail is fixed; on a phone the rail is a
// drawer that is inert while closed, so the control sits in two places outside it (the foot of the desktop rail, and a
// compact pill in the mobile header beside the menu button), and a banner above the routed view says what it means.

// The shell only: every screen is a stub (as App.test.ts).
vi.mock('../../views/Overview.svelte', () => ({ default: () => {} }));
vi.mock('../../views/GraphView.svelte', () => ({ default: () => {} }));
vi.mock('../../views/Browser.svelte', () => ({ default: () => {} }));
vi.mock('../../views/Decisions.svelte', () => ({ default: () => {} }));
vi.mock('../../views/Heartbeat.svelte', () => ({ default: () => {} }));
vi.mock('../../views/Observability.svelte', () => ({ default: () => {} }));
vi.mock('../../views/Retrieval.svelte', () => ({ default: () => {} }));
vi.mock('../../views/Health.svelte', () => ({ default: () => {} }));
vi.mock('../../views/Rubric.svelte', () => ({ default: () => {} }));
vi.mock('../../views/Work.svelte', () => ({ default: () => {} }));
vi.mock('../../views/ModelCalls.svelte', () => ({ default: () => {} }));
vi.mock('../../views/Consolidation.svelte', () => ({ default: () => {} }));
vi.mock('../../views/DagView.svelte', () => ({ default: () => {} }));
vi.mock('../../views/Subtasks.svelte', () => ({ default: () => {} }));
vi.mock('../../views/Identity.svelte', () => ({ default: () => {} }));
vi.mock('../../views/Harness.svelte', () => ({ default: () => {} }));
vi.mock('../../views/Intentions.svelte', () => ({ default: () => {} }));
vi.mock('../../views/Autonomy.svelte', () => ({ default: () => {} }));
vi.mock('../../views/Integrations.svelte', () => ({ default: () => {} }));

const { default: App } = await import('../../App.svelte');
const { default: HaltSwitch } = await import('./HaltSwitch.svelte');
const { haltAction, haltPoll } = await import('../stores/halt');

const running = (over: Partial<BackgroundState> = {}): BackgroundState => ({
  halted: false, source: 'none', since: null, by: null, surface: null, verified: false, reason: null, persisted: true, ...over,
});
const halted = (over: Partial<BackgroundState> = {}): BackgroundState =>
  running({ halted: true, source: 'stored', since: '2026-11-15T09:31:00Z', by: 'dashboard', surface: 'dashboard', verified: true, ...over });

let stateAnswer: BackgroundState = running();
let posts: string[] = [];

beforeEach(() => {
  cleanup();
  haltAction.set({ pending: null, outcome: null }); // the store is the app's one: nothing said from the last test
  location.hash = '#/work';
  stateAnswer = running();
  posts = [];
  vi.stubGlobal('fetch', vi.fn(async (url: string, init: RequestInit = {}) => {
    if (url === '/background/state') return new Response(JSON.stringify(stateAnswer), { status: 200 });
    if (init.method === 'POST' && url === '/background/halt') {
      posts.push(url);
      stateAnswer = halted();
      return new Response(JSON.stringify({
        halted: true, already: false, verified: false, persisted: true, text: 'Halted.',
        report: { cancelled: { chains: 0, subtasks: 0, subtasks_running: 0, dags: 0, commands: 0, jobs: 0 }, failed: [], jobs_checked: true },
      }), { status: 200 });
    }
    return new Promise<Response>(() => {}); // the attention poll never answers
  }));
});
afterEach(() => vi.unstubAllGlobals());

/** The shell, once its first halt poll has landed (the store is the app's one, so it may still hold the last test's
 * answer: wait for this render's fetch, so none is in flight when the test unmounts it). */
async function renderApp(want = 'Background work running') {
  const r = render(App);
  await waitFor(() => {
    expect(get(haltPoll).loading).toBe(false);
    expect(r.container.querySelector('aside.sidebar .halt-switch .status-pill')?.textContent).toBe(want);
  });
  return r;
}

describe('placement: always in view', () => {
  it('the desktop block and the mobile pill both render outside the drawer, which is inert while closed', async () => {
    const { container } = await renderApp();
    const drawer = container.querySelector<HTMLElement>('#mobile-drawer')!;
    expect(drawer.inert).toBe(true); // Svelte sets the property (jsdom has it), not the attribute
    expect(drawer.querySelector('.halt-switch')).toBeNull();

    const block = container.querySelector('aside.sidebar .halt-switch');
    expect(block).not.toBeNull();
    expect(block!.classList.contains('halt-switch--compact')).toBe(false);
    expect(block!.textContent).toContain('Background work running');
    expect(block!.querySelector('button')?.textContent?.trim()).toBe('Halt background work');

    const pill = container.querySelector('header.mobile-header .halt-switch');
    expect(pill).not.toBeNull();
    expect(pill!.classList.contains('halt-switch--compact')).toBe(true);
    // Beside the menu button, in the header itself.
    expect(pill!.parentElement?.querySelector(':scope > button.hamburger')).not.toBeNull();
    expect(pill!.querySelector('button')?.textContent?.trim()).toBe('Halt background work');

    expect(container.querySelectorAll('.halt-switch')).toHaveLength(2);
  });

  it('the compact control is 44px tall (it shows only at phone width)', async () => {
    const { container } = await renderApp();
    const button = container.querySelector('header.mobile-header .halt-switch button')!;
    expect(button.classList.contains('min-h-11')).toBe(true);
  });

  it('the banner sits above the routed view while halted, and is gone while running', async () => {
    const { container } = await renderApp();
    expect(container.querySelector('.halt-banner')).toBeNull();

    cleanup();
    stateAnswer = halted();
    const r = await renderApp('Background work halted');
    const banner = r.container.querySelector('.halt-banner')!;
    expect(banner.parentElement?.id).toBe('main-content');
    expect(banner.parentElement?.firstElementChild).toBe(banner);
    expect(banner.textContent).toContain('Halted by you on Nov 15 09:31 UTC.');
    expect(banner.textContent).toContain('Resuming does not restart work that was cancelled.');
  });

  it('one tap on the Halt button posts once and turns the control to Resume', async () => {
    const { container } = await renderApp();
    const halt = container.querySelector<HTMLButtonElement>('aside.sidebar .halt-switch button')!;
    await fireEvent.click(halt);
    await waitFor(() => expect(container.querySelector('aside.sidebar .halt-switch')?.textContent).toContain('Background work halted'));
    expect(posts).toEqual(['/background/halt']);
    const labels = Array.from(container.querySelectorAll('aside.sidebar .halt-switch button')).map((b) => b.textContent?.trim());
    expect(labels).toEqual(['Resume background work']);
  });
});

describe('the control by itself', () => {
  async function renderSwitch(state: BackgroundState, compact = false) {
    stateAnswer = state;
    await haltPoll.refresh();
    return render(HaltSwitch, { props: { compact } });
  }

  it('Resume is there while halted, in both placements', async () => {
    for (const compact of [false, true]) {
      cleanup();
      const { container } = await renderSwitch(halted(), compact);
      const names = Array.from(container.querySelectorAll('button')).map((b) => b.textContent?.trim());
      expect(names, `compact ${compact}`).toEqual(['Resume background work']);
    }
  });

  it('Resume is absent while the host holds the halt; the state is said in its pill', async () => {
    for (const compact of [false, true]) {
      cleanup();
      const { container } = await renderSwitch(halted({ source: 'host', surface: 'host' }), compact);
      expect(container.querySelectorAll('button'), `compact ${compact}`).toHaveLength(0);
      expect(container.querySelector('.status-pill')?.textContent?.trim()).toBe('Halted by the host');
    }
  });

  it('no switch role (the shell has none) and no rail of its own', async () => {
    const { container } = await renderSwitch(running());
    expect(container.querySelectorAll('[role="switch"], .rail')).toHaveLength(0);
  });
});
