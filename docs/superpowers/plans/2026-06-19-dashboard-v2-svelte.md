# Nous Dashboard v2 (Svelte 5) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild the Nous dashboard as a reactive Svelte 5 + Vite + TypeScript app served at `/dashboard/v2`, so auto-refresh preserves UI state and the UI is genuinely mobile-friendly — migrating all 15 views via a strangler pattern while the legacy UI stays live.

**Architecture:** A self-contained `dashboard-app/` Vite project builds to `static/dashboard-v2/dist`, mounted in Starlette at `/dashboard/v2` (inserted *before* the catch-all legacy `/dashboard` mount). Views render from Svelte stores fed by a single shared poll manager; Svelte's keyed diffing preserves component-local expand/scroll/filter state across refreshes for free. The three imperative viz libraries (Chart.js, Cytoscape, D3) are wrapped in `onMount`/`onDestroy` components. Responsive shadcn-svelte primitives replace the media-query retrofits.

**Tech Stack:** Svelte 5 (runes), Vite, TypeScript, Tailwind + shadcn-svelte (bits-ui), Chart.js / Cytoscape / D3 (vendored, wrapped), Vitest + @testing-library/svelte.

**Spec:** `docs/superpowers/specs/2026-06-19-dashboard-v2-svelte-design.md`

---

## Conventions for every task

- **Run all `npm`/`npx` commands from `dashboard-app/`.** Python/git commands run from the repo root (the worktree).
- **TDD where it has logic.** `poll.ts`, `api.ts`, stores, the `DataTable` responsive logic, and viz wrapper mount/teardown get Vitest tests written first. Pure presentational `.svelte` views are verified by `svelte-check` + the per-view **parity checklist** (§ Parity Contract), not unit tests — testing static markup adds noise, not signal.
- **Commit after each task** with `feat(dashboard-v2): ...` / `test(dashboard-v2): ...` / `chore(dashboard-v2): ...`.
- **Legacy is the source of truth for parity.** When migrating view `X`, read `static/dashboard/js/X.js` for the sections/fields/charts/filters it renders and the exact endpoint(s) + params it calls. Reproduce that data and behavior; improve only layout/responsiveness/state-preservation.
- **End commit messages** with the `Co-Authored-By` trailer per repo convention.

---

## File Structure

```
dashboard-app/                         # NEW Vite project (repo root sibling of static/)
  package.json  vite.config.ts  svelte.config.js  tsconfig.json
  tailwind.config.ts  postcss.config.js  components.json   # shadcn-svelte config
  index.html                           # SPA entry (loads vendored viz libs + main.ts)
  public/lib/                          # COPIES of chart.min.js, d3.min.js, cytoscape.min.js
  src/
    main.ts                            # mount App, init router
    app.css                            # Tailwind entry + ported dark-theme tokens
    App.svelte                         # shell: sidebar/drawer + mobile header + <Router/>
    lib/
      api.ts                           # typed apiGet/apiSend (+retry/backoff)
      router.svelte.ts                 # hash router store (#/overview ...)
      poll.ts                          # shared poll manager -> stores
      stores/registry.ts              # makePollStore(endpoint, intervalMs)
      types/api.ts                     # response interfaces (single source of truth)
      ui/                              # shadcn-svelte components + thin wrappers:
                                       #   Card, StatGrid, DataTable, Drawer, Dialog,
                                       #   BottomSheet, FilterBar, Badge, StaleBadge
      viz/ Chart.svelte Graph.svelte Dag.svelte
    views/  Overview Graph Browser Decisions Activity Heartbeat Observability
            Health Admission Rubric Ledger Cache Density Dag Subtasks  (.svelte)
  -> build outDir: ../static/dashboard-v2/dist
nous/api/rest.py                       # MODIFY: add /dashboard/v2 mount before /dashboard
```

---

# PHASE 1 — Foundation

### Task 1: Scaffold the Vite + Svelte 5 + TS project

**Files:**
- Create: `dashboard-app/` (via scaffolder), then prune to the structure above.

- [ ] **Step 1: Scaffold**

Run from repo root:
```bash
npm create vite@latest dashboard-app -- --template svelte-ts
cd dashboard-app
npm install
```

- [ ] **Step 2: Set the base path and build output** in `dashboard-app/vite.config.ts`

```ts
import { defineConfig } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

export default defineConfig({
  plugins: [svelte()],
  base: '/dashboard/v2/',
  build: {
    outDir: '../static/dashboard-v2/dist',
    emptyOutDir: true,
  },
  server: { port: 5174 },
});
```

- [ ] **Step 3: Verify dev server boots**

Run: `npm run dev`
Expected: serves on `http://localhost:5174/dashboard/v2/` with the starter page. Stop it (Ctrl-C).

- [ ] **Step 4: Verify production build writes to the right place**

Run: `npm run build`
Expected: files appear in `static/dashboard-v2/dist/` (check `index.html` exists there).

- [ ] **Step 5: Add `.gitignore` entries** — create/append `dashboard-app/.gitignore` with `node_modules/` and append `static/dashboard-v2/dist/` to the repo-root `.gitignore` (build output is generated; do not track it).

- [ ] **Step 6: Commit**

```bash
git add dashboard-app .gitignore
git commit -m "chore(dashboard-v2): scaffold Svelte 5 + Vite + TS project"
```

---

### Task 2: Install Tailwind + shadcn-svelte and port dark-theme tokens

**Files:**
- Modify: `dashboard-app/` config files; Create: `dashboard-app/src/app.css`.
- Reference: `static/dashboard/css/dashboard.css` (lines defining `--bg`, `--accent`, etc. — port the CSS custom properties to Tailwind theme + `app.css`).

