/// <reference types="node" />
import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { render, cleanup, fireEvent } from '@testing-library/svelte';
import { tick } from 'svelte';
import Renderer from './Renderer.svelte';
import { store } from './store.svelte';

// The three phones of the approved prototype (approval, report, decision review), composed only from catalog
// components, rendered through the real Renderer: they render whole (no placeholder) and carry no colour of their
// own. Behaviour, not pixels; the screenshot round is the visual check.

type Fixture = { components: Record<string, unknown>[]; dataModel: Record<string, unknown> };
const load = (name: string): Fixture =>
  JSON.parse(readFileSync(join(__dirname, 'catalog', '__fixtures__', `pr6-${name}.json`), 'utf-8'));

const SURFACE = 'phones-test-surface';

function mount(name: string) {
  const fx = load(name);
  store.apply(null, {
    version: 'v1.0',
    createSurface: {
      surfaceId: SURFACE,
      catalogId: 'nous-core',
      components: fx.components,
      dataModel: fx.dataModel,
      metadata: { extensions: { com_nous_nonce: 'n', com_nous_theme: 'nous-default' } },
    },
  } as never);
  return render(Renderer, { props: { surfaceId: SURFACE, componentId: 'root' } });
}

/** Every inline style in the tree: a fixture must never carry a colour literal. */
function inlineColours(container: HTMLElement): string[] {
  return [...container.querySelectorAll('[style]')]
    .map((e) => e.getAttribute('style') ?? '')
    .filter((s) => /#[0-9a-f]{3,8}\b|\brgba?\(|\bhsla?\(/i.test(s));
}

beforeEach(() => store.reset());
afterEach(() => {
  cleanup();
  store.reset();
});

describe('the three prototype phones render from catalog components', () => {
  it('approval: the escalation, three evidence tiles, one radio group with the recommendation chosen, two actions', () => {
    const { container } = mount('approval');
    expect(container.querySelector('.ph')).toBeNull();
    expect(container.querySelector('.panel h2')?.textContent).toBe('Turn on frame carry?');
    expect(container.querySelectorAll('.tile').length).toBe(3);
    const radios = [...container.querySelectorAll<HTMLInputElement>('input[type="radio"]')];
    expect(radios.length).toBe(3);
    expect(new Set(radios.map((r) => r.name)).size).toBe(1);
    expect(radios.filter((r) => r.checked).map((r) => r.value)).toEqual(['wait']);
    expect(container.querySelector('.option.selected')?.textContent).toContain('recommended');
    expect([...container.querySelectorAll('button.btn')].map((b) => b.textContent?.trim())).toEqual([
      'Confirm choice',
      'Ask Nous',
    ]);
    expect(inlineColours(container)).toEqual([]);
  });

  it('approval: choosing another option moves the chosen card', async () => {
    const { container } = mount('approval');
    const on = [...container.querySelectorAll<HTMLInputElement>('input[type="radio"]')].find((r) => r.value === 'on')!;
    await fireEvent.click(on);
    await tick();
    expect(container.querySelector('.option.selected')?.textContent).toContain('Turn it on now');
    expect(container.querySelectorAll('.option.selected').length).toBe(1);
  });

  it('report: header, two metric cards, a four-series line chart, the video, bars, the tool table, a meter and the timeline', () => {
    const { container } = mount('report');
    expect(container.querySelector('.ph')).toBeNull();
    expect(container.querySelector('.app-header h2')?.textContent).toBe('Recall since frame context');
    expect(container.querySelectorAll('.metric').length).toBe(2);
    expect(container.querySelectorAll('.line polyline').length).toBe(4);
    expect(container.querySelector('video.vid')).not.toBeNull();
    expect(container.querySelectorAll('.hrow').length).toBe(4);
    expect(container.querySelectorAll('.hrow i.neg').length).toBe(1);
    expect([...container.querySelectorAll('table.dtable tbody tr')].map((r) => r.firstElementChild?.textContent)).toEqual([
      'recall_deep',
      'db_query',
      'read_file',
    ]);
    expect(container.querySelector('[role="meter"]')?.getAttribute('aria-valuenow')).toBe('0.72');
    expect(container.querySelectorAll('.timeline li').length).toBe(4);
    expect(container.querySelectorAll('.timeline li.flag').length).toBe(1);
    expect(inlineColours(container)).toEqual([]);
  });

  it('decision review: two tabs, chips, the meter, what changed and the record, two actions', async () => {
    const { container, getByRole } = mount('decision');
    expect(container.querySelector('.ph')).toBeNull();
    const tabs = [...container.querySelectorAll('[role="tab"]')];
    expect(tabs.map((t) => t.textContent?.trim())).toEqual(['Due for review 2', 'Decided 14']);
    expect(tabs[0].getAttribute('aria-selected')).toBe('true');
    expect(container.querySelector('h2')?.textContent).toBe('Keep frame carry off until 13 November');
    expect([...container.querySelectorAll('.chips .chip .v')].map((c) => c.textContent)).toEqual([
      'process',
      'medium',
      '13 Nov',
    ]);
    expect(container.querySelectorAll('.deltas li').length).toBe(3);
    expect([...container.querySelectorAll('table.kv th')].map((t) => t.textContent)).toEqual([
      'Recorded',
      'By',
      'Outcome',
    ]);
    expect(inlineColours(container)).toEqual([]);
    await fireEvent.click(getByRole('tab', { name: 'Decided 14' }));
    await tick();
    expect(container.textContent).toContain('14 decisions are settled.');
  });
});
