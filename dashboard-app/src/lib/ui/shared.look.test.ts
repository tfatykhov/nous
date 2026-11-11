import { describe, it, expect, beforeEach } from 'vitest';
import { render, cleanup } from '@testing-library/svelte';
import { createRawSnippet } from 'svelte';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { DARK_SEL, PAPER_SEL, parseBlocks, styleBlocks } from '../testing/css';
import StatGrid from './StatGrid.svelte';
import StaleBadge from './StaleBadge.svelte';
import { Badge } from './badge';
import { Button } from './button';

// The shared pieces are used by every screen, migrated or not. Each declaration converted from a literal to a token
// must resolve, in app.css's dark block, to the literal it replaced: that is the proof the dark screens do not move.
const APP_CSS = parseBlocks(readFileSync(join(__dirname, '../../app.css'), 'utf-8'));
const DARK = APP_CSS.get(DARK_SEL) ?? {};
const PAPER = APP_CSS.get(PAPER_SEL) ?? {};

// [file, selector, property, token, the literal the declaration held before]
const CONVERTED: [string, string, string, string, string][] = [
  ['StatGrid.svelte', '.stat-card', 'border-radius', '--card-radius-sm', '8px'],
  ['StatGrid.svelte', '.stat-card', 'border', '--card-border', '1px solid var(--border)'],
  // No box-shadow before: a transparent zero shadow paints the same, and unlike `none` it stays valid inside the
  // shadow list Tailwind composes (shadow-[var(--card-shadow)] with a focus ring on a Button).
  ['StatGrid.svelte', '.stat-card', 'box-shadow', '--card-shadow', '0 0 #0000'],
  ['StatGrid.svelte', '.stat-label', 'text-transform', '--label-transform', 'uppercase'],
  ['StatGrid.svelte', '.stat-label', 'letter-spacing', '--label-tracking', '0.05em'],
  ['StatGrid.svelte', '.tone-warn', 'color', '--stat-warn', '#f59e0b'],
  ['StatGrid.svelte', '.tone-ok', 'color', '--stat-ok', '#10b981'],
  ['StatGrid.svelte', '.tone-error', 'color', '--err-text', '#f87171'], // was var(--red)
  ['DataTable.svelte', 'th', 'text-transform', '--label-transform', 'uppercase'],
  ['DataTable.svelte', 'th', 'letter-spacing', '--label-tracking', '0.05em'],
  ['DataTable.svelte', '.dt--cards tr', 'border-radius', '--card-radius-sm', '8px'],
  ['FilterBar.svelte', '.filter-btn.active', 'color', '--on-accent', 'var(--bg)'],
  ['StaleBadge.svelte', '.stale-badge.error', 'color', '--err-text', '#f87171'], // was var(--red)
  ['StaleBadge.svelte', '.stale-badge.error', 'border-color', '--err-text', '#f87171'], // was var(--red)
  ['StaleBadge.svelte', '.stale-badge.error', 'background', '--stale-err-bg', 'rgba(248, 113, 113, 0.08)'],
];

describe('converted declarations do not move in dark', () => {
  it('--red, the old value of two of them, is the literal used below', () => {
    expect(DARK['--red']).toBe('#f87171');
  });

  it.each(CONVERTED)('%s %s { %s } reads %s, whose dark value is %s', (file, selector, prop, token, literal) => {
    const blocks = parseBlocks(styleBlocks(readFileSync(join(__dirname, file), 'utf-8')));
    expect(blocks.get(selector)?.[prop], `${file} ${selector}`).toBe(`var(${token})`);
    expect(DARK[token], token).toBe(literal);
    expect(token in PAPER, `${token} has a paper value`).toBe(true);
  });

  it('the filter pill keeps its 999px radius', () => {
    const blocks = parseBlocks(styleBlocks(readFileSync(join(__dirname, 'FilterBar.svelte'), 'utf-8')));
    expect(blocks.get('.filter-btn')?.['border-radius']).toBe('999px');
  });
});

describe('StatGrid', () => {
  beforeEach(() => cleanup());

  it('keeps its class names and label text for all five tones', () => {
    const tones = ['waiting', 'unknown', 'error', 'warn', 'ok'] as const;
    const { container } = render(StatGrid, {
      props: { stats: tones.map((tone, i) => ({ label: `Label ${tone}`, value: i, note: `note ${i}`, tone })) },
    });
    const cards = Array.from(container.querySelectorAll('.stat-card'));
    expect(cards).toHaveLength(5);
    cards.forEach((card, i) => {
      expect(card.querySelector('.stat-value')?.textContent).toBe(String(i));
      expect(card.querySelector('.stat-value')?.classList.contains(`tone-${tones[i]}`)).toBe(true);
      expect(card.querySelector('.stat-label')?.textContent).toBe(`Label ${tones[i]}`);
      expect(card.querySelector('.stat-note')?.textContent).toBe(`note ${i}`);
    });
  });
});

describe('Badge', () => {
  beforeEach(() => cleanup());
  const text = (t: string) => createRawSnippet(() => ({ render: () => `<span>${t}</span>` }));

  it.each([
    ['ok', 'ok-bg', 'ok-text'],
    ['warn', 'warn-bg', 'warn-text'],
    ['error', 'err-bg', 'err-text'],
  ] as const)('variant %s renders its text on the status tint', (variant, bg, fg) => {
    const { container } = render(Badge, { props: { variant, children: text('Waiting') } });
    const el = container.querySelector('[data-slot="badge"]')!;
    expect(el.textContent).toBe('Waiting');
    expect(el.className).toContain(bg);
    expect(el.className).toContain(fg);
    expect(el.className).toContain('rounded-full');
    expect(el.className.split(' ')).toContain('text-xs');
  });
});

