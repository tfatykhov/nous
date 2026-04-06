# Dashboard Mobile UI Overhaul — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix broken mobile layout, improve responsive design, add typography/visual polish, and fix accessibility gaps in the Nous dashboard.

**Architecture:** Pure CSS + vanilla JS changes. No framework, no build step, no new dependencies. Modifies `dashboard.css`, `index.html`, and select JS view modules. All changes are additive to the existing vanilla SPA pattern.

**Tech Stack:** CSS3 (media queries, custom properties, animations), vanilla JavaScript, Chart.js (responsive config), D3.js (viewport-aware detail panel)

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `static/dashboard/css/dashboard.css` | Modify | All CSS changes: mobile sidebar, responsive grid, typography, animations, accessibility |
| `static/dashboard/index.html` | Modify | Google Fonts link, viewport meta (already present), ARIA attributes on nav |
| `static/dashboard/js/app.js` | Modify | Mobile sidebar toggle logic (drawer), responsive Chart.js legend helper |
| `static/dashboard/js/overview.js` | Modify | Use responsive legend helper for doughnut charts |
| `static/dashboard/js/graph.js` | Modify | Full-screen detail panel on mobile, D3 touch-action fix |
| `static/dashboard/js/activity.js` | Modify | Responsive grid (remove hardcoded 360px column) |
| `static/dashboard/js/admission.js` | Modify | Use responsive legend helper for doughnut chart |
| `static/dashboard/js/rubric.js` | Modify | Use responsive legend helper for doughnut chart |
| `static/dashboard/js/observability.js` | Modify | Use responsive legend helper for doughnut chart |

---

### Task 1: Mobile Sidebar — Slide-Out Drawer

**Files:**
- Modify: `static/dashboard/css/dashboard.css` (lines 50-161 sidebar section, lines 949-981 responsive section)
- Modify: `static/dashboard/index.html` (line 76 toggle button, lines 12-79 sidebar nav)
- Modify: `static/dashboard/js/app.js` (lines 299-311 DOMContentLoaded)

**Problem:** At 768px, the sidebar becomes a horizontal icon row that consumes vertical space and has no labels. Icons are indistinguishable. The `overflow: hidden` on `html, body` clips the content below.

**Solution:** Convert the 768px breakpoint to a slide-out drawer pattern with overlay. Keep the 1024px collapse-to-icons behavior.

- [ ] **Step 1: Add mobile drawer CSS**

In `dashboard.css`, replace the `@media (max-width: 768px)` block (lines 949-981) with a mobile drawer pattern:

```css
@media (max-width: 768px) {
    html, body {
        overflow: auto;
        height: auto;
    }

    .sidebar {
        position: fixed;
        top: 0;
        left: 0;
        width: 260px;
        height: 100vh;
        transform: translateX(-100%);
        transition: transform 0.25s ease;
        z-index: 200;
        border-right: 1px solid var(--border);
        flex-direction: column;
    }

    .sidebar.mobile-open {
        transform: translateX(0);
    }

    .sidebar .nav-link span {
        display: inline;
    }

    .sidebar .sidebar-title {
        display: inline;
    }

    .sidebar-header {
        border-bottom: 1px solid var(--border);
        padding: 20px 16px;
    }

    .sidebar-toggle {
        display: none;
    }

    .sidebar-overlay {
        display: none;
        position: fixed;
        inset: 0;
        background: rgba(0, 0, 0, 0.5);
        z-index: 199;
    }

    .sidebar-overlay.visible {
        display: block;
    }

    .content {
        margin-left: 0;
        height: auto;
        min-height: 100dvh;
        overflow-y: auto;
        padding: 16px;
    }

    .mobile-header {
        display: flex;
        align-items: center;
        gap: 12px;
        padding: 12px 0;
        margin-bottom: 8px;
    }

    .mobile-menu-btn {
        background: none;
        border: 1px solid var(--border);
        border-radius: var(--radius-sm);
        color: var(--text);
        padding: 12px;
        min-width: 44px;
        min-height: 44px;
        cursor: pointer;
        display: flex;
        align-items: center;
        justify-content: center;
        transition: all var(--transition);
    }

    .mobile-menu-btn:hover {
        border-color: var(--accent-dim);
        background: var(--surface);
    }

    .mobile-menu-btn svg {
        width: 20px;
        height: 20px;
    }

    .mobile-header-title {
        font-size: 16px;
        font-weight: 700;
        color: var(--accent);
    }

    .graph-container {
        height: 60vh;
    }
}
```

