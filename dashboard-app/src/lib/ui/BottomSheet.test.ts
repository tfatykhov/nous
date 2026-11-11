import { describe, it, expect, afterEach } from 'vitest';
import { render, cleanup, screen } from '@testing-library/svelte';
import { tick } from 'svelte';
import BottomSheet from './BottomSheet.svelte';

// The sheet is portalled to <body>, outside <main>: without the page ground of its own it would read the :root (dark)
// tokens under the paper look. Every route is paper, so it always carries the page ground.
afterEach(() => {
  cleanup();
});

describe('BottomSheet', () => {
  it('is the page ground (class page-ground), though portalled outside <main>', async () => {
    render(BottomSheet, { props: { open: true, title: 'Node detail' } });
    await tick();
    const sheet = await screen.findByLabelText('Node detail');
    expect(sheet.classList.contains('page-ground')).toBe(true);
    expect(sheet.classList.contains('nous-sheet')).toBe(true);
    expect(sheet.hasAttribute('data-ground')).toBe(false);
    expect(sheet.closest('main')).toBeNull(); // portalled: the route's <main> cannot hand it the tokens
  });
});
