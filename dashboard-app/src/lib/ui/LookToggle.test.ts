import { describe, it, expect, beforeEach } from 'vitest';
import { render, fireEvent, cleanup } from '@testing-library/svelte';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import LookToggle from './LookToggle.svelte';
import { setLook } from '../look';

describe('LookToggle', () => {
  beforeEach(() => {
    cleanup();
    setLook('dark');
    localStorage.clear();
  });

  it('is a switch named "New look", off by default', () => {
    const { getByRole } = render(LookToggle);
    const sw = getByRole('switch', { name: 'New look' });
    expect(sw.getAttribute('aria-checked')).toBe('false');
    expect(sw.textContent).toContain('Off');
  });

  it('turns the paper look on and off', async () => {
    const { getByRole } = render(LookToggle);
    const sw = getByRole('switch', { name: 'New look' });
    await fireEvent.click(sw);
    expect(sw.getAttribute('aria-checked')).toBe('true');
    expect(sw.textContent).toContain('On');
    expect(document.documentElement.dataset.look).toBe('paper');
    expect(localStorage.getItem('nous.look')).toBe('paper');
    await fireEvent.click(sw);
    expect(sw.getAttribute('aria-checked')).toBe('false');
    expect(document.documentElement.hasAttribute('data-look')).toBe(false);
  });

  it('is a real <button type="button">, so Enter and Space activate it natively', () => {
    const { getByRole } = render(LookToggle);
    const sw = getByRole('switch', { name: 'New look' });
    expect(sw.tagName).toBe('BUTTON');
    expect(sw.getAttribute('type')).toBe('button');
    expect(sw.hasAttribute('tabindex')).toBe(false);
  });

  it('follows a look switched elsewhere (another tab)', async () => {
    const { getByRole } = render(LookToggle);
    window.dispatchEvent(new StorageEvent('storage', { key: 'nous.look', newValue: 'paper' }));
    await Promise.resolve();
    expect(getByRole('switch', { name: 'New look' }).getAttribute('aria-checked')).toBe('true');
  });

  it('has a 44px touch target on a phone', () => {
    const src = readFileSync(join(__dirname, 'LookToggle.svelte'), 'utf-8');
    expect(src).toMatch(/@media \(max-width: 768px\)[^}]*\{[^}]*min-height: 44px/);
  });
});