- [ ] **Step 2: Hide mobile elements on desktop**

Add above the 1024px media query in `dashboard.css`:

```css
/* Mobile-only elements (hidden on desktop) */
.sidebar-overlay {
    display: none;
}

.mobile-header {
    display: none;
}
```

- [ ] **Step 3: Add overlay and mobile header to HTML**

In `index.html`:

**First**, insert the overlay div between the closing `</nav>` (line 79) and the existing `<main>` (line 81). Do NOT duplicate the `<main>` tag:

```html
    <div class="sidebar-overlay" id="sidebar-overlay"></div>
```

**Second**, insert the mobile header as the first child inside the existing `<main class="content" id="content">` tag (after line 81, before the view divs):

```html
        <div class="mobile-header">
            <button class="mobile-menu-btn" id="mobile-menu-btn" aria-label="Open navigation menu">
                <svg viewBox="0 0 20 20" fill="currentColor"><path fill-rule="evenodd" d="M3 5a1 1 0 011-1h12a1 1 0 110 2H4a1 1 0 01-1-1zm0 5a1 1 0 011-1h12a1 1 0 110 2H4a1 1 0 01-1-1zm0 5a1 1 0 011-1h6a1 1 0 110 2H4a1 1 0 01-1-1z" clip-rule="evenodd"/></svg>
            </button>
            <span class="mobile-header-title">Nous</span>
        </div>
```

- [ ] **Step 4: Add mobile drawer JS logic**

In `app.js`, replace the DOMContentLoaded handler (lines 299-311) with:

```javascript
document.addEventListener('DOMContentLoaded', function() {
    // Sidebar toggle (desktop collapse)
    var toggleBtn = document.getElementById('sidebar-toggle');
    var sidebar = document.getElementById('sidebar');
    if (toggleBtn && sidebar) {
        toggleBtn.addEventListener('click', function() {
            sidebar.classList.toggle('collapsed');
        });
    }

    // Mobile drawer
    var mobileMenuBtn = document.getElementById('mobile-menu-btn');
    var overlay = document.getElementById('sidebar-overlay');

    function openMobileNav() {
        if (sidebar) {
            sidebar.classList.add('mobile-open');
            sidebar.removeAttribute('aria-hidden');
            sidebar.removeAttribute('inert');
        }
        if (overlay) overlay.classList.add('visible');
    }

    function closeMobileNav() {
        if (sidebar) {
            sidebar.classList.remove('mobile-open');
            if (window.innerWidth <= 768) {
                sidebar.setAttribute('aria-hidden', 'true');
                sidebar.setAttribute('inert', '');
            }
        }
        if (overlay) overlay.classList.remove('visible');
    }

    if (mobileMenuBtn) {
        mobileMenuBtn.addEventListener('click', openMobileNav);
    }

    if (overlay) {
        overlay.addEventListener('click', closeMobileNav);
    }

    // Close drawer on nav link click (mobile)
    document.querySelectorAll('.nav-link').forEach(function(link) {
        link.addEventListener('click', function() {
            if (window.innerWidth <= 768) {
                closeMobileNav();
            }
        });
    });

    // Escape key closes drawer
    document.addEventListener('keydown', function(e) {
        if (e.key === 'Escape' && sidebar && sidebar.classList.contains('mobile-open')) {
            closeMobileNav();
        }
    });

    // Close drawer on orientation change / resize past breakpoint
    window.addEventListener('resize', function() {
        if (window.innerWidth > 768) {
            closeMobileNav();
            if (sidebar) {
                sidebar.removeAttribute('aria-hidden');
                sidebar.removeAttribute('inert');
            }
        }
    });

    // Initial route
    handleRoute();
});
```

- [ ] **Step 5: Verify and commit**

Open dashboard in browser at 360px width. Verify:
- Hamburger menu visible, sidebar hidden by default
- Tapping hamburger slides in drawer with overlay
- Tapping overlay or nav link closes drawer
- All 12 nav links visible with labels in drawer

