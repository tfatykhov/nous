# Nous Dashboard v2 — Svelte 5 Reactive Rewrite (Design Spec)

**Date:** 2026-06-19
**Status:** Approved (brainstorming) — pending spec review, then implementation plan + multi-team review
**Branch / worktree:** `worktree-dashboard-v2-svelte` (`.claude/worktrees/dashboard-v2-svelte`)
**FORGE decision:** architecture rewrite, gated on architecture-review guardrail (multi-team plan review satisfies it)

---

## 1. Problem

Two user-reported pains share one root cause.

1. **Auto-refresh destroys UI state.** Six views poll on a timer — `ledger` (15s), and `heartbeat` / `cache` / `observability` / `dag` / `subtasks` (30s). Every poll re-renders with `container.innerHTML = html`, which blows away and rebuilds the DOM subtree, resetting expand/collapse, scroll position, input focus, and filter selections. Only `ledger.js` mitigates this — it scrapes `.expanded` session IDs and active filter classes out of the DOM *before* the rebuild and replays them after. That one-off workaround is proof the underlying pattern does not scale: every stateful view would need its own bespoke save/restore.

2. **Not mobile-friendly.** A prior overhaul (FORGE decision `6a9e5811`, 2026-04-06, success) added a viewport tag, a drawer, and `@media` breakpoints at 1024px/768px. It was pure CSS/JS patching of the existing vanilla base. The user reports mobile is still broken — you cannot reliably make a non-reactive, innerHTML-rebuild UI responsive by layering media queries; tables, detail panels, and grids need real component-level responsive behavior.

**Root cause:** rendering is "blow away and rebuild," and there is no client-side state model. A diffing/reactive renderer fixes both classes of problem structurally instead of per-symptom.

### Current-state facts (verified by code exploration)

