import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { chartPalette, readToken, withAlpha } from './overviewCharts';
import { ROOT_SEL, block, parseBlocks } from './testing/css';

// Review I4: the Overview's two charts read their colours from the tokens (Chart.js draws on a canvas and cannot read
// var()).
const PAPER = block(parseBlocks(readFileSync(join(__dirname, '..', 'app.css'), 'utf-8')), ROOT_SEL, 50);

describe('withAlpha', () => {
  it('turns a hex or rgb colour into rgba with the alpha; leaves anything else alone', () => {
    expect(withAlpha('#274455', 0.5)).toBe('rgba(39, 68, 85, 0.5)');
    expect(withAlpha('#fff', 0.1)).toBe('rgba(255, 255, 255, 0.1)');
    expect(withAlpha('rgb(1, 2, 3)', 0.3)).toBe('rgba(1, 2, 3, 0.3)');
    expect(withAlpha('rgba(1, 2, 3, 0.9)', 0.3)).toBe('rgba(1, 2, 3, 0.3)');
    expect(withAlpha('', 0.5)).toBe('');
    expect(withAlpha('papayawhip', 0.5)).toBe('papayawhip');
  });
});

describe('the chart palette', () => {
  it('the palette: outcome marks, the series-7 slate for pending and anything else, the series-0 navy (no frame colour)', () => {
    const p = chartPalette((n) => PAPER[n] ?? '');
    expect(p.outcome('Something else')).toBe(p.outcome('pending'));
    // PR 4 review I2: success is --oc-success and failure --err-text, apart under red-green colour blindness (as Decisions).
    expect(['success', 'partial', 'failure', 'pending'].map((l) => p.outcome(l))).toEqual(['#3f8772', '#b7791f', '#b02a22', '#4f5f6f']);
    // Review N2: pending is "not known yet", a neutral, and far from partial's amber.
    expect(p.outcome('pending')).not.toBe(p.outcome('partial'));
    // Review M1 (PR 2): the bars were the accent, on paper the question frame's blue, beside the day timeline's frames.
    expect(p.barBorder).toBe('#274455');
    expect(p.bar).toBe('rgba(39, 68, 85, 0.5)');
    expect(p.barBorder).not.toBe(PAPER['--f-question']);
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
