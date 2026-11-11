import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { styleBlocks } from './lib/testing/css';
import { sourceFiles } from './lib/testing/sources';

// Final step A8 (the documenter's "not canonized" list): motion and corners come from the system's scale. A state
// change takes --transition (0.2s on the decelerating curve), and a corner is a token on the radius scale, a full pill,
// a circle, or a small bar end of 4px or less. A file that must differ is named below with its reason.

const read = (f: string) => readFileSync(join(__dirname, f), 'utf-8');
const STYLED = [...sourceFiles().filter((f) => f.endsWith('.svelte')), 'app.css'];
const css = (f: string) => (f.endsWith('.css') ? read(f).replace(/\/\*[\s\S]*?\*\//g, '') : styleBlocks(read(f)));

/** A transition value on the scale: none, or each part names a property and takes var(--transition). */
const ON_SCALE_TRANSITION = /^(?:none|[\w-]+ var\(--transition\)(?:, [\w-]+ var\(--transition\))*)$/;
/** A radius on the scale: a token, a full pill, a circle, or a bar end of 4px or less. */
const ON_SCALE_RADIUS = /^(?:var\(--[\w-]+\)|999px|9999px|50%|0|[0-4]px|[0-4]px [0-4]px)$/;

/** Kept off the scale on purpose. */
const RADIUS_ALLOWED: Record<string, string> = {
  'views/work/Trace.svelte:10px': "the critic's verdict pill: half its one-line height, so a wrapped verdict stays a rounded box, not a stadium",
};

describe('motion and corners on the scale', () => {
  it('the checks spare the scale and catch a hand-written value', () => {
    for (const ok of ['none', 'all var(--transition)', 'transform var(--transition)', 'background var(--transition), color var(--transition)']) {
      expect(ok, ok).toMatch(ON_SCALE_TRANSITION);
    }
    for (const bad of ['all 0.15s ease', 'transform 0.25s ease', 'background 0.1s', 'filter var(--transition, 0.2s ease)']) {
      expect(bad, bad).not.toMatch(ON_SCALE_TRANSITION);
    }
    for (const ok of ['var(--radius-sm)', '999px', '50%', '4px', '2px']) expect(ok, ok).toMatch(ON_SCALE_RADIUS);
    for (const bad of ['6px', '12px', '0.75rem', '10px']) expect(bad, bad).not.toMatch(ON_SCALE_RADIUS);
  });

  it.each(STYLED)('%s: every transition takes --transition', (f) => {
    const values = [...css(f).matchAll(/(?<![\w-])transition:\s*([^;]+);/g)].map((m) => m[1].replace(/\s*!important$/, '').trim());
    expect(values.filter((v) => !ON_SCALE_TRANSITION.test(v))).toEqual([]);
  });

  it.each(STYLED.filter((f) => f !== 'app.css'))('%s: every corner is on the radius scale', (f) => {
    const values = [...css(f).matchAll(/border-radius:\s*([^;]+);/g)].map((m) => m[1].trim());
    expect(values.filter((v) => !ON_SCALE_RADIUS.test(v) && !(`${f}:${v}` in RADIUS_ALLOWED))).toEqual([]);
  });

  it.each(Object.keys(RADIUS_ALLOWED))('%s is still there (no stale allowance)', (key) => {
    const [f, v] = key.split(':');
    expect(css(f)).toContain(`border-radius: ${v};`);
  });
});
