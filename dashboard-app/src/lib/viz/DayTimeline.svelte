<script lang="ts">
  import { FRAME_IDS, frameVar } from '../frames';
  import { ROWS, hhmm, layout, rowOf, sentence, spanMinutes, summaryAt, type RowId } from '../dayTimeline';
  import type { WorkTodayTurn } from '../types/api';

  // The Overview's day timeline: today's turns by time and kind of work, one bar per turn in its frame's colour, an
  // underline for an error (red) or a gated call (amber). Compact on purpose: it must not dominate the page. One
  // replay control drives it: bars after the chosen minute fade, and a sentence derived from the data says what had
  // happened by then. Laid out at the measured width (1 SVG unit = 1 px), so the text keeps its size at phone width.
  // The bars are links for the mouse only (tabindex -1): a day can hold 3,000 of them, and the visually hidden
  // summary per row is the text alternative; the Work list is the keyboard path to a turn.
  let { turns, now, truncated }: { turns: WorkTodayTurn[]; now: string; truncated: boolean } = $props();

  let measured = $state(0);
  // The width, watched where ResizeObserver exists (jsdom has none: the layout falls back to 960 px there).
  function measure(node: HTMLElement) {
    if (typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver((entries) => (measured = Math.round(entries[0].contentRect.width)));
    ro.observe(node);
    return { destroy: () => ro.disconnect() };
  }
  let W = $derived(measured > 0 ? measured : 960);
  let narrow = $derived(W < 560);

  let nowMs = $derived(Date.parse(now));
  let dayStart = $derived.by(() => {
    const d = new Date(nowMs);
    return Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate());
  });
  let nowMinute = $derived(Number.isNaN(nowMs) ? 0 : Math.max(0, Math.floor((nowMs - dayStart) / 60000)));
  let span = $derived(spanMinutes(nowMinute));

  // Geometry: a label column when wide; a label line above each row when narrow.
  const BAR = 14;
  const TOP = 12;
  let X0 = $derived(narrow ? 0 : 172);
  let X1 = $derived(W - 6);
  let RH = $derived(narrow ? 42 : 30);
  let barY = (i: number) => TOP + i * RH + (narrow ? 18 : 0);
  let plotBottom = $derived(TOP + ROWS.length * RH - (narrow ? 4 : 8));
  let H = $derived(plotBottom + 24);
  let x = (minute: number) => X0 + (minute / span) * (X1 - X0);

  let bars = $derived(layout(turns, Number.isNaN(nowMs) ? 0 : nowMs, X1 - X0));
  let hours = $derived(Array.from({ length: span / 60 + 1 }, (_, h) => h));
  // An hour label is about 32 px wide in the dark look's font (no condensed width): every 3 hours needs 60 px apart.
  let labelEvery = $derived(((X1 - X0) / (span / 60)) * 3 < 60 ? 6 : 3);

  // Replay: the chosen minute, until `now` moves (a refresh brings the slider back to now).
  let scrub = $state<{ at: string; m: number } | null>(null);
  let minute = $derived(scrub && scrub.at === now ? scrub.m : nowMinute);
  const setMinute = (m: number) => (scrub = { at: now, m: Math.min(nowMinute, Math.max(0, Math.round(m))) });
  function onInput(e: Event) {
    const v = Number((e.currentTarget as HTMLInputElement).value);
    if (Number.isFinite(v)) setMinute(v);
  }
  // Native arrows step one minute, which takes too long across a day: five minutes, an hour with Page Up/Down.
  const KEYS: Record<string, (m: number) => number> = {
    ArrowLeft: (m) => m - 5, ArrowDown: (m) => m - 5, ArrowRight: (m) => m + 5, ArrowUp: (m) => m + 5,
    PageDown: (m) => m - 60, PageUp: (m) => m + 60, Home: () => 0, End: () => Infinity,
  };
  function onKey(e: KeyboardEvent) {
    const step = KEYS[e.key];
    if (!step) return;
    e.preventDefault();
    setMinute(step(minute));
  }

  let summary = $derived(summaryAt(turns, minute));
  let perRow = $derived(
    ROWS.map((r) => {
      const mine = turns.filter((t) => rowOf(t.work_kind) === r.id);
      return { label: r.label, n: mine.length, errors: mine.filter((t) => t.error).length, gated: mine.filter((t) => t.gated).length };
    }),
  );
  let frames = $derived.by(() => {
    const present = new Set(turns.map((t) => t.frame_id).filter((f): f is string => f != null && f !== ''));
    return [...FRAME_IDS.filter((f) => present.has(f)), ...[...present].filter((f) => !(FRAME_IDS as readonly string[]).includes(f))];
  });
  const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;
  let label = $derived(`Today's turns by time and kind of work, 00:00 to ${hhmm(nowMinute)} UTC: ${plural(turns.length, 'turn', 'turns')}.`);
  const rowIds: RowId[] = ROWS.map((r) => r.id);
  const KNOWN: ReadonlySet<string> = new Set(FRAME_IDS);
  /** The bar's frame class; an unknown or missing frame takes the conversation grey (no class). */
  const frameClass = (f: string | null) => (f != null && KNOWN.has(f) ? `f-${f}` : '');
