import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { chartPalette, readToken, withAlpha } from './overviewCharts';
import { DARK_SEL, PAPER_SEL, parseBlocks } from './testing/css';

// Review I4: the Overview's two charts read their colours from the ground's tokens (Chart.js draws on a canvas and
// cannot read var()), so the paper look gets the paper palette and the dark look keeps today's colours exactly.
const BLOCKS = parseBlocks(readFileSync(join(__dirname, '..', 'app.css'), 'utf-8'));
const DARK = BLOCKS.get(DARK_SEL) ?? {};
const PAPER = { ...DARK, ...(BLOCKS.get(PAPER_SEL) ?? {}) }; // a paper ground inherits what it does not redefine

describe('withAlpha', () => {
  it('turns a hex or rgb colour into rgba with the alpha; leaves anything else alone', () => {
    expect(withAlpha('#7c6af7', 0.5)).toBe('rgba(124, 106, 247, 0.5)');
    expect(withAlpha('#fff', 0.1)).toBe('rgba(255, 255, 255, 0.1)');
    expect(withAlpha('rgb(1, 2, 3)', 0.3)).toBe('rgba(1, 2, 3, 0.3)');
    expect(withAlpha('rgba(1, 2, 3, 0.9)', 0.3)).toBe('rgba(1, 2, 3, 0.3)');
    expect(withAlpha('', 0.5)).toBe('');
    expect(withAlpha('papayawhip', 0.5)).toBe('papayawhip');
  });
});

describe('the chart palette', () => {
  it('under the dark look it is exactly the colours the charts had (dark parity)', () => {
    const p = chartPalette((n) => DARK[n] ?? '');
    expect(['success', 'partial', 'failure', 'pending', 'Something else'].map((l) => p.outcome(l)))
      .toEqual(['#34d399', '#fbbf24', '#f87171', '#6b6b8a', '#6b6b8a']);
    expect(p.bar).toBe('rgba(124, 106, 247, 0.5)');
    expect(p.barBorder).toBe('#7c6af7');
  });

  it('under the paper look it is the paper palette: status fills, the series-7 slate, the series-0 violet (no frame colour)', () => {
    const p = chartPalette((n) => PAPER[n] ?? '');
    expect(['success', 'partial', 'failure', 'pending'].map((l) => p.outcome(l))).toEqual(['#2f8a4e', '#b7791f', '#d2352b', '#4f5f6f']);
    // Review N2: pending is "not known yet", a neutral as under dark, and far from partial's amber.
    expect(p.outcome('pending')).not.toBe(p.outcome('partial'));
    // Review M1 (PR 2): the bars were the accent, on paper the question frame's blue, beside the day timeline's frames.
    expect(p.barBorder).toBe('#8b5cf6');
    expect(p.bar).toBe('rgba(139, 92, 246, 0.5)');
    expect(p.barBorder).not.toBe(PAPER['--f-question']);
    for (const c of [p.outcome('success'), p.outcome('pending'), p.bar, p.barBorder]) expect(c).not.toMatch(/7c6af7|124, 106, 247/);
  });

  it('readToken reads a custom property off the element, trimmed', () => {
    const el = document.createElement('div');
    el.style.setProperty('--green', '  #2f8a4e ');
    document.body.appendChild(el);
    expect(readToken(el, '--green')).toBe('#2f8a4e');
    expect(readToken(el, '--nope')).toBe('');
    el.remove();
  });
});
