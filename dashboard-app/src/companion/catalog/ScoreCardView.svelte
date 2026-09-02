<script lang="ts">
  // F096 ScoreCard — a verdict on an objective with its evidence: title +
  // uppercase status pill, an optional headline figure, evidence rows each
  // carrying their own tone, and an italic note. Tone drives the TOP rule
  // (the reference's own treatment, not a left-border stripe) and the pill.
  // A card with no value is legal: the verdict plus evidence is the content.
  // Values are preformatted; row tones are closed by normalizeTone at render.
  import { store } from '../store.svelte';
  import { flexGrow, omittedNote, resolveDynamic, splitTruncation, toDisplayString } from '../functions';
  import { normalizeTone, toneInkVar } from '../chart';
  import type { Scope } from '../pointer';
  import type { A2uiComponent } from '../store.svelte';

  interface Row {
    label: string;
    value: string;
    ink: string;
  }

  let {
    surfaceId,
    comp,
    scope = null,
  }: {
    surfaceId: string;
    comp: A2uiComponent;
    scope?: Scope | null;
    depth?: number;
    ancestors?: readonly string[];
  } = $props();

  const ctx = $derived({ dataModel: store.surfaces[surfaceId]?.dataModel ?? {}, scope });
  const title = $derived(toDisplayString(resolveDynamic(comp.title, ctx)));
  const status = $derived(toDisplayString(resolveDynamic(comp.status, ctx)));
  const value = $derived(toDisplayString(resolveDynamic(comp.value, ctx)));
  const unit = $derived(toDisplayString(resolveDynamic(comp.unit, ctx)));
  const caption = $derived(toDisplayString(resolveDynamic(comp.caption, ctx)));
  const note = $derived(toDisplayString(resolveDynamic(comp.note, ctx)));
  // Literal or {path} binding (per-record tone under a repeat template);
  // normalizeTone closes the resolved value.
  const tone = $derived(normalizeTone(resolveDynamic(comp.tone, ctx)));
  // Non-array (absent, None for this item, a wrong shape) ⇒ no rows, never a
  // throw: a goal with no evidence rows is a legal state (spec §3.2).
  // A server-bounded evidence list may end in the truncation marker (codex
  // P2 on #630): not a row — filtered, and its count shown.
  const split = $derived.by(() => {
    const resolved = resolveDynamic(comp.items, ctx);
    return splitTruncation(Array.isArray(resolved) ? resolved : []);
  });
  const rows = $derived.by((): Row[] => {
    return split.rows.map((row) => {
      const r = (typeof row === 'object' && row !== null ? row : {}) as Record<string, unknown>;
      return {
        label: toDisplayString(r.label),
        value: toDisplayString(r.value),
        ink: toneInkVar(normalizeTone(r.tone)),
      };
    });
  });
</script>

<div class="score" style:flex-grow={flexGrow(comp.weight)} style:--ink={toneInkVar(tone)}>
  <div class="head">
    <h4>{title}</h4>
    <span class="status">{status}</span>
  </div>
  {#if value}
    <div class="value">{value}{#if unit}<span class="unit">{unit}</span>{/if}</div>
    {#if caption}<div class="caption">{caption}</div>{/if}
  {/if}
  {#if rows.length > 0}
    <ul>
      {#each rows as row, i (i)}
        <li style:--row-ink={row.ink}>
          <span class="rl">{row.label}</span>
          <b class="rv">{row.value}</b>
        </li>
      {/each}
    </ul>
  {/if}
  {#if split.omitted !== null}<p class="omitted">{omittedNote(split.omitted)}</p>{/if}
  {#if note}<p class="note">{note}</p>{/if}
</div>

<style>
  .score {
    display: flex;
    flex-direction: column;
    gap: 0.3rem;
    min-width: 0;
    padding: 0.95rem 1rem 0.75rem;
    background: var(--surface);
    border: 1px solid var(--border);
    border-top: 3px solid var(--ink);
    border-radius: var(--radius);
  }
  .head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 0.6rem;
  }
  h4 {
    margin: 0;
    font-family: var(--font-display);
    font-size: 1.15rem;
    font-weight: 600;
    letter-spacing: -0.01em;
    min-width: 0;
    overflow-wrap: anywhere;
  }
  .status {
    flex: 0 0 auto;
    font-size: 0.66rem;
    font-weight: 700;
    letter-spacing: 0.09em;
    text-transform: uppercase;
    padding: 0.15rem 0.55rem;
    border-radius: 999px;
    white-space: nowrap;
    color: var(--ink);
    background: color-mix(in srgb, var(--ink) 14%, transparent);
  }
  .value {
    margin-top: 0.35rem;
    font-family: var(--font-numeric);
    font-variant-numeric: tabular-nums;
    font-size: 2rem;
    font-weight: 600;
    line-height: 1;
    overflow-wrap: anywhere;
  }
  .unit {
    margin-left: 0.3rem;
    color: var(--muted);
    font-family: var(--font-ui);
    font-size: 0.85rem;
    font-weight: 400;
  }
  .caption {
    color: var(--muted);
    font-size: 0.78rem;
  }
  ul {
    list-style: none;
    margin: 0.5rem 0 0.35rem;
    padding: 0;
  }
  li {
    display: flex;
    justify-content: space-between;
    gap: 0.6rem;
    padding: 0.3rem 0;
    border-bottom: 1px dashed var(--border);
    font-size: 0.85rem;
  }
  li:last-child {
    border-bottom: 0;
  }
  .rl {
    color: var(--muted);
    min-width: 0;
    overflow-wrap: anywhere;
  }
  .rv {
    flex: 0 0 auto;
    color: var(--row-ink);
    font-family: var(--font-numeric);
    font-variant-numeric: tabular-nums;
    font-size: 0.8rem;
    font-weight: 600;
  }
  .note,
  .omitted {
    margin: 0;
    color: var(--muted);
    font-size: 0.74rem;
    font-style: italic;
  }
</style>