```bash
git add static/dashboard/css/dashboard.css static/dashboard/index.html static/dashboard/js/app.js
git commit -m "feat(dashboard): mobile slide-out drawer sidebar at 768px"
```

---

### Task 2: Mobile Layout & Scroll Fix

**Files:**
- Modify: `static/dashboard/css/dashboard.css` (lines 37-39 html/body overflow, lines 228-233 stat-grid, lines 298-303 chart-grid, lines 389-394 cell-truncate)

**Problem:** `html, body { overflow: hidden }` prevents scrolling on mobile. Stat grid and chart grid minmax values are too large for phones. Table cell truncation at 400px overflows.

- [ ] **Step 1: Fix html/body overflow**

In `dashboard.css`, change the `html, body` rule (lines 37-40) to scope overflow to desktop only:

```css
html {
    height: 100%;
}

body {
    height: 100%;
    overflow: hidden;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.5;
    -webkit-font-smoothing: antialiased;
}
```

The `html { overflow: auto }` in the 768px media query (from Task 1) handles mobile. The body `overflow: hidden` is correct for the desktop sidebar layout and gets overridden by `.content { overflow-y: auto }`.

- [ ] **Step 2: Add mobile-specific grid and table overrides**

Add these rules inside the `@media (max-width: 768px)` block in `dashboard.css`:

```css
    .stat-grid {
        grid-template-columns: repeat(auto-fill, minmax(120px, 1fr));
        gap: 10px;
    }

    .chart-grid {
        grid-template-columns: 1fr;
        gap: 12px;
    }

    .chart-container {
        height: 200px;
    }

    .chart-container.tall {
        height: 240px;
    }

    .data-table-wrap {
        overflow-x: auto;
        -webkit-overflow-scrolling: touch;
    }

    .cell-truncate {
        max-width: 200px;
    }

    .detail-grid {
        grid-template-columns: 100px 1fr;
    }

    .view-header h1 {
        font-size: 18px;
    }

    .stat-card .stat-value {
        font-size: 22px;
    }

    .tab-bar {
        overflow-x: auto;
        -webkit-overflow-scrolling: touch;
        scrollbar-width: none;
    }

    .tab-bar::-webkit-scrollbar {
        display: none;
    }
```

- [ ] **Step 3: Fix Activity view hardcoded grid**

In `activity.js`, change the hardcoded 2-column grid (line 52) from:

```javascript
    html += '<div style="display:grid;grid-template-columns:1fr 360px;gap:24px;align-items:start">';
```

to:

```javascript
    html += '<div class="activity-grid">';
```

Then add to `dashboard.css` (near the `.timeline` section, after line 876):

```css
/* Activity layout */
.activity-grid {
    display: grid;
    grid-template-columns: 1fr 360px;
    gap: 24px;
    align-items: start;
}

@media (max-width: 1024px) {
    .activity-grid {
        grid-template-columns: 1fr;
    }
}
```

- [ ] **Step 4: Commit**

```bash
git add static/dashboard/css/dashboard.css static/dashboard/js/activity.js
git commit -m "feat(dashboard): fix mobile scroll, responsive grids, activity layout"
```

---

### Task 3: Graph Detail Panel — Full-Screen on Mobile

**Files:**
- Modify: `static/dashboard/css/dashboard.css` (lines 777-818 graph-detail-panel)
- Modify: `static/dashboard/js/graph.js` (detail panel open/close logic)

**Problem:** The `.graph-detail-panel` is 320px fixed width, which overlaps/overflows on 360px phone screens.

- [ ] **Step 1: Add mobile graph panel CSS**

Add inside the `@media (max-width: 768px)` block:

```css
    .graph-detail-panel {
        width: 100%;
        border-left: none;
        border-top: 1px solid var(--border);
        position: absolute;
        top: auto;
        bottom: 0;
        height: 50%;
        border-radius: var(--radius) var(--radius) 0 0;
        touch-action: pan-y;
    }

    .graph-detail-panel.open {
        animation: slideInUp 0.2s ease;
    }

    .graph-container svg {
        touch-action: manipulation;
    }

    .graph-controls {
        top: 8px;
        left: 8px;
    }

    .graph-stats-overlay {
        bottom: 8px;
        left: 8px;
        gap: 12px;
        font-size: 11px;
    }
```

