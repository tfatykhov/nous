// PWA install criteria live in static HTML that no component test renders,
// so regressions here are silent until someone tries Add-to-Home-Screen on
// a phone. Prod diagnosis 2026-09-01: behind oauth2-proxy the DEFAULT
// (uncredentialed) manifest fetch got a 401, Chrome concluded there was no
// manifest, and the "installed app" degraded to a browser-tab bookmark.
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

const html = readFileSync(resolve(__dirname, '../../companion.html'), 'utf-8');
const manifest = JSON.parse(readFileSync(resolve(__dirname, '../../public/companion.webmanifest'), 'utf-8'));
const meta = (name: string) => new RegExp(`<meta name="${name}" content="([^"]*)"`).exec(html)?.[1];

describe('companion PWA install criteria', () => {
  it('fetches the manifest WITH credentials (oauth2-proxy 401 regression)', () => {
    const link = html.match(/<link rel="manifest"[^>]*>/)?.[0] ?? '';
    expect(link).toContain('companion.webmanifest');
    expect(link).toContain('crossorigin="use-credentials"');
  });

  it('keeps the standalone metas iOS and Chromium require', () => {
    expect(html).toContain('name="mobile-web-app-capable"');
    expect(html).toContain('name="apple-mobile-web-app-capable"');
    expect(html).toContain('rel="apple-touch-icon"');
  });

  it('the browser chrome is the paper look: a light page under the Atlantic-blue bar (PR 6)', () => {
    expect(meta('color-scheme')).toBe('light');
    // Android paints the status bar in theme-color: the bar's navy, so the bar runs up under it.
    expect(meta('theme-color')).toBe('#12304f');
    // iOS ignores the manifest colours; the page draws under the status bar, whose white text sits on the navy bar.
    expect(meta('apple-mobile-web-app-status-bar-style')).toBe('black-translucent');
    expect(manifest.theme_color).toBe('#12304f');
    expect(manifest.background_color).toBe('#eef2f3');
  });
});
