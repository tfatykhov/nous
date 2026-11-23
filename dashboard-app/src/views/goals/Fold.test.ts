import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/svelte';
import { createRawSnippet } from 'svelte';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import Fold from './Fold.svelte';
import { FOLD_KEY, type FoldId } from '../../lib/goals';
import { parseBlocks, styleBlocks } from '../../lib/testing/css';

// A folding section of a goal's page: one button (the title, a summary, and Show or Hide), and a panel that stays
// in the page while it is hidden. The viewer's choice is kept in the browser.

const inside = createRawSnippet(() => ({ render: () => '<p class="inside">The journal rows</p>' }));
type Props = { id: FoldId; refOf: string; title: string; summary?: string; defaultOpen: boolean; children: typeof inside };
const props = (over: Partial<Props> = {}): Props => ({
  id: 'journal', refOf: 'G-0001', title: 'Journal', defaultOpen: true, children: inside, ...over,
});
const button = () => screen.getByRole('button') as HTMLButtonElement;
const panel = () => document.getElementById('goal-b.G-0001.journal') as HTMLElement;

beforeEach(() => localStorage.clear());
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe('a folding section', () => {
  it('is open or closed by default as its prop says, and names its panel', () => {
    render(Fold, props());
    expect(button().getAttribute('aria-expanded')).toBe('true');
    expect(button().id).toBe('goal-f.G-0001.journal');
    expect(button().getAttribute('aria-controls')).toBe('goal-b.G-0001.journal');
    expect(panel().hidden).toBe(false);
    expect(button().textContent).toContain('Hide');
    cleanup();
    render(Fold, props({ defaultOpen: false }));
    expect(button().getAttribute('aria-expanded')).toBe('false');
    expect(panel().hidden).toBe(true);
    expect(button().textContent).toContain('Show');
  });

  it('a click flips it, says so in words, and keeps the choice', async () => {
    render(Fold, props());
    await fireEvent.click(button());
    expect(button().getAttribute('aria-expanded')).toBe('false');
    expect(panel().hidden).toBe(true);
    expect(button().textContent).toContain('Show');
    expect(JSON.parse(localStorage.getItem(FOLD_KEY)!)).toEqual({ journal: false });
    await fireEvent.click(button());
    expect(panel().hidden).toBe(false);
    expect(JSON.parse(localStorage.getItem(FOLD_KEY)!)).toEqual({ journal: true });
  });

  it('a stored choice wins over the default on a fresh render', () => {
    localStorage.setItem(FOLD_KEY, JSON.stringify({ journal: false, plan: true }));
    render(Fold, props());
    expect(button().getAttribute('aria-expanded')).toBe('false');
    cleanup();
    render(Fold, props({ id: 'plan', title: 'Plan', defaultOpen: false }));
    expect(button().getAttribute('aria-expanded')).toBe('true');
  });

  it('with the storage refusing, it still opens and closes', async () => {
    vi.stubGlobal('localStorage', { getItem: () => { throw new Error('blocked'); }, setItem: () => { throw new Error('blocked'); } });
    render(Fold, props());
    expect(button().getAttribute('aria-expanded')).toBe('true');
    await fireEvent.click(button());
    expect(button().getAttribute('aria-expanded')).toBe('false');
    expect(panel().hidden).toBe(true);
  });

  it('shows the summary beside the title, and the section is named by the title alone', () => {
    render(Fold, props({ id: 'tools', title: 'Tools this goal may use', summary: '2 allowed', defaultOpen: false }));
    expect(button().textContent).toContain('Tools this goal may use');
    expect(button().textContent).toContain('2 allowed');
    expect(screen.getByRole('region', { name: 'Tools this goal may use' })).toBeTruthy();
    expect(screen.getByRole('heading', { level: 3 }).contains(button())).toBe(true);
  });

  it('is a native button, so Enter and Space work as a click', () => {
    render(Fold, props());
    expect(button().tagName).toBe('BUTTON');
    expect(button().getAttribute('type')).toBe('button');
  });

  it('keeps its content in the page while it is closed', async () => {
    render(Fold, props());
    await fireEvent.click(button());
    expect(panel().querySelector('.inside')?.textContent).toBe('The journal rows');
  });

  it('the button shows a focus outline, and the panel has no display rule that would undo hidden', () => {
    const css = parseBlocks(styleBlocks(readFileSync(join(__dirname, 'Fold.svelte'), 'utf-8')));
    expect(css.get('.toggle:focus-visible')?.outline).toMatch(/^2px solid var\(--focus-ring\)$/);
    for (const [selector, rule] of css) {
      if (selector.includes('.panel')) expect(rule.display, selector).toBeUndefined();
    }
  });
});