Add the `slideInUp` keyframe after the existing `slideInRight` keyframe (after line 799):

```css
@keyframes slideInUp {
    from { transform: translateY(20px); opacity: 0; }
    to   { transform: translateY(0); opacity: 1; }
}
```

**Note:** The `.graph-container` already has `position: relative` (implied by its absolute-positioned children). Verify during implementation — if missing, add `position: relative` to `.graph-container`.

- [ ] **Step 2: Add touch event isolation in graph.js**

In `graph.js`, find where `detailEl` is assigned (around line 40). Add event listeners to prevent D3 zoom from consuming touch events on the detail panel:

```javascript
// After detailEl is assigned, add:
detailEl.addEventListener('pointerdown', function(e) {
    e.stopPropagation();
});
detailEl.addEventListener('touchstart', function(e) {
    e.stopPropagation();
}, { passive: true });
```

- [ ] **Step 3: Commit**

```bash
git add static/dashboard/css/dashboard.css static/dashboard/js/graph.js
git commit -m "feat(dashboard): full-screen graph detail panel on mobile"
```

---

### Task 4: Responsive Chart Legends

**Files:**
- Modify: `static/dashboard/js/app.js` (add responsive helper)
- Modify: `static/dashboard/js/overview.js` (doughnut charts — has `position: 'right'` at lines 226, 270)
- Modify: `static/dashboard/js/admission.js` (doughnut chart — has `position: 'right'` at line 529)
- Modify: `static/dashboard/js/rubric.js` (doughnut chart — has `position: 'right'` at line 197)
- Modify: `static/dashboard/js/observability.js` (doughnut chart — has `position: 'right'` at line 583)

**Note:** `decisions.js` and `health.js` do NOT contain `position: 'right'` — do not modify them for this task.

**Problem:** Doughnut charts use `legend: { position: 'right' }` which gets crushed on narrow viewports. Chart.js has no built-in responsive legend positioning.

**Known limitation:** `legendPosition()` evaluates viewport width at chart creation time only. If a user rotates their device without navigating away, legends won't update until the view is re-entered. This is acceptable for a dev dashboard.

- [ ] **Step 1: Add responsive legend helper to app.js**

Add this as the last property of the `Dashboard` object (before the closing `};`), in `app.js`:

```javascript
    /**
     * Get responsive legend position based on viewport width.
     * Returns 'bottom' on mobile, 'right' on desktop.
     */
    legendPosition: function() {
        return window.innerWidth <= 768 ? 'bottom' : 'right';
    }
```

**Important:** Ensure a comma follows the previous property (`outcomeBadge` method).

- [ ] **Step 2: Update overview.js doughnut charts**

In `overview.js`, find both `position: 'right'` occurrences in `createCategoriesChart` and `createOutcomesChart` and replace with:

```javascript
                    position: Dashboard.legendPosition(),
```

- [ ] **Step 3: Update admission.js, rubric.js, observability.js**

In each file, find `position: 'right'` in chart legend configs and replace with `Dashboard.legendPosition()`:
- `admission.js` line 529
- `rubric.js` line 197  
- `observability.js` line 583

- [ ] **Step 4: Commit**

```bash
git add static/dashboard/js/app.js static/dashboard/js/overview.js static/dashboard/js/admission.js static/dashboard/js/rubric.js static/dashboard/js/observability.js
git commit -m "feat(dashboard): responsive chart legend positioning for mobile"
```

---

### Task 5: Typography & Visual Depth

**Files:**
- Modify: `static/dashboard/index.html` (add Google Fonts link)
- Modify: `static/dashboard/css/dashboard.css` (font-family, card depth, surface contrast)

**Problem:** System font stack gives zero visual identity. Cards barely separate from background (only 3% lightness difference). No visual depth.

**Solution:** Load two fonts — one for UI text (DM Sans: geometric, slightly wider than system, distinctive), one for monospace values (JetBrains Mono, already referenced in CSS `.mono` class). Increase card/surface contrast. Add subtle gradient background.

- [ ] **Step 1: Add Google Fonts to index.html**

In `index.html`, add after line 8 (after the `<title>` tag):

```html
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=DM+Sans:ital,opsz,wght@0,9..40,400;0,9..40,500;0,9..40,600;0,9..40,700;1,9..40,400&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
```

