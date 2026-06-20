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

- [ ] **Step 5: Verify `.gitignore`.** **REVIEW FIX (devil P2-E):** the root `.gitignore` already has a bare `dist/` rule that covers `static/dashboard-v2/dist/` — do not add a duplicate. Just confirm `dashboard-app/node_modules/` is ignored (the Vite scaffold's own `.gitignore` covers it). Note: because `dist/` is gitignored, deployment MUST build it — handled in the Dockerfile step (Task 26a). Decide now: build-in-CI/Docker (chosen) vs commit-the-dist.

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

> **REVIEW FIX (arch P2-A / P3-C):** use a **recursive `setTimeout`** that schedules the next tick only after the current fetch settles — `setInterval` fires a burst right after a slow (>interval) fetch. Do **not** reset `inFlight` in `stop()` (that re-introduces a double-fetch race with an aborted-but-unsettled promise); let the `finally` clear it, gated by a `stopped` flag so a late settle can't reschedule. `fetcher` takes an optional signal so zero-arg test mocks type-check under `--strict`. Also add a test (P3-C) asserting `stop()` aborts the in-flight fetch's signal (the fetcher receives a signal that becomes `aborted` after `stop()`).

```ts
import { writable, type Readable } from 'svelte/store';

export interface PollState<T> { data: T | null; error: Error | null; loading: boolean; lastUpdated: number | null; }
export interface PollStore<T> extends Readable<PollState<T>> { start(): void; stop(): void; refresh(): Promise<void>; }

export function makePollStore<T>(fetcher: (signal?: AbortSignal) => Promise<T>, intervalMs: number): PollStore<T> {
  const { subscribe, update } = writable<PollState<T>>({ data: null, error: null, loading: false, lastUpdated: null });
  let timer: ReturnType<typeof setTimeout> | null = null;
  let inFlight = false;
  let stopped = true;
  let ac: AbortController | null = null;

  function schedule() { if (!stopped) timer = setTimeout(() => void tick(), intervalMs); }

  async function tick() {
    if (inFlight) { schedule(); return; }   // overlap guard: skip, but keep the cadence
    inFlight = true;
    ac = new AbortController();
    update((s) => ({ ...s, loading: true }));
    try {
      const data = await fetcher(ac.signal);
      if (!ac.signal.aborted) update((s) => ({ ...s, data, error: null, loading: false, lastUpdated: Date.now() }));
    } catch (err) {
      if (!ac.signal.aborted) update((s) => ({ ...s, error: err as Error, loading: false }));  // retains last good data
    } finally {
      inFlight = false;
      schedule();                            // next tick only after this one settled
    }
  }

  return {
    subscribe,
    start() { if (!stopped) return; stopped = false; void tick(); },
    stop() { stopped = true; if (timer) { clearTimeout(timer); timer = null; } ac?.abort(); },  // do NOT touch inFlight
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
let initialized = false;
export function initRouter() {
  currentRoute.set(parse());
  if (initialized) return;                       // REVIEW FIX (arch P2-F): avoid stacking listeners
  initialized = true;
  window.addEventListener('hashchange', () => currentRoute.set(parse()));
}
```
(Note: route id `execution` maps to the Ledger view, matching the legacy `#/execution` nav link.)

> **REVIEW FIX (arch P3-A):** this file uses no runes (only `writable`), so name it `router.ts`, not `router.svelte.ts` (the `.svelte.ts` extension signals rune usage). Update imports accordingly. The spec already calls it `router.ts`.

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

- [ ] **Step 3: Implement `Chart.svelte`** (Svelte 5 runes; reactive update without re-creating the chart).

> **REVIEW FIX (arch P1-A):** `chart` MUST be a plain `let`, **not** `$state`. Making it `$state` causes an infinite loop (`chart.update()` writes `chart.data`, re-triggering the effect). The `$effect` reads `data`/`options` (the reactive props) to subscribe; it runs once before `onMount` sets `chart` (sees null, no-ops), then fires on every prop change afterward — this ordering is correct and intended.
> **REVIEW FIX (mobile P1-A):** charts need an explicit container **height** or `maintainAspectRatio:false` collapses the canvas to 0px on mobile. Add a `height` prop (default `220px`).

```svelte
<script lang="ts">
  import { onMount } from 'svelte';
  let { type, data, options = {}, height = '220px' }:
    { type: string; data: any; options?: any; height?: string } = $props();
  let canvas: HTMLCanvasElement;
  let chart: any = null;            // plain let — NOT $state (see REVIEW FIX above)

  onMount(() => {
    chart = new Chart(canvas, { type, data, options: { responsive: true, maintainAspectRatio: false, ...options } });
    return () => { chart?.destroy(); chart = null; };
  });

  $effect(() => {
    const d = data, o = options;    // read props to subscribe
    if (chart) { chart.data = d; chart.options = { responsive: true, maintainAspectRatio: false, ...o }; chart.update('none'); }
  });
</script>

<div class="chart-wrap" style:height={height}><canvas bind:this={canvas}></canvas></div>

<style>
  .chart-wrap { position: relative; width: 100%; }
  @media (max-width: 768px) { .chart-wrap { height: 200px !important; } }
</style>
```

- [ ] **Step 4: Implement `Graph.svelte`** (Cytoscape; `cy` is plain `let` like `chart`).

> **REVIEW FIX (mobile P1-B):** do NOT put `touch-action:none` on the canvas — it blocks page scroll over the whole graph, which is the prior overhaul's reported mobile failure. Instead wrap the canvas in a `touch-action: pan-y` scroll layer so one-finger vertical scroll passes through, and let Cytoscape own gestures inside its box. Give the wrapper an explicit, bounded height so the page can scroll past it on mobile.

```svelte
<script lang="ts">
  import { onMount } from 'svelte';
  let { elements, layout = { name: 'cose' }, style = [] }:
    { elements: any[]; layout?: any; style?: any[] } = $props();
  let el: HTMLDivElement;
  let cy: any = null;             // plain let — NOT $state

  onMount(() => {
    cy = cytoscape({ container: el, elements, layout, style });
    return () => { cy?.destroy(); cy = null; };
  });
  $effect(() => { const e = elements; if (cy) { cy.json({ elements: e }); cy.layout(layout).run(); } });
</script>

<div class="cy-scroll"><div class="cy" bind:this={el}></div></div>
<style>
  .cy-scroll { touch-action: pan-y; width: 100%; }       /* page scroll survives */
  .cy { width: 100%; height: 60vh; min-height: 320px; }   /* bounded; not full-screen on mobile */
  @media (max-width: 768px) { .cy { height: 50vh; } }
</style>
```

- [ ] **Step 5: Implement `Dag.svelte`** — port the D3 force-layout init from `static/dashboard/js/dag.js` into `onMount`, returning a teardown that removes the SVG and stops the simulation. Use the same data-binding `$effect` pattern (read the prop, guard on the instance).

> **REVIEW FIX (mobile P2-D):** D3's `d3.zoom()` calls `preventDefault` on touch and blocks page scroll. After `svg.call(zoom)`, detach the touch handler — `svg.call(zoom).on('touchstart.zoom', null).on('touchmove.zoom', null)` — and set `touch-action: pan-y` on the SVG wrapper so vertical scroll still works on mobile. (Read `dag.js` for the exact force config and node/edge rendering; reproduce it.)

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

- [ ] **Step 3: Implement `DataTable.svelte`** — a typed table that on narrow viewports either horizontally scrolls (`mode='scroll'`, default) or collapses each row into a labelled card (`mode='cards'`). The expanded boolean lives in component-local `$state` keyed by row id — this is what survives refresh.

> **REVIEW FIX (arch P1-B):** Svelte 5 runes components cannot use `<slot name="detail" {row}>` (named slots with let-bindings are dead in runes mode). Use a **snippet prop** `detail?: Snippet<[any]>` + `{@render detail(row)}`. Consumers pass `{#snippet detail(row)}…{/snippet}` as a child.
> **REVIEW FIX (mobile P2-B / P3-B):** `min-height` on `<tr>` is a no-op in table layout — use cell `padding` for the 44px target. Suppress the `data-label` `::before` on the detail row.

```svelte
<script lang="ts">
  import type { Snippet } from 'svelte';
  type Col = { key: string; label: string };
  let { columns, rows, mode = 'scroll', rowKey = (r: any, i: number) => String(i), detail }:
    { columns: Col[]; rows: any[]; mode?: 'scroll' | 'cards';
      rowKey?: (r: any, i: number) => string; detail?: Snippet<[any]> } = $props();
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
        {#if detail && expanded[rowKey(row, i)]}
          <tr class="detail"><td colspan={columns.length}>{@render detail(row)}</td></tr>
        {/if}
      {/each}
    </tbody>
  </table>
</div>

<style>
  .dt { overflow-x: auto; -webkit-overflow-scrolling: touch; }
  table { width: 100%; border-collapse: collapse; }
  th, td { text-align: left; padding: 0.75rem; border-bottom: 1px solid var(--border); }  /* 0.75rem ≈ 44px target */
  @media (max-width: 640px) {
    .dt--cards table, .dt--cards thead, .dt--cards tbody, .dt--cards tr, .dt--cards td { display: block; }
    .dt--cards thead { display: none; }
    .dt--cards tr { margin-bottom: 0.75rem; border: 1px solid var(--border); border-radius: 8px; }
    .dt--cards td { display: flex; justify-content: space-between; border: none; min-height: 44px; align-items: center; }
    .dt--cards td::before { content: attr(data-label); font-weight: 600; color: var(--text-dim); }
    .dt--cards tr.detail td { display: block; }
    .dt--cards tr.detail td::before { content: none; }   /* detail row has no data-label */
  }
</style>
```

- [ ] **Step 4: Implement** `StatGrid.svelte` (auto-fill grid of stat cards, `minmax(140px,1fr)` on mobile / `minmax(200px,1fr)` desktop — **REVIEW FIX mobile P2-C:** 140 not 120, to stay safe at 375px once main-content padding is counted; keep mobile content padding at 16px), `StaleBadge.svelte` (shows "updated Ns ago" / "stale" / "error — retrying" from a `PollState`), `BottomSheet.svelte` (wraps shadcn-svelte Drawer `side="bottom"`; **REVIEW FIX mobile P3-C:** inherit `closeOnEscape`, add an `aria-label`, scroll-lock the background on iOS, render a drag handle), `FilterBar.svelte` (toggle buttons bound to a parent `$state` value — survives refresh), and `Placeholder.svelte` (**REVIEW FIX devil P2-G:** "This view is being migrated — use /dashboard/ for now", takes a `route` prop; used by `App.svelte` for not-yet-migrated routes during Phases 2–3).

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

- [ ] **Step 1: Implement `App.svelte`** — responsive shell: desktop sidebar rail + mobile off-canvas Drawer + mobile header with hamburger + the route switch. Port the 15 nav links + SVG icons from `static/dashboard/index.html`.

> **REVIEW FIX (mobile P1-C — iOS Safari):** use `min-h-[100dvh]` (NOT `h-screen`/`100vh`) on layout containers; add `viewport-fit=cover` to the viewport meta in `index.html`; apply `padding-bottom: env(safe-area-inset-bottom)` to the bottom nav/footer; ensure `body` is NOT `overflow:hidden` on mobile (legacy had to undo this). Add these to the mobile checklist too.
> **REVIEW FIX (mobile P2-A — drawer a11y):** set shadcn-svelte Drawer `closeOnEscape` + `closeOnOutsideClick`, ensure focus trap + restore-focus-on-close, and add a resize reaction that closes the drawer when widening past the breakpoint: `$effect(() => { if (innerWidth > 768) drawerOpen = false; })` (bind `innerWidth` via `<svelte:window bind:innerWidth>`).
> **REVIEW FIX (mobile P3-D / devil P2-G):** render the active view with `{#if route === 'x'}`, **not** `{#key $currentRoute}` — `{#key}` destroys component-local state (filters/expansions) every time you navigate away and back. Each view's poll store is module-level (a singleton per view) and `start()`/`stop()` on mount/unmount, so it keeps state across nav but only polls while visible. For routes not yet migrated (Phases 2–3), render `<Placeholder route={...} />` ("This view is being migrated — use /dashboard/ for now"), NOT a fallback to Overview.

```svelte
<script lang="ts">
  import { currentRoute, initRouter, ROUTES } from './lib/router';
  import Placeholder from './lib/ui/Placeholder.svelte';
  // import each view as it is implemented; map route -> component.
  initRouter();
  let drawerOpen = $state(false);
  let innerWidth = $state(0);
  $effect(() => { if (innerWidth > 768) drawerOpen = false; });
</script>

<svelte:window bind:innerWidth />
<!-- <aside> sidebar (desktop) + Drawer (mobile) + <main class="min-h-[100dvh]"> with the {#if}-switched active view; unmigrated routes -> <Placeholder/> -->
```

- [ ] **Step 2: `main.ts`**

```ts
import { mount } from 'svelte';
import './app.css';
import App from './App.svelte';
export default mount(App, { target: document.getElementById('app')! });
```

- [ ] **Step 3: Add the Starlette v2 mount.** In `nous/api/rest.py`.

> **REVIEW FIX (arch P1-D / devil P1-B — MANDATORY, not optional):** `_NoCacheStaticFiles` is currently defined *inside* the `if os.path.isdir(dashboard_dir):` block (rest.py:2635). The v2 mount references it, so it MUST be **hoisted unconditionally above both `if` blocks** — otherwise a `NameError` at startup whenever the legacy dir is absent (tests, post-cutover). First hoist the class, then add the v2 block before the legacy block.
> **REVIEW FIX (arch P2-E):** `/dashboard/v2` does not collide with the `/dashboard/graph` etc. JSON routes — those are `Route`s registered before any `Mount`, and Starlette tries the route list top-to-bottom; `Route` (exact) entries win over `Mount` (prefix) by being earlier. Ordering only matters *between the two static mounts*: v2 first.

```python
    # Hoisted above both mounts so either dir can use it.
    class _NoCacheStaticFiles(StaticFiles):
        async def get_response(self, path, scope):
            response = await super().get_response(path, scope)
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
            return response

    # Dashboard v2 (Svelte) — registered BEFORE the catch-all /dashboard mount.
    dashboard_v2_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        "static", "dashboard-v2", "dist",
    )
    if os.path.isdir(dashboard_v2_dir):
        routes.append(
            Mount("/dashboard/v2", app=_NoCacheStaticFiles(directory=dashboard_v2_dir, html=True)),
        )

    # Legacy dashboard (unchanged) — keep LAST (catch-all for /dashboard/*).
    dashboard_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        "static", "dashboard",
    )
    if os.path.isdir(dashboard_dir):
        routes.append(
            Mount("/dashboard", app=_NoCacheStaticFiles(directory=dashboard_dir, html=True)),
        )
```

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

- [ ] **Step 2: Add the type** to `src/lib/types/api.ts` matching the **real** response (verified against `cache.js` + `rest.py:1646`). The legacy guess was wrong; this is the actual shape:

```ts
export interface CacheSummary {
  total_calls: number; total_input_tokens: number; total_cache_read: number;
  total_cache_created: number; overall_hit_rate: number; total_breaks: number;
  break_rate: number; tokens_lost_to_breaks: number;
}
export interface CacheSession {
  session_id: string; calls: number; input_tokens: number; cache_read: number;
  cache_created: number; hit_rate: number; breaks: number;
}
export interface CacheTimelineEntry {
  timestamp: string; session_id: string; turn: number; model: string;
  input_tokens: number; cache_read: number; cache_created: number;
  hit_rate: number; cache_break: boolean;
}
export interface CacheData {
  summary: CacheSummary;
  break_components: Record<string, number>;   // {name: count} dict — convert to array for the bar chart
  sessions: CacheSession[];
  timeline: CacheTimelineEntry[];             // array of per-call entries, NOT a precomputed series
}
```

> **REVIEW FIX (all reviewers, P1):** field names are `summary.overall_hit_rate`, `summary.total_calls`, `summary.total_cache_read`, `summary.total_breaks`/`break_rate`; there is no `hits`/`misses`/`tokens_saved`. `timeline` is an array. `cache.js` builds THREE charts (token-breakdown doughnut, break-components bar, efficiency timeline line) — reproduce all three, not one. For **every** view (Tasks 11–24), derive the interface from the live payload (`curl '<host>/dashboard/<x>' | jq`) BEFORE writing markup; the field names below in other tasks are guidance, not gospel.

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
    { key: 'calls', label: 'Calls' },
    { key: 'cache_read', label: 'Cache read' },
    { key: 'hit_rate', label: 'Hit rate' },
    { key: 'breaks', label: 'Breaks' },
  ];
