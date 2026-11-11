import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, cleanup, waitFor, fireEvent } from '@testing-library/svelte';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import Retrieval from './Retrieval.svelte';
import { DARK_SEL, parseBlocks, styleBlocks } from '../lib/testing/css';

// Retrieval on the paper look: the disposition palette is tokens (four hue families, position stays authoritative),
// the count on a funnel segment reads --seg-ink, and the notation glyphs are words; the <details> groups keep the
// browser's own disclosure marker. Every value below is made up.
const DARK = parseBlocks(readFileSync(join(__dirname, '..', 'app.css'), 'utf-8')).get(DARK_SEL) ?? {};
const SRC = readFileSync(join(__dirname, 'Retrieval.svelte'), 'utf-8');

/** DISPOSITION_META's colours before the tokens, in segment order. */
const OLD: [string, string][] = [
  ['rendered', '#34d399'], ['returned_to_script', '#22d3ee'], ['sliced_off', '#f59e0b'], ['budget_truncated', '#fcd34d'],
  ['below_floor', '#a78bfa'], ['filter_dropped', '#c4b5fd'], ['deduped', '#94a3b8'], ['superseded', '#cbd5e1'],
  ['replaced_at_merge', '#64748b'], ['f071_excluded', '#7c8da3'], ['type_excluded', '#475569'], ['unaccounted', '#f472b6'],
];
const DISPOSITIONS = OLD.map(([d]) => d);
const token = (d: string) => `--disp-${d.replace(/_/g, '-')}`;

const QUERY = 'q'.repeat(1000);
const T = new Date().toISOString();
const counts = Object.fromEntries(DISPOSITIONS.map((d, i) => [d, i + 1]));
const ENTRY = {
  id: 'r-1', session_id: 's-1', turn_number: 3, trace_id: null, timestamp: T, path: 'pipeline', query: QUERY,
  duration_ms: 1200, legs: [], excluded_types: [], n_candidates: 78, n_rendered: 1, n_expansions: 3,
  disposition_counts: counts, truncated: false, has_candidates: true,
};
const LIST = {
  entries: [ENTRY], disposition_totals: counts, count: 1, sampled_count: 1,
  leg_totals: { vector: { attempted: 1, returned: 4, deduped: 0, dropped: 0, errors: 0 } },
};
const candidate = (d: string, i: number) => ({
  id: `c-${i}`, type: 'fact', entry_leg: 'vector', entry_score: 0.62, entry_rank: i, snippet: `snippet ${i}`,
  mutations: d === 'replaced_at_merge' ? [{ stage: 'merge', score_before: 0.62, score_after: 0.55, reason: null }] : [],
  final_rank: null, disposition: d, disposition_stage: d === 'rendered' ? null : 'merge', restored_from: null,
});
const expansion = (seed: string, neighbour: string) => ({
  seed_id: seed, seed_type: 'fact', seed_score: 0.8, neighbor_id: neighbour, neighbor_type: 'episode', stage: 'graph',
  hop: 1, edge_relation: 'supports', edge_weight: 0.5, extraction_method: 'heuristic', path_strength: 0.4, won_best_path: true,
});
const DETAIL = {
  ...ENTRY,
  candidates_by_disposition: Object.fromEntries(DISPOSITIONS.map((d, i) => [d, [candidate(d, i)]])),
  // Three seeds; n-1 is reached from two of them.
  expansions: [expansion('seed-a', 'n-1'), expansion('seed-b', 'n-1'), expansion('seed-c', 'n-2')],
};

function install() {
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    const body = url.startsWith('/dashboard/retrieval/') ? DETAIL : url.startsWith('/dashboard/retrieval') ? LIST : null;
    if (!body) return { ok: false, status: 404, json: async () => ({}) } as Response;
    return { ok: true, status: 200, json: async () => body } as Response;
  }));
}