- [ ] **Step 2: Update CSS variables and font stack**

In `dashboard.css`, update the `:root` block (lines 2-28):

**Replace** the existing `--surface: #111118` (line 5) with `--surface: #12121a`.
**Replace** the existing `--surface-hover: #16161f` (line 6) with `--surface-hover: #181824`.
**Add** these two new font variables anywhere in the `:root` block:

```css
    --font-ui: 'DM Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
    --font-mono: 'JetBrains Mono', 'Cascadia Code', 'Fira Code', monospace;
```

Do NOT duplicate `--surface` or `--surface-hover` — replace the existing values.

Update the `body` rule font-family (line 43):
```css
    font-family: var(--font-ui);
```

Update the `.mono` class (line 985):
```css
.mono {
    font-family: var(--font-mono);
    font-size: 12px;
}
```

Update monospace references in `.action-args` (line 1468) and `.action-detail` (line 1524) and `.score-cell` (line 1034):
```css
    font-family: var(--font-mono);
```

- [ ] **Step 3: Increase card depth with subtle border glow**

Update `.stat-card` (lines 235-242):

```css
.stat-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 20px;
    transition: all var(--transition);
    cursor: default;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.2);
}
```

Update `.chart-card` (lines 305-310):

```css
.chart-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 20px;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.2);
}
```

- [ ] **Step 4: Add subtle background texture to body**

Update the `body` background (line 44):

```css
    background: var(--bg);
    background-image: radial-gradient(ellipse at 50% 0%, rgba(124, 106, 247, 0.03) 0%, transparent 60%);
```

- [ ] **Step 5: Commit**

```bash
git add static/dashboard/index.html static/dashboard/css/dashboard.css
git commit -m "feat(dashboard): DM Sans + JetBrains Mono typography, card depth"
```

---

### Task 6: Micro-Interactions & Staggered Animations

**Files:**
- Modify: `static/dashboard/css/dashboard.css`

**Problem:** All stat cards appear at once. No hover transitions on badges. Tab switching is instant with no indicator animation.

- [ ] **Step 1: Add staggered stat card entrance animation**

Add after the `viewFadeIn` keyframe (after line 191) in `dashboard.css`:

```css
@keyframes cardFadeIn {
    from { opacity: 0; transform: translateY(12px); }
    to   { opacity: 1; transform: translateY(0); }
}

.stat-grid .stat-card {
    animation: cardFadeIn 0.3s ease both;
}

.stat-grid .stat-card:nth-child(1) { animation-delay: 0.03s; }
.stat-grid .stat-card:nth-child(2) { animation-delay: 0.06s; }
.stat-grid .stat-card:nth-child(3) { animation-delay: 0.09s; }
.stat-grid .stat-card:nth-child(4) { animation-delay: 0.12s; }
.stat-grid .stat-card:nth-child(5) { animation-delay: 0.15s; }
.stat-grid .stat-card:nth-child(6) { animation-delay: 0.18s; }
.stat-grid .stat-card:nth-child(7) { animation-delay: 0.21s; }
.stat-grid .stat-card:nth-child(8) { animation-delay: 0.24s; }
```

- [ ] **Step 2: Add chart card staggered entrance**

```css
.chart-grid .chart-card {
    animation: cardFadeIn 0.3s ease both;
}

.chart-grid .chart-card:nth-child(1) { animation-delay: 0.05s; }
.chart-grid .chart-card:nth-child(2) { animation-delay: 0.10s; }
.chart-grid .chart-card:nth-child(3) { animation-delay: 0.15s; }
.chart-grid .chart-card:nth-child(4) { animation-delay: 0.20s; }
```

- [ ] **Step 3: Add badge hover transition**

Update the `.badge` class (line 434-442):

```css
.badge {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.3px;
    transition: transform 0.15s ease, box-shadow 0.15s ease;
}

.badge:hover {
    transform: translateY(-1px);
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.2);
}
```

- [ ] **Step 4: Add tab indicator slide animation**

Update `.tab-btn.active` (line 483-486):

```css
.tab-btn.active {
    color: var(--accent);
    border-bottom-color: var(--accent);
    border-bottom-width: 2px;
    transition: border-bottom-color 0.2s ease, color 0.2s ease;
}
```

