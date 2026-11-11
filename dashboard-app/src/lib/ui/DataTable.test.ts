import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/svelte';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import DataTable from './DataTable.svelte';
import { parseBlocks, styleBlocks } from '../testing/css';

const cols = [{ key: 'name', label: 'Name' }, { key: 'n', label: 'Count' }];
const rows = [{ name: 'a', n: 1 }, { name: 'b', n: 2 }];

describe('DataTable', () => {
  it('renders a row per item with all columns', () => {
    const { getByText } = render(DataTable, { props: { columns: cols, rows } });
    expect(getByText('a')).toBeTruthy();
    expect(getByText('2')).toBeTruthy();
  });
  it('applies the card-collapse class when mode=cards', () => {
    const { container } = render(DataTable, { props: { columns: cols, rows, mode: 'cards' } });
    expect(container.querySelector('.dt--cards')).toBeTruthy();
  });
});

describe('DataTable — row detail reachable by keyboard', () => {
  it('gives each expandable row a disclosure button that toggles aria-expanded', async () => {
    const { createRawSnippet } = await import('svelte');
    const { fireEvent } = await import('@testing-library/svelte');
    const detail = createRawSnippet((row: () => { name: string }) => ({
      render: () => `<div class="det">detail ${row().name}</div>`,
    }));
    const { container, getByRole } = render(DataTable, {
      props: { columns: cols, rows, detail, rowLabel: (r: { name: string }) => r.name },
    });
    const btn = getByRole('button', { name: 'Show details for a' });
    expect(btn.getAttribute('aria-expanded')).toBe('false');
    await fireEvent.click(btn);
    expect(btn.getAttribute('aria-expanded')).toBe('true');
    const panel = container.querySelector(`#${btn.getAttribute('aria-controls')}`);
    expect(panel?.textContent).toContain('detail a');
    await fireEvent.click(btn);
    expect(btn.getAttribute('aria-expanded')).toBe('false');
  });

  it('has no disclosure column when there is no detail', () => {
    const { container } = render(DataTable, { props: { columns: cols, rows } });
    expect(container.querySelector('button')).toBeNull();
  });
});

// Redesign PR 5 review I2: in cards mode (phone width) each cell is a flex row inside a card that clips, so a long
// unbroken value (a URL, a path) ran to the card's edge and stopped. A labelled cell may now break anywhere; the detail
// panel's cell has no data-label, so it inherits nothing.
describe('DataTable — cards mode at phone width (source contract)', () => {
  /** The body of the first `@media (max-width: 640px)` block of the component's style. */
  function phoneBlock(): string {
    const css = styleBlocks(readFileSync(join(__dirname, 'DataTable.svelte'), 'utf-8'));
    const start = css.indexOf('@media (max-width: 640px)');
    expect(start, 'the 640px block').toBeGreaterThanOrEqual(0);
    const open = css.indexOf('{', start);
    let depth = 0;
    for (let i = open; i < css.length; i++) {
      if (css[i] === '{') depth++;
      else if (css[i] === '}' && --depth === 0) return css.slice(open + 1, i);
    }
    throw new Error('unclosed @media block');
  }

  it('a labelled cell wraps a long unbroken value inside its card instead of running off the edge', () => {
    expect(parseBlocks(phoneBlock()).get('.dt--cards td[data-label]')).toMatchObject({ 'overflow-wrap': 'anywhere' });
    // Only the cell's own text: an element inside (a snippet's spans, as Autonomy's job fixes have) wraps at words as before,
    // or a squeezed flex item breaks a normal word ("Faile / d often" under dark). Global, since a snippet's elements carry
    // the calling view's scope class, which a scoped `> *` never matches.
    // With no specificity (:where), so it only replaces the inherited value: a view's own rule for its element still wins
    // (Identity's fact content, which must break anywhere).
    expect(parseBlocks(phoneBlock()).get(':where(.dt--cards td[data-label]) > :global(*)')).toMatchObject({ 'overflow-wrap': 'normal' });
  });
});
