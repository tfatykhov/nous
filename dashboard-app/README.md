# Nous dashboard

The Svelte dashboard and the companion, served by Nous at `/dashboard/v2/` (`npm run build` writes `../static/dashboard-v2/dist/`).

**One look:** the dashboard has a single look (chart-paper ground, Atlantic-blue rail); its tokens live in `src/app.css` (`:root`, and `.rail` for the navigation). The companion (`companion.html`) has its own stylesheet and themes.

```bash
npx vitest run   # the tests
npm run check    # svelte-check and tsc
npm run build    # the production build
```
