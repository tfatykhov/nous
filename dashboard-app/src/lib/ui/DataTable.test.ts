import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/svelte';
import DataTable from './DataTable.svelte';

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