async function openDetail(container: HTMLElement) {
  await waitFor(() => expect(container.querySelector('button.rrow')).not.toBeNull());
  await fireEvent.click(container.querySelector('button.rrow')!);
  await screen.findByRole('heading', { name: 'Candidates' });
}

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe('Retrieval on the paper look', () => {
  it('the legend names all twelve dispositions in segment order, each dot a --disp token', async () => {
    install();
    const { container } = render(Retrieval);
    await waitFor(() => expect(container.querySelectorAll('.disp-legend li:not(.fam-sep)')).toHaveLength(12));
    const items = Array.from(container.querySelectorAll('.disp-legend li:not(.fam-sep)'));
    expect(items.map((li) => li.querySelector('.k')?.textContent)).toEqual(DISPOSITIONS);
    expect(items.map((li) => li.querySelector('.dot')?.getAttribute('style'))).toEqual(DISPOSITIONS.map((d) => `background: var(${token(d)});`));
    const segs = Array.from(container.querySelectorAll('.funnel-seg'));
    expect(segs).toHaveLength(12);
    for (const [i, s] of segs.entries()) expect(s.getAttribute('style'), DISPOSITIONS[i]).toContain(`background: var(${token(DISPOSITIONS[i])})`);
    // The count sits on the bar in --seg-ink (white on paper, every fill is 4.5:1 or more against it).
    expect(container.querySelectorAll('.funnel-seg .seg-label')).toHaveLength(11);
    expect(styleBlocks(SRC)).toMatch(/\.seg-label \{[^}]*color: var\(--seg-ink\);/);
  });

  it('dark: every disposition token resolves to the colour it had (the dark parity table)', () => {
    for (const [d, hex] of OLD) expect(DARK[token(d)], d).toBe(hex);
    expect(SRC).toContain("return DISPOSITION_META[d]?.color ?? 'var(--disp-replaced-at-merge)';");
  });

  it('the row says how many reached the model in words; no arrow, no multiplication sign, no triangle', async () => {
    install();
    const { container } = render(Retrieval);
    await waitFor(() => expect(container.querySelector('.rbar-n')).not.toBeNull());
    // The same words as the funnel's accessible name ("N of M candidates reached the model").
    expect(container.querySelector('.rbar-n')?.textContent?.replace(/\s+/g, ' ').trim()).toBe('1 of 78 reached the model');
    await openDetail(container);
    const text = document.body.textContent ?? '';
    expect(text).not.toMatch(/[→←×▸▾]/);
    expect(text).toContain('2 seeds');
    const mut = container.querySelector('.mut')!;
    expect(mut.textContent?.trim()).toBe('to 0.550');
    expect(mut.getAttribute('title')).toBe('merge: from 0.620 to 0.550');
  });

  it('the groups are native <details>: their summaries are keyboard-operable and keep the browser\'s marker', async () => {
    install();
    const { container } = render(Retrieval);
    await openDetail(container);
    const summaries = Array.from(container.querySelectorAll('details > summary'));
    expect(summaries.length).toBe(12 + 3); // a group per disposition, one per seed
    for (const s of summaries) expect(s.classList.contains('disp-head') || s.classList.contains('seed-head')).toBe(true);
    const css = styleBlocks(SRC);
    expect(css).not.toMatch(/details-marker|content: '[^']/);
    // A summary laid out as flex loses its marker: the row inside is the flex box, the summary stays a list item.
    expect(css).not.toMatch(/\.disp-head, \.seed-head \{[^}]*display: flex/);
    expect(css).toMatch(/\.sum-row \{[^}]*display: inline-flex/);
    // The disposition badge's border is a color-mix, not a hex alpha suffix.
    expect(container.querySelector('.disp-badge')?.getAttribute('style')).toContain('border-color: color-mix(in srgb, var(--disp-rendered) 33.3%, transparent)');
  });

  it('a 1,000-character query renders as text and wraps in the detail', async () => {
    install();
    const { container } = render(Retrieval);
    await openDetail(container);
    expect(container.querySelector('.detail-query')?.textContent).toBe(QUERY);
    expect(styleBlocks(SRC)).toMatch(/\.detail-query \{[^}]*overflow-wrap: anywhere;/);
    expect(SRC).not.toContain('{@html');
  });

  it('on paper, a memory-type chip\'s text is its colour mixed 70% with the ink: 4.5:1 or more on the chip', () => {
    const PAPER = parseBlocks(readFileSync(join(__dirname, '..', 'app.css'), 'utf-8')).get('html[data-look="paper"] [data-ground="paper"]') ?? {};
    const rgb = (h: string) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
    const lum = (c: number[]) => {
      const [r, g, b] = c.map((v) => { const s = v / 255; return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4; });
      return 0.2126 * r + 0.7152 * g + 0.0722 * b;
    };
    const ratio = (a: number[], b: number[]) => { const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p); return (x + 0.05) / (y + 0.05); };
    const ink = rgb(PAPER['--text']);
    for (const k of ['fact', 'episode', 'decision', 'procedure', 'chunk', 'censor']) {
      const mixed = rgb(PAPER[`--${k}-color`]).map((v, i) => 0.7 * v + 0.3 * ink[i]);
      expect(ratio(mixed, rgb(PAPER['--surface-hover'])), k).toBeGreaterThanOrEqual(4.5);
      expect(SRC).toContain(`.chip.t-${k} { color: color-mix(in srgb, var(--${k}-color) 70%, var(--text)); }`);
    }
  });

  it('source contract: the badge rules read the badge tokens; no colour literal anywhere in the file', () => {
    const css = styleBlocks(SRC);
    expect(css).toContain('.badge-good { background: var(--badge-good-bg); color: var(--ok-text); }');
    expect(css).toContain('.badge-bad  { background: var(--badge-bad-bg); color: var(--err-text); }');
    expect(SRC).not.toMatch(/#[0-9a-fA-F]{3,8}\b|rgba?\(/);
  });

  it('source contract (jsdom cannot lay out; the 390 px overflow check in the screenshot round is the proof): the header may wrap, and the split\'s cards have min-width: 0', () => {
    const css = styleBlocks(SRC);
    expect(css).toMatch(/\.view-head \{[^}]*flex-wrap: wrap;/);
    expect(css).toContain('.split > * { min-width: 0; }');
  });

  it('source contract (jsdom cannot lay out; the 390 px captures are the proof): under 640 px the funnel bar has a grid row of its own, row metadata wraps by item, a summary row is baseline-aligned', () => {
    const css = styleBlocks(SRC);
    // Squeezed between the two headline numbers, the bar had no width left at 390 px.
    expect(css).toMatch(/@media \(max-width: 640px\) \{[^@]*\.funnel \{[^}]*grid-template-areas: "in out" "bar bar";/);
    expect(css).toMatch(/\.funnel-track \{ grid-area: bar; \}/);
    expect(css).toMatch(/\.rrow-meta \{[^}]*flex-wrap: wrap;/);
    expect(css).toMatch(/\.rrow-meta > \* \{ white-space: nowrap; \}/);
    // A summary's marker aligns with the first line of its row (the row's baseline), not the middle of a wrapped row.
    expect(css).not.toMatch(/\.sum-row \{[^}]*vertical-align: middle/);
  });
});
