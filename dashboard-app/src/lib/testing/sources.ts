// The source walk the guards read (hexGuard, glyphs, the token-name check), in place of hand-kept route lists: a new
// file is guarded the moment it exists. Test-only; not imported by the app.
import { readdirSync } from 'node:fs';
import { join, relative } from 'node:path';

/** dashboard-app/src. */
export const SRC = join(__dirname, '..', '..');

/** Every file under `dir`, as a path relative to src/ with forward slashes. */
export function filesUnder(dir: string = SRC): string[] {
  return readdirSync(dir, { withFileTypes: true })
    .flatMap((e) => (e.isDirectory() ? filesUnder(join(dir, e.name)) : [relative(SRC, join(dir, e.name)).split('\\').join('/')]))
    .sort();
}

/** Every `*.svelte` and `*.ts` file of the dashboard under src/, except the companion's (`companion/`: its own themes
 * and guards), the tests (`*.test.ts`) and the test helpers (`lib/testing/`). */
export function sourceFiles(): string[] {
  return filesUnder().filter(
    (f) => /\.(svelte|ts)$/.test(f) && !f.startsWith('companion/') && !f.endsWith('.test.ts') && !f.startsWith('lib/testing/'),
  );
}