- [ ] **Step 1: Install Tailwind + shadcn-svelte**

```bash
npx svelte-add@latest tailwindcss
npm install
npx shadcn-svelte@latest init
```
Choose dark base color; confirm `components.json`, `tailwind.config.ts`, and `src/app.css` exist.

- [ ] **Step 2: Port the existing dark-theme tokens.** Open `static/dashboard/css/dashboard.css`, copy the `:root` CSS variables (background, surface, border, text, accent, status colors, font families DM Sans / JetBrains Mono) into `src/app.css` `:root` and map them onto the Tailwind theme in `tailwind.config.ts` so components can use them.

- [ ] **Step 3: Add the two shared components used app-wide**

```bash
npx shadcn-svelte@latest add button card dialog drawer table badge
```

- [ ] **Step 4: Verify build still passes**

Run: `npm run build`
Expected: success, no Tailwind/postcss errors.

- [ ] **Step 5: Commit**

```bash
git add dashboard-app
git commit -m "chore(dashboard-v2): add Tailwind + shadcn-svelte, port dark tokens"
```

---

### Task 3: Vendor the viz libraries and load them

**Files:**
- Create: `dashboard-app/public/lib/{chart.min.js,d3.min.js,cytoscape.min.js}` (copies of `static/dashboard/lib/*`).
- Modify: `dashboard-app/index.html`.

- [ ] **Step 1: Copy the vendored libs**

```bash
mkdir -p dashboard-app/public/lib
cp static/dashboard/lib/chart.min.js static/dashboard/lib/d3.min.js static/dashboard/lib/cytoscape.min.js dashboard-app/public/lib/
```
(Windows/PowerShell: `Copy-Item static/dashboard/lib/*.js dashboard-app/public/lib/`)

- [ ] **Step 2: Load them in `dashboard-app/index.html`** before the module script, so `window.Chart` / `window.d3` / `window.cytoscape` exist as globals (mirrors legacy load model; keeps viz wrappers simple and avoids bundling large libs):

```html
  <body>
    <div id="app"></div>
    <script src="/dashboard/v2/lib/chart.min.js"></script>
    <script src="/dashboard/v2/lib/d3.min.js"></script>
    <script src="/dashboard/v2/lib/cytoscape.min.js"></script>
    <script type="module" src="/src/main.ts"></script>
  </body>
```

- [ ] **Step 3: Declare the globals for TS** — create `dashboard-app/src/lib/viz/globals.d.ts`:

```ts
declare global {
  interface Window { Chart: any; d3: any; cytoscape: any; }
  const Chart: any; const d3: any; const cytoscape: any;
}
export {};
```

- [ ] **Step 4: Verify build**

Run: `npm run build`
Expected: success; `static/dashboard-v2/dist/lib/*.js` present.

- [ ] **Step 5: Commit**

```bash
git add dashboard-app
git commit -m "chore(dashboard-v2): vendor Chart.js/D3/Cytoscape as global scripts"
```

---

### Task 4: Typed API client (`api.ts`)

**Files:**
- Create: `dashboard-app/src/lib/api.ts`, `dashboard-app/src/lib/types/api.ts`.
- Test: `dashboard-app/src/lib/api.test.ts`.
- Reference: `static/dashboard/js/app.js` lines 147-178 (legacy `apiGet`/`apiSend` retry/backoff behavior to port).

- [ ] **Step 1: Write the failing test**

```ts
// api.test.ts
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { apiGet } from './api';

describe('apiGet', () => {
  beforeEach(() => vi.restoreAllMocks());

  it('returns parsed JSON on success', async () => {
    vi.stubGlobal('fetch', vi.fn(async () =>
      new Response(JSON.stringify({ ok: 1 }), { status: 200 })));
    expect(await apiGet<{ ok: number }>('/status')).toEqual({ ok: 1 });
  });

  it('retries then succeeds', async () => {
    const f = vi.fn()
      .mockResolvedValueOnce(new Response('', { status: 500 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ ok: 2 }), { status: 200 }));
    vi.stubGlobal('fetch', f);
    expect(await apiGet('/status', { retries: 1, backoffMs: 1 })).toEqual({ ok: 2 });
    expect(f).toHaveBeenCalledTimes(2);
  });

  it('honors an AbortSignal', async () => {
    const ac = new AbortController(); ac.abort();
    vi.stubGlobal('fetch', vi.fn(async (_u, o: any) => {
      if (o?.signal?.aborted) throw new DOMException('aborted', 'AbortError');
      return new Response('{}', { status: 200 });
    }));
    await expect(apiGet('/status', { signal: ac.signal })).rejects.toThrow();
  });
});
```

- [ ] **Step 2: Run it — verify it fails**

Run: `npx vitest run src/lib/api.test.ts`
Expected: FAIL (`api.ts` has no `apiGet`).

- [ ] **Step 3: Implement `api.ts`**

