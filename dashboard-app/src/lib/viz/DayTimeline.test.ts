import { describe, it, expect, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/svelte';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import DayTimeline from './DayTimeline.svelte';
import type { WorkTodayTurn } from '../types/api';

const NOW = '2026-11-10T09:40:00+00:00';
const t = (id: string, hhmm: string, over: Partial<WorkTodayTurn> = {}): WorkTodayTurn => ({
  id, session_id: 'sess-1', started_at: `2026-11-10T${hhmm}:00+00:00`, ended_at: `2026-11-10T${hhmm}:05+00:00`,
  work_kind: 'conversation', frame_id: 'question', error: false, gated: false, tool_calls: 2, ...over,
});
const TURNS = [
  t('a', '04:00', { work_kind: 'heartbeat', frame_id: 'initiation' }),
  t('b', '05:30', { work_kind: 'subtask', frame_id: 'task', gated: true }),
  t('c', '09:12', { error: true, tool_calls: 3 }),
  t('d', '09:21', { frame_id: 'decision' }),
];

describe('DayTimeline', () => {
  afterEach(() => cleanup());

  it('draws four rows, one link per turn with its title, and the error and gated underlines', () => {
    const { container } = render(DayTimeline, { turns: TURNS, now: NOW, truncated: false });
    const labels = Array.from(container.querySelectorAll('svg .rowlabel')).map((e) => e.textContent);
    expect(labels).toEqual(['Your conversations', 'Heartbeat and background', 'Subtasks', 'Workflows']);
    const links = Array.from(container.querySelectorAll('svg a'));
    expect(links).toHaveLength(4);
    const c = links.find((a) => a.getAttribute('href') === '#/work/s/sess-1/c')!;
    expect(c.querySelector('title')!.textContent).toBe('09:12 UTC. question frame. 3 tool calls. An error.');
    expect(container.querySelectorAll('svg .under.error')).toHaveLength(1);
    expect(container.querySelectorAll('svg .under.gated')).toHaveLength(1);
    // The bar's colour is its frame's token.
    expect(c.querySelector('rect.bar')!.getAttribute('class')).toContain('f-question');
    // Bars are mouse targets only: 3,000 of them must not be 3,000 tab stops (the Work list is the keyboard path).
    for (const a of links) expect(a.getAttribute('tabindex')).toBe('-1');
  });

  it('the picture has a text alternative: a label and a visually hidden summary per row', () => {
    const { container } = render(DayTimeline, { turns: TURNS, now: NOW, truncated: false });
    const svg = container.querySelector('svg')!;
    expect(svg.getAttribute('role')).toBe('img');
    // No width attribute: a fixed width would push the page wider than a phone (the layout follows the measured box).
    expect(svg.getAttribute('width')).toBeNull();
    expect(svg.getAttribute('viewBox')).toMatch(/^0 0 960 [0-9]+$/);
    expect(svg.getAttribute('aria-label')).toBe("Today's turns by time and kind of work, 00:00 to 09:40 UTC: 4 turns.");
    const items = Array.from(container.querySelectorAll('.sr-only li')).map((l) => l.textContent);
    expect(items).toEqual([
      'Your conversations: 2 turns, 1 error, 0 gated.',
      'Heartbeat and background: 1 turn, 0 errors, 0 gated.',
      'Subtasks: 1 turn, 0 errors, 1 gated.',
      'Workflows: 0 turns, 0 errors, 0 gated.',
    ]);
  });

  it('the frame key lists the frames of the day, with the underline key', () => {
    const { container } = render(DayTimeline, { turns: TURNS, now: NOW, truncated: false });
    const key = Array.from(container.querySelectorAll('.key li')).map((l) => l.textContent?.trim());
    expect(key).toEqual(['question', 'task', 'decision', 'initiation', 'error', 'gated']);
  });

  it('replay: the slider runs from 00:00 to now; moving it fades the later bars and updates the sentence', async () => {
    const { container } = render(DayTimeline, { turns: TURNS, now: NOW, truncated: false });
    const range = screen.getByRole('slider', { name: 'Replay time, minutes after 00:00 UTC' }) as HTMLInputElement;
    expect(range.min).toBe('0');
    expect(range.max).toBe('580');
    expect(range.value).toBe('580');
    expect(container.querySelector('.at')!.textContent).toBe('09:40 UTC. 4 turns so far: 2 yours, 2 background; 1 error, 1 gated.');
    expect(container.querySelectorAll('svg a.later')).toHaveLength(0);
    await fireEvent.input(range, { target: { value: '360' } });
    expect(container.querySelector('output')!.textContent).toBe('06:00 UTC');
    const later = Array.from(container.querySelectorAll('svg a.later')).map((a) => a.getAttribute('href'));
    expect(later).toEqual(['#/work/s/sess-1/c', '#/work/s/sess-1/d']);
    expect(container.querySelector('.at')!.textContent).toBe('06:00 UTC. 2 turns so far: 0 yours, 2 background; 0 errors, 1 gated.');
  });

  it('a bar fades exactly when the sentence stops counting it (the same minute rule)', async () => {
    const edge = [
      t('on', '06:00', { started_at: '2026-11-10T06:00:30+00:00' }),   // minute 360.5: counts at 06:00
      t('next', '06:01', { started_at: '2026-11-10T06:01:00+00:00' }), // minute 361: not yet at 06:00
    ];
    const { container } = render(DayTimeline, { turns: edge, now: NOW, truncated: false });
    const range = screen.getByRole('slider', { name: 'Replay time, minutes after 00:00 UTC' }) as HTMLInputElement;
    const faded = () => Array.from(container.querySelectorAll('svg a.later')).map((a) => a.getAttribute('href'));
    await fireEvent.input(range, { target: { value: '360' } });
    expect(faded()).toEqual(['#/work/s/sess-1/next']);
    expect(container.querySelector('.at')!.textContent).toContain('1 turn so far');
    await fireEvent.input(range, { target: { value: '361' } });
    expect(faded()).toEqual([]);
    expect(container.querySelector('.at')!.textContent).toContain('2 turns so far');
  });

  it('keyboard: arrows step five minutes, Page keys an hour, Home and End the ends', async () => {
    const { container } = render(DayTimeline, { turns: TURNS, now: NOW, truncated: false });
    const range = screen.getByRole('slider', { name: 'Replay time, minutes after 00:00 UTC' }) as HTMLInputElement;
    const out = () => container.querySelector('output')!.textContent;
    await fireEvent.keyDown(range, { key: 'ArrowLeft' });
    expect(out()).toBe('09:35 UTC');
    await fireEvent.keyDown(range, { key: 'PageDown' });
    expect(out()).toBe('08:35 UTC');
    await fireEvent.keyDown(range, { key: 'ArrowRight' });
    expect(out()).toBe('08:40 UTC');
    await fireEvent.keyDown(range, { key: 'Home' });
    expect(out()).toBe('00:00 UTC');
    await fireEvent.keyDown(range, { key: 'ArrowLeft' });
    expect(out()).toBe('00:00 UTC');
    await fireEvent.keyDown(range, { key: 'End' });
    expect(out()).toBe('09:40 UTC');
    await fireEvent.keyDown(range, { key: 'PageUp' });
    expect(out()).toBe('09:40 UTC');
  });

  it('says when the list was cut, and the sentence says it counts the turns shown', () => {
    const { container } = render(DayTimeline, { turns: TURNS, now: NOW, truncated: true });
    expect(container.textContent).toContain('Showing the latest 3,000 turns of the day.');
    expect(container.querySelector('.at')!.textContent).toContain('4 of the turns shown so far');
  });

  it('an empty day still draws its rows and says so', () => {
    const { container } = render(DayTimeline, { turns: [], now: NOW, truncated: false });
    expect(container.querySelectorAll('svg .rowlabel')).toHaveLength(4);
    expect(container.querySelectorAll('svg a')).toHaveLength(0);
    expect(container.querySelector('.at')!.textContent).toBe('09:40 UTC. 0 turns so far: 0 yours, 0 background; 0 errors, 0 gated.');
  });

  // A smoke bound, not a benchmark: it catches a per-bar pass over every turn (quadratic), not a slow machine. Warm,
  // alone, the mount takes about 300 ms in jsdom; under the full suite's parallel load it has taken 920 ms, so the
  // bound is 1,500 ms rather than the plan's 500 (which failed under load).
  it('3,000 turns render in under 1,500 ms (a smoke bound, not a benchmark)', () => {
    const many = Array.from({ length: 3000 }, (_, i) => {
      const m = Math.floor((i * 580) / 3000);
      const hh = String(Math.floor(m / 60)).padStart(2, '0');
      const mm = String(m % 60).padStart(2, '0');
      return t(`t${i}`, `${hh}:${mm}`, { work_kind: ['conversation', 'heartbeat', 'subtask', 'workflow_step'][i % 4], error: i % 97 === 0 });
    });
    // Warm up once (the first mount pays for compiling the component's code paths), then time a cold DOM.
    render(DayTimeline, { turns: many.slice(0, 50), now: NOW, truncated: false });
    cleanup();
    const t0 = performance.now();
    const { container } = render(DayTimeline, { turns: many, now: NOW, truncated: true });
    const ms = performance.now() - t0;
    expect(container.querySelectorAll('svg a')).toHaveLength(3000);
    expect(ms).toBeLessThan(1500);
  });

  it('text only: no raw-HTML directive, no colour literal in the style', () => {
    const src = readFileSync(resolve(__dirname, 'DayTimeline.svelte'), 'utf-8');
    expect(src).not.toContain('{@html');
    expect(src).not.toMatch(/#[0-9a-fA-F]{6}\b|rgba?\(/);
  });
});