- **Stack:** ~7,000 lines of hand-written vanilla JS, no build step, no package.json near the dashboard. Libraries vendored as minified files in `static/dashboard/lib/`.
- **App framework:** `static/dashboard/js/app.js` (~415 lines) — hash router + `Dashboard.registerView(name, loadFn)` pattern; `loadView()` hides all `.view` divs, shows one, destroys prior Chart.js instances, tears down Cytoscape/D3 on leave; `Dashboard.apiGet(path, retries)` with exponential backoff and `Dashboard.apiSend(path, body, method)`.
- **Views (15):** overview, graph (Cytoscape), browser, decisions, activity, heartbeat\*, observability\*, health, admission, rubric, execution/ledger\*, cache\*, density, dag\* (D3), subtasks\* (\* = auto-refresh pollers).
- **Libraries:** `chart.min.js` (overview, decisions, health, admission, cache, observability, heartbeat), `cytoscape.min.js` (graph), `d3.min.js` (dag only). All framework-agnostic.
- **Serving:** Starlette serves `static/dashboard/` (exact static route to be confirmed in `nous/api/rest.py` during planning). Backend exposes `/status`, `/status?dashboard=true`, and `/dashboard/*` endpoints; no backend logic change is required by this rewrite.
- **State today:** no localStorage/sessionStorage; per-view state lives in closures and DOM classes and is lost on each `innerHTML` assignment (except ledger's manual scrape).

---

## 2. Goals & Non-Goals

### Goals
- Auto-refresh updates data **without** resetting expand/collapse, scroll, focus, or filters — automatically, for every view, with no per-view save/restore code.
- Genuinely usable on mobile (responsive nav, tables, detail panels, charts; 44px touch targets).
- A maintainable, componentized codebase that future dashboard features extend cheaply.
- Zero disruption to the existing dashboard during migration (old UI stays live until cutover).

### Non-Goals
- No backend/API/schema changes (beyond adding a static route for the v2 build output).
- No rewrite of chart/graph *logic* — viz behavior is ported as-is inside framework wrappers.
- No new data sources, no WebSocket/real-time push (polling is retained; only the render path changes).
- No unrelated refactoring of Python code.

---

## 3. Decisions (all user-approved unless noted)

| # | Decision | Choice | Rationale |
|---|----------|--------|-----------|
| D1 | Fix aggressiveness | **Reactive rewrite, separate app** | Both pains share the innerHTML+no-state root; a diffing renderer kills the bug class. |
| D2 | Framework | **Svelte 5 + Vite** | Compiles away (smallest runtime), least boilerplate; stores map cleanly to poll→store→auto-patch; onMount/onDestroy wrap imperative viz libs cleanly. |
| D3 | Rollout | **Strangler, prioritized** | New app at `/dashboard/v2`; migrate the 6 pollers first; old UI live until parity, then flip default. Bounds blast radius, value lands early. |
| D4 | Language | **TypeScript** | Type REST response shapes once; turns FE/BE key mismatches (a known recurring miss) into build errors across 15 views. |
| D5 | Viz libraries | **Keep all three, wrap them** | Chart.js/Cytoscape/D3 are framework-agnostic; wrapping ports logic as-is and avoids scope creep into chart rewrites. |
| D6 | Styling / components | **Adopt a Svelte UI component library** | Ready-made responsive, accessible primitives (drawer, dialog, data table) — the fastest path to good mobile. |
| D6a | *Which* component lib | **Recommendation: shadcn-svelte** (bits-ui + Tailwind); **Skeleton** as fallback. *Sole open sub-choice.* | shadcn-svelte = you own the component source → easiest to match the existing dark tokens, accessible primitives, no version lock. Skeleton = batteries-included AppShell but more restyling to match the current identity. To be finalized at plan kickoff. |

---

## 4. Architecture

A new, self-contained Svelte 5 + Vite + TypeScript app. Built to static assets and served alongside the untouched legacy UI.

```
dashboard-app/                     # new top-level dir (sibling to static/)
  package.json
  vite.config.ts                   # base path /dashboard/v2/, outDir ../static/dashboard-v2/dist
  svelte.config.js
  tsconfig.json
  tailwind.config.ts               # (if shadcn-svelte/Skeleton path)
  index.html                       # SPA entry
  src/
    main.ts                        # mount root + hash router init
    App.svelte                     # app shell: responsive sidebar/drawer, mobile header, <Router/>
    lib/
      api.ts                       # typed fetch + retry/backoff (ports app.js apiGet/apiSend)
      router.ts                    # hash router (#/overview ...), 15 routes
      poll.ts                      # ONE shared poll manager: interval -> store update; auto-pauses off-view; AbortController; in-flight debounce
      stores/                      # one store per view (or per data source)
      ui/                          # shared primitives from the component lib + thin wrappers:
                                   #   Card, StatGrid, DataTable (responsive: scroll or card-collapse),
                                   #   Drawer, Dialog, BottomSheet, FilterBar, Badge
      viz/
        Chart.svelte               # wraps Chart.js (onMount new Chart, onDestroy .destroy)
        Graph.svelte               # wraps Cytoscape (init + teardown, touch isolation)
        Dag.svelte                 # wraps D3 force layout (init + teardown)
    views/                         # 15 components, 1:1 with legacy views
      Overview.svelte  Graph.svelte  Browser.svelte  Decisions.svelte
      Activity.svelte  Heartbeat.svelte  Observability.svelte  Health.svelte
      Admission.svelte  Rubric.svelte  Ledger.svelte  Cache.svelte
      Density.svelte  Dag.svelte  Subtasks.svelte
    types/
      api.ts                       # interfaces for every /dashboard/* and /status response — single source of truth
  -> build output: static/dashboard-v2/dist  (served at /dashboard/v2)
```

### Why refresh-state survives for free
A view renders from a store. On each poll, `poll.ts` updates the store; Svelte diffs keyed `{#each}` lists and patches only changed nodes. A row's `expanded` state is **component-local** (or keyed store state) and is never touched by a data update. No manual scrape/replay anywhere — the `ledger.js` workaround disappears entirely.

### Shared poll manager (`poll.ts`)
Replaces six independent `setInterval` blocks with one coordinator:
- Registers `(endpoint, interval, store)`; fires only while its view is active (router-aware), pausing when navigated away.
- Per-source in-flight guard (skip if a fetch is outstanding) + `AbortController` cancel on view leave.
- Errors surface as a store field the view renders (stale-data badge / retry), never a blank panel.

### App shell & routing
`App.svelte` owns the responsive sidebar (desktop) / drawer (mobile, accessible via the component lib's Drawer — focus trap, Escape, `inert` background), the mobile header, and a hash `<Router/>` that lazy-mounts view components. View teardown (chart/graph destroy) is handled by each component's `onDestroy`.

### Serving (strangler)
- Legacy: `static/dashboard/` served at `/dashboard` — **untouched**.
- v2: `static/dashboard-v2/dist/` served at `/dashboard/v2` via a new Starlette static mount (added in `rest.py`; verified during planning).
- Cutover (Phase 4): point `/dashboard` at the v2 build, retire `static/dashboard/js/*`.

---

## 5. Mobile design

Real responsive components, not media-query retrofits:
- **Nav:** persistent rail on desktop; off-canvas Drawer on mobile (hamburger), with focus trap / Escape / background `inert`.
- **Grids:** stat/chart grids collapse to single column; cards stack.
- **Tables:** `DataTable` either horizontal-scrolls with momentum or collapses each row to a card on narrow widths (per-view choice).
- **Detail panels:** the graph/ledger right-rail detail becomes a **bottom sheet** on mobile.
- **Touch:** ≥44px targets; Cytoscape/D3 touch gestures isolated so page scroll still works around the canvas.

---

## 6. Migration plan (phases)

1. **Phase 1 — Foundation.** Scaffold `dashboard-app/`; Vite build → `static/dashboard-v2/dist`; Starlette `/dashboard/v2` route; hash router; `App.svelte` shell (responsive nav); `api.ts`; `poll.ts`; viz wrappers (`Chart`/`Graph`/`Dag`); shared `ui/` primitives; `types/api.ts` seeded from real endpoint responses. Finalize D6a (shadcn-svelte vs Skeleton). Deliverable: empty-but-navigable shell at `/dashboard/v2`.
2. **Phase 2 — The 6 pollers.** Migrate `ledger`, `heartbeat`, `cache`, `observability`, `dag`, `subtasks`. This is where the pain lives; proves refresh-state-preservation + mobile end to end. Ship at `/dashboard/v2`.
3. **Phase 3 — Remaining 9.** `overview`, `graph`, `browser`, `decisions`, `activity`, `health`, `admission`, `rubric`, `density`.
4. **Phase 4 — Cutover.** Flip default route to v2; retire legacy `static/dashboard/js/*` (and `lib/` if fully superseded).

Each phase is a single revertable PR. Old UI stays live through Phase 3.

---

## 7. Per-view parity contract

Every migrated view ships with a checklist diffed against the **live legacy view**:
- **Data parity:** same fields/sections rendered; same endpoint(s) hit with same params.
- **Behavior parity:** charts/graph render equivalently; filters, pagination, search, expansion all work.
- **Refresh parity (the win):** trigger a poll with a row expanded + a filter active + scrolled down → state is preserved.
- **Mobile checklist:** nav, tables, detail panel, charts usable at 375px width; 44px targets; no horizontal page overflow.

---

## 8. Testing & verification

- **Type safety:** `tsc`/`svelte-check` clean — the compile-time guard on API contract drift (D4).
- **Component tests:** Vitest + @testing-library/svelte for `poll.ts` (state preserved across updates), `DataTable` responsive modes, viz wrapper mount/teardown (no leaked instances).
- **Manual parity:** the §7 checklist per view, executed against a running instance (legacy vs v2 side by side).
- **Build:** `vite build` produces `static/dashboard-v2/dist`; served route returns the app.

---

## 9. Risks & mitigations

| Risk | Mitigation |
|------|------------|
| 15-view rewrite is large | Strangler order; one PR per phase; old UI always live; value after Phase 2. |
| FE/BE field mismatches across views | `types/api.ts` single source of truth (D4); compile errors not runtime blanks. |
| Viz libs misbehave under a framework | Wrap in dedicated components with explicit onMount/onDestroy; port logic verbatim; touch isolation carried over from prior overhaul learnings. |
| Adding a build step to a no-build repo | Build output is plain static assets served exactly like today; no runtime/server dependency on Node. Document `npm run build` in repo. |
| Scope creep into chart redesign | Non-goal §2; viz is ported as-is. |
| Regression vs current behavior | Per-view parity contract §7; 3-agent review of the plan before code. |

---

## 10. Review approach

Reuse the 3-agent review team that scored 0.92/success on the prior UI plan (decision `6a9e5811`):
- **nous-ui-arch** — Svelte/Vite app structure, store/poll model, build+serve wiring, router, viz wrapping correctness.
- **nous-ui-mobile** — responsive components, drawer a11y, touch isolation, table/detail-panel behavior, 44px targets.
- **nous-ui-devil** — cross-reference plan vs real endpoints/`rest.py`, parity gaps, phantom APIs, migration-order hazards, cutover safety.

Run against the **implementation plan** (writing-plans output) before any code. This review also satisfies the FORGE `require-architecture-review` guardrail that currently blocks finalizing the architecture decision.

---

## 11. Open items
- **D6a:** shadcn-svelte (recommended) vs Skeleton — finalize at plan kickoff.
- Confirm exact Starlette static-mount mechanism + path in `nous/api/rest.py` (planning).
- Confirm which `/dashboard/*` endpoints each legacy view actually calls (seed `types/api.ts`).
