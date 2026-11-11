import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { markupOf, parseBlocks, styleBlocks } from './lib/testing/css';
import { sourceFiles } from './lib/testing/sources';

// Finish review "ceiling" and the craft floor's browser surfaces: the parts the browser draws carry the design too. A
// checkbox or radio takes the action colour, a select its own drawn arrow (from tokens), every disclosure marker the
// muted ink, Identity's text areas the world's radius; and the Graph sheet's backdrop is a scrim, not a blur.

const read = (f: string) => readFileSync(join(__dirname, f), 'utf-8');
const APP = parseBlocks(read('app.css'));

describe('browser surfaces', () => {
  it('checkboxes, radios and range sliders take the action colour', () => {
    expect(APP.get(':root input[type="checkbox"], :root input[type="radio"], :root input[type="range"]')).toEqual({ 'accent-color': 'var(--action)' });
  });

  // Final step A5: the Graph's range slider (and Admission's, and three checkbox rules) named the saturated --accent.
  it.each(sourceFiles().filter((f) => f.endsWith('.svelte')))('%s: any accent-color it sets is the action colour', (f) => {
    const values = [...styleBlocks(read(f)).matchAll(/accent-color:\s*([^;]+);/g)].map((m) => m[1].trim());
    expect(values.filter((v) => v !== 'var(--action)')).toEqual([]);
  });

  // Final step A4: a field's boundary is the field rule (3:1 on white), as Identity's are; Work's search box and select,
  // and six more, drew it in the 1.3:1 rule colour.
  const FIELD = /(?<![\w-])(?:select|textarea)(?![\w-])|input\[type=['"]?search|\.search-input|\.censor-select|\.ack input/;
  it('the field check finds the fields and spares the controls that are not text fields', () => {
    for (const sel of ['select', "select, input[type='search']", '.search-input', '.censor-select', 'textarea', '.ack input']) expect(sel, sel).toMatch(FIELD);
    for (const sel of ['.type-check input', '.range-slider', '.selected-row', '.input']) expect(sel, sel).not.toMatch(FIELD);
  });
  it.each(sourceFiles().filter((f) => f.endsWith('.svelte')))('%s: every field it styles has the field rule for a border', (f) => {
    const wrong = [...parseBlocks(styleBlocks(read(f)))].filter(([sel, d]) => FIELD.test(sel) && !/:focus/.test(sel) && d.border !== undefined
      && !d.border.includes('var(--field-border)')).map(([sel, d]) => `${sel}: ${d.border}`);
    expect(wrong).toEqual([]);
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
