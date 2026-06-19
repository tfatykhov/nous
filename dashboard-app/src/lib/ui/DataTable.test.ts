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
