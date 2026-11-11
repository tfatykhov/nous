/// <reference types="node" />
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, cleanup, fireEvent } from '@testing-library/svelte';
import { readFileSync } from 'node:fs';
import Companion from './Companion.svelte';
import SectionView from './catalog/SectionView.svelte';
import { store } from './store.svelte';
import { transport } from './transport';
import { contrast, hexLuminance } from './testing/color';
import { parseThemes } from './testing/themes';

// F093 §3 — theme wire (data-theme per surface) + §6.1 Section.layout, and
// the R2 contrast guard: themes are code, so they get a WCAG-AA test.

const SURFACE = 'theme-test-surface';

function seed(theme = '') {
  store.reset();
  store.connection = 'live';
  store.apply(null, {
    version: 'v1.0',
    createSurface: {
      surfaceId: SURFACE,
      catalogId: 'nous-core',
      components: [{ id: 'root', component: 'Text', text: 'hi' }],
      dataModel: {},
      metadata: { extensions: { com_nous_nonce: 'n', com_nous_theme: theme } },
    },
  } as never);
}

beforeEach(() => {
  vi.spyOn(transport, 'connect').mockResolvedValue(undefined);
  vi.spyOn(transport, 'stop').mockImplementation(() => {});
  location.hash = '';
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  store.reset();
});

describe('theme wire (F093 §3.2)', () => {
  it('stamps data-theme on the surface wrapper from the envelope', () => {
    seed('alpine-dusk');
    location.hash = '#/s/' + encodeURIComponent(SURFACE);
    const { container } = render(Companion);
    const section = container.querySelector('section.surface');
    expect(section?.getAttribute('data-theme')).toBe('alpine-dusk');
  });

  it('sets no data-theme for the default (byte-identical render)', () => {
    seed('');
    location.hash = '#/s/' + encodeURIComponent(SURFACE);
    const { container } = render(Companion);
    expect(container.querySelector('section.surface')?.getAttribute('data-theme')).toBeNull();
  });
});

describe('Section.layout (F093 §6.1)', () => {
  function renderSection(layout: string) {
    store.reset();
    store.apply(null, {
      version: 'v1.0',
      createSurface: {
        surfaceId: SURFACE,
        catalogId: 'nous-core',
        components: [{ id: 'b', component: 'Text', text: 'body' }],
        dataModel: {},
      },
    } as never);
    return render(SectionView, {
      props: { surfaceId: SURFACE, comp: { id: 's', component: 'Section', title: 'S', child: 'b', layout } },
    });
  }

  it('maps the layout enum to a class', () => {
    expect(renderSection('grid-2').container.querySelector('.app-section.grid-2')).not.toBeNull();
    cleanup();
    expect(renderSection('hero').container.querySelector('.app-section.hero')).not.toBeNull();
  });

  it('falls back to stack for an unknown layout', () => {
    const { container } = renderSection('spiral');
    expect(container.querySelector('.app-section.stack')).not.toBeNull();
  });
});

// --- F093 R2: WCAG-AA contrast on each theme's text-on-bg -----------------
// Colour maths and the theme cascade come from the shared test helpers (testing/color.ts, testing/themes.ts); the
// local copy weighted blue by 0.4152 where WCAG says 0.0722.

describe('theme contrast (F093 R2 — themes are code, so they get tests)', () => {
  // Read the source file directly (vitest cwd = dashboard-app). The tailwind
  // vite plugin rewrites `.css` imports, so a `?raw` import would not be the
  // literal file — fs is the only way to assert on the authored tokens.
  const sheet = parseThemes(readFileSync('src/companion/companion.css', 'utf-8'));

  const ALL = ['nous-default', 'nous-dark', 'alpine-dusk', 'harbor', 'paper', 'signal', 'report'];
  const themes: Record<string, Record<string, string>> = Object.fromEntries(ALL.map((id) => [id, sheet.resolved(id)]));

  it('parsed all seven themes (the default and the old default, nous-dark, among them)', () => {
    expect([...sheet.ids, 'nous-default'].sort()).toEqual([...ALL].sort());
  });

  it('report: --soft is lighter than --muted so a neutral pill never reads weaker than its caption', () => {
    // F096 §5 — caught on the design canvas: the reference's "flat" slate
    // (#94a3b8) is the neutral ink, and it sits ABOVE muted (#8d99ae).
    const t = themes['report'];
    expect(hexLuminance(t['--soft'])).toBeGreaterThan(hexLuminance(t['--muted']));
    expect(contrast(t['--soft'], t['--surface'])).toBeGreaterThanOrEqual(4.5);
  });

  for (const name of ALL) {
    it(`${name}: --text on --bg meets AA (≥4.5:1)`, () => {
      const t = themes[name];
      // hex-only pairs (the token values are all hex)
      expect(contrast(t['--text'], t['--bg'])).toBeGreaterThanOrEqual(4.5);
      // surface is a common text backdrop too
      expect(contrast(t['--text'], t['--surface'])).toBeGreaterThanOrEqual(4.5);
    });
  }
});

describe('Section.layout accordion', () => {
  function renderAccordion() {
    store.reset();
    store.apply(null, {
      version: 'v1.0',
      createSurface: {
        surfaceId: SURFACE,
        catalogId: 'nous-core',
        components: [
          { id: 'b', component: 'Text', text: 'hidden detail' },
        ],
        dataModel: {},
      },
    } as never);
    return render(SectionView, {
      props: {
        surfaceId: SURFACE,
        comp: { id: 's', component: 'Section', title: 'Raw data', child: 'b', layout: 'accordion' },
      },
    });
  }

  it('starts collapsed and does not render the panel subtree at all', () => {
    const { container } = renderAccordion();
    const btn = container.querySelector('button.toggle');
    expect(btn?.getAttribute('aria-expanded')).toBe('false');
    // Mirrors Tabs: a collapsed panel is not merely hidden — its subtree does
    // not run.
    expect(container.textContent).not.toContain('hidden detail');
  });

  it('expands on click and collapses again', async () => {
    const { container } = renderAccordion();
    const btn = container.querySelector('button.toggle') as HTMLButtonElement;
    // the toggle says what it will do in a word, not a caret glyph (review I6)
    expect(btn.querySelector('.caret')?.textContent).toBe('Show');
    await fireEvent.click(btn);
    expect(btn.getAttribute('aria-expanded')).toBe('true');
    expect(container.textContent).toContain('hidden detail');
    expect(btn.querySelector('.caret')?.textContent).toBe('Hide');
    await fireEvent.click(btn);
    expect(container.textContent).not.toContain('hidden detail');
  });

  it('non-accordion layouts keep a plain, non-interactive heading', () => {
    store.reset();
    store.apply(null, {
      version: 'v1.0',
      createSurface: {
        surfaceId: SURFACE,
        catalogId: 'nous-core',
        components: [{ id: 'b', component: 'Text', text: 'always visible' }],
        dataModel: {},
      },
    } as never);
    const { container } = render(SectionView, {
      props: {
        surfaceId: SURFACE,
        comp: { id: 's', component: 'Section', title: 'S', child: 'b', layout: 'stack' },
      },
    });
    expect(container.querySelector('button.toggle')).toBeNull();
    expect(container.textContent).toContain('always visible');
  });
});