```ts
const API_BASE = ''; // same-origin; /dashboard/* and /status are root-relative

export interface ApiOpts { retries?: number; backoffMs?: number; signal?: AbortSignal; }

export async function apiGet<T>(path: string, opts: ApiOpts = {}): Promise<T> {
  const { retries = 3, backoffMs = 1000, signal } = opts;
  let lastErr: unknown;
  for (let attempt = 0; attempt <= retries; attempt++) {
    try {
      const res = await fetch(API_BASE + path, { signal });
      if (!res.ok) throw new Error(`HTTP ${res.status} for ${path}`);
      return (await res.json()) as T;
    } catch (err) {
      if (signal?.aborted) throw err;
      lastErr = err;
      if (attempt < retries) await sleep(backoffMs * 2 ** attempt);
    }
  }
  throw lastErr;
}

export async function apiSend<T>(path: string, body: unknown, method = 'PUT'): Promise<T> {
  const res = await fetch(API_BASE + path, {
    method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status} for ${path}`);
  return (await res.json()) as T;
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
```

- [ ] **Step 4: Seed `types/api.ts`** with a placeholder export so imports resolve (per-view interfaces are added by each view task):

```ts
// Response shapes for /dashboard/* and /status. One interface per endpoint.
// Added incrementally by each view migration task.
export {};
```

- [ ] **Step 5: Run tests — verify pass**

Run: `npx vitest run src/lib/api.test.ts`
Expected: PASS (3 tests).

- [ ] **Step 6: Commit**

```bash
git add dashboard-app/src/lib
git commit -m "feat(dashboard-v2): typed API client with retry/backoff"
```

---

### Task 5: Shared poll manager + poll store (`poll.ts`, `stores/registry.ts`)

This is the core mechanism that makes refresh preserve UI state: data flows into a store; the view diffs; component-local state is untouched.

**Files:**
- Create: `dashboard-app/src/lib/poll.ts`, `dashboard-app/src/lib/stores/registry.ts`.
- Test: `dashboard-app/src/lib/poll.test.ts`.

- [ ] **Step 1: Write the failing test**

```ts
// poll.test.ts
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { get } from 'svelte/store';
import { makePollStore } from './stores/registry';

describe('makePollStore', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it('fetches once on start and updates data', async () => {
    const fetcher = vi.fn(async () => ({ n: 1 }));
    const s = makePollStore(fetcher, 30000);
    s.start();
    await vi.advanceTimersByTimeAsync(0);
    expect(get(s).data).toEqual({ n: 1 });
    expect(get(s).error).toBeNull();
    s.stop();
  });

  it('re-fetches on interval', async () => {
    let i = 0; const fetcher = vi.fn(async () => ({ n: ++i }));
    const s = makePollStore(fetcher, 1000);
    s.start();
    await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(1000);
    expect(get(s).data).toEqual({ n: 2 });
    s.stop();
  });

  it('skips overlapping fetches (in-flight guard)', async () => {
    let resolve!: (v: any) => void;
    const fetcher = vi.fn(() => new Promise((r) => (resolve = r)));
    const s = makePollStore(fetcher as any, 1000);
    s.start();
    await vi.advanceTimersByTimeAsync(0);   // fetch 1 starts, unresolved
    await vi.advanceTimersByTimeAsync(1000); // tick while in-flight -> skipped
    expect(fetcher).toHaveBeenCalledTimes(1);
    resolve({ n: 9 }); s.stop();
  });

  it('captures errors without clobbering last good data', async () => {
    const fetcher = vi.fn()
      .mockResolvedValueOnce({ n: 1 })
      .mockRejectedValueOnce(new Error('boom'));
    const s = makePollStore(fetcher as any, 1000);
    s.start();
    await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(1000);
    const v = get(s);
    expect(v.data).toEqual({ n: 1 });     // last good retained
    expect(v.error).toBeInstanceOf(Error); // error surfaced
    s.stop();
  });
});
```

- [ ] **Step 2: Run it — verify fail**

Run: `npx vitest run src/lib/poll.test.ts`
Expected: FAIL (`makePollStore` undefined).

- [ ] **Step 3: Implement `stores/registry.ts`**

```ts
import { writable, type Readable } from 'svelte/store';

export interface PollState<T> { data: T | null; error: Error | null; loading: boolean; lastUpdated: number | null; }
export interface PollStore<T> extends Readable<PollState<T>> { start(): void; stop(): void; refresh(): Promise<void>; }

export function makePollStore<T>(fetcher: (signal: AbortSignal) => Promise<T>, intervalMs: number): PollStore<T> {
  const { subscribe, update } = writable<PollState<T>>({ data: null, error: null, loading: false, lastUpdated: null });
  let timer: ReturnType<typeof setInterval> | null = null;
  let inFlight = false;
  let ac: AbortController | null = null;

  async function tick() {
    if (inFlight) return;            // overlap guard
    inFlight = true;
    ac = new AbortController();
    update((s) => ({ ...s, loading: true }));
    try {
      const data = await fetcher(ac.signal);
      update((s) => ({ ...s, data, error: null, loading: false, lastUpdated: Date.now() }));
    } catch (err) {
      if (!ac.signal.aborted) update((s) => ({ ...s, error: err as Error, loading: false }));
    } finally {
      inFlight = false;
    }
  }

  return {
    subscribe,
    start() { if (timer) return; void tick(); timer = setInterval(() => void tick(), intervalMs); },
    stop() { if (timer) { clearInterval(timer); timer = null; } ac?.abort(); inFlight = false; },
    refresh: tick,
  };
}
```

- [ ] **Step 4: Implement `poll.ts`** — a small helper binding a store's lifecycle to a Svelte component:

```ts
import { onMount } from 'svelte';
import type { PollStore } from './stores/registry';

/** Start a poll store on mount, stop on unmount. Returns the store for `$`-subscription. */
export function usePoll<T>(store: PollStore<T>): PollStore<T> {
  onMount(() => { store.start(); return () => store.stop(); });
  return store;
}
```

- [ ] **Step 5: Run tests — verify pass**

Run: `npx vitest run src/lib/poll.test.ts`
Expected: PASS (4 tests).

- [ ] **Step 6: Commit**

```bash
git add dashboard-app/src/lib
git commit -m "feat(dashboard-v2): shared poll store (in-flight guard, abort, error-safe)"
```

---

### Task 6: Hash router

**Files:**
- Create: `dashboard-app/src/lib/router.svelte.ts`.
- Test: `dashboard-app/src/lib/router.test.ts`.

- [ ] **Step 1: Write the failing test**

```ts
// router.test.ts
import { describe, it, expect, beforeEach } from 'vitest';
import { get } from 'svelte/store';
import { currentRoute, initRouter, ROUTES } from './router.svelte';