- [ ] **Step 5: Commit**

```bash
git add static/dashboard/css/dashboard.css
git commit -m "feat(dashboard): staggered card animations, badge hover, tab transitions"
```

---

### Task 7: Accessibility Improvements

**Files:**
- Modify: `static/dashboard/index.html` (ARIA attributes)
- Modify: `static/dashboard/css/dashboard.css` (focus-visible styles)

**Problem:** No ARIA labels on nav icons, no role="navigation", no focus-visible styles, no skip-to-content link. Color-only status indicators.

- [ ] **Step 1: Add ARIA attributes to navigation**

In `index.html`, update the sidebar nav (line 12):

```html
    <nav class="sidebar" id="sidebar" role="navigation" aria-label="Main navigation">
```

Add `aria-label` to each nav link. Update each `<a>` tag, for example line 18:

```html
            <a href="#/overview" class="nav-link active" data-view="overview" aria-label="Overview">
```

Line 22:
```html
            <a href="#/graph" class="nav-link" data-view="graph" aria-label="Knowledge Graph">
```

Line 26:
```html
            <a href="#/browser" class="nav-link" data-view="browser" aria-label="Memory Browser">
```

Line 30:
```html
            <a href="#/decisions" class="nav-link" data-view="decisions" aria-label="Decisions">
```

Line 34:
```html
            <a href="#/activity" class="nav-link" data-view="activity" aria-label="Activity">
```

Line 38:
```html
            <a href="#/heartbeat" class="nav-link" data-view="heartbeat" aria-label="Heartbeat">
```

Line 44:
```html
            <a href="#/observability" class="nav-link" data-view="observability" aria-label="Observability">
```

Line 51:
```html
            <a href="#/health" class="nav-link" data-view="health" aria-label="Graph Health">
```

Line 55:
```html
            <a href="#/admission" class="nav-link" data-view="admission" aria-label="Admission">
```

Line 59:
```html
            <a href="#/rubric" class="nav-link" data-view="rubric" aria-label="Rubric">
```

Line 63:
```html
            <a href="#/execution" class="nav-link" data-view="execution" aria-label="Execution">
```

Line 67:
```html
            <a href="#/cache" class="nav-link" data-view="cache" aria-label="Cache">
```

- [ ] **Step 2: Add skip-to-content link**

In `index.html`, add as the first child of `<body>` (after line 11):

```html
    <a href="#content" class="skip-link">Skip to content</a>
```

Update `<main>` to have `id="content"` (it already does per line 81 — verify).

- [ ] **Step 3: Add focus-visible styles and skip-link CSS**

Add to `dashboard.css` after the reset section (after line 48):

```css
/* Skip link */
.skip-link {
    position: absolute;
    top: -40px;
    left: 0;
    background: var(--accent);
    color: #fff;
    padding: 8px 16px;
    z-index: 300;
    font-size: 14px;
    border-radius: 0 0 var(--radius-sm) 0;
    transition: top 0.2s ease;
}

.skip-link:focus {
    top: 0;
}

/* Focus-visible */
:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
}

.nav-link:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: -2px;
    border-radius: var(--radius-sm);
}

.btn:focus-visible,
.tab-btn:focus-visible,
.filter-btn:focus-visible,
.filter-select:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
}
```

- [ ] **Step 4: Add visually-hidden text for color-only indicators**

Add utility class to `dashboard.css`:

```css
.sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    margin: -1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
    border-width: 0;
}
```

- [ ] **Step 5: Commit**

```bash
git add static/dashboard/index.html static/dashboard/css/dashboard.css
git commit -m "feat(dashboard): ARIA labels, skip-link, focus-visible, sr-only"
```

---

## Integration Verification

After all 7 tasks are complete, verify:

1. **Desktop (1440px):** Sidebar visible, all views render correctly, new typography active
2. **Tablet (1024px):** Sidebar collapses to icons, charts stack to single column, activity grid stacks
3. **Mobile (375px):** Hamburger menu, slide-out drawer, scrollable content, charts with bottom legends, graph detail panel as bottom sheet
4. **Accessibility:** Tab through all nav links, focus-visible outlines appear, skip-link works, screen reader announces nav labels
5. **Animations:** Stat cards stagger in, badges hover-lift, chart cards stagger in
