import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, cleanup } from '@testing-library/svelte';
import { tick } from 'svelte';
import Chart from './Chart.svelte';
import { setLook } from '../look';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { styleBlocks } from '../testing/css';

type Config = { type: string; data: any; options: any };

/** A Chart stub recording its construction and every update (with the data and options it had then). */
function stubChart() {
  const made: Config[] = [];
  const updates: { data: any; options: any }[] = [];
  const destroy = vi.fn();
  // vi.fn with a regular function (not arrow) so `new Chart(...)` works as a constructor.
  const ctor = vi.fn(function (this: any, _el: unknown, cfg: Config) {
    made.push(cfg);
    const inst = {
      destroy,
      data: cfg.data,
      options: cfg.options,
      update: vi.fn(() => updates.push({ data: inst.data, options: inst.options })),
    };
    return inst;
  });
  vi.stubGlobal('Chart', ctor);
  return { ctor, made, updates, destroy };
}

/** A paper-or-dark ground to render into, with token values set on it (jsdom has no stylesheet). */
function groundEl(kind: 'paper' | 'dark', vars: Record<string, string> = {}): HTMLElement {
  const el = document.createElement('main');
  el.setAttribute('data-ground', kind);
  for (const [k, v] of Object.entries(vars)) el.style.setProperty(k, v);
  document.body.appendChild(el);
  return el;
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  setLook('dark');
  document.body.innerHTML = '';
});

describe('Chart.svelte', () => {
  it('a chart is 200 px tall at phone width unless it asks for more (redesign PR 4 review I3)', () => {
    stubChart();
    const props = { type: 'line', data: { labels: [], datasets: [] }, options: {} };
    const plain = render(Chart, { props }).container.querySelector('.chart-wrap') as HTMLElement;
    expect(plain.style.getPropertyValue('--phone-height')).toBe('200px');
    const tall = render(Chart, { props: { ...props, phoneHeight: '300px' } }).container.querySelector('.chart-wrap') as HTMLElement;
    expect(tall.style.getPropertyValue('--phone-height')).toBe('300px');
  });

  it('source contract: under 768 px the chart takes its phone height', () => {
    const css = styleBlocks(readFileSync(join(__dirname, 'Chart.svelte'), 'utf-8'));
    expect(css).toMatch(/@media \(max-width: 768px\) \{\s*\.chart-wrap \{\s*height: var\(--phone-height\) !important;/);
  });

  it('constructs a Chart on mount and destroys on unmount', async () => {
    const { ctor, destroy } = stubChart();
    const { unmount } = render(Chart, { props: { type: 'line', data: { labels: [], datasets: [] }, options: {} } });
    expect(ctor).toHaveBeenCalledTimes(1);
    unmount();
    expect(destroy).toHaveBeenCalledTimes(1);
  });

  it('on a dark ground, literal colours and options pass through byte for byte (unconverted screens)', () => {
    const { made } = stubChart();
    const data = { labels: ['a', 'b'], datasets: [{ data: [1, 2], backgroundColor: ['#34d399', 'rgba(124, 106, 247, 0.5)'], borderColor: '#7c6af7' }] };
    const options = { cutout: '60%', plugins: { legend: { position: 'bottom' } } };
    render(Chart, { target: groundEl('dark'), props: { type: 'doughnut', data, options } });
    expect(made[0].data).toEqual(data);
    expect(made[0].options).toEqual({ responsive: true, maintainAspectRatio: false, ...options });
  });

  it('on a dark ground, a token resolves to the dark value', () => {
    const { made } = stubChart();
    const data = { labels: ['a'], datasets: [{ data: [1], borderColor: 'var(--series-1)', backgroundColor: 'tint(--series-1, 0.1)' }] };
    render(Chart, { target: groundEl('dark'), props: { type: 'line', data } });
    expect(made[0].data.datasets[0].borderColor).toBe('#60a5fa');
    expect(made[0].data.datasets[0].backgroundColor).toBe('rgba(96, 165, 250, 0.1)');
    expect(made[0].options.color).toBeUndefined();
    expect(made[0].options.scales).toBeUndefined();
  });

  it('on a paper ground, the axes and legend take the chart text and grid colours', () => {
    setLook('paper');
    const { made } = stubChart();
    const target = groundEl('paper', { '--chart-text': '#56687a', '--chart-grid': '#e3e9ec' });
    render(Chart, { target, props: { type: 'bar', data: { labels: [], datasets: [] }, options: { scales: { y: { grid: { display: false } } } } } });
    const o = made[0].options;
    expect(o.color).toBe('#56687a');
    expect(o.scales.x.ticks.color).toBe('#56687a');
    expect(o.scales.x.grid.color).toBe('#e3e9ec');
    expect(o.scales.y.grid).toEqual({ color: '#e3e9ec', display: false });
    expect(o.responsive).toBe(true);
  });

  it('a look switch re-resolves every token and updates the chart once', async () => {
    const { made, updates } = stubChart();
    const target = groundEl('paper');
    const data = { labels: ['a'], datasets: [{ data: [1], backgroundColor: ['var(--series-1)', '#123456'] }] };
    render(Chart, { target, props: { type: 'doughnut', data } });
    await tick();
    expect(made[0].data.datasets[0].backgroundColor).toEqual(['#60a5fa', '#123456']);
    const before = updates.length;
    target.style.setProperty('--series-1', '#1f5fa8'); // what the paper ground's stylesheet declares
    setLook('paper');
    await tick();
    expect(updates.length - before).toBe(1);
    expect(updates.at(-1)!.data.datasets[0].backgroundColor).toEqual(['#1f5fa8', '#123456']);
    expect(updates.at(-1)!.options.color).toBeDefined();
  });
});
