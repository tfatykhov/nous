import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/svelte';
import HoldUndo from './HoldUndo.svelte';

const ID = '11111111-2222-3333-4444-555555555555';

describe('HoldUndo', () => {
  afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

  it('shows both guarded statements for the given id', () => {
    const { container } = render(HoldUndo, { id: ID });
    const sql = Array.from(container.querySelectorAll('code.sql')).map((c) => c.textContent ?? '');
    expect(sql).toHaveLength(2);
    for (const s of sql) {
      expect(s).toContain(`WHERE id = '${ID}' AND status = 'unknown'`);
    }
    expect(sql[0]).toContain("status = 'success'");
    expect(sql[1]).toContain("status = 'error'");
    expect(container.textContent).toContain('Releasing re-sends nothing by itself');
  });

  it('each button copies exactly the SQL it sits under', async () => {
    const writeText = vi.fn(async () => {});
    vi.stubGlobal('navigator', { clipboard: { writeText } });
    const { container } = render(HoldUndo, { id: ID });
    const sql = Array.from(container.querySelectorAll('code.sql')).map((c) => c.textContent ?? '');
    await fireEvent.click(screen.getByRole('button', { name: 'Copy SQL: they got it' }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(sql[0]));
    await fireEvent.click(screen.getByRole('button', { name: 'Copy SQL: nobody got it' }));
    await waitFor(() => expect(writeText).toHaveBeenLastCalledWith(sql[1]));
    expect(container.textContent).toContain('Copied to the clipboard.');
  });

  it('falls back to selecting the text when the clipboard is unavailable', async () => {
    vi.stubGlobal('navigator', { clipboard: { writeText: vi.fn(async () => { throw new Error('insecure'); }) } });
    const { container } = render(HoldUndo, { id: ID });
    await fireEvent.click(screen.getByRole('button', { name: 'Copy SQL: they got it' }));
    await waitFor(() => expect(container.textContent).toContain('Selected — press Ctrl+C'));
  });
});

describe('HoldUndo on paper (redesign PR 3, review M4)', () => {
  it('source contract: the copy buttons take the control radius (pills on paper) and the SQL blocks the small card radius', async () => {
    const { readFileSync } = await import('node:fs');
    const { join } = await import('node:path');
    const { styleBlocks } = await import('../testing/css');
    const css = styleBlocks(readFileSync(join(__dirname, 'HoldUndo.svelte'), 'utf-8'));
    expect(css).toMatch(/\.btn \{[^}]*border-radius: var\(--ctl-radius\);/);
    expect(css).toMatch(/\.sql \{[^}]*border-radius: var\(--card-radius-sm\);/);
  });
});
