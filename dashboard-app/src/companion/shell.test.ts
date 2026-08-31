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
      // Grammar-shaped: lint_micro_app mandates root = Column whose FIRST
      // child is the AppHeader, and the chip resolves the header through root
      // (as Renderer does) rather than scanning by type.
      components: [
        {
          id: 'root',
          component: 'Column',
          children: [...(headerTitle ? ['header'] : []), 'body'],
        },
        { id: 'body', component: 'Text', text: `body of ${surfaceId}` },
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

    // Two-tap confirmation: the first tap only ARMS the button.
    await fireEvent.click(getByText('close all apps (2)'));
    await settle();
    expect(spy).not.toHaveBeenCalled();
    await fireEvent.click(getByText('sure? close 2 apps'));
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
          { id: 'root', component: 'Column', children: ['header', 'body'] },
          { id: 'body', component: 'Text', text: 'body' },
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
          {
            id: 'root',
            component: 'Column',
            children: [...(opts.header !== undefined ? ['header'] : []), 'body'],
          },
          { id: 'body', component: 'Text', text: 'b' },
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

describe('Companion shell — grapheme-cluster truncation', () => {
  it('never splits a flag or a combining mark', () => {
    // codex P2 (twice): UTF-16 units split surrogate pairs, code points still
    // split CLUSTERS — a flag is two regional indicators, and a letter plus a
    // combining mark is two code points rendering as one character.
    for (const [id, title] of [
      ['nous:chat:micro_app:flag01', 'x'.repeat(21) + '🇺🇸🇬🇧'],
      ['nous:chat:micro_app:comb01', 'y'.repeat(21) + 'é' + 'ǫ̈'],
    ] as const) {
      store.reset();
      store.connection = 'live';
      store.apply(null, {
        version: 'v1.0',
        createSurface: {
          surfaceId: id,
          catalogId: 'nous-core',
          components: [
            { id: 'root', component: 'Column', children: ['header', 'body'] },
            { id: 'body', component: 'Text', text: 'b' },
            { id: 'header', component: 'AppHeader', title },
          ],
          dataModel: {},
          metadata: { extensions: { com_nous_nonce: 'n' } },
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

      const { container, unmount } = render(Companion);
      const chip = [...container.querySelectorAll('.switcher .chip')]
        .map((c) => c.textContent ?? '')
        .find((c) => c.startsWith('xxx') || c.startsWith('yyy'));
      expect(chip).toBeDefined();
      // A well-formed flag CONTAINS surrogates, so the tell of a split is an
      // UNPAIRED one — a high surrogate with no low after it, or vice versa.
      const lone = /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/;
      expect(lone.test(chip!)).toBe(false);
      // ...and regional indicators survive in pairs, never as half a flag.
      const halves = [...chip!].filter((ch) => ch >= '\u{1F1E6}' && ch <= '\u{1F1FF}').length;
      expect(halves % 2).toBe(0);
      unmount();
    }
  });
});

describe('Companion shell — word boundary measured in graphemes', () => {
  it('keeps two astral-prefixed names distinguishable', () => {
    // codex P2: `lastIndexOf` returns a UTF-16 offset compared against a
    // grapheme-based CHIP_MAX, so astral graphemes before the space made it
    // look far earlier than it was — and two distinct names collapsed to the
    // SAME chip, which is the exact failure this PR exists to fix.
    const seed = (id: string, title: string) =>
      store.apply(null, {
        version: 'v1.0',
        createSurface: {
          surfaceId: id,
          catalogId: 'nous-core',
          components: [
            { id: 'root', component: 'Column', children: ['header', 'body'] },
            { id: 'body', component: 'Text', text: 'b' },
            { id: 'header', component: 'AppHeader', title },
          ],
          dataModel: {},
          metadata: { extensions: { com_nous_nonce: 'n-' + id } },
        },
      } as never);

    seed('nous:chat:micro_app:ast001', '🚀'.repeat(7) + ' AlphaMonitoringDashboard');
    seed('nous:chat:micro_app:ast002', '🚀'.repeat(7) + ' BetaMonitoringDashboard');

    const { container } = render(Companion);
    const chips = [...container.querySelectorAll('.switcher .chip')]
      .map((c) => c.textContent?.trim() ?? '')
      .filter((c) => c.includes('🚀'));

    expect(chips).toHaveLength(2);
    expect(chips[0]).not.toBe(chips[1]);
  });
});

describe('Companion shell — header resolved through the current root', () => {
  it('ignores a stale orphan header left behind by a refine', async () => {
    // codex P2: `updateComponents` merges by id and NEVER deletes, so a refine
    // that replaces the header under a new id leaves the old one as an
    // invisible orphan. Scanning by TYPE returned that orphan first (insertion
    // order), so the chip disagreed with the header actually on screen.
    store.apply(null, {
      version: 'v1.0',
      createSurface: {
        surfaceId: 'nous:chat:micro_app:orph01',
        catalogId: 'nous-core',
        components: [
          { id: 'root', component: 'Column', children: ['header_old', 'body'] },
          { id: 'body', component: 'Text', text: 'b' },
          { id: 'header_old', component: 'AppHeader', title: 'Stale Name' },
        ],
        dataModel: {},
        metadata: { extensions: { com_nous_nonce: 'n1' } },
      },
    } as never);
    // The refine: a new root pointing at a NEW header. The old one lingers.
    store.apply(null, {
      version: 'v1.0',
      updateComponents: {
        surfaceId: 'nous:chat:micro_app:orph01',
        components: [
          { id: 'root', component: 'Column', children: ['header_new', 'body'] },
          { id: 'header_new', component: 'AppHeader', title: 'Fresh Name' },
        ],
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
    await settle();
    const chips = [...container.querySelectorAll('.switcher a.chip')].map((c) =>
      c.textContent?.trim(),
    );
    expect(chips).toContain('Fresh Name');
    expect(chips).not.toContain('Stale Name');
  });
});

describe('Companion shell — tooltip reveals what the chip truncated', () => {
  it('hovers the header title, not a divergent record title', async () => {
    // codex P2: the chip shortened `headerTitle` while the tooltip used
    // `surface.title`, so hovering a truncated chip showed a DIFFERENT string
    // — defeating the one job a tooltip has here.
    const header = 'Quarterly Revenue Breakdown By Region';
    seed('nous:chat:micro_app:tip001', 0, 'A totally different record title', header);
    seed('nous:sweep:decision_sweep:bbb002');

    const { container } = render(Companion);
    await settle();
    const chip = [...container.querySelectorAll('.switcher a.chip')].find((c) =>
      c.getAttribute('href')?.includes('tip001'),
    )!;
    const text = chip.textContent!.trim();
    const tip = chip.getAttribute('title')!;

    expect(text.endsWith('…')).toBe(true);
    // The tooltip reveals the FULL header title the chip cut down.
    expect(tip).toContain(header);
    expect(tip).not.toContain('A totally different record title');
    expect(header.startsWith(text.slice(0, -1))).toBe(true);
  });
});

describe('Companion shell — a malformed background app cannot crash the shell', () => {
  it('falls back when a header title throws, and still renders the focused app', async () => {
    // codex P2: `{call: "@index"}` throws by design outside a collection
    // scope, and chips resolve at scope: null. The switcher resolves EVERY
    // live surface, so one bad background app took down the whole shell —
    // including the ability to navigate away from it.
    seed('nous:chat:micro_app:good01', 0, undefined, 'Good App');
    store.apply(null, {
      version: 'v1.0',
      createSurface: {
        surfaceId: 'nous:chat:micro_app:bad001',
        catalogId: 'nous-core',
        components: [
          { id: 'root', component: 'Column', children: ['header', 'body'] },
          { id: 'body', component: 'Text', text: 'b' },
          { id: 'header', component: 'AppHeader', title: { call: '@index' } },
        ],
        dataModel: {},
        metadata: { extensions: { com_nous_nonce: 'n2', com_nous_title: 'Record Name' } },
      },
    } as never);

    location.hash = '#/s/' + encodeURIComponent('nous:chat:micro_app:good01');
    const { container } = render(Companion);
    await settle();

    // The shell rendered, the focused app is on screen, and the bad app fell
    // back to its record title rather than exploding.
    expect(container.querySelector('section.surface')).not.toBeNull();
    const chips = [...container.querySelectorAll('.switcher a.chip')].map((c) =>
      c.textContent?.trim(),
    );
    expect(chips).toContain('Good App');
    expect(chips).toContain('Record Name');
  });
});

describe('Companion shell — chip titles never execute anything', () => {
  it('does not fire openUrl from a header title while rendering chips', async () => {
    // codex P2: `title` is a DynamicString, so it may hold a function call —
    // and `openUrl` calls window.open. Chips resolve EVERY live surface,
    // twice (label + tooltip), so a call here fired unsolicited navigation
    // merely because a surface existed in the feed.
    const opened = vi.fn();
    vi.stubGlobal('open', opened);

    seed('nous:chat:micro_app:good01', 0, undefined, 'Good App');
    store.apply(null, {
      version: 'v1.0',
      createSurface: {
        surfaceId: 'nous:chat:micro_app:evil01',
        catalogId: 'nous-core',
        components: [
          { id: 'root', component: 'Column', children: ['header', 'body'] },
          { id: 'body', component: 'Text', text: 'b' },
          {
            id: 'header',
            component: 'AppHeader',
            title: { call: 'openUrl', args: { url: 'https://evil.example/beacon' } },
          },
        ],
        dataModel: {},
        metadata: { extensions: { com_nous_nonce: 'n2', com_nous_title: 'Record Name' } },
      },
    } as never);

    // FOCUS the good app, so the evil one is a BACKGROUND surface: its
    // components are never rendered, and the only thing that touches its
    // title is the chip. That is exactly the reported scenario.
    location.hash = '#/s/' + encodeURIComponent('nous:chat:micro_app:good01');
    const { container } = render(Companion);
    await settle();

    expect(opened).not.toHaveBeenCalled();
    const chips = [...container.querySelectorAll('.switcher a.chip')].map((c) =>
      c.textContent?.trim(),
    );
    // Falls back to the record title rather than evaluating the call.
    expect(chips).toContain('Record Name');
    vi.unstubAllGlobals();
  });
});

describe('Companion shell — pure title functions are allowed, effectful ones are not', () => {
  function seedTitle(id: string, title: unknown, record: string) {
    store.apply(null, {
      version: 'v1.0',
      createSurface: {
        surfaceId: id,
        catalogId: 'nous-core',
        components: [
          { id: 'root', component: 'Column', children: ['header', 'body'] },
          { id: 'body', component: 'Text', text: 'b' },
          { id: 'header', component: 'AppHeader', title },
        ],
        dataModel: { meta: { name: 'Ops' } },
        metadata: { extensions: { com_nous_nonce: 'n-' + id, com_nous_title: record } },
      },
    } as never);
  }

  it('names the chip from a pure formatter, matching the header', async () => {
    seedTitle(
      'nous:chat:micro_app:pure01',
      { call: 'pluralize', args: { value: 3, one: 'alert', other: 'alerts' } },
      'Record Name',
    );
    seed('nous:sweep:decision_sweep:bbb002');
    const { container } = render(Companion);
    await settle();
    const chips = [...container.querySelectorAll('.switcher a.chip')].map((c) =>
      c.textContent?.trim(),
    );
    // Resolved from the pure function, not the record-title fallback.
    expect(chips).toContain('alerts');
    expect(chips).not.toContain('Record Name');
  });

  it('does not evaluate a formatString title — its template can hide a call', async () => {
    // `"${openUrl(url:'…')}"` is a PRIMITIVE to the object walk, so recursion
    // cannot see it. formatString is the only entry to the ${…} scanner, so it
    // is excluded outright rather than re-parsed (codex P2).
    const opened = vi.fn();
    vi.stubGlobal('open', opened);
    seedTitle(
      'nous:chat:micro_app:tmpl01',
      { call: 'formatString', args: { value: "${openUrl(url:'https://evil.example/x')}" } },
      'Record Name',
    );
    seed('nous:chat:micro_app:good01', 0, undefined, 'Good App');
    location.hash = '#/s/' + encodeURIComponent('nous:chat:micro_app:good01');
    const { container } = render(Companion);
    await settle();

    expect(opened).not.toHaveBeenCalled();
    const chips = [...container.querySelectorAll('.switcher a.chip')].map((c) =>
      c.textContent?.trim(),
    );
    expect(chips).toContain('Record Name');
    vi.unstubAllGlobals();
  });

  it('refuses a pure wrapper smuggling an effectful argument', async () => {
    // The reason isPureTitle RECURSES: checking only the outer name would wave
    // this straight through and fire window.open from a background chip.
    const opened = vi.fn();
    vi.stubGlobal('open', opened);
    seedTitle(
      'nous:chat:micro_app:evil02',
      {
        call: 'formatString',
        args: { value: { call: 'openUrl', args: { url: 'https://evil.example/x' } } },
      },
      'Record Name',
    );
    seed('nous:chat:micro_app:good01', 0, undefined, 'Good App');
    location.hash = '#/s/' + encodeURIComponent('nous:chat:micro_app:good01');
    const { container } = render(Companion);
    await settle();

    expect(opened).not.toHaveBeenCalled();
    const chips = [...container.querySelectorAll('.switcher a.chip')].map((c) =>
      c.textContent?.trim(),
    );
    expect(chips).toContain('Record Name');
    vi.unstubAllGlobals();
  });
});

describe('Companion shell — destructive actions need a second tap', () => {
  it('close-all disarms by itself after the timeout', async () => {
    vi.useFakeTimers();
    seed('nous:chat:micro_app:aaa001');
    seed('nous:chat:micro_app:bbb002');
    const spy = vi
      .spyOn(transport, 'postAction')
      .mockResolvedValue({ ok: true, message: '', resolved: true });
    const { getByText } = render(Companion);

    await fireEvent.click(getByText('close all apps (2)'));
    expect(getByText('sure? close 2 apps')).toBeTruthy();
    // Walk past the 4s disarm window — the button reverts, nothing fired.
    vi.advanceTimersByTime(4500);
    await tick();
    expect(getByText('close all apps (2)')).toBeTruthy();
    expect(spy).not.toHaveBeenCalled();
    vi.useRealTimers();
  });
});

describe('Companion shell — close-all confirms the set the user SAW', () => {
  it('an app arriving during the confirmation window is not swept', async () => {
    // codex P1: the second tap used to read the LIVE list, so an app that
    // arrived after arming was irreversibly closed by a confirmation that
    // never showed it.
    seed('nous:chat:micro_app:aaa001');
    seed('nous:chat:micro_app:bbb002');
    const spy = vi
      .spyOn(transport, 'postAction')
      .mockResolvedValue({ ok: true, message: '', resolved: true });
    const { getByText } = render(Companion);

    await fireEvent.click(getByText('close all apps (2)'));
    // A third app lands while armed…
    seed('nous:chat:micro_app:ccc003');
    await settle();
    // …the confirmation still names the snapshot, and confirming closes ONLY it.
    await fireEvent.click(getByText('sure? close 2 apps'));
    await settle();

    expect(spy).toHaveBeenCalledTimes(2);
    expect(spy).toHaveBeenCalledWith('nous:chat:micro_app:aaa001', 'app.close', 'footer', {});
    expect(spy).toHaveBeenCalledWith('nous:chat:micro_app:bbb002', 'app.close', 'footer', {});
    expect(spy).not.toHaveBeenCalledWith('nous:chat:micro_app:ccc003', 'app.close', 'footer', {});
  });
});
