import { describe, it, expect } from 'vitest';
import { filesUnder, sourceFiles } from './lib/testing/sources';

// Dashboard redesign PR 7: the guards walk every source file instead of the route lists the switch needed (a screen
// joined them when its route turned paper). A hand-kept list has a hole a walk does not: a new file is unguarded until
// someone lists it. The walk lives in lib/testing/sources.ts, so the guards import it without running this file twice.

describe('the source walk', () => {
  const files = sourceFiles();

  it('reads the shell, the views, the shared components, the charts and the plain modules', () => {
    expect(files.length).toBeGreaterThan(100);
    for (const f of ['App.svelte', 'main.ts', 'views/Overview.svelte', 'views/work/Trace.svelte', 'views/health/GraphTab.svelte',
      'lib/ui/Nav.svelte', 'lib/ui/drawer/drawer-content.svelte', 'lib/ui/badge/index.ts', 'lib/viz/Dag.svelte',
      'lib/chartColors.ts', 'lib/overviewCharts.ts', 'lib/status.ts']) {
      expect(files, f).toContain(f);
    }
  });

  it('leaves out the tests, the test helpers and the companion', () => {
    expect(files.filter((f) => f.endsWith('.test.ts'))).toEqual([]);
    expect(files.filter((f) => f.startsWith('lib/testing/'))).toEqual([]);
    expect(files.filter((f) => f.startsWith('companion/'))).toEqual([]);
  });

  it('src holds no other kind of file the walk would miss (a .js, .tsx or second stylesheet widens it first)', () => {
    const others = filesUnder().filter((f) => !f.startsWith('companion/') && !/\.(svelte|ts)$/.test(f));
    expect(others).toEqual(['app.css']); // the token table, read by tokens.test.ts and tokenUse.test.ts
  });
});
