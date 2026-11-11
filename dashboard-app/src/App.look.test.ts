import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, cleanup, fireEvent } from '@testing-library/svelte';
import { tick } from 'svelte';
import { existsSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { ROUTES } from './lib/router';

// The shell only: every screen is a stub (the real ones need Chart.js, cytoscape and their data).
vi.mock('./views/Overview.svelte', () => ({ default: () => {} }));
vi.mock('./views/GraphView.svelte', () => ({ default: () => {} }));
vi.mock('./views/Browser.svelte', () => ({ default: () => {} }));
vi.mock('./views/Decisions.svelte', () => ({ default: () => {} }));
vi.mock('./views/Heartbeat.svelte', () => ({ default: () => {} }));
vi.mock('./views/Observability.svelte', () => ({ default: () => {} }));
vi.mock('./views/Retrieval.svelte', () => ({ default: () => {} }));
vi.mock('./views/Health.svelte', () => ({ default: () => {} }));
vi.mock('./views/Rubric.svelte', () => ({ default: () => {} }));
vi.mock('./views/Work.svelte', () => ({ default: () => {} }));
vi.mock('./views/ModelCalls.svelte', () => ({ default: () => {} }));
vi.mock('./views/Consolidation.svelte', () => ({ default: () => {} }));
vi.mock('./views/DagView.svelte', () => ({ default: () => {} }));
vi.mock('./views/Subtasks.svelte', () => ({ default: () => {} }));
vi.mock('./views/Identity.svelte', () => ({ default: () => {} }));
vi.mock('./views/Harness.svelte', () => ({ default: () => {} }));
vi.mock('./views/Intentions.svelte', () => ({ default: () => {} }));
vi.mock('./views/Autonomy.svelte', () => ({ default: () => {} }));

const { default: App } = await import('./App.svelte');

function attrs(el: Element): Record<string, string> {
  return Object.fromEntries(Array.from(el.attributes).map((a) => [a.name, a.value]));
}

async function renderAt(hash: string) {
  location.hash = hash;
  const r = render(App);
  await tick();
  return r;
}

describe('App shell grounds', () => {
  beforeEach(() => {
    cleanup();
    document.documentElement.removeAttribute('data-look');
    vi.stubGlobal('fetch', vi.fn(() => new Promise(() => {}))); // the attention poll never answers
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('main is the page ground (class page-ground) on every route, and nothing carries data-ground', async () => {
    for (const route of ROUTES) {
      cleanup();
      const { container } = await renderAt(`#/${route}`);
      const main = container.querySelector('main#main-content');
      expect(main?.classList.contains('page-ground'), route).toBe(true);
      expect(container.querySelector('[data-ground]'), route).toBeNull();
    }
  });

  it('the sidebar, the mobile header and the drawer are the rail (class rail); the skip link is in neither region', async () => {
    const { container } = await renderAt('#/work');
    expect(container.querySelector('aside.sidebar')?.classList.contains('rail')).toBe(true);
    expect(container.querySelector('header.mobile-header')?.classList.contains('rail')).toBe(true);
    expect(container.querySelector('#mobile-drawer')?.classList.contains('rail')).toBe(true);
    expect(Array.from(container.querySelectorAll('.rail')).map((e) => e.id || e.tagName.toLowerCase())).toEqual(['aside', 'header', 'mobile-drawer']);
    expect(container.querySelector('a.skip-link')?.closest('.rail, .page-ground')).toBeNull();
  });

  it('the look switch sits at the foot of the sidebar and of the drawer', async () => {
    const { container } = await renderAt('#/overview');
    const switches = Array.from(container.querySelectorAll('[role="switch"]'));
    expect(switches.map((s) => s.textContent?.replace(/\s+/g, ' ').trim())).toEqual(['New look Off', 'New look Off']);
    expect(switches[0].closest('aside.sidebar')).not.toBeNull();
    expect(switches[1].closest('#mobile-drawer')).not.toBeNull();
  });

  it('with no data-look, rendering App changes nothing on <html>', async () => {
    const before = attrs(document.documentElement);
    await renderAt('#/work');
    await renderAt('#/observability');
    expect(attrs(document.documentElement)).toEqual(before);
    expect(document.documentElement.hasAttribute('data-look')).toBe(false);
  });

  it('the drawer still opens, traps Tab and closes on Escape', async () => {
    const { container } = await renderAt('#/work');
    const hamburger = container.querySelector<HTMLButtonElement>('button.hamburger')!;
    const drawer = container.querySelector<HTMLElement>('#mobile-drawer')!;
    await fireEvent.click(hamburger);
    expect(drawer.classList.contains('drawer-open')).toBe(true);
    expect(drawer.getAttribute('aria-hidden')).toBe('false');
    const focusable = Array.from(
      drawer.querySelectorAll<HTMLElement>('a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])'),
    );
    // The close button first, the look switch last (after the 18 links).
    expect(focusable[0].classList.contains('drawer-close')).toBe(true);
    expect(focusable.at(-1)?.getAttribute('role')).toBe('switch');
    focusable.at(-1)!.focus();
    await fireEvent.keyDown(drawer, { key: 'Tab' });
    expect(document.activeElement).toBe(focusable[0]);
    await fireEvent.keyDown(drawer, { key: 'Tab', shiftKey: true });
    expect(document.activeElement).toBe(focusable.at(-1));
    await fireEvent.keyDown(drawer, { key: 'Escape' });
    expect(drawer.classList.contains('drawer-open')).toBe(false);
    expect(document.activeElement).toBe(hamburger);
  });
});

describe('App styles', () => {
  const src = readFileSync(join(__dirname, 'App.svelte'), 'utf-8');

  it('the wordmark size and the logo glyph read tokens (21px and hidden in the rail)', () => {
    expect(src).toMatch(/\.sidebar-logo\s*\{[^}]*display:\s*var\(--logo-display\)/);
    expect(src).toMatch(/\.sidebar-title\s*\{[^}]*font-size:\s*var\(--brand-size\)/);
  });

  it('source contract (jsdom cannot lay out; the 390 px overflow check in the screenshot round is the proof): main has min-width: 0, so as a flex item it may shrink below its content', () => {
    expect(src).toMatch(/\.main-content \{[^}]*min-width: 0;/);
  });

  it('Svelte writes no data-look (only look.ts does)', () => {
    expect(src).not.toMatch(/data-look|dataset\.look/);
  });

  it('the dark island component is gone (every route is paper; it had no importer)', () => {
    expect(existsSync(join(__dirname, 'lib/ui/Ground.svelte'))).toBe(false);
    expect(src).not.toMatch(/data-ground|groundFor/);
  });
});
