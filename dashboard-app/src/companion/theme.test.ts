/// <reference types="node" />
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, cleanup, fireEvent } from '@testing-library/svelte';
import { readFileSync } from 'node:fs';
import Companion from './Companion.svelte';
import SectionView from './catalog/SectionView.svelte';
import { store } from './store.svelte';
import { transport } from './transport';

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

function parseVars(block: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const m of block.matchAll(/(--[\w-]+):\s*([^;]+);/g)) out[m[1]] = m[2].trim();
  return out;
}

function luminance(hex: string): number {
  const h = hex.replace('#', '');
  const rgb = [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16) / 255);
  const lin = rgb.map((c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
  return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.4152 * lin[2];
}

function contrast(a: string, b: string): number {
  const [l1, l2] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (l1 + 0.05) / (l2 + 0.05);
}

describe('theme contrast (F093 R2 — themes are code, so they get tests)', () => {
  // Read the source file directly (vitest cwd = dashboard-app). The tailwind
  // vite plugin rewrites `.css` imports, so a `?raw` import would not be the
  // literal file — fs is the only way to assert on the authored tokens.
  const css = readFileSync('src/companion/companion.css', 'utf-8');

  const themes: Record<string, Record<string, string>> = { 'nous-default': {} };
  const rootMatch = css.match(/:root\s*\{([^}]+)\}/);
  if (rootMatch) themes['nous-default'] = parseVars(rootMatch[1]);
  for (const m of css.matchAll(/\[data-theme='([\w-]+)'\]\s*\{([^}]+)\}/g)) {
    themes[m[1]] = { ...themes['nous-default'], ...parseVars(m[2]) };
  }

  it('parsed all five themes', () => {
    expect(Object.keys(themes).sort()).toEqual(
      ['alpine-dusk', 'harbor', 'nous-default', 'paper', 'signal'].sort(),
    );
  });

  for (const name of ['nous-default', 'alpine-dusk', 'harbor', 'paper', 'signal']) {
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
    await fireEvent.click(btn);
    expect(btn.getAttribute('aria-expanded')).toBe('true');
    expect(container.textContent).toContain('hidden detail');
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
