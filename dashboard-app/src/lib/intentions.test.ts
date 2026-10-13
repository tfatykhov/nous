import { describe, it, expect } from 'vitest';
import { fmtSeconds, countNodes } from './intentions';

describe('intentions helpers', () => {
  it('formats latency honestly', () => {
    expect(fmtSeconds(null)).toBe('—');
    expect(fmtSeconds(38)).toBe('38 s');
    expect(fmtSeconds(123)).toBe('2 min 3 s');
    expect(fmtSeconds(0)).toBe('0 s');
  });
  it('counts every descendant', () => {
    const leaf = { children: [] };
    expect(countNodes({ children: [{ children: [leaf, leaf] }, leaf] } as never)).toBe(4);
  });
});