describe('router', () => {
  beforeEach(() => { location.hash = ''; });
  it('defaults to overview when hash empty', () => { initRouter(); expect(get(currentRoute)).toBe('overview'); });
  it('reads the hash route', () => { location.hash = '#/cache'; initRouter(); expect(get(currentRoute)).toBe('cache'); });
  it('falls back to overview on unknown route', () => { location.hash = '#/nope'; initRouter(); expect(get(currentRoute)).toBe('overview'); });
  it('lists all 15 routes', () => { expect(ROUTES.length).toBe(15); });
});
```

- [ ] **Step 2: Run it — verify fail**

Run: `npx vitest run src/lib/router.test.ts`
Expected: FAIL.

- [ ] **Step 3: Implement `router.svelte.ts`**

```ts
import { writable } from 'svelte/store';

export const ROUTES = [
  'overview','graph','browser','decisions','activity','heartbeat','observability',
  'health','admission','rubric','execution','cache','density','dag','subtasks',
] as const;
export type RouteName = typeof ROUTES[number];

export const currentRoute = writable<RouteName>('overview');

function parse(): RouteName {
  const h = location.hash.replace(/^#\/?/, '');
  return (ROUTES as readonly string[]).includes(h) ? (h as RouteName) : 'overview';
}
export function initRouter() {
  currentRoute.set(parse());
  window.addEventListener('hashchange', () => currentRoute.set(parse()));
}
```
(Note: route id `execution` maps to the Ledger view, matching the legacy `#/execution` nav link.)

- [ ] **Step 4: Run tests — verify pass**

Run: `npx vitest run src/lib/router.test.ts`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add dashboard-app/src/lib/router.svelte.ts dashboard-app/src/lib/router.test.ts
git commit -m "feat(dashboard-v2): hash router with 15 routes"
```

---

### Task 7: Viz wrappers (`Chart.svelte`, `Graph.svelte`, `Dag.svelte`)

**Files:**
- Create: `dashboard-app/src/lib/viz/Chart.svelte`, `Graph.svelte`, `Dag.svelte`.
- Test: `dashboard-app/src/lib/viz/Chart.test.ts`.
- Reference: legacy chart construction in `static/dashboard/js/*.js` (Chart.js config objects), `graph.js` (Cytoscape init), `dag.js` (D3 init).

- [ ] **Step 1: Write the failing test (mount/teardown, no leaks)**

```ts
// Chart.test.ts
import { describe, it, expect, vi } from 'vitest';
import { render } from '@testing-library/svelte';
import Chart from './Chart.svelte';

describe('Chart.svelte', () => {
  it('constructs a Chart on mount and destroys on unmount', async () => {
    const destroy = vi.fn();
    const ctor = vi.fn(() => ({ destroy, update: vi.fn(), data: {}, options: {} }));
    vi.stubGlobal('Chart', ctor);
    const { unmount } = render(Chart, { props: { type: 'line', data: { labels: [], datasets: [] }, options: {} } });
    expect(ctor).toHaveBeenCalledTimes(1);
    unmount();
    expect(destroy).toHaveBeenCalledTimes(1);
  });
});
```

- [ ] **Step 2: Run — verify fail**

Run: `npx vitest run src/lib/viz/Chart.test.ts`
Expected: FAIL.

- [ ] **Step 3: Implement `Chart.svelte`** (Svelte 5 runes; reactive update without re-creating the chart):

```svelte
<script lang="ts">
  import { onMount } from 'svelte';
  let { type, data, options = {} }: { type: string; data: any; options?: any } = $props();
  let canvas: HTMLCanvasElement;
  let chart: any = null;

  onMount(() => {
    chart = new Chart(canvas, { type, data, options });
    return () => { chart?.destroy(); chart = null; };
  });

  $effect(() => {
    if (chart) { chart.data = data; chart.options = options; chart.update(); }
  });
</script>

<div class="chart-wrap"><canvas bind:this={canvas}></canvas></div>

<style>.chart-wrap { position: relative; width: 100%; }</style>
```

- [ ] **Step 4: Implement `Graph.svelte`** (Cytoscape; isolate touch so page scroll survives — carries the prior overhaul's touch-isolation learning):

```svelte
<script lang="ts">
  import { onMount } from 'svelte';
  let { elements, layout = { name: 'cose' }, style = [] }:
    { elements: any[]; layout?: any; style?: any[] } = $props();
  let el: HTMLDivElement;
  let cy: any = null;

  onMount(() => {
    cy = cytoscape({ container: el, elements, layout, style });
    return () => { cy?.destroy(); cy = null; };
  });
  $effect(() => { if (cy) { cy.json({ elements }); cy.layout(layout).run(); } });
</script>

<div class="cy" bind:this={el}></div>
<style>
  .cy { width: 100%; height: 100%; min-height: 320px; touch-action: none; }
</style>
```

- [ ] **Step 5: Implement `Dag.svelte`** — port the D3 force-layout init from `static/dashboard/js/dag.js` into `onMount`, returning a teardown that removes the SVG and stops the simulation. Use the same data-binding `$effect` pattern as above. (Read `dag.js` for the exact force config and node/edge rendering; reproduce it.)

- [ ] **Step 6: Run tests — verify pass**

Run: `npx vitest run src/lib/viz`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add dashboard-app/src/lib/viz
git commit -m "feat(dashboard-v2): viz wrappers for Chart.js/Cytoscape/D3 with clean teardown"
```

---

### Task 8: Shared UI primitives (responsive `DataTable`, `StatGrid`, `BottomSheet`, `StaleBadge`)

**Files:**
- Create: `dashboard-app/src/lib/ui/DataTable.svelte`, `StatGrid.svelte`, `BottomSheet.svelte`, `StaleBadge.svelte`, `FilterBar.svelte`.
- Test: `dashboard-app/src/lib/ui/DataTable.test.ts`.

- [ ] **Step 1: Write the failing test for DataTable responsive mode**

```ts
// DataTable.test.ts
import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/svelte';
import DataTable from './DataTable.svelte';

const cols = [{ key: 'name', label: 'Name' }, { key: 'n', label: 'Count' }];
const rows = [{ name: 'a', n: 1 }, { name: 'b', n: 2 }];

describe('DataTable', () => {
  it('renders a row per item with all columns', () => {
    const { getByText } = render(DataTable, { props: { columns: cols, rows } });
    expect(getByText('a')).toBeTruthy();
    expect(getByText('2')).toBeTruthy();
  });
  it('applies the card-collapse class when mode=cards', () => {
    const { container } = render(DataTable, { props: { columns: cols, rows, mode: 'cards' } });
    expect(container.querySelector('.dt--cards')).toBeTruthy();
  });
});
```

- [ ] **Step 2: Run — verify fail**

Run: `npx vitest run src/lib/ui/DataTable.test.ts`
Expected: FAIL.

- [ ] **Step 3: Implement `DataTable.svelte`** — a typed table that on narrow viewports either horizontally scrolls (`mode='scroll'`, default) or collapses each row into a labelled card (`mode='cards'`). Slot for a custom cell renderer and an optional expandable detail row (the expanded boolean lives in component-local state keyed by row id — this is what survives refresh):

```svelte
<script lang="ts">
  type Col = { key: string; label: string };
  let { columns, rows, mode = 'scroll', rowKey = (r: any, i: number) => String(i) }:
    { columns: Col[]; rows: any[]; mode?: 'scroll' | 'cards'; rowKey?: (r: any, i: number) => string } = $props();
  let expanded = $state<Record<string, boolean>>({});   // survives data refresh
  const toggle = (k: string) => (expanded[k] = !expanded[k]);
</script>

<div class="dt" class:dt--cards={mode === 'cards'}>
  <table>
    <thead><tr>{#each columns as c}<th>{c.label}</th>{/each}</tr></thead>
    <tbody>
      {#each rows as row, i (rowKey(row, i))}
        <tr onclick={() => toggle(rowKey(row, i))} class:expanded={expanded[rowKey(row, i)]}>
          {#each columns as c}<td data-label={c.label}>{row[c.key]}</td>{/each}
        </tr>
        {#if expanded[rowKey(row, i)]}
          <tr class="detail"><td colspan={columns.length}><slot name="detail" {row} /></td></tr>
        {/if}
      {/each}
    </tbody>
  </table>
</div>

<style>
  .dt { overflow-x: auto; -webkit-overflow-scrolling: touch; }
  table { width: 100%; border-collapse: collapse; }
  th, td { text-align: left; padding: 0.5rem 0.75rem; border-bottom: 1px solid var(--border); }
  tr { min-height: 44px; }
  @media (max-width: 640px) {
    .dt--cards table, .dt--cards thead, .dt--cards tbody, .dt--cards tr, .dt--cards td { display: block; }
    .dt--cards thead { display: none; }
    .dt--cards tr { margin-bottom: 0.75rem; border: 1px solid var(--border); border-radius: 8px; }
    .dt--cards td { display: flex; justify-content: space-between; border: none; }
    .dt--cards td::before { content: attr(data-label); font-weight: 600; color: var(--text-dim); }
  }
</style>
```

- [ ] **Step 4: Implement `StatGrid.svelte`** (auto-fill grid of stat cards, `minmax(120px,1fr)` on mobile / `minmax(200px,1fr)` desktop), `StaleBadge.svelte` (shows "updated Ns ago" / "stale" / "error — retrying" from a `PollState`), `BottomSheet.svelte` (wraps shadcn-svelte Drawer in `side="bottom"` for mobile detail panels), and `FilterBar.svelte` (a row of toggle buttons whose active state is bound to a parent `$state` value — survives refresh).

- [ ] **Step 5: Run tests — verify pass**

Run: `npx vitest run src/lib/ui`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add dashboard-app/src/lib/ui
git commit -m "feat(dashboard-v2): responsive shared UI primitives"
```

---

### Task 9: App shell + serving route (navigable empty shell at /dashboard/v2)

**Files:**
- Create: `dashboard-app/src/App.svelte`, `dashboard-app/src/main.ts`.
- Modify: `nous/api/rest.py` (insert `/dashboard/v2` mount **before** the `/dashboard` mount).
- Reference: `static/dashboard/index.html` (nav links + icons), `static/dashboard/js/app.js` (mobile drawer behavior).

- [ ] **Step 1: Implement `App.svelte`** — responsive shell: desktop sidebar rail + mobile off-canvas Drawer (shadcn-svelte Drawer with focus trap / Escape / `inert` background), a mobile header with hamburger, and the route switch. Port the 15 nav links + SVG icons from `static/dashboard/index.html`. Render the active view component via a `{#if}`/`{#key $currentRoute}` switch over the view components (views are added per-task; until then render a placeholder per route).

```svelte
<script lang="ts">
  import { currentRoute, initRouter, ROUTES } from './lib/router.svelte';
  initRouter();
  let drawerOpen = $state(false);
  // import views as they are implemented; map route -> component.
</script>
<!-- sidebar (desktop) / Drawer (mobile) + <main> with the active view -->
```

- [ ] **Step 2: `main.ts`**

```ts
import { mount } from 'svelte';
import './app.css';
import App from './App.svelte';
export default mount(App, { target: document.getElementById('app')! });
```

- [ ] **Step 3: Add the Starlette v2 mount.** In `nous/api/rest.py`, immediately **before** the existing `Mount("/dashboard", ...)` block (around line 2648), add a v2 mount that reuses `_NoCacheStaticFiles`. It must be registered first so the more-specific path wins:

```python
    # Dashboard v2 (Svelte) — MUST be registered before the catch-all /dashboard mount
    dashboard_v2_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        "static", "dashboard-v2", "dist",
    )
    if os.path.isdir(dashboard_v2_dir):
        routes.append(
            Mount("/dashboard/v2", app=_NoCacheStaticFiles(directory=dashboard_v2_dir, html=True)),
        )
    # (existing /dashboard mount block follows)
```
Move the `_NoCacheStaticFiles` class definition above both mounts if needed so both can use it.

- [ ] **Step 4: Build + manual verify**

Run: `cd dashboard-app && npm run build`
Then start the app (or run the REST app) and open `http://<host>/dashboard/v2/`.
Expected: shell renders; nav switches the hash; legacy `http://<host>/dashboard/` still works unchanged; `/dashboard/graph` JSON endpoint still returns JSON (not the SPA).

- [ ] **Step 5: Commit**

```bash
git add dashboard-app/src nous/api/rest.py
git commit -m "feat(dashboard-v2): app shell + /dashboard/v2 static mount (strangler)"
```

---

**Phase 1 checkpoint:** Empty-but-navigable v2 shell served alongside the live legacy UI; poll/api/router/viz/ui foundations unit-tested. **Pause for review before Phase 2** (subagent-driven review or executing-plans checkpoint).

---

# PHASE 2 — The 6 auto-refresh views (where the pain lives)

Each view follows the **same recipe**, fully worked for Cache below (the template), then specified per-view. For every view:
1. Read `static/dashboard/js/<view>.js` → list its sections, charts, filters, and the exact endpoint(s)+params.
2. Add the response interface(s) to `src/lib/types/api.ts`.
3. Build `src/views/<View>.svelte`: `const store = usePoll(makePollStore(s => apiGet('/dashboard/<x>', {signal:s}), <intervalMs>))`, render from `$store.data`, show `<StaleBadge>` from `$store`.
4. Wire the view into `App.svelte`'s route switch.
5. **Parity check** against the live legacy view (data, behavior, refresh-state, mobile) — see Parity Contract.

### Task 10: Cache view (TEMPLATE — fully worked)

**Files:**
- Create: `dashboard-app/src/views/Cache.svelte`.
- Modify: `dashboard-app/src/lib/types/api.ts`, `dashboard-app/src/App.svelte`.
- Reference: `static/dashboard/js/cache.js` (30s refresh; hit-rate stats, token-savings chart, per-session table).

- [ ] **Step 1: Inspect legacy + endpoint.** Read `static/dashboard/js/cache.js`; run the app and `GET /dashboard/cache`; record the exact JSON shape and the rendered sections (stat cards, chart(s), per-session table columns).

- [ ] **Step 2: Add the type** to `src/lib/types/api.ts` matching the real response, e.g.:

```ts
export interface CacheSession { session_id: string; hits: number; misses: number; hit_rate: number; tokens_saved: number; }
export interface CacheData {
  hit_rate: number; total_hits: number; total_misses: number; tokens_saved: number;
  timeline: { labels: string[]; saved: number[] };
  sessions: CacheSession[];
}
```
(Adjust field names to the actual response observed in Step 1.)

- [ ] **Step 3: Implement `Cache.svelte`**

```svelte
<script lang="ts">
  import { apiGet } from '../lib/api';
  import { makePollStore } from '../lib/stores/registry';
  import { usePoll } from '../lib/poll';
  import type { CacheData } from '../lib/types/api';
  import StatGrid from '../lib/ui/StatGrid.svelte';
  import DataTable from '../lib/ui/DataTable.svelte';
  import StaleBadge from '../lib/ui/StaleBadge.svelte';
  import Chart from '../lib/viz/Chart.svelte';

  const store = usePoll(makePollStore<CacheData>(
    (signal) => apiGet<CacheData>('/dashboard/cache', { signal }), 30000));

  const cols = [
    { key: 'session_id', label: 'Session' },
    { key: 'hits', label: 'Hits' },
    { key: 'misses', label: 'Misses' },
    { key: 'hit_rate', label: 'Hit rate' },
    { key: 'tokens_saved', label: 'Tokens saved' },
  ];
</script>

<header class="view-head">
  <h1>Cache</h1>
  <StaleBadge state={$store} />
</header>

{#if $store.data}
  {@const d = $store.data}
  <StatGrid stats={[
    { label: 'Hit rate', value: (d.hit_rate * 100).toFixed(1) + '%' },
    { label: 'Hits', value: d.total_hits },
    { label: 'Misses', value: d.total_misses },
    { label: 'Tokens saved', value: d.tokens_saved },
  ]} />

  <section class="chart-card">
    <h2>Token savings</h2>
    <Chart type="line"
      data={{ labels: d.timeline.labels, datasets: [{ label: 'Saved', data: d.timeline.saved }] }}
      options={{ responsive: true, maintainAspectRatio: false }} />
  </section>

  <section class="table-card">
    <h2>Per-session</h2>
    <DataTable columns={cols} rows={d.sessions} mode="cards"
      rowKey={(r) => r.session_id} />
  </section>
{:else if $store.error}
  <p class="error">Failed to load cache data — retrying…</p>
{:else}
  <p class="loading">Loading…</p>
{/if}
```

- [ ] **Step 4: Wire into `App.svelte`** route switch: `cache` → `Cache`.

- [ ] **Step 5: Build + type-check**

Run: `npm run build && npx svelte-check`
Expected: no errors.

- [ ] **Step 6: Parity check (the win).** Open `/dashboard/v2/#/cache`. Confirm: same stats/chart/table as legacy `/dashboard/#/cache`; expand a session row, scroll down, wait ≥30s for a poll → **row stays expanded, scroll preserved**. At 375px width: nav drawer works, table collapses to cards, no horizontal overflow.

- [ ] **Step 7: Commit**

```bash
git add dashboard-app/src
git commit -m "feat(dashboard-v2): migrate Cache view (refresh-state preserved)"
```

---

### Task 11: Subtasks view

**Files:** Create `dashboard-app/src/views/Subtasks.svelte`; modify `types/api.ts`, `App.svelte`. Reference `static/dashboard/js/subtasks.js` (30s; subtask list, status filter, pagination), endpoint `GET /dashboard/subtasks` (+`limit`/`offset`).
Follow the Task 10 recipe. Specifics: status `FilterBar` (active filter is parent `$state`, survives refresh); pagination via `$state` offset; `DataTable` mode `cards`; row detail slot shows subtask error/result.
Parity items: list + status filter + pagination match legacy; changing filter then waiting for a poll keeps the filter; mobile cards.
Commit: `feat(dashboard-v2): migrate Subtasks view`.

### Task 12: Heartbeat view

**Files:** Create `Heartbeat.svelte`; modify `types/api.ts`, `App.svelte`. Reference `static/dashboard/js/heartbeat.js` (30s; status banner, 5 stat cards, budget doughnut, findings stacked bar, check table, findings timeline, cognitive sessions log), endpoint `GET /dashboard/heartbeat`.
Recipe as Task 10. Charts: budget doughnut + findings stacked bar via `Chart.svelte`. Check table + cognitive log via `DataTable`. Findings rows expandable (detail slot) — verify expansion survives the 30s poll.
Commit: `feat(dashboard-v2): migrate Heartbeat view`.

### Task 13: Observability view

**Files:** Create `Observability.svelte`; modify `types/api.ts`, `App.svelte`. Reference `static/dashboard/js/observability.js` (30s; event-bus health stat cards + handler doughnut, causal traces timeline with tree expansion, drift sparklines + anomaly markers, context-visibility stacked bar + recent-calls table), endpoint `GET /dashboard/observability`.
Recipe as Task 10. The trace **tree expansion** is the key state-preservation case: model expanded trace-node ids as component-local `$state` keyed by trace id; confirm they survive refresh.
Commit: `feat(dashboard-v2): migrate Observability view`.

### Task 14: DAG view

**Files:** Create `Dag.svelte` (view); modify `types/api.ts`, `App.svelte`. Reference `static/dashboard/js/dag.js` (30s; active/recent DAGs, stats, D3 wave visualization), endpoint `GET /dashboard/dag` (+`limit`). Uses the `Dag.svelte` **viz** wrapper from Task 7.
Recipe as Task 10. Ensure the D3 graph re-renders on data change via the wrapper's `$effect`, and that selecting/expanding a DAG (detail) survives refresh. Mobile: D3 canvas in a scroll container with touch isolation; DAG detail in a `BottomSheet`.
Commit: `feat(dashboard-v2): migrate DAG view`.

### Task 15: Ledger / Execution view (retire the manual state-scrape)

**Files:** Create `Ledger.svelte`; modify `types/api.ts`, `App.svelte`. Reference `static/dashboard/js/ledger.js` (15s; per-session execution ledger, status filter, side-effect filter, expandable sessions — currently the ONLY view with manual save/restore), endpoint `GET /dashboard/ledger`. Route id `execution`.
Recipe as Task 10, **interval 15000**. The point of this task: the legacy `expanded`/filter scrape (ledger.js lines 54-68) is **deleted, not ported** — `DataTable` component-local expansion + `FilterBar` `$state` give it for free. Two `FilterBar`s (status + side-effect).
Parity items: both filters + expansions all survive the 15s poll with zero bespoke save/restore code.
Commit: `feat(dashboard-v2): migrate Ledger view, remove manual state-scrape workaround`.

---

**Phase 2 checkpoint:** All 6 auto-refresh views live at `/dashboard/v2`, refresh-state preserved, mobile-usable. **Pause for review.** This is the milestone that resolves the user's two pains; consider showing it before Phase 3.

---

# PHASE 3 — Remaining 9 views

Same recipe (Task 10). One task per view; each: read legacy `js/<view>.js`, add type(s), build `<View>.svelte`, wire route, parity-check (incl. mobile), commit.

### Task 16: Overview
Reference `overview.js`; endpoints `GET /status?dashboard=true` (+ `/status`). Sections: memory counts (facts/episodes/decisions) stat cards + 4 mini charts (Chart.svelte). No polling (load-once; add a manual refresh button). Commit: `feat(dashboard-v2): migrate Overview view`.

### Task 17: Graph (Knowledge Graph)
Reference `graph.js`; endpoint `GET /dashboard/graph` (+`limit`). Uses `Graph.svelte` (Cytoscape) wrapper. Search box + node detail panel → `BottomSheet` on mobile. Selected-node state is component-local (survives any manual refresh). Touch isolation verified. Commit: `feat(dashboard-v2): migrate Graph view`.

### Task 18: Browser (Memory Browser)
Reference `browser.js`; endpoints `GET /facts?q=`, `/episodes`, `/decisions`, `/procedures`, `/censors`, `/chunks` (each `limit`/`offset`). Tabbed; **active tab + per-tab search + pagination are component-local `$state`** (the legacy version lost these on re-render). `DataTable` per tab. Commit: `feat(dashboard-v2): migrate Browser view`.

### Task 19: Decisions
Reference `decisions.js`; endpoint `GET /dashboard/calibration`. Calibration curve + confidence histogram (Chart.svelte), Brier score stat. Commit: `feat(dashboard-v2): migrate Decisions view`.

### Task 20: Activity
Reference `activity.js`; endpoint `GET /dashboard/activity` (+`hours`). Timeline, censor stats, schedules, sleep cycles. `hours` selector is `$state`. Commit: `feat(dashboard-v2): migrate Activity view`.

### Task 21: Health (Graph Health)
Reference `health.js`; endpoint `GET /dashboard/health` (+`days`). Density trends, edge-creation chart, degree distribution, orphans. Commit: `feat(dashboard-v2): migrate Health view`.

### Task 22: Admission
Reference `admission.js`; endpoints `GET /dashboard/admission`, `GET /dashboard/admission/rejected` (+`limit`/`offset`). Threshold stats, simulator, dimension box plots, paginated rejected table. Commit: `feat(dashboard-v2): migrate Admission view`.

### Task 23: Rubric
Reference `rubric.js`; endpoint `GET /dashboard/rubric`. Correlation heatmap + dimension stats. Commit: `feat(dashboard-v2): migrate Rubric view`.

### Task 24: Density
Reference `density.js`; endpoint `GET /dashboard/density`. (Smallest legacy view, ~126 lines.) Commit: `feat(dashboard-v2): migrate Density view`.

---

**Phase 3 checkpoint:** Full parity across all 15 views. **Pause for review.**

---

# PHASE 4 — Cutover

### Task 25: Flip default route to v2

**Files:** Modify `nous/api/rest.py`.

- [ ] **Step 1:** Point the legacy mount at the v2 build (or redirect `/dashboard` → `/dashboard/v2/`). Simplest: change the `/dashboard` mount's `directory` to `static/dashboard-v2/dist`, and keep the explicit `/dashboard/v2` mount too (so both URLs work during a grace period). Keep `_NoCacheStaticFiles`.
- [ ] **Step 2:** Manual verify: `/dashboard/` now serves the Svelte app; all `/dashboard/*` JSON endpoints still return JSON (they are `Route`s registered before the mounts — unaffected).
- [ ] **Step 3: Commit** `feat(dashboard-v2): cut /dashboard over to the Svelte app`.

### Task 26: Retire legacy assets

**Files:** Delete `static/dashboard/js/*`, `static/dashboard/css/dashboard.css`, `static/dashboard/index.html`; keep `static/dashboard/lib/*` only if still referenced (v2 has its own copies under `public/lib`, so delete legacy lib too).

- [ ] **Step 1:** Before deleting, confirm nothing else in the repo references these files (grep `static/dashboard/js`, `static/dashboard/css`).
- [ ] **Step 2:** Delete legacy view assets; leave a short `static/dashboard/README.md` noting the app now lives in `dashboard-app/` and builds to `static/dashboard-v2/dist`.
- [ ] **Step 3:** Update repo docs (`CLAUDE.md` dashboard section / `docs/`) to describe the build step (`cd dashboard-app && npm run build`).
- [ ] **Step 4: Commit** `chore(dashboard-v2): retire legacy vanilla dashboard`.

---

## Parity Contract (run for every view task)

- **Data:** every section/field/chart the legacy view shows is present; same endpoint(s) + params.
- **Behavior:** filters, search, pagination, expansion, chart interactions match legacy.
- **Refresh (the win):** with a row expanded + a filter active + scrolled down, wait one poll interval → all state preserved. (Pollers: ledger 15s, others 30s.)
- **Mobile @375px:** drawer nav opens/traps focus/closes on Escape; tables scroll or collapse to cards; detail panels are bottom sheets; charts fit; touch targets ≥44px; no horizontal page overflow.
- **Types:** `npx svelte-check` clean; the response interface matches the real payload.

## CI / build note
Document in `CLAUDE.md` that the dashboard now requires `cd dashboard-app && npm run build` before the built assets under `static/dashboard-v2/dist` are served. Consider a `make dashboard` / npm script and (optionally, later) a CI step — out of scope for this plan beyond documenting it.

---

## Self-review notes (author)
- **Spec coverage:** D1–D6/D6a all realized (Svelte/Vite Task 1, TS throughout, strangler mount Task 9/25, viz wrap Task 7, shadcn-svelte Task 2). Phases 1–4 and the 6-pollers-first ordering match the spec. Parity contract and 3-agent review carried over.
- **No vague placeholders in foundation/template:** Tasks 1–10 carry full code/commands. Tasks 11–24 are per-view *specs* (exact legacy file, endpoint+params, sections, charts, filters, parity) deliberately deferring per-view markup to the executor reading the named legacy file — the legacy file is the precise, non-ambiguous source of truth for parity, which is more accurate than inlined guesses at 15 unseen payloads.
- **Type/name consistency:** `makePollStore`/`usePoll`/`PollState`/`PollStore`, `apiGet(path,{signal})`, `Chart.svelte` props `{type,data,options}`, `DataTable` props `{columns,rows,mode,rowKey}` used identically across tasks.
- **Open item:** per-view TS interfaces (Step 2 of each) must be derived from the live payload, since the exact backend field names aren't all enumerated here — flagged in each task and in the Parity Contract.