</script>

<div class="tl" use:measure>
  <ul class="key" aria-label="Key">
    {#each frames as f (f)}<li><span class="kdot" aria-hidden="true" style:--fc={frameVar(f)}></span>{f}</li>{/each}
    <li class="sw"><span class="uline error" aria-hidden="true"></span>error</li>
    <li><span class="uline gated" aria-hidden="true"></span>gated</li>
  </ul>
  {#if truncated}<p class="cut">Showing the latest 3,000 turns of the day.</p>{/if}

  <figure>
    <svg role="img" aria-label={label} viewBox="0 0 {W} {H}">
      {#if X1 > x(nowMinute)}
        <rect class="notyet" x={x(nowMinute)} y={TOP - 8} width={X1 - x(nowMinute)} height={plotBottom - TOP + 8} rx="10" />
        {#if X1 - x(nowMinute) > 64}<text class="notyet-label" x={x(nowMinute) + 10} y={TOP + 8}>not yet</text>{/if}
      {/if}
      {#each hours as h (h)}
        <line class="grid" x1={x(h * 60)} y1={TOP - 8} x2={x(h * 60)} y2={plotBottom} />
        {#if h % labelEvery === 0}
          <text class="hour" x={x(h * 60)} y={H - 6} text-anchor={h === 0 && narrow ? 'start' : h * 60 === span ? 'end' : 'middle'}>{String(h).padStart(2, '0')}:00</text>
        {/if}
      {/each}
      {#each rowIds as id, i (id)}
        <text class="rowlabel" x="0" y={narrow ? TOP + i * RH + 10 : barY(i) + BAR / 2 + 4.5}>{ROWS[i].label}</text>
        {#each bars[id] as b (b.id)}
          <a href={b.href} tabindex="-1" class:later={b.minute > minute + 1}>
            <title>{b.title}</title>
            <rect class="bar {frameClass(b.frame)}" x={X0 + b.x} y={barY(i)} width={b.w} height={BAR} rx="3" />
            {#if b.status}<rect class="under {b.status}" x={X0 + b.x} y={barY(i) + BAR + 3} width={Math.max(b.w, 8)} height="3" rx="1.5" />{/if}
          </a>
        {/each}
      {/each}
      <line class="cursor" x1={x(minute)} y1={TOP - 8} x2={x(minute)} y2={plotBottom} />
    </svg>
    <figcaption class="sr-only">
      <ul>
        {#each perRow as r (r.label)}<li>{r.label}: {plural(r.n, 'turn', 'turns')}, {plural(r.errors, 'error', 'errors')}, {r.gated} gated.</li>{/each}
      </ul>
    </figcaption>
  </figure>

  <div class="replay">
    <label for="day-replay">Replay</label>
    <input id="day-replay" type="range" min="0" max={nowMinute} step="1" value={minute}
      aria-label="Replay time, minutes after 00:00 UTC" aria-valuetext={`${hhmm(minute)} UTC`} oninput={onInput} onkeydown={onKey} />
    <output for="day-replay" class="readout">{hhmm(minute)} UTC</output>
  </div>
  <p class="at" aria-live="polite">{sentence(minute, summary, truncated)}</p>
</div>

<style>
  /* Colours are tokens only (hexGuard): frame colours per ground, the status fills, the rule. */
  .tl { display: flex; flex-direction: column; gap: 0.5rem; min-width: 0; }
  .key { list-style: none; margin: 0; padding: 0; display: flex; flex-wrap: wrap; align-items: center; gap: 0.25rem 1rem; font-size: 0.78125rem; color: var(--muted); }
  .key li { display: inline-flex; align-items: center; gap: 6px; overflow-wrap: anywhere; }
  .key .sw { margin-left: auto; }
  .kdot { width: 10px; height: 10px; border-radius: 50%; background: var(--fc); flex: none; }
  .uline { width: 14px; height: 4px; border-radius: 2px; flex: none; }
  .uline.error { background: var(--red); }
  .uline.gated { background: var(--yellow); }
  .cut { margin: 0; font-size: 0.8125rem; color: var(--muted); }
  figure { margin: 0; min-width: 0; }
  /* As wide as its box, never wider: the viewBox follows the measured width (1 unit = 1 px), and no width attribute
     can push the page wider than a phone. */
  svg { display: block; width: 100%; height: auto; font-family: var(--font-ui); }
  .notyet { fill: var(--surface-hover); }
  .notyet-label { fill: var(--muted); font-size: 12px; }
  .grid { stroke: var(--border); stroke-linecap: round; }
  .hour { fill: var(--muted); font-size: 11.5px; font-stretch: 82%; }
  .rowlabel { fill: var(--text); font-size: 13px; }
  /* The bar's colour by class, not an inline custom property: 3,000 style writes are slow to mount. */
  .bar { fill: var(--f-conversation); fill-opacity: 0.85; }
  .bar.f-question { fill: var(--f-question); }
  .bar.f-task { fill: var(--f-task); }
  .bar.f-decision { fill: var(--f-decision); }
  .bar.f-debug { fill: var(--f-debug); }
  .bar.f-creative { fill: var(--f-creative); }
  .bar.f-initiation { fill: var(--f-initiation); }
  .under.error { fill: var(--red); }
  .under.gated { fill: var(--yellow); }
  a { transition: opacity var(--transition); }
  a.later { opacity: 0.28; }
  a:hover .bar { fill-opacity: 1; }
  .cursor { stroke: hsl(var(--primary)); stroke-width: 2.5; stroke-linecap: round; }

  .replay { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; align-items: center; gap: 1rem; padding-top: 0.5rem; border-top: 1px solid var(--border); }
  .replay label { font-size: 0.8125rem; color: var(--muted); }
  .readout { font-size: 0.9375rem; font-weight: 650; font-stretch: 82%; min-width: 9ch; text-align: right; }
  .replay input { width: 100%; height: 24px; margin: 0; background: transparent; appearance: none; -webkit-appearance: none; cursor: pointer; }
  .replay input:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
  .replay input::-webkit-slider-runnable-track { height: 4px; border-radius: 2px; background: var(--border); }
  .replay input::-moz-range-track { height: 4px; border-radius: 2px; background: var(--border); }
  .replay input::-webkit-slider-thumb { -webkit-appearance: none; appearance: none; width: 18px; height: 18px; margin-top: -7px; border-radius: 50%;
    background: hsl(var(--primary)); border: 3px solid var(--surface); box-shadow: 0 0 0 1px var(--border); }
  .replay input::-moz-range-thumb { width: 12px; height: 12px; border-radius: 50%; background: hsl(var(--primary)); border: 3px solid var(--surface); box-shadow: 0 0 0 1px var(--border); }
  .at { margin: 0; font-size: 0.84375rem; }
  @media (max-width: 768px) {
    .replay input { height: 44px; }
  }
</style>
