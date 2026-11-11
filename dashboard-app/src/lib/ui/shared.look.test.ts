import { describe, it, expect, beforeEach } from 'vitest';
import { render, cleanup } from '@testing-library/svelte';
import { createRawSnippet } from 'svelte';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { ROOT_SEL, block, parseBlocks, styleBlocks } from '../testing/css';
import StatGrid from './StatGrid.svelte';
import StaleBadge from './StaleBadge.svelte';
import { Badge } from './badge';
import { Button } from './button';

// The shared pieces are used by every screen. Each declaration the redesign converted from a literal reads its token
// (declared at :root) or, where the token was a one-value alias, the value itself (PR 7 T5).
const PAPER = block(parseBlocks(readFileSync(join(__dirname, '../../app.css'), 'utf-8')), ROOT_SEL, 50);

// [file, selector, property, the value now, what it held before the redesign]
const CONVERTED: [string, string, string, string, string][] = [
  ['StatGrid.svelte', '.stat-card', 'border-radius', 'var(--card-radius-sm)', '8px'],
  ['StatGrid.svelte', '.stat-card', 'border', 'var(--card-border)', '1px solid var(--border)'],
  // No box-shadow before: a transparent zero shadow paints the same, and unlike `none` it stays valid inside the
  // shadow list Tailwind composes (shadow-[var(--card-shadow)] with a focus ring on a Button).
  ['StatGrid.svelte', '.stat-card', 'box-shadow', 'var(--card-shadow)', '0 0 #0000'],
  ['StatGrid.svelte', '.stat-label', 'text-transform', 'none', 'uppercase'], // --label-transform, one value: inlined
  ['StatGrid.svelte', '.stat-label', 'letter-spacing', '0', '0.05em'], // --label-tracking, one value: inlined
  ['StatGrid.svelte', '.tone-warn', 'color', 'var(--warn-text)', 'the old StatGrid amber'], // --stat-warn was its alias
  ['StatGrid.svelte', '.tone-ok', 'color', 'var(--ok-text)', 'the old StatGrid green'], // --stat-ok was its alias
  ['StatGrid.svelte', '.tone-error', 'color', 'var(--err-text)', 'var(--red)'],
  ['DataTable.svelte', 'th', 'text-transform', 'none', 'uppercase'],
  ['DataTable.svelte', 'th', 'letter-spacing', '0', '0.05em'],
  ['DataTable.svelte', '.dt--cards tr', 'border-radius', 'var(--card-radius-sm)', '8px'],
  ['FilterBar.svelte', '.filter-btn.active', 'color', 'var(--on-accent)', 'var(--bg)'],
  ['StaleBadge.svelte', '.stale-badge.error', 'color', 'var(--err-text)', 'var(--red)'],
  ['StaleBadge.svelte', '.stale-badge.error', 'border-color', 'var(--err-text)', 'var(--red)'],
  ['StaleBadge.svelte', '.stale-badge.error', 'background', 'var(--err-bg)', 'a red tint'], // --stale-err-bg was its alias
];

describe('converted declarations read their tokens', () => {
  it.each(CONVERTED)('%s %s { %s } is %s (it held %s)', (file, selector, prop, value) => {
    const blocks = parseBlocks(styleBlocks(readFileSync(join(__dirname, file), 'utf-8')));
    expect(blocks.get(selector)?.[prop], `${file} ${selector}`).toBe(value);
    const token = /^var\((--[\w-]+)\)$/.exec(value)?.[1];
    if (token) expect(token in PAPER, `${token} is declared at :root`).toBe(true);
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

  // Review M11: a focused button gets the screen's 2px --focus-ring outline (the 50% halo alone is about 2.3:1 on white).
  it('focus: a 2px --focus-ring outline (its width was --btn-focus-width, one value: inlined in PR 7)', () => {
    for (const variant of ['default', 'outline'] as const) {
      const { container } = render(Button, { props: { variant, children: label } });
      const classes = container.querySelector('[data-slot="button"]')!.className.split(' ');
      for (const c of ['focus-visible:outline-solid', 'focus-visible:outline-2',
        'focus-visible:outline-[color:var(--focus-ring)]', 'focus-visible:outline-offset-2']) expect(classes, `${variant} ${c}`).toContain(c);
      cleanup();
    }
    expect(PAPER['--btn-focus-width']).toBeUndefined();
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

// From interfacesPinned.test.ts (deleted with the look switch, PR 7): the variants the screens use stay declared.
describe('the variants the screens use', () => {
  it('Badge has ok, warn, error and secondary; Button has outline', () => {
    const badge = readFileSync(join(__dirname, 'badge/badge.svelte'), 'utf-8');
    for (const v of ['ok', 'warn', 'error', 'secondary']) expect(badge, v).toMatch(new RegExp(`\\b${v}: "`));
    expect(readFileSync(join(__dirname, 'button/button.svelte'), 'utf-8')).toMatch(/\boutline: "/);
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
