import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, fireEvent, cleanup } from '@testing-library/svelte';
import { tick } from 'svelte';
import Companion from './Companion.svelte';
import { store } from './store.svelte';
import { transport } from './transport';

// F092 Phase 4: the shell's switcher, close-all, and the #/a/ route alias.
// transport.connect is spied out — these tests exercise the shell against
// the store, not the network.

function seed(surfaceId: string, priority = 0) {
  store.apply(null, {
    version: 'v1.0',
    createSurface: {
      surfaceId,
      catalogId: 'nous-core',
      components: [{ id: 'root', component: 'Text', text: `body of ${surfaceId}` }],
      dataModel: {},
      metadata: { extensions: { com_nous_nonce: 'n-' + surfaceId, com_nous_priority: priority } },
    },
  } as never);
}

async function settle(): Promise<void> {
  await new Promise((r) => setTimeout(r, 0));
  await tick();
}

beforeEach(() => {
  store.reset();
  store.connection = 'live';
  location.hash = '';
  vi.spyOn(transport, 'connect').mockResolvedValue(undefined);
  vi.spyOn(transport, 'stop').mockImplementation(() => {});
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  location.hash = '';
});

describe('Companion shell — switcher', () => {
  it('renders a chip per live surface once there is more than one', () => {
    seed('nous:chat:micro_app:aaa001');
    seed('nous:sweep:decision_sweep:bbb002');
    const { container } = render(Companion);

    const chips = [...container.querySelectorAll('.switcher .chip')].map((c) =>
      c.textContent?.trim(),
    );
    expect(chips[0]).toBe('all (2)');
    expect(chips).toContain('app');
    expect(chips).toContain('decisions');
  });

  it('stays hidden for a single surface', () => {
    seed('nous:chat:micro_app:aaa001');
    const { container } = render(Companion);

    expect(container.querySelector('.switcher')).toBeNull();
  });

  it('chips deep-link to the surface view', () => {
    seed('nous:chat:micro_app:aaa001');
    seed('nous:dag:dag_monitor:bbb002');
    const { container } = render(Companion);

    const appChip = [...container.querySelectorAll('a.chip')].find(
      (c) => c.textContent?.trim() === 'app',
    ) as HTMLAnchorElement;
    expect(appChip.getAttribute('href')).toBe('#/s/nous%3Achat%3Amicro_app%3Aaaa001');
  });
});

describe('Companion shell — close-all', () => {
  it('appears with two or more micro-apps and closes each', async () => {
    seed('nous:chat:micro_app:aaa001');
    seed('nous:chat:micro_app:bbb002');
    seed('nous:sweep:decision_sweep:ccc003');
    const spy = vi
      .spyOn(transport, 'postAction')
      .mockResolvedValue({ ok: true, message: '', resolved: true });
    const { getByText } = render(Companion);

    await fireEvent.click(getByText('close all apps (2)'));
    await settle();

    // Micro-apps only — the decision sweep is not disposable.
    expect(spy).toHaveBeenCalledTimes(2);
    expect(spy).toHaveBeenCalledWith('nous:chat:micro_app:aaa001', 'app.close', 'footer', {});
    expect(spy).toHaveBeenCalledWith('nous:chat:micro_app:bbb002', 'app.close', 'footer', {});
  });

  it('is absent with fewer than two micro-apps', () => {
    seed('nous:chat:micro_app:aaa001');
    seed('nous:sweep:decision_sweep:ccc003');
    const { container } = render(Companion);

    expect(container.querySelector('.close-all')).toBeNull();
  });
});

describe('Companion shell — #/a/ route alias', () => {
  it('focuses the surface exactly like #/s/', () => {
    seed('nous:chat:micro_app:aaa001');
    seed('nous:dag:dag_monitor:bbb002');
    location.hash = '#/a/nous%3Achat%3Amicro_app%3Aaaa001';
    const { container } = render(Companion);

    const sections = container.querySelectorAll('section.surface');
    expect(sections.length).toBe(1);
    expect(sections[0].getAttribute('aria-label')).toBe('nous:chat:micro_app:aaa001');
  });
});
