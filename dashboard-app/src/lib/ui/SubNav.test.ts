import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/svelte';
import SubNav from './SubNav.svelte';

const items = [
  { id: 'sessions', label: 'Sessions', href: '#/work' },
  { id: 'patterns', label: 'Patterns', href: '#/work/patterns' },
  { id: 'changes', label: 'Changes', href: '#/work/changes' },
];

describe('SubNav', () => {
  it('renders a labelled nav with one link per item, in order, with its href', () => {
    const { container } = render(SubNav, { props: { items, current: 'sessions', label: 'Work views' } });
    const nav = container.querySelector('nav');
    expect(nav?.getAttribute('aria-label')).toBe('Work views');
    const links = Array.from(container.querySelectorAll('nav a'));
    expect(links.map((a) => a.textContent?.trim())).toEqual(['Sessions', 'Patterns', 'Changes']);
    expect(links.map((a) => a.getAttribute('href'))).toEqual(['#/work', '#/work/patterns', '#/work/changes']);
  });

  it('marks exactly the current item with aria-current="page"', () => {
    const { container } = render(SubNav, { props: { items, current: 'patterns', label: 'Work views' } });
    const current = container.querySelectorAll('a[aria-current="page"]');
    expect(current.length).toBe(1);
    expect(current[0].getAttribute('href')).toBe('#/work/patterns');
    expect(container.querySelectorAll('a[aria-current]').length).toBe(1);
  });

  it('marks nothing when the current id is not an item', () => {
    const { container } = render(SubNav, { props: { items, current: 'nope', label: 'Work views' } });
    expect(container.querySelectorAll('a[aria-current]').length).toBe(0);
  });

  it('renders an item label as text, never as markup', () => {
    const { container } = render(SubNav, {
      props: { items: [{ id: 'x', label: '<b>bold</b>', href: '#/work' }], current: 'x', label: 'Work views' },
    });
    expect(container.querySelector('nav b')).toBeNull();
    expect(container.querySelector('nav a')?.textContent?.trim()).toBe('<b>bold</b>');
  });
});
