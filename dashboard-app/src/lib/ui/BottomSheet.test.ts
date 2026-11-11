import { describe, it, expect, afterEach } from 'vitest';
import { render, cleanup, screen } from '@testing-library/svelte';
import { tick } from 'svelte';
import BottomSheet from './BottomSheet.svelte';
import { currentRoute } from '../router';
import { groundFor } from '../look';

// The sheet is portalled to <body>, outside <main>: without a ground of its own it would read the :root (dark) tokens
// on a paper page. It takes its route's ground, so an unconverted route keeps a dark sheet.
afterEach(() => {
  cleanup();
  currentRoute.set('overview');
});

describe('BottomSheet', () => {
  for (const route of ['overview', 'observability'] as const) {
    it(`carries the ground of its route (${route}: ${groundFor(route)})`, async () => {
      currentRoute.set(route);
      render(BottomSheet, { props: { open: true, title: 'Node detail' } });
      await tick();
      const sheet = await screen.findByLabelText('Node detail');
      expect(sheet.getAttribute('data-ground')).toBe(groundFor(route));
      expect(sheet.closest('main')).toBeNull(); // portalled: the route's <main> cannot hand it the tokens
    });
  }

  it('follows a route change while open', async () => {
    currentRoute.set('overview');
    render(BottomSheet, { props: { open: true, title: 'Node detail' } });
    const sheet = await screen.findByLabelText('Node detail');
    expect(sheet.getAttribute('data-ground')).toBe('paper');
    currentRoute.set('observability');
    await tick();
    expect(sheet.getAttribute('data-ground')).toBe('dark');
  });
});
