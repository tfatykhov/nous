import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { markupOf, parseBlocks, styleBlocks } from './lib/testing/css';

// Finish review "ceiling" and the craft floor's browser surfaces: the parts the browser draws carry the design too. A
// checkbox or radio takes the action colour, a select its own drawn arrow (from tokens), every disclosure marker the
// muted ink, Identity's text areas the world's radius; and the Graph sheet's backdrop is a scrim, not a blur.

const read = (f: string) => readFileSync(join(__dirname, f), 'utf-8');
const APP = parseBlocks(read('app.css'));

describe('browser surfaces', () => {
  it('checkboxes and radios take the action colour', () => {
    expect(APP.get(':root input[type="checkbox"], :root input[type="radio"]')).toEqual({ 'accent-color': 'var(--action)' });
  });

  it('a select draws its own arrow from tokens, beating a view\'s background shorthand, with room for it', () => {
    const rule = APP.get(':root select:not([multiple])');
    expect(rule).toMatchObject({ appearance: 'none', 'padding-right': '2rem', 'background-repeat': 'no-repeat' });
    expect(rule?.['background-image']).toMatch(/linear-gradient\(45deg, transparent 50%, var\(--muted\) 50%\), linear-gradient\(135deg, var\(--muted\) 50%, transparent 50%\)/);
  });

  it('every native disclosure marker is the muted ink', () => {
    expect(APP.get('summary::marker')).toEqual({ color: 'var(--muted)' });
  });

  it('Identity\'s text areas: the world\'s small radius and a vertical-only resize', () => {
    expect(parseBlocks(styleBlocks(read('views/Identity.svelte'))).get('textarea')).toMatchObject(
      { 'border-radius': 'var(--card-radius-sm)', resize: 'vertical' });
  });

  it('the Graph sheet\'s backdrop is a scrim with no blur', () => {
    const overlay = markupOf(read('lib/ui/drawer/drawer-overlay.svelte'));
    expect(overlay).toContain('bg-black/10');
    expect(overlay).not.toMatch(/backdrop-blur/);
  });
});