</script>

<header class="view-head">
  <h1>Cache</h1>
  <StaleBadge state={$store} />
</header>

{#if $store.data}
  {@const d = $store.data}
  <StatGrid stats={[
    { label: 'Total API calls', value: d.summary.total_calls },
    { label: 'Hit rate', value: (d.summary.overall_hit_rate).toFixed(1) + '%' },
    { label: 'Tokens saved (cache_read)', value: d.summary.total_cache_read },
    { label: 'Cache breaks', value: d.summary.total_breaks },
  ]} />

  <section class="chart-card">
    <h2>Token breakdown</h2>
    <Chart type="doughnut"
      data={{ labels: ['Cache read', 'Cache created', 'Fresh input'],
              datasets: [{ data: [d.summary.total_cache_read, d.summary.total_cache_created, d.summary.total_input_tokens] }] }} />
  </section>

  <section class="chart-card">
    <h2>Cache breaks by component</h2>
    <Chart type="bar"
      data={{ labels: Object.keys(d.break_components),
              datasets: [{ label: 'Breaks', data: Object.values(d.break_components) }] }}
      options={{ indexAxis: 'y' }} />
  </section>

  <section class="chart-card">
    <h2>Hit-rate over time</h2>
    <Chart type="line"
      data={{ labels: d.timeline.map((t) => t.timestamp),
              datasets: [{ label: 'Hit rate', data: d.timeline.map((t) => t.hit_rate) }] }} />
  </section>

  <section class="table-card">
    <h2>Per-session</h2>
    <DataTable columns={cols} rows={d.sessions} mode="cards" rowKey={(r) => r.session_id} />
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

> **REVIEW FIX (devil P1-D):** this is **not** a paginated list. `subtasks.js` calls `GET /dashboard/subtasks?hours=24` and renders an **analytics** page: 5-state outcome counts, daily trend, tokens-by-outcome, top-failing tasks, DAG correlation. There is no `limit`/`offset`/status-filter.

**Files:** Create `dashboard-app/src/views/Subtasks.svelte`; modify `types/api.ts`, `App.svelte`. Reference `static/dashboard/js/subtasks.js`; endpoint `GET /dashboard/subtasks?hours=24` (poll 30s; `hours` max 168). Response carries `window_hours`.
Follow the Task 10 recipe but: add a `hours` `$state` selector (24 / 72 / 168) that re-fetches on change (rebuild the store or pass `hours` into the fetcher); render the outcome doughnut, daily-trend line, and tokens-by-outcome bar via `Chart.svelte`; top-failing tasks via `DataTable`. No status FilterBar, no pagination.
Parity items: same metrics/charts as legacy; changing `hours` re-fetches; expansions/scroll survive the 30s poll; mobile cards + sized charts.
Commit: `feat(dashboard-v2): migrate Subtasks view`.

### Task 12: Heartbeat view

**Files:** Create `Heartbeat.svelte`; modify `types/api.ts`, `App.svelte`. Reference `static/dashboard/js/heartbeat.js` (30s; status banner, 5 stat cards, budget doughnut, findings stacked bar, check table, findings timeline, cognitive sessions log), endpoint `GET /dashboard/heartbeat`.
Recipe as Task 10. Charts: budget doughnut + findings stacked bar via `Chart.svelte`. Check table + cognitive log via `DataTable`. Findings rows expandable (detail slot) — verify expansion survives the 30s poll.
Commit: `feat(dashboard-v2): migrate Heartbeat view`.

### Task 13: Observability view

**Files:** Create `Observability.svelte`; modify `types/api.ts`, `App.svelte`. Reference `static/dashboard/js/observability.js` (30s; event-bus health stat cards + handler doughnut, causal traces timeline with tree expansion, drift sparklines + anomaly markers, context-visibility stacked bar + recent-calls table), endpoint `GET /dashboard/observability`.
Recipe as Task 10. The trace **tree expansion** is the key state-preservation case. **REVIEW FIX (arch P3-D):** model expanded nodes as a flat `$state<Set<string>>` of trace-node ids (a `SvelteSet` from `svelte/reactivity`), NOT nested reactive objects — the flat keyed set survives data refresh without relying on object identity. Confirm expansions survive the 30s poll.
Commit: `feat(dashboard-v2): migrate Observability view`.

### Task 14: DAG view

**Files:** Create `Dag.svelte` (view); modify `types/api.ts`, `App.svelte`. Reference `static/dashboard/js/dag.js`; endpoint `GET /dashboard/dag` (+`limit`). Uses the `Dag.svelte` **viz** wrapper from Task 7.
**REVIEW FIX (devil P1-C):** poll interval is **15000** (15s), matching `dag.js:63` — NOT 30s like the other pollers.
Recipe as Task 10. Ensure the D3 graph re-renders on data change via the wrapper's `$effect`, and that selecting/expanding a DAG (detail) survives refresh. Mobile: D3 touch handler detached + `touch-action: pan-y` wrapper (Task 7 Step 5 fix); DAG detail in a `BottomSheet`.
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
Reference `overview.js`; **single** endpoint `GET /status?dashboard=true` (**REVIEW FIX devil P2-B:** one call, not two — the response has `memory`, `calibration`, and `dashboard` sub-objects). Sections: memory counts (facts/episodes/decisions) stat cards + 4 mini charts (Chart.svelte). No polling (load-once; add a manual refresh button). Commit: `feat(dashboard-v2): migrate Overview view`.

### Task 17: Graph (Knowledge Graph)
Reference `graph.js`; endpoint `GET /dashboard/graph` (+`limit`). Uses `Graph.svelte` (Cytoscape) wrapper. Search box + node detail panel → `BottomSheet` on mobile. Selected-node state is component-local (survives any manual refresh). Touch isolation verified. Commit: `feat(dashboard-v2): migrate Graph view`.

### Task 18: Browser (Memory Browser)
Reference `browser.js`; endpoints `GET /facts?q=`, `/episodes`, `/decisions`, `/procedures`, `/censors`, `/chunks` (each `limit`/`offset`; **Chunks** also supports `q=` + `episode_id=` per `rest.py:517`). Tabbed; **active tab + per-tab search + pagination are component-local `$state`** (the legacy version lost these on re-render). `DataTable` per tab.
**REVIEW FIX (devil P2-A):** the Censors tab is NOT read-only — it has a tier dropdown + active toggle + Save that calls `apiSend('/censors/{id}', { action, active }, 'PUT')` (`browser.js:370`). Wire this write in the Censors row detail; without it the dashboard loses censor editing (a regression). This is the only `apiSend` in Browser.
Commit: `feat(dashboard-v2): migrate Browser view`.

### Task 19: Decisions
Reference `decisions.js`; endpoint `GET /dashboard/calibration`. Calibration curve + confidence histogram (Chart.svelte), Brier score stat. Commit: `feat(dashboard-v2): migrate Decisions view`.

### Task 20: Activity
Reference `activity.js`; endpoint `GET /dashboard/activity?hours=168` (**REVIEW FIX devil P2-C:** legacy hardcodes `hours=168`; there is no selector. Match parity — hardcode 168. A selector would be a *new feature beyond parity*, out of scope here). Timeline, censor stats, schedules, sleep cycles. Commit: `feat(dashboard-v2): migrate Activity view`.

### Task 21: Health (Graph Health)
Reference `health.js`; endpoint `GET /dashboard/health?days=30` (**REVIEW FIX devil P2-D:** legacy hardcodes `days=30`; verify whether `dashboard_health` in `rest.py` actually reads the `days` query param before adding any selector — if it ignores it, a selector is dead UI. Hardcode 30 to match parity). Density trends, edge-creation chart, degree distribution, orphans. Commit: `feat(dashboard-v2): migrate Health view`.

### Task 22: Admission
Reference `admission.js`; **two-phase fetch (REVIEW FIX devil P2-F)** — `GET /dashboard/admission` is a one-shot main load (NOT auto-polled), and `GET /dashboard/admission/rejected?limit=25&offset=N` is fetched on demand by the rejected-table pagination controls. A single `usePoll` store does NOT cover both: use one load store for the main payload + a separate manual-fetch action for the paginated rejected list. The threshold **simulator** is pure client-side recompute over `data.score_distribution` (no POST). Sections: threshold stats, simulator, dimension box plots, paginated rejected table. Commit: `feat(dashboard-v2): migrate Admission view`.

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

> **REVIEW FIX (arch P2-G / devil P2-H):** prefer a **redirect** `/dashboard` → `/dashboard/v2/` over aliasing the legacy mount's directory to the v2 dist. The v2 build's `index.html` + assets are rooted at `/dashboard/v2/` (Vite `base`); serving the same dist under `/dashboard/` mixes two URL prefixes and leaves `/dashboard/lib/*` and `/dashboard/css/*` 404ing. A redirect keeps one canonical prefix.

- [ ] **Step 1:** Add a `Route("/dashboard", RedirectResponse("/dashboard/v2/"))` (and `/dashboard/` ) registered before the v2 mount. Keep the `/dashboard/v2` mount as the canonical app. (Do NOT also keep the legacy `/dashboard` static mount — it would shadow the redirect.)
- [ ] **Step 2:** Manual verify: visiting `/dashboard` 302-redirects to `/dashboard/v2/`; all `/dashboard/*` JSON endpoints still return JSON (they are `Route`s registered before the mounts — unaffected; the redirect Route is exact-match `/dashboard`, so it does not catch `/dashboard/graph`).
- [ ] **Step 3: Commit** `feat(dashboard-v2): redirect /dashboard to the Svelte app`.

### Task 26a: Wire the dashboard build into deployment (REVIEW FIX — devil P2-I, production blocker)

**Files:** Modify `Dockerfile` (repo root).

Because `static/dashboard-v2/dist/` is gitignored, the running container will have NO v2 assets unless the image builds them — and the `if os.path.isdir(dashboard_v2_dir)` guard fails *silently* (serves only legacy, no error). This must be fixed before cutover (Task 25) is meaningful in prod.

- [ ] **Step 1:** Add a Node build stage to the `Dockerfile`. Read the current Dockerfile first; add (multi-stage preferred):

```dockerfile
# --- dashboard build stage ---
FROM node:22-slim AS dashboard
WORKDIR /build
COPY dashboard-app/ ./dashboard-app/
RUN cd dashboard-app && npm ci && npm run build   # writes ../static/dashboard-v2/dist

# --- in the final Python stage, after COPY static/ ---
COPY --from=dashboard /build/static/dashboard-v2/dist ./static/dashboard-v2/dist
```
Adjust paths to match the existing Dockerfile's `COPY static/ ...` line and WORKDIR.

- [ ] **Step 2:** Document in `CLAUDE.md` the local build command `cd dashboard-app && npm install && npm run build`, and that the Docker image now requires the dashboard build stage.
- [ ] **Step 3:** Verify `docker build` produces an image whose `/dashboard/v2/` serves the app.
- [ ] **Step 4: Commit** `chore(dashboard-v2): build Svelte dashboard in Docker image`.

### Task 26: Retire legacy assets

**Files:** Delete `static/dashboard/js/*`, `static/dashboard/css/dashboard.css`, `static/dashboard/index.html`; keep `static/dashboard/lib/*` only if still referenced (v2 has its own copies under `public/lib`, so delete legacy lib too).

- [ ] **Step 1:** Before deleting, confirm nothing else in the repo references these files (grep `static/dashboard/js`, `static/dashboard/css`).
- [ ] **Step 2:** Delete legacy view assets; leave a short `static/dashboard/README.md` noting the app now lives in `dashboard-app/` and builds to `static/dashboard-v2/dist`.
- [ ] **Step 3:** Update repo docs (`CLAUDE.md` dashboard section / `docs/`) to describe the build step (`cd dashboard-app && npm run build`).
- [ ] **Step 4: Commit** `chore(dashboard-v2): retire legacy vanilla dashboard`.

---

## Parity Contract (run for every view task)

**How to verify:** run a local Nous instance and drive both UIs with the `claude-in-chrome` browser tools (load schemas via ToolSearch `select:mcp__claude-in-chrome__*`). For each view, open legacy `/dashboard/#/<view>` and v2 `/dashboard/v2/#/<view>` side by side; compare rendered data; exercise filters/expansion; resize the window to 375px (`resize_window`) for the mobile checklist; and for the refresh win, expand a row + scroll, then wait one poll interval and confirm state is intact (read console via `read_console_messages` if anything looks off). Capture a GIF of the refresh-preserves-state behavior per poller view for the review.

- **Data:** every section/field/chart the legacy view shows is present; same endpoint(s) + params.
- **Behavior:** filters, search, pagination, expansion, chart interactions match legacy.
- **Refresh (the win):** with a row expanded + a filter active + scrolled down, wait one poll interval → all state preserved. (Pollers: ledger 15s, others 30s.)
- **Mobile @375px:** drawer nav opens/traps focus/closes on Escape; tables scroll or collapse to cards; detail panels are bottom sheets; charts fit; touch targets ≥44px; no horizontal page overflow.
- **Types:** `npx svelte-check` clean; the response interface matches the real payload.

## CI / build note
Document in `CLAUDE.md` that the dashboard now requires `cd dashboard-app && npm run build` before the built assets under `static/dashboard-v2/dist` are served. Consider a `make dashboard` / npm script and (optionally, later) a CI step — out of scope for this plan beyond documenting it.

---

## Review revisions applied (3-agent team, 2026-06-19)

All three reviewers (nous-ui-arch / nous-ui-mobile / nous-ui-devil) returned **APPROVE WITH REVISIONS**. Findings folded in above as inline **REVIEW FIX** notes:

- **P1 (blocking):** Chart `$effect` infinite-loop trap (plain `let`) + chart container height; DataTable named-slot → Svelte 5 snippet; `_NoCacheStaticFiles` mandatory hoist (NameError); Cache `CacheData` real shape + 3 charts; Cytoscape/D3 touch isolation (was blocking page scroll); iOS Safari `100dvh`/safe-area/body-overflow; DAG interval 15s; Subtasks is a `?hours=24` analytics view not a list.
- **P2:** recursive `setTimeout` + `stop()` race; fetcher typing; router listener guard; drawer a11y props + resize-close; DataTable 44px-via-padding + detail-row `::before`; StatGrid `minmax(140px)`; Browser censors `PUT` write; Overview single endpoint; Activity `hours=168` / Health `days=30` hardcoded (parity); Admission two-phase fetch; graceful `Placeholder` for unmigrated routes; cutover via redirect; Dockerfile build stage (Task 26a).
- **P3:** `{#if}` not `{#key}` (state across nav); Observability flat `SvelteSet` tree-state; abort-signal test; `router.ts` filename; density is real (not a placeholder).

## Self-review notes (author)
- **Spec coverage:** D1–D6/D6a all realized (Svelte/Vite Task 1, TS throughout, strangler mount Task 9/25, viz wrap Task 7, shadcn-svelte Task 2). Phases 1–4 and the 6-pollers-first ordering match the spec. Parity contract and 3-agent review carried over.
- **No vague placeholders in foundation/template:** Tasks 1–10 carry full code/commands. Tasks 11–24 are per-view *specs* (exact legacy file, endpoint+params, sections, charts, filters, parity) deliberately deferring per-view markup to the executor reading the named legacy file — the legacy file is the precise, non-ambiguous source of truth for parity, which is more accurate than inlined guesses at 15 unseen payloads.
- **Type/name consistency:** `makePollStore`/`usePoll`/`PollState`/`PollStore`, `apiGet(path,{signal})`, `Chart.svelte` props `{type,data,options}`, `DataTable` props `{columns,rows,mode,rowKey}` used identically across tasks.
- **Open item:** per-view TS interfaces (Step 2 of each) must be derived from the live payload, since the exact backend field names aren't all enumerated here — flagged in each task and in the Parity Contract.
