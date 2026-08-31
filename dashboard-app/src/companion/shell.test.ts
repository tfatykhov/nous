import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, fireEvent, cleanup } from '@testing-library/svelte';
import { tick } from 'svelte';
import Companion from './Companion.svelte';
import { store } from './store.svelte';
import { transport } from './transport';

// F092 Phase 4: the shell's switcher, close-all, and the #/a/ route alias.
// transport.connect is spied out — these tests exercise the shell against
// the store, not the network.

function seed(surfaceId: string, priority = 0, title?: string, headerTitle?: string) {
  store.apply(null, {
    version: 'v1.0',
    createSurface: {
      surfaceId,
      catalogId: 'nous-core',
      components: [
        { id: 'root', component: 'Text', text: `body of ${surfaceId}` },
        ...(headerTitle
          ? [{ id: 'header', component: 'AppHeader', title: headerTitle }]
          : []),
      ],
      dataModel: {},
      metadata: {
        extensions: {
          com_nous_nonce: 'n-' + surfaceId,
          com_nous_priority: priority,
          ...(title ? { com_nous_title: title } : {}),
        },
      },
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

describe('Companion shell — chip labels (micro-app naming)', () => {
  it('labels a micro-app chip with its own title, not the constant "app"', async () => {
    seed('nous:chat:micro_app:aaa001', 0, 'Health Monitor');
    seed('nous:chat:micro_app:bbb002', 0, 'Crypto Note');
    const { container } = render(Companion);
    await settle();
    const chips = [...container.querySelectorAll('.switcher a.chip')].map((c) =>
      c.textContent?.trim(),
    );
    expect(chips).toContain('Health Monitor');
    expect(chips).toContain('Crypto Note');
    // The whole point: two live apps are no longer indistinguishable.
    expect(chips).not.toContain('app');
  });

  it('falls back to the kind label when a micro-app has no title', async () => {
    seed('nous:chat:micro_app:aaa001');
    seed('nous:sweep:decision_sweep:bbb002');
    const { container } = render(Companion);
    await settle();
    const chips = [...container.querySelectorAll('.switcher a.chip')].map((c) =>
      c.textContent?.trim(),
    );
    expect(chips).toContain('app');
  });

  it('keeps the curated label for template kinds even when titled', async () => {
    seed('nous:chat:micro_app:aaa001', 0, 'Health Monitor');
    seed('nous:heartbeat:heartbeat_findings:ccc003', 0, 'Heartbeat Triage \u2014 Aug 30 2026 21:00 UTC');
    const { container } = render(Companion);
    await settle();
    const chips = [...container.querySelectorAll('.switcher a.chip')].map((c) =>
      c.textContent?.trim(),
    );
    expect(chips).toContain('findings');
    expect(chips).not.toContain('Heartbeat Triage \u2014 Aug 30 2026 21:00 UTC');
  });

  it('truncates a long title on a word boundary and keeps the full text on hover', async () => {
    const long = 'Crypto Note: Six Months, Forward View';
    seed('nous:chat:micro_app:aaa001', 0, long);
    seed('nous:sweep:decision_sweep:bbb002');
    const { container } = render(Companion);
    await settle();
    const chip = [...container.querySelectorAll('.switcher a.chip')].find((c) =>
      c.getAttribute('title')?.startsWith(long),
    );
    expect(chip).toBeTruthy();
    const text = chip!.textContent!.trim();
    expect(text.length).toBeLessThanOrEqual(23);
    expect(text.endsWith('\u2026')).toBe(true);
    expect(long.startsWith(text.slice(0, -1))).toBe(true);
    expect(chip!.getAttribute('title')).toContain('nous:chat:micro_app:aaa001');
  });
});

describe('Companion shell — chip label source precedence', () => {
  it('prefers the AppHeader title over the longer record title', async () => {
    seed(
      'nous:chat:micro_app:aaa001',
      0,
      'Crypto Note: Six Months, Forward View',
      'Crypto Note',
    );
    seed('nous:sweep:decision_sweep:bbb002');
    const { container } = render(Companion);
    await settle();
    const chips = [...container.querySelectorAll('.switcher a.chip')].map((c) =>
      c.textContent?.trim(),
    );
    // Short authored name wins outright — no truncation ellipsis.
    expect(chips).toContain('Crypto Note');
  });

  it('names both live apps distinctly from their headers', async () => {
    seed('nous:chat:micro_app:aaa001', 0, undefined, 'Health Monitor');
    seed('nous:chat:micro_app:bbb002', 0, undefined, 'Crypto Note');
    const { container } = render(Companion);
    await settle();
    const chips = [...container.querySelectorAll('.switcher a.chip')].map((c) =>
      c.textContent?.trim(),
    );
    expect(chips).toContain('Health Monitor');
    expect(chips).toContain('Crypto Note');
    expect(chips).not.toContain('app');
  });
});

describe('Companion shell — chip label resolves a bound header title', () => {
  it('names the chip from a {path} title, matching what the header renders', () => {
    // codex P2: `typeof title === 'string'` rejected a DynamicString that
    // AppHeaderView resolves fine, so the chip fell back to the record title
    // (or "app") and DISAGREED with the visible header.
    store.apply(null, {
      version: 'v1.0',
      createSurface: {
        surfaceId: 'nous:chat:micro_app:dyn001',
        catalogId: 'nous-core',
        components: [
          { id: 'root', component: 'Text', text: 'body' },
          { id: 'header', component: 'AppHeader', title: { path: '/meta/name' } },
        ],
        dataModel: { meta: { name: 'Daylight Watch' } },
        metadata: { extensions: { com_nous_nonce: 'n1' } },
      },
    } as never);
    store.apply(null, {
      version: 'v1.0',
      createSurface: {
        surfaceId: 'nous:sweep:decision_sweep:bbb002',
        catalogId: 'nous-core',
        components: [{ id: 'root', component: 'Text', text: 'b' }],
        dataModel: {},
        metadata: { extensions: { com_nous_nonce: 'n2' } },
      },
    } as never);

    const { container } = render(Companion);
    const chips = [...container.querySelectorAll('.switcher .chip')].map((c) =>
      c.textContent?.trim(),
    );
    expect(chips).toContain('Daylight Watch');
    expect(chips).not.toContain('app');
  });
});

describe('Companion shell — chip label edge cases', () => {
  function seedApp(id: string, opts: { header?: unknown; title?: string; dm?: unknown } = {}) {
    store.apply(null, {
      version: 'v1.0',
      createSurface: {
        surfaceId: id,
        catalogId: 'nous-core',
        components: [
          { id: 'root', component: 'Text', text: 'b' },
          ...(opts.header !== undefined
            ? [{ id: 'header', component: 'AppHeader', title: opts.header }]
            : []),
        ],
        dataModel: opts.dm ?? {},
        metadata: {
          extensions: {
            com_nous_nonce: 'n-' + id,
            ...(opts.title ? { com_nous_title: opts.title } : {}),
          },
        },
      },
    } as never);
  }

  it('falls through a whitespace header title to the record title', () => {
    // codex P2: a whitespace title is TRUTHY, so `headerTitle() || title`
    // short-circuited on it and the chip landed back on "app".
    seedApp('nous:chat:micro_app:ws0001', { header: '   ', title: 'Trend Fit' });
    seedApp('nous:sweep:decision_sweep:bbb002');

    const { container } = render(Companion);
    const chips = [...container.querySelectorAll('.switcher .chip')].map((c) =>
      c.textContent?.trim(),
    );
    expect(chips).toContain('Trend Fit');
    expect(chips).not.toContain('app');
  });

  it('never splits a multi-unit grapheme when truncating', () => {
    // `slice` counts UTF-16 units, so a cut inside a surrogate pair rendered a
    // replacement glyph. 30 rockets, no spaces — forces the hard cut.
    seedApp('nous:chat:micro_app:emo001', { header: '🚀'.repeat(30) });
    seedApp('nous:sweep:decision_sweep:bbb002');

    const { container } = render(Companion);
    const chip = [...container.querySelectorAll('.switcher .chip')]
      .map((c) => c.textContent?.trim())
      .find((c) => c?.includes('🚀'));
    expect(chip).toBeDefined();
    expect(chip).not.toContain('�');
    // every rocket kept whole
    expect([...chip!].filter((ch) => ch === '\uD83D' || ch === '\uDE80')).toHaveLength(0);
  });
});