describe('Button', () => {
  beforeEach(() => cleanup());
  const label = createRawSnippet(() => ({ render: () => '<span>Refresh</span>' }));

  it('is a <button type="button"> by default, a rail-blue pill', () => {
    const { container } = render(Button, { props: { children: label } });
    const el = container.querySelector('[data-slot="button"]')!;
    expect(el.tagName).toBe('BUTTON');
    expect(el.getAttribute('type')).toBe('button');
    expect(el.textContent).toBe('Refresh');
    expect(el.className).toContain('rounded-full');
    expect(el.className).toContain('bg-primary');
    expect(el.className.split(' ')).toContain('text-sm'); // text-[var(--on-accent)] merged as a colour, not a size
  });

  it('outline: the variant border colour replaces the transparent base border, and the size stays', () => {
    const { container } = render(Button, { props: { variant: 'outline', children: label } });
    const classes = container.querySelector('[data-slot="button"]')!.className.split(' ');
    expect(classes).toContain('border-[var(--border)]');
    expect(classes).not.toContain('border-transparent');
    expect(classes).toContain('text-sm');
  });

  // Review M11: on paper a focused button gets the screen's 2px --focus-ring outline (the 50% halo alone is about
  // 2.3:1 on white); under dark the outline is 0px wide, so the dark focus look does not move.
  it('focus: a --focus-ring outline whose width is the ground\'s --btn-focus-width (0 dark, 2px paper)', () => {
    for (const variant of ['default', 'outline'] as const) {
      const { container } = render(Button, { props: { variant, children: label } });
      const classes = container.querySelector('[data-slot="button"]')!.className.split(' ');
      for (const c of ['focus-visible:outline-solid', 'focus-visible:outline-[length:var(--btn-focus-width)]',
        'focus-visible:outline-[color:var(--focus-ring)]', 'focus-visible:outline-offset-2']) expect(classes, `${variant} ${c}`).toContain(c);
      cleanup();
    }
    expect(DARK['--btn-focus-width']).toBe('0px');
    expect(PAPER['--btn-focus-width']).toBe('2px');
  });

  it('is an <a> with href; a disabled link has aria-disabled and no href', () => {
    const { container } = render(Button, { props: { href: '#/work', variant: 'outline', children: label } });
    const a = container.querySelector('a[data-slot="button"]')!;
    expect(a.getAttribute('href')).toBe('#/work');
    cleanup();
    const { container: c2 } = render(Button, { props: { href: '#/work', disabled: true, children: label } });
    const off = c2.querySelector('a[data-slot="button"]')!;
    expect(off.getAttribute('aria-disabled')).toBe('true');
    expect(off.hasAttribute('href')).toBe(false);
  });

  it('the default and outline variants never key off the OS colour scheme (dark:), only the ground', () => {
    const src = readFileSync(join(__dirname, 'button/button.svelte'), 'utf-8');
    const variant = (name: string) => src.match(new RegExp(`\\b${name}: "([^"]*)"`))?.[1] ?? '';
    for (const v of ['default', 'outline']) {
      expect(variant(v), v).not.toBe('');
      expect(variant(v), v).not.toMatch(/\bdark:/);
    }
  });
});

describe('shadcn pieces read tokens, no colour literals', () => {
  it.each(['card/card.svelte', 'button/button.svelte', 'badge/badge.svelte'])('%s', (file) => {
    const src = readFileSync(join(__dirname, file), 'utf-8');
    expect(src).not.toMatch(/#[0-9a-fA-F]{3,8}\b|rgba?\(/);
  });

  it('Card is a rounded card with the ground\'s shadow and border, no ring', () => {
    const src = readFileSync(join(__dirname, 'card/card.svelte'), 'utf-8');
    expect(src).toContain('rounded-[var(--card-radius)]');
    expect(src).toContain('shadow-[var(--card-shadow)]');
    expect(src).toContain('[border:var(--card-border)]');
    expect(src).not.toMatch(/\bring-1\b/);
  });
});

describe('StaleBadge', () => {
  beforeEach(() => cleanup());

  it('keeps its three label forms', () => {
    const now = Date.now();
    const ok = render(StaleBadge, { props: { state: { data: {}, error: null, loading: false, lastUpdated: now - 3000 } } });
    expect(ok.container.querySelector('.stale-badge')?.textContent?.trim()).toBe('updated 3s ago');
    cleanup();
    const err = render(StaleBadge, { props: { state: { data: {}, error: new Error('x'), loading: false, lastUpdated: now } } });
    const errEl = err.container.querySelector('.stale-badge');
    expect(errEl?.textContent?.trim()).toBe('error — retrying');
    expect(errEl?.classList.contains('error')).toBe(true);
    cleanup();
    const loading = render(StaleBadge, { props: { state: { data: null, error: null, loading: true, lastUpdated: null } } });
    expect(loading.container.querySelector('.stale-badge')?.textContent?.trim()).toBe('loading…');
  });
});
